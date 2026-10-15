#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for ps4_manager.py.

No PS4 is needed. The parsing/back-end tests run anywhere; the saved-server
(save -> restart -> auto-connect) tests build real Tk widgets and therefore skip
themselves when there is no display.

    python3 -m unittest -v test_ps4_manager.py
"""

from __future__ import annotations

import json
import os
import struct
import tempfile
import time
import unittest
from pathlib import Path

import ps4_manager as m

# The saved-server tests build real Tk widgets; they skip themselves when there is
# no display (or no tkinter) so this file still runs on a headless box.
def _tk_usable() -> bool:
    if not getattr(m, "TK_OK", False):
        return False
    if not (os.environ.get("DISPLAY") or os.name == "nt"):
        return False
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()          # do not flash a window while probing
        root.destroy()
        return True
    except Exception:
        return False


REAL_INI = """\
[default]
/data/GoldHEN/plugins/example_plugin.prx=false

[CUSA12345]
/data/GoldHEN/plugins/example_game_plugin.prx=true
/data/GoldHEN/plugins/second_example.sprx=true
/data/GoldHEN/plugins/third_example.prx=false
"""


class TestPluginsIni(unittest.TestCase):
    def test_parse_real_file(self) -> None:
        doc = m.PluginsIni.parse(REAL_INI)
        self.assertEqual(doc.section_names(), ["default", "CUSA12345"])
        default = doc.find_section("default")
        self.assertIsNotNone(default)
        self.assertEqual(len(default.entries), 1)
        self.assertFalse(default.entries[0].enabled)
        self.assertEqual(doc.stats(), (2, 4))
        self.assertEqual(doc.titles(), ["CUSA12345"])

    def test_roundtrip_is_byte_identical(self) -> None:
        self.assertEqual(m.PluginsIni.parse(REAL_INI).dumps(), REAL_INI)

    def test_roundtrip_with_comments_and_odd_lines(self) -> None:
        text = (
            "; GoldHEN plugins\r\n"
            "\r\n"
            "[default]\r\n"
            "; my notes\r\n"
            "/data/GoldHEN/plugins/a.prx\r\n"
            "\r\n"
            "[CUSA12345]\r\n"
            "/mnt/usb0/b.sprx=true\r\n"
            "/data/GoldHEN/plugins/odd.prx=0x1234\r\n"
        )
        doc = m.PluginsIni.parse(text)
        # line endings are normalised, everything else survives
        self.assertEqual(doc.dumps().replace("\r\n", "\n"), text.replace("\r\n", "\n"))
        odd = doc.find_section("CUSA12345").entries[1]
        self.assertEqual(odd.value, "0x1234")
        self.assertFalse(odd.togglable)

    def test_toggle_flag_style(self) -> None:
        doc = m.PluginsIni.parse(REAL_INI)
        entry = doc.find_section("CUSA12345").entries[0]
        self.assertTrue(entry.enabled)
        entry.set_enabled(False)
        self.assertIn("/data/GoldHEN/plugins/example_game_plugin.prx=false", doc.dumps())
        entry.set_enabled(True)
        self.assertEqual(entry.value, "true")
        self.assertEqual(doc.dumps(), REAL_INI)

    def test_toggle_bare_style(self) -> None:
        doc = m.PluginsIni.parse("[default]\n/data/GoldHEN/plugins/a.prx\n")
        entry = doc.find_section("default").entries[0]
        self.assertIsNone(entry.value)
        self.assertTrue(entry.enabled)
        entry.set_enabled(False)
        self.assertEqual(doc.dumps(), "[default]\n/data/GoldHEN/plugins/a.prx=false\n")
        entry.set_enabled(True)
        # a bare entry goes back to being bare, an explicit '=true' stays explicit
        self.assertEqual(doc.dumps(), "[default]\n/data/GoldHEN/plugins/a.prx\n")

    def test_custom_value_is_restored_when_re_enabled(self) -> None:
        doc = m.PluginsIni.parse("[default]\n/data/GoldHEN/plugins/odd.prx=0x1234\n")
        entry = doc.find_section("default").entries[0]
        entry.set_enabled(False)
        self.assertEqual(entry.value, "false")
        entry.set_enabled(True)
        self.assertEqual(entry.value, "0x1234")

    def test_plugin_without_section_lands_in_default(self) -> None:
        doc = m.PluginsIni.parse("/data/GoldHEN/plugins/loose.prx\n\n[CUSA12345]\n/x.prx\n")
        self.assertEqual(doc.section_names(), ["default", "CUSA12345"])
        self.assertEqual(doc.dumps(),
                         "[default]\n/data/GoldHEN/plugins/loose.prx\n\n[CUSA12345]\n/x.prx\n")

    def test_add_and_remove(self) -> None:
        doc = m.PluginsIni.parse(REAL_INI)
        doc.add("CUSA12345", "/data/GoldHEN/plugins/afr.prx", enabled=True)
        self.assertIn("[CUSA12345]", doc.dumps())
        self.assertIn("/data/GoldHEN/plugins/afr.prx=true", doc.dumps())
        # adding the same path again updates instead of duplicating
        doc.add("CUSA12345", "/data/GoldHEN/plugins/afr.prx", enabled=False)
        self.assertEqual(doc.dumps().count("/data/GoldHEN/plugins/afr.prx=false"), 1)
        self.assertTrue(doc.remove_section("CUSA12345"))
        self.assertNotIn("CUSA12345", doc.dumps())
        self.assertFalse(doc.remove_section("nope"))

    def test_default_section_is_created_first(self) -> None:
        doc = m.PluginsIni.parse("; header comment\n[CUSA12345]\n/x.prx\n")
        doc.add("default", "/data/GoldHEN/plugins/g.prx", enabled=True)
        text = doc.dumps()
        self.assertTrue(text.startswith("; header comment\n[default]\n"))
        self.assertLess(text.index("[default]"), text.index("[CUSA12345]"))
        self.assertIn("/data/GoldHEN/plugins/g.prx=true\n", text)

    def test_empty_document(self) -> None:
        doc = m.PluginsIni.parse("")
        self.assertEqual(doc.sections(), [])
        self.assertEqual(doc.stats(), (0, 0))
        self.assertEqual(doc.titles(), [])


class TestListParsing(unittest.TestCase):
    def test_file_and_dir(self) -> None:
        entry = m.parse_list_line("-rw-r--r--   1 root  root     4096 Oct 15 11:40 plugins.ini", "/data/GoldHEN")
        self.assertIsNotNone(entry)
        self.assertFalse(entry.is_dir)
        self.assertEqual(entry.name, "plugins.ini")
        self.assertEqual(entry.path, "/data/GoldHEN/plugins.ini")
        self.assertEqual(entry.size, 4096)
        self.assertIsNotNone(entry.mtime)

        folder = m.parse_list_line("drwxr-xr-x   2 root  root        0 Jan  1  1970 plugins", "/data/GoldHEN")
        self.assertTrue(folder.is_dir)
        self.assertEqual(folder.path, "/data/GoldHEN/plugins")

    def test_names_with_spaces_and_symlinks(self) -> None:
        entry = m.parse_list_line("-rw-r--r-- 1 1000 1000 128 Oct 15 2024 my plugin file.prx", "/data")
        self.assertEqual(entry.name, "my plugin file.prx")
        link = m.parse_list_line("lrwxrwxrwx 1 root root 12 Oct 15 11:40 alias.prx -> real.prx", "/data")
        self.assertEqual(link.name, "alias.prx")

    def test_ignores_total_and_dot_entries(self) -> None:
        self.assertIsNone(m.parse_list_line("total 12", "/"))
        self.assertIsNone(m.parse_list_line("drwxr-xr-x 2 root root 0 Jan 1 1970 .", "/"))
        self.assertIsNone(m.parse_list_line("", "/"))

    def test_root_directory_join(self) -> None:
        entry = m.parse_list_line("drwxr-xr-x 2 root root 0 Jan 1 1970 data", "/")
        self.assertEqual(entry.path, "/data")


class TestSfo(unittest.TestCase):
    @staticmethod
    def build_sfo(pairs) -> bytes:
        keys = b""
        key_offsets = []
        for key, _value in pairs:
            key_offsets.append(len(keys))
            keys += key.encode("utf-8") + b"\x00"
        values = b""
        value_offsets = []
        for _key, value in pairs:
            value_offsets.append(len(values))
            values += value.encode("utf-8") + b"\x00"
        header_size = 20
        entry_size = 16 * len(pairs)
        key_table = header_size + entry_size
        data_table = key_table + len(keys) + (4 - len(keys) % 4) % 4
        entries = b""
        for index, (_key, value) in enumerate(pairs):
            entries += struct.pack("<HHIII", key_offsets[index], 0x0204, len(value) + 1,
                                   len(value) + 1, value_offsets[index])
        padding = b"\x00" * ((4 - len(keys) % 4) % 4)
        return (b"\x00PSF" + struct.pack("<IIII", 0x00000101, key_table, data_table, len(pairs))
                + entries + keys + padding + values)

    def test_parse_title(self) -> None:
        data = self.build_sfo([("TITLE", "Example Game"), ("TITLE_ID", "CUSA12345")])
        info = m.parse_sfo(data)
        self.assertEqual(info["TITLE"], "Example Game")
        self.assertEqual(info["TITLE_ID"], "CUSA12345")

    def test_parse_garbage(self) -> None:
        self.assertEqual(m.parse_sfo(b""), {})
        self.assertEqual(m.parse_sfo(b"not an sfo at all"), {})
        self.assertEqual(m.parse_sfo(b"\x00PSF" + b"\x00" * 8), {})


class TestHelpers(unittest.TestCase):
    def test_title_id(self) -> None:
        self.assertTrue(m.is_title_id("CUSA12345"))
        self.assertTrue(m.is_title_id("ppsa01234"))
        self.assertFalse(m.is_title_id("CUSA411"))
        self.assertFalse(m.is_title_id("default"))
        self.assertEqual(m.norm_title_id(" cusa12345 "), "CUSA12345")

    def test_remote_paths(self) -> None:
        self.assertEqual(m.join_remote("/data/GoldHEN/plugins", "a.prx"), "/data/GoldHEN/plugins/a.prx")
        self.assertEqual(m.join_remote("/", "a.prx"), "/a.prx")
        self.assertEqual(m.join_remote("/data/", "a.prx"), "/data/a.prx")
        self.assertEqual(m.parent_remote("/data/GoldHEN/plugins.ini"), "/data/GoldHEN")
        self.assertEqual(m.parent_remote("/plugins.ini"), "/")
        self.assertEqual(m.parent_remote("/"), "/")

    def test_fmt_size(self) -> None:
        self.assertEqual(m.fmt_size(0), "0 B")
        self.assertEqual(m.fmt_size(4096), "4.0 KB")
        self.assertEqual(m.fmt_size(None), "-")

    def test_title_name_from_json(self) -> None:
        self.assertEqual(m.title_name_from_json('{"titleName": "Bloodborne"}'), "Bloodborne")
        self.assertEqual(m.title_name_from_json('{"titleName": ""}'), "")
        self.assertEqual(m.title_name_from_json("nonsense"), "")


class TestConfig(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = m.Config(host="192.168.1.50", port=2122, user="u", password="p",
                              remote_ini="/data/x/plugins.ini", auto_apply=False, timeout=30)
            config.remember_host("192.168.1.50")
            config.save(path)
            loaded = m.Config.load(path)
            self.assertEqual(loaded.host, "192.168.1.50")
            self.assertEqual(loaded.port, 2122)
            self.assertEqual(loaded.user, "u")
            self.assertEqual(loaded.password, "p")
            self.assertEqual(loaded.remote_ini, "/data/x/plugins.ini")
            self.assertFalse(loaded.auto_apply)
            self.assertEqual(loaded.timeout, 30)
            self.assertTrue(loaded.auto_connect)
            self.assertEqual(loaded.recent_hosts, ["192.168.1.50"])

    def test_auto_connect_flag_roundtrips(self) -> None:
        """'Connect automatically on startup' has to survive a restart."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            m.Config(host="10.0.0.7", auto_connect=False).save(path)
            self.assertFalse(m.Config.load(path).auto_connect)
            m.Config(host="10.0.0.7", auto_connect=True).save(path)
            self.assertTrue(m.Config.load(path).auto_connect)

    def test_remember_host_dedupes_and_limits(self) -> None:
        config = m.Config()
        for index in range(12):
            config.remember_host(f"10.0.0.{index}")
        config.remember_host("10.0.0.5")            # use an old one again
        self.assertEqual(config.recent_hosts[0], "10.0.0.5")
        self.assertEqual(len(config.recent_hosts), 10)
        self.assertEqual(len(set(config.recent_hosts)), 10)
        config.remember_host("   ")
        self.assertEqual(len(config.recent_hosts), 10)

    def test_missing_or_broken_file_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            self.assertEqual(m.Config.load(missing).port, m.DEFAULT_PORT)
            broken = Path(tmp) / "broken.json"
            broken.write_text("{{{ not json", encoding="utf-8")
            self.assertEqual(m.Config.load(broken).remote_ini, m.DEFAULT_INI)

    def test_bad_types_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"port": "not a number", "passive": "yes", "unknown": 5}),
                            encoding="utf-8")
            config = m.Config.load(path)
            self.assertEqual(config.port, m.DEFAULT_PORT)
            self.assertTrue(config.passive)

    def test_make_ftp(self) -> None:
        config = m.Config(host="1.2.3.4", password="secret", timeout=1)
        ftp = config.make_ftp()
        self.assertEqual(ftp.host, "1.2.3.4")
        self.assertEqual(ftp.password, "secret")
        self.assertGreaterEqual(ftp.timeout, 3)  # clamped to something sane


