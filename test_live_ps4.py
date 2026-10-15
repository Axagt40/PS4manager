#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live tests against a real PS4 running GoldHEN's FTP server.

They are skipped unless you point them at your console::

    PS4_HOST=192.168.1.50 python3 -m unittest -v test_live_ps4.py

The write/GUI tests only ever touch a scratch folder
(``/data/GoldHEN/_ps4manager_selftest``) that they create and delete again,
and they assert that ``plugins.ini`` and the plugins folder are untouched.
"""

from __future__ import annotations

import os
import time
import unittest
from pathlib import Path

import ps4_manager as m

PS4_HOST = os.environ.get("PS4_HOST", "").strip()
SCRATCH = "/data/GoldHEN/_ps4manager_selftest"
SCRATCH_INI = f"{SCRATCH}/plugins.ini"


def make_ftp() -> "m.Ps4Ftp":
    ftp = m.Ps4Ftp(PS4_HOST, int(os.environ.get("PS4_PORT", m.DEFAULT_PORT)),
                   user=os.environ.get("PS4_USER", "anonymous"),
                   password=os.environ.get("PS4_PASS", ""),
                   logger=lambda line: print("   [ftp]", line))
    ftp.connect()
    return ftp


@unittest.skipUnless(PS4_HOST, "set PS4_HOST=<ip> to run the live tests")
class TestLiveReadOnly(unittest.TestCase):
    """Nothing here writes to the console."""

    ftp: "m.Ps4Ftp"

    @classmethod
    def setUpClass(cls) -> None:
        cls.ftp = make_ftp()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.ftp.close()

    def test_root_listing(self) -> None:
        names = {e.name for e in self.ftp.listdir("/")}
        self.assertIn("data", names)
        self.assertIn("user", names)

    def test_reads_and_round_trips_plugins_ini(self) -> None:
        exists, is_dir = self.ftp.exists(m.DEFAULT_INI)
        if not exists:
            self.skipTest(f"{m.DEFAULT_INI} does not exist on this console")
        self.assertFalse(is_dir)
        text = self.ftp.read_text(m.DEFAULT_INI)
        self.assertEqual(self.ftp.size(m.DEFAULT_INI), len(text.encode("utf-8")))
        doc = m.PluginsIni.parse(text)
        self.assertTrue(doc.sections())
        # untouched content must serialise back byte for byte
        self.assertEqual(doc.dumps(), text)
        for section in doc.sections():
            for entry in section.entries:
                self.assertTrue(entry.path.startswith("/"), entry.path)

    def test_plugins_folder_lists_both_ways(self) -> None:
        mlsd = self.ftp.listdir(m.DEFAULT_PLUGINS_DIR)
        original = self.ftp._ftp.mlsd  # type: ignore[union-attr]

        def no_mlsd(*_args, **_kwargs):
            import ftplib

            raise ftplib.error_perm("500 MLSD not understood")

        self.ftp._ftp.mlsd = no_mlsd  # type: ignore[union-attr]
        try:
            listed = self.ftp.listdir(m.DEFAULT_PLUGINS_DIR)
        finally:
            self.ftp._ftp.mlsd = original  # type: ignore[union-attr]
        self.assertEqual(sorted(e.name for e in mlsd), sorted(e.name for e in listed))
        self.assertTrue(listed, "plugins folder is empty")

    def test_directory_is_not_reported_as_a_file(self) -> None:
        # GoldHEN answers SIZE for folders, so this is worth pinning down.
        self.assertEqual(self.ftp.exists("/data"), (True, True))
        self.assertEqual(self.ftp.exists("/data/GoldHEN"), (True, True))
        self.assertEqual(self.ftp.exists("/definitely/not/here"), (False, False))

    def test_installed_game_names(self) -> None:
        titles = []
        for directory in m.APPMETA_DIRS:
            try:
                titles += [e.name for e in self.ftp.listdir(directory) if e.is_dir and m.is_title_id(e.name)]
            except m.FtpError:
                continue
        self.assertTrue(titles, "no installed games found")
        resolved = 0
        for title in sorted(set(titles)):
            try:
                info = m.parse_sfo(self.ftp.read_bytes(f"/system_data/priv/appmeta/{title}/param.sfo"))
            except m.FtpError:
                continue
            if info.get("TITLE"):
                resolved += 1
                print(f"   {title} -> {info['TITLE']!r}")
        print(f"   resolved {resolved} game name(s) out of {len(set(titles))}")
        self.assertGreater(resolved, 0, "no game names could be read")


@unittest.skipUnless(PS4_HOST, "set PS4_HOST=<ip> to run the live tests")
class TestLiveWrite(unittest.TestCase):
    """Creates and removes a scratch folder; never touches plugins.ini."""

    def setUp(self) -> None:
        self.ftp = make_ftp()
        self.ini_before = self.ftp.read_bytes(m.DEFAULT_INI)
        self.plugins_before = sorted(e.name for e in self.ftp.listdir(m.DEFAULT_PLUGINS_DIR))
        self.ftp.ensure_dir(SCRATCH)

    def tearDown(self) -> None:
        for path in (f"{SCRATCH}/t.ini", f"{SCRATCH}/big.ini", f"{SCRATCH}/plugins.ini",
                     f"{SCRATCH}/sub", SCRATCH):
            try:
                self.ftp.delete(path)
            except m.FtpError:
                pass
        self.assertEqual(self.ftp.exists(SCRATCH), (False, False))
        self.assertEqual(self.ftp.read_bytes(m.DEFAULT_INI), self.ini_before)
        self.assertEqual(sorted(e.name for e in self.ftp.listdir(m.DEFAULT_PLUGINS_DIR)),
                         self.plugins_before)
        self.ftp.close()

    def test_write_overwrite_and_cleanup(self) -> None:
        first = "[default]\n/data/GoldHEN/plugins/a.prx=true\n"
        self.ftp.write_text(f"{SCRATCH}/t.ini", first)
        self.assertEqual(self.ftp.read_text(f"{SCRATCH}/t.ini"), first)

        second = "[default]\n/data/GoldHEN/plugins/a.prx=false\n[CUSA12345]\n/x.prx=true\n"
        self.ftp.write_text(f"{SCRATCH}/t.ini", second)  # overwrite path
        self.assertEqual(self.ftp.read_text(f"{SCRATCH}/t.ini"), second)
        self.assertFalse([n for n in self.ftp.listdir(SCRATCH) if n.name.endswith(".tmp")])

    def test_upload_download_round_trip(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "plugin.prx"
            source.write_bytes(os.urandom(600_000))
            self.ftp.upload(source, f"{SCRATCH}/plugin.prx")
            back = Path(tmp) / "back.prx"
            self.ftp.download(f"{SCRATCH}/plugin.prx", back)
            self.assertEqual(source.read_bytes(), back.read_bytes())
            try:
                self.ftp.delete(f"{SCRATCH}/plugin.prx")
            except m.FtpError:
                pass


@unittest.skipUnless(PS4_HOST, "set PS4_HOST=<ip> to run the live tests")
@unittest.skipUnless(m.TK_OK, "tkinter is required for the GUI test")
@unittest.skipUnless(os.environ.get("DISPLAY") or os.name == "nt", "needs a display")
class TestLiveGui(unittest.TestCase):
    """Drives the real UI against the console, but through a scratch plugins.ini."""

    def setUp(self) -> None:
        self.ftp = make_ftp()
        self.real_ini = self.ftp.read_text(m.DEFAULT_INI)
        self.ini_before = self.ftp.read_bytes(m.DEFAULT_INI)
        self.backups_before = {p.name for p in m.list_backups()}
        self.ftp.ensure_dir(SCRATCH)
        self.ftp.write_text(SCRATCH_INI, self.real_ini)

    def tearDown(self) -> None:
        try:
            if hasattr(self, "app"):
                if self.app.ftp is not None:
                    self.app.ftp.close()
                    self.app.ftp = None
                self.app.destroy()
        except Exception:
            pass
        try:
            self.ftp.delete(SCRATCH_INI)
            self.ftp.delete(SCRATCH)
        except m.FtpError:
            pass
        self.assertEqual(self.ftp.read_bytes(m.DEFAULT_INI), self.ini_before,
                         "the real plugins.ini must never be modified by this test")
        for path in m.list_backups():
            if path.name not in self.backups_before:
                path.unlink()
        self.ftp.close()

    def pump(self, predicate, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.app.update()
            if predicate():
                return True
            time.sleep(0.05)
        self.app.update()
        return predicate()

    def connected(self) -> bool:
        return self.app.ftp is not None

    def loaded(self) -> bool:
        return bool(self.views_rows())

    def views_rows(self):
        view = self.app.views.get("plugins")
        return list(view._rows) if view is not None else []

    def ini_rows(self):
        """(iid, section, entry) for every row the plugin view is showing."""
        view = self.app.views["plugins"]
        return [(iid, s, self.app.ini.find_section(s).items[i])
                for iid, (s, i) in view._rows.items()]

    def rows_for(self, wanted):
        """The first (iid, section, entry) whose entry satisfies ``wanted``."""
        for iid, section, entry in self.ini_rows():
            if wanted(section, entry):
                return iid, section, entry
        return None, None, None

    def plugins_on_console(self):
        return {e.name for e in self.ftp.listdir(m.DEFAULT_PLUGINS_DIR)}

    def test_full_gui_flow(self) -> None:
        config = m.Config(
            host=PS4_HOST, port=int(os.environ.get("PS4_PORT", m.DEFAULT_PORT)),
            remote_ini=SCRATCH_INI, remote_plugins_dir=m.DEFAULT_PLUGINS_DIR,
            auto_connect=True, auto_apply=False,
        )
        self.app = m.Ps4ManagerApp(config, m.parse_args(["--no-connect"]))
        # stay invisible: the tests must never steal focus from whatever the user
        # is doing on this machine (widgets work fine unmapped)
        self.app.withdraw()
        self.app.update()
        self.assertTrue(self.pump(self.connected), "never connected")
        self.assertTrue(self.pump(self.loaded), "plugins.ini never loaded")
        # let the background title lookup finish (game names come from param.sfo)
        self.pump(lambda: bool(self.app.titles), timeout=60)

        # Everything below works off the console's own plugins.ini and plugins
        # folder - no plugin or title is hard-coded here - so the test runs
        # against any PS4 without having to be edited first.
        view = self.app.views["plugins"]
        rows = self.ini_rows()
        on_console = self.plugins_on_console()
        self.assertTrue(rows, "plugins.ini has no entries on this console")
        self.assertTrue(on_console, "the plugins folder is empty")
        print(f"   {len(rows)} parsed entries, {len(on_console)} files in the plugins folder")

        # every entry reaches the tree (column 1 is the full path on the PS4)
        shown = {view.tree.item(iid, "values")[1] for iid, _s, _e in rows}
        self.assertTrue({e.path for _i, _s, e in rows} & shown,
                        "the entries are not reaching the plugin view")

        # real game names from param.sfo make it into the section header
        game_section = next((s for s in self.app.ini.section_names() if s != "default"), None)
        if game_section:
            self.assertIn(game_section, self.app.ini.dumps())
            print("   section label:", self.app.section_label(game_section))

        # plugins listed in the ini but missing from the plugins folder are flagged
        plugins_dir = (self.app.config.remote_plugins_dir or m.DEFAULT_PLUGINS_DIR).rstrip("/")
        missing = [(iid, e) for iid, _s, e in rows
                   if e.path.rsplit("/", 1)[0].rstrip("/") == plugins_dir
                   and e.path.split("/")[-1] not in on_console]
        for iid, entry in missing:
            self.assertIn("⚠", view.display_name(entry))
        print("   entries flagged as missing on the console:", len(missing))

        # --- toggle through the UI, then save to the console -------------
        iid, section, entry = self.rows_for(
            lambda _s, e: e.togglable and e.path.split("/")[-1] in on_console)
        self.assertIsNotNone(iid, "no toggleable plugin whose file exists on the console")
        target_path, before = entry.path, entry.enabled
        view.toggle_row(iid)
        after = self.app.ini.find_section(section).find(target_path).enabled
        self.assertNotEqual(before, after, "the toggle did nothing")
        self.assertTrue(self.app.dirty)
        self.app.apply_async()
        self.assertTrue(self.pump(lambda: not self.app.dirty, timeout=30), "save never finished")

        uploaded = self.ftp.read_text(SCRATCH_INI)
        expected = f"{target_path}={'true' if after else 'false'}"
        self.assertIn(expected, uploaded)
        self.assertEqual(m.PluginsIni.parse(uploaded).dumps(), uploaded)
        print("   saved to console:", expected)

        # --- auto-apply: no explicit save call ---------------------------
        self.app.config.auto_apply = True
        iid2, section2, entry2 = self.rows_for(
            lambda _s, e: e.togglable and e.path != target_path)
        self.assertIsNotNone(iid2, "plugins.ini has only one toggleable plugin")
        other_path = entry2.path
        view.toggle_row(iid2)
        now = self.app.ini.find_section(section2).find(other_path).enabled
        wanted = f"{other_path}={'true' if now else 'false'}"
        self.assertTrue(self.pump(lambda: wanted in self.ftp.read_text(SCRATCH_INI), timeout=30),
                        "auto-apply did not upload the change")
        print("   auto-apply uploaded:", wanted)

        # --- add a plugin through the dialog ----------------------------
        new_path = f"{m.DEFAULT_PLUGINS_DIR}/ps4manager_selftest.prx"
        dialog = m.AddPluginDialog(self.app, remote_files=self.app.remote_entries)
        dialog.update()
        if game_section:
            dialog.var_game.set(True)
            dialog._set_scope("game")
            dialog.var_title.set(game_section)
            self.assertTrue(dialog._validate_title())
            want_section = game_section
        else:
            want_section = "default"
        dialog.var_path.set(new_path)
        dialog._submit()
        result = dialog.result
        self.assertIsNotNone(result, "the Add dialog did not produce a result")
        self.assertEqual(result["section"], want_section)
        self.app.ini.add(result["section"], result["path"], enabled=result["enabled"])
        view.rebuild()
        self.app.mark_dirty()
        added = f"{new_path}={str(bool(result['enabled'])).lower()}"
        self.assertTrue(self.pump(lambda: added in self.ftp.read_text(SCRATCH_INI), timeout=30),
                        "the added plugin never reached the console")
        print("   added plugin saved:", result)
        dialog.close()

        # --- local backup was written ------------------------------------
        self.assertTrue([p for p in m.list_backups() if p.name not in self.backups_before],
                        "no local backup was written")
        print("   status:", self.app.status_label.cget("text"))