@unittest.skipUnless(_tk_usable(), "needs tkinter + a display")
class TestSavedServerFlow(unittest.TestCase):
    """Settings -> Save -> next start connects on its own."""

    HOST = "192.168.1.50"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._real_config_file = m.CONFIG_FILE
        m.CONFIG_FILE = Path(self._tmp.name) / "ps4_manager_config.json"
        self._apps: list = []

    def tearDown(self) -> None:
        for app in self._apps:
            try:
                app.worker.stop()
                app.destroy()
            except Exception:
                pass
        m.CONFIG_FILE = self._real_config_file
        self._tmp.cleanup()

    def make_app(self, config=None):
        app = m.Ps4ManagerApp(config or m.Config())
        self._apps.append(app)
        app.geometry("1080x700+40+40")
        # Hide the window: a test run must never pop up over the user's desktop.
        # Everything these tests assert (config file, label texts, callbacks)
        # works while the toplevel is unmapped.
        app.withdraw()
        return app

    def pump(self, app, seconds: float = 0.5) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.update()
            time.sleep(0.03)

    def stored(self) -> dict:
        return json.loads(Path(m.CONFIG_FILE).read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- save ---
    def test_settings_save_writes_the_ftp_details(self) -> None:
        app = self.make_app()
        self.pump(app)
        app.show_view("settings")
        view = app.views["settings"]
        view.vars["host"].set(self.HOST)
        view.vars["port"].set("2121")
        view.vars["user"].set("anonymous")
        self.assertTrue(view.save())
        self.pump(app, 0.1)
        data = self.stored()
        self.assertEqual(data["host"], self.HOST)
        self.assertEqual(data["port"], 2121)
        self.assertEqual(data["user"], "anonymous")
        self.assertTrue(data["auto_connect"])
        self.assertIn(self.HOST, data["recent_hosts"])
        # the card must tell the user what will happen next time
        self.assertIn("connects automatically", view.saved_label.cget("text"))

    def test_next_launch_connects_by_itself(self) -> None:
        self.make_app()          # 1st run: nothing saved
        app1 = self._apps[-1]
        self.pump(app1)
        app1.show_view("settings")
        app1.views["settings"].vars["host"].set(self.HOST)
        self.assertTrue(app1.views["settings"].save())

        # 2nd run: same config file, fresh app - startup must connect on its own
        app2 = self.make_app(m.Config.load())
        calls: list = []
        app2.connect_async = lambda load=True, announce=True: calls.append((load, announce))
        self.pump(app2, 0.8)
        self.assertEqual(calls, [(True, False)])
        self.assertEqual(app2.config.host, self.HOST)

    def test_auto_connect_off_starts_offline(self) -> None:
        cfg = m.Config(host=self.HOST, auto_connect=False)
        cfg.save()
        app = self.make_app(m.Config.load())
        calls: list = []
        app.connect_async = lambda load=True, announce=True: calls.append((load, announce))
        self.pump(app, 0.8)
        self.assertEqual(calls, [])
        self.assertEqual(app.current_view, "settings")
        self.assertIn("off", app.views["settings"].saved_label.cget("text"))

    def test_forget_saved_ps4_clears_it(self) -> None:
        cfg = m.Config(host=self.HOST, password="secret", auto_connect=True)
        cfg.remember_host(self.HOST)
        cfg.save()
        app = self.make_app(m.Config.load())
        self.pump(app)
        app.show_view("settings")
        app.views["settings"].forget_saved()
        self.pump(app, 0.1)
        data = self.stored()
        self.assertEqual(data["host"], "")
        self.assertEqual(data["password"], "")
        self.assertEqual(data["recent_hosts"], [])
        self.assertIn("Nothing saved yet", app.views["settings"].saved_label.cget("text"))

    def test_closing_the_window_saves_the_typed_address(self) -> None:
        app = self.make_app()
        self.pump(app)
        app.show_view("settings")
        app.views["settings"].vars["host"].set(self.HOST)
        self.pump(app, 0.2)
        app.on_close()                      # no Save button pressed
        self.assertTrue(Path(m.CONFIG_FILE).exists())
        self.assertEqual(self.stored()["host"], self.HOST)


if __name__ == "__main__":
    unittest.main(verbosity=2)
