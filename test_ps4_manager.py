#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for ps4_manager.py.

No PS4 is needed. The parsing/back-end tests run anywhere; the saved-server
(save -> restart -> auto-connect) tests build real Tk widgets and therefore skip
themselves when there is no display.

    python3 -m unittest -v test_ps4_manager.py
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import socketserver
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import pkg_manager as pm
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


# ---------------------------------------------------------------------------
# PKG manager - parsing/back-end (no PS4, no console service needed)
# ---------------------------------------------------------------------------


class TestPkgHelpers(unittest.TestCase):
    def test_split_package_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "game.pkg").write_bytes(b"merged")
            (folder / "split_0.pkg").write_bytes(b"a")
            (folder / "split_1.pkg").write_bytes(b"b")
            # a merged package is sent on its own
            self.assertEqual(pm.split_package_parts(folder / "game.pkg"), [folder / "game.pkg"])
            # pieces are found and sent in numeric order
            self.assertEqual(pm.split_package_parts(folder / "split_1.pkg"),
                             [folder / "split_0.pkg", folder / "split_1.pkg"])
            # a gap in the numbering is not a complete package - send just the file
            (folder / "split_10.pkg").write_bytes(b"c")
            self.assertEqual(pm.split_package_parts(folder / "split_0.pkg"), [folder / "split_0.pkg"])

    def test_title_id_from_name(self) -> None:
        self.assertEqual(pm.title_id_from_name("UP0001-CUSA12345_00-EXAMPLE0000000.pkg"), "CUSA12345")
        self.assertEqual(pm.title_id_from_name("up0001-cusa12345_00-x.pkg"), "CUSA12345")
        self.assertEqual(pm.title_id_from_name("CUSA12345.pkg"), "CUSA12345")
        self.assertIsNone(pm.title_id_from_name("some-game-v2.pkg"))

    def test_looks_like_manifest(self) -> None:
        self.assertTrue(pm.looks_like_manifest("http://host/dir/MANIFEST.JSON"))
        self.assertTrue(pm.looks_like_manifest("https://gs2.example/app.json?token=abc"))
        self.assertFalse(pm.looks_like_manifest("http://host/dir/game.pkg"))

    def test_pkg_file_filter(self) -> None:
        names = ("a.pkg", "b.PKG", "c.pkg.part", "d.pkg.resume", "readme.txt", "games")
        entries = [m.RemoteEntry(name=n, path="/data/pkg/" + n, is_dir=False) for n in names]
        self.assertEqual([e.name for e in pm.pkg_entries(iter(entries))],
                         ["a.pkg", "b.PKG", "c.pkg.part", "d.pkg.resume"])

    def test_tcp_reachable(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            self.assertTrue(pm.tcp_reachable("127.0.0.1", listener.getsockname()[1], timeout=2))
        finally:
            listener.close()
        self.assertFalse(pm.tcp_reachable("127.0.0.1", 1, timeout=0.5))


class TestPkgLocalServer(unittest.TestCase):
    """The console downloads the package from this server, so it has to behave
    exactly like a file host: full GET, ranged GET (the installer reads the
    package header with one), HEAD, and nothing else."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # a space in the name on purpose - the URL must not need escaping
        self.path = Path(self._tmp.name) / "an example package.pkg"
        self.data = (bytes(range(256)) * 512) + b"tail"
        self.path.write_bytes(self.data)
        self.server = pm.LocalPkgServer([self.path], "127.0.0.1", port=0, allow_from="127.0.0.1")
        self.server.start()
        self.url = self.server.url_for(self.path)

    def tearDown(self) -> None:
        self.server.stop()
        self._tmp.cleanup()

    def fetch(self, url: str, headers=None, method: str = "GET"):
        request = urllib.request.Request(url, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_url_is_token_scoped_and_space_free(self) -> None:
        self.assertTrue(self.url.startswith("http://127.0.0.1:"))
        self.assertNotIn(" ", self.url)
        self.assertIn("/%s/" % self.server.token, self.url)
        self.assertEqual(self.server.urls(), [self.url])

    def test_full_download(self) -> None:
        status, headers, body = self.fetch(self.url)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.data)
        self.assertEqual(headers.get("Content-Length"), str(len(self.data)))
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")
        self.assertGreaterEqual(self.server.served_total(), len(self.data))
        self.assertEqual(self.server.expected_total(), len(self.data))

    def test_ranged_download_that_the_installer_needs(self) -> None:
        status, headers, body = self.fetch(self.url, {"Range": "bytes=1000-1099"})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.data[1000:1100])
        self.assertEqual(headers.get("Content-Range"), f"bytes 1000-1099/{len(self.data)}")
        status, _headers, tail = self.fetch(self.url, {"Range": f"bytes={len(self.data) - 10}-"})
        self.assertEqual(status, 206)
        self.assertEqual(tail, self.data[-10:])

    def test_head(self) -> None:
        status, headers, body = self.fetch(self.url, method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("Content-Length"), str(len(self.data)))

    def test_unknown_path_is_404(self) -> None:
        status, _headers, _body = self.fetch("http://127.0.0.1:%d/not-a-package.pkg" % self.server.port)
        self.assertEqual(status, 404)

    def test_other_hosts_are_refused(self) -> None:
        other = pm.LocalPkgServer([self.path], "127.0.0.1", port=0, allow_from="10.99.99.99")
        other.start()
        try:
            status, _headers, _body = self.fetch(other.url_for(self.path))
            self.assertEqual(status, 403)
        finally:
            other.stop()

    def test_stop_closes_the_port(self) -> None:
        self.server.stop()
        self.assertFalse(self.server.running)
        with self.assertRaises(urllib.error.URLError):
            self.fetch(self.url)


class MockInstaller:
    """A stand-in for Remote Package Installer on port 12800.

    It answers with the raw strings the console really sends - including the
    0x... hex literals, which are not valid JSON and have to be cleaned up by
    the client before parsing.  With ``download=True`` it also behaves like the
    console's downloader: it reads the package header with a ranged GET first,
    then fetches the whole file from the URL it was handed.
    """

    def __init__(self, download: bool = False) -> None:
        self.requests: list = []
        self.download = download
        self.downloaded: bytes | None = None
        self.range_requested = False
        self.download_error = ""
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                try:
                    body = json.loads(raw or "{}")
                except ValueError:
                    body = {}
                owner.requests.append((self.path, body))
                payload = owner.reply_for(self.path, body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        class Server(http.server.ThreadingHTTPServer):
            # no getfqdn() while binding: this is a test, not a DNS exercise
            def server_bind(self_inner) -> None:
                socketserver.TCPServer.server_bind(self_inner)
                self_inner.server_name = "mock-console"
                self_inner.server_port = self_inner.server_address[1]

        self.httpd = Server(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def start_download(self, url: str) -> None:
        """What the console's bgft downloader does, on a thread of its own."""
        def run() -> None:
            try:
                header = urllib.request.Request(url, headers={"Range": "bytes=0-1023"})
                with urllib.request.urlopen(header, timeout=15) as response:
                    self.range_requested = response.status == 206
                with urllib.request.urlopen(url, timeout=30) as response:
                    self.downloaded = response.read()
            except Exception as exc:  # pragma: no cover - the test asserts on the result
                self.download_error = repr(exc)

        threading.Thread(target=run, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def reply_for(self, path: str, body: dict) -> str:
        if path == "/api/is_exists":
            if body.get("title_id") == "CUSA11111":
                # exactly how the console writes it: a hex literal
                return '{"status":"success","exists":"true","size":0x40000000}'
            return '{"status":"success","exists":"false"}'
        if path == "/api/install":
            urls = list(body.get("packages") or []) + [body.get("url") or ""]
            if any("bad" in url for url in urls):
                return '{"status":"error","error":0x8002000b}'
            if self.download and body.get("packages"):
                self.start_download(body["packages"][0])
            return '{"status":"success","task_id":7,"title":"Example Game"}'
        if path == "/api/get_task_progress":
            task = body.get("task_id")
            if self.download and task == 7:
                if self.downloaded is None:
                    return '{"error":0,"preparing_percent":100,"length_total":0,"transferred_total":0,"local_copy_percent":0}'
                return '{"error":0,"preparing_percent":100,"length_total":0x10,"transferred_total":0x10,"local_copy_percent":100}'
            if task == 99:
                return '{"error":0,"preparing_percent":100,"length_total":0x100,"transferred_total":0x100,"local_copy_percent":100}'
            if task == 50:
                return '{"error":0,"preparing_percent":100,"length_total":0x1000,"transferred_total":0x1000,"local_copy_percent":40}'
            if task == 42:
                return '{"error":0,"preparing_percent":42,"length_total":0,"transferred_total":0,"local_copy_percent":0}'
            if task == 13:
                return '{"error":0x8002000b}'
            return '{"error":0,"preparing_percent":100,"length_total":0x1000,"transferred_total":0x800,"local_copy_percent":0}'
        if path.startswith("/api/uninstall") or path.endswith("_task"):
            return '{"status":"success"}'
        return '{"status":"success"}'


class TestPkgRemoteInstaller(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = MockInstaller()
        self.installer = pm.RemoteInstaller("127.0.0.1", port=self.mock.port, timeout=5)

    def tearDown(self) -> None:
        self.mock.close()

    def test_probe_finds_the_service(self) -> None:
        self.assertTrue(self.installer.probe())
        self.assertEqual(self.mock.requests[-1][0], "/api/is_exists")

    def test_probe_is_false_when_nothing_listens(self) -> None:
        missing = pm.RemoteInstaller("127.0.0.1", port=1, timeout=1)
        self.assertFalse(missing.probe())
        with self.assertRaises(pm.InstallerUnavailable):
            missing.install_direct(["http://host/a.pkg"])

    def test_install_direct_sends_every_piece_in_order(self) -> None:
        urls = ["http://192.168.1.50:9898/tok/aa.pkg", "http://192.168.1.50:9898/tok/bb.pkg"]
        task = self.installer.install_direct(urls)
        self.assertEqual(task.task_id, 7)
        self.assertEqual(task.title, "Example Game")
        self.assertEqual(self.mock.requests[-1],
                         ("/api/install", {"type": "direct", "packages": urls}))

    def test_install_manifest_uses_the_reference_type(self) -> None:
        manifest = "https://gs2.example/app.json"
        self.installer.install_manifest(manifest)
        self.assertEqual(self.mock.requests[-1],
                         ("/api/install", {"type": "ref_pkg_url", "url": manifest}))

    def test_a_refused_install_raises(self) -> None:
        with self.assertRaises(pm.InstallerError) as caught:
            self.installer.install_direct(["http://host/bad.pkg"])
        self.assertIn("refused", str(caught.exception))

    def test_progress_phases(self) -> None:
        downloading = self.installer.progress(7)
        self.assertEqual(downloading.state, "downloading")
        self.assertAlmostEqual(downloading.percent, 50.0)
        self.assertEqual((downloading.transferred, downloading.total), (0x800, 0x1000))
        self.assertFalse(downloading.failed)

        preparing = self.installer.progress(42)
        self.assertEqual(preparing.state, "preparing")
        self.assertAlmostEqual(preparing.percent, 42.0)

        installing = self.installer.progress(50)
        self.assertEqual(installing.state, "installing")
        self.assertAlmostEqual(installing.percent, 40.0)

        done = self.installer.progress(99)
        self.assertEqual(done.state, "done")
        self.assertTrue(done.done)
        self.assertAlmostEqual(done.percent, 100.0)

        failed = self.installer.progress(13)
        self.assertTrue(failed.failed)
        self.assertEqual(failed.state, "error")
        self.assertIn("0x8002000b", failed.detail)

    def test_is_exists_reads_the_hex_size(self) -> None:
        self.assertEqual(self.installer.is_exists("CUSA11111"), (True, 0x40000000))
        self.assertEqual(self.installer.is_exists("CUSA99999"), (False, None))

    def test_uninstall_calls(self) -> None:
        self.installer.uninstall("game", "CUSA12345")
        self.assertEqual(self.mock.requests[-1], ("/api/uninstall_game", {"title_id": "CUSA12345"}))
        self.installer.uninstall("patch", "CUSA12345")
        self.assertEqual(self.mock.requests[-1], ("/api/uninstall_patch", {"title_id": "CUSA12345"}))
        self.installer.uninstall("ac", "UP0001-CUSA12345_00-EXAMPLE0000")
        self.assertEqual(self.mock.requests[-1],
                         ("/api/uninstall_ac", {"content_id": "UP0001-CUSA12345_00-EXAMPLE0000"}))
        with self.assertRaises(pm.InstallerError):
            self.installer.uninstall("nonsense", "CUSA12345")

    def test_task_control(self) -> None:
        self.installer.task_action("stop", 7)
        self.assertEqual(self.mock.requests[-1], ("/api/stop_task", {"task_id": 7}))
        self.installer.task_action("resume", 7)
        self.assertEqual(self.mock.requests[-1], ("/api/resume_task", {"task_id": 7}))


@unittest.skipUnless(_tk_usable(), "needs tkinter + a display")
class TestPkgManagerView(unittest.TestCase):
    """The PKG manager tab inside the plugin tool."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._real_config_file = m.CONFIG_FILE
        m.CONFIG_FILE = Path(self._tmp.name) / "ps4_manager_config.json"
        self.app = m.Ps4ManagerApp(m.Config(host="192.168.1.50"))
        self.app.geometry("1080x700+40+40")
        self.app.withdraw()          # never flash a window over the desktop

    def tearDown(self) -> None:
        try:
            self.app.worker.stop()
            self.app.destroy()
        except Exception:
            pass
        m.CONFIG_FILE = self._real_config_file
        self._tmp.cleanup()

    def test_tab_is_part_of_the_plugin_tool(self) -> None:
        view = self.app.views["pkgs"]
        self.assertIsInstance(view, m.PkgManagerView)
        self.app.show_view("pkgs")
        self.assertEqual(self.app.current_view, "pkgs")
        # the sidebar keeps the plugin tool lit for every tab inside it
        self.assertTrue(self.app.nav_buttons["plugins"]._active)
        self.assertTrue(any(key == "pkgs" for key, _button in self.app.tool_tabs))

    def test_defaults_and_helpers(self) -> None:
        view = self.app.views["pkgs"]
        self.assertEqual(view.pkg_dir(), "/data/pkg")
        self.assertEqual(view.console_host(), "192.168.1.50")
        installer = pm.RemoteInstaller("192.168.1.50")
        self.assertEqual(installer.port, 12800)

    def test_pkg_dir_roundtrips_through_the_config(self) -> None:
        config = m.Config(host="192.168.1.50", pkg_dir="/data/my pkgs")
        path = Path(self._tmp.name) / "roundtrip.json"
        config.save(path)
        self.assertEqual(m.Config.load(path).pkg_dir, "/data/my pkgs")
        self.assertEqual(m.Config.load(Path(self._tmp.name) / "missing.json").pkg_dir, "/data/pkg")

    def test_a_bad_link_is_refused_before_touching_the_console(self) -> None:
        view = self.app.views["pkgs"]
        view.var_url.set("definitely not a link")
        view.install_from_link()
        self.assertIn("http", self.app.status_label.cget("text"))
        view.var_url.set("")
        view.install_from_link()
        self.assertIn("link", self.app.status_label.cget("text"))

    def test_install_without_a_console_address_explains_itself(self) -> None:
        self.app.config.host = ""
        view = self.app.views["pkgs"]
        view.var_url.set("http://example.com/game.pkg")
        view.install_from_link()
        self.assertIn("Connect", self.app.status_label.cget("text"))

    def test_shutdown_is_safe_when_nothing_is_running(self) -> None:
        self.app.views["pkgs"].shutdown()
        self.app.on_close()


@unittest.skipUnless(_tk_usable(), "needs tkinter + a display")
class TestPkgInstallFlow(unittest.TestCase):
    """The whole install path, with only the console itself faked.

    The view starts its real HTTP server for a real .pkg file, the mock console
    fetches that URL the way the PS4 downloader does (ranged header read, then
    the file), and the view polls the task until it reports success.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._real_config_file = m.CONFIG_FILE
        m.CONFIG_FILE = Path(self._tmp.name) / "cfg.json"
        self.pkg = Path(self._tmp.name) / "an example_0.pkg"
        self.pkg.write_bytes(bytes(range(256)) * 1200)      # 300 KB
        self.mock = MockInstaller(download=True)
        self._real_installer = pm.RemoteInstaller
        mock_port = self.mock.port
        # the view builds RemoteInstaller(host) with the real port default; send
        # it to the mock instead
        pm.RemoteInstaller = lambda host, port=None, **kw: self._real_installer(
            host, port=mock_port if port is None else port, **kw)
        # no console FTP in these tests: the point is the install path
        self.app = m.Ps4ManagerApp(m.Config(host="127.0.0.1", auto_connect=False))
        self.app.geometry("1080x700+40+40")
        self.app.withdraw()
        # the app writes its own status line 120 ms after it is built; let that
        # finish so the assertions below see the status this test causes
        self.pump(0.4)

    def tearDown(self) -> None:
        pm.RemoteInstaller = self._real_installer
        try:
            self.app.views["pkgs"].shutdown()
            self.app.worker.stop()
            self.app.destroy()
        except Exception:
            pass
        self.mock.close()
        m.CONFIG_FILE = self._real_config_file
        self._tmp.cleanup()

    def pump(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.app.update()
            time.sleep(0.03)

    def test_local_package_reaches_the_console_and_installs(self) -> None:
        view = self.app.views["pkgs"]
        view.local_pkg = self.pkg
        view.install_from_pc()
        self.pump(2.0)

        self.assertIsNotNone(view.task, "the install task was never tracked")
        self.assertEqual(view.task.task_id, 7)
        install_calls = [body for path, body in self.mock.requests if path == "/api/install"]
        self.assertEqual(len(install_calls), 1)
        urls = install_calls[0]["packages"]
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith("http://127.0.0.1:"), urls[0])
        # the served URL really is fetched, and byte for byte the right file
        self.assertEqual(self.mock.download_error, "")
        self.assertEqual(self.mock.downloaded, self.pkg.read_bytes())  # 300 KB, byte for byte
        self.assertTrue(self.mock.range_requested, "the console reads the header with a range request")

        # then the progress poll walks the task to 'installed'
        self.pump(5.0)
        self.assertIsNone(view.task)
        self.assertEqual(view.progress_label.cget("text"), "Installed.")
        self.assertIn("installed", self.app.status_label.cget("text").lower())
        self.assertAlmostEqual(float(view.progress["value"]), 100.0)
        self.assertFalse(view.server is not None and view.server.running,
                         "the HTTP server must stop once the install is done")

    def test_install_from_a_link_uses_the_console_installer(self) -> None:
        view = self.app.views["pkgs"]
        view.var_url.set("http://files.example/UP0001-CUSA12345_00-EXAMPLE.pkg")
        view.install_from_link()
        self.pump(2.0)
        install_calls = [body for path, body in self.mock.requests if path == "/api/install"]
        self.assertEqual(len(install_calls), 1)
        self.assertEqual(install_calls[0]["type"], "direct")
        self.assertEqual(install_calls[0]["packages"],
                         ["http://files.example/UP0001-CUSA12345_00-EXAMPLE.pkg"])
        # nothing to serve: a link install must not open a local server
        self.assertIsNone(view.server)
        view.shutdown()

    def test_uninstall_asks_the_console(self) -> None:
        view = self.app.views["pkgs"]
        view.installed_rows["app::CUSA12345"] = m.RemoteEntry(
            name="CUSA12345", path="/user/appmeta/CUSA12345", is_dir=True)
        original = m.messagebox.askyesno
        m.messagebox.askyesno = lambda *a, **kw: True
        try:
            view.apps_tree.insert("", "end", iid="app::CUSA12345", text="CUSA12345")
            view.apps_tree.selection_set("app::CUSA12345")
            view.uninstall_selected()
            self.pump(2.0)
        finally:
            m.messagebox.askyesno = original
        uninstalls = [body for path, body in self.mock.requests if path == "/api/uninstall_game"]
        self.assertEqual(uninstalls, [{"title_id": "CUSA12345"}])
        self.assertIn("Uninstalled CUSA12345", self.app.status_label.cget("text"))
        self.assertEqual(self.app.worker.busy, 0, "the busy indicator must be cleared")

    def test_console_without_the_service_reports_it(self) -> None:
        self.mock.close()                       # the console stops answering
        view = self.app.views["pkgs"]
        view.var_url.set("http://files.example/game.pkg")
        view.install_from_link()
        self.pump(1.0)
        # the status line is shared with the rest of the app, so assert on the
        # view's own state and on the (append-only) log
        self.assertEqual(view.service_pill.label.cget("text"), "Installer not running")
        self.assertIn("12800", self.app.log_text.get("1.0", "end"))
        self.assertIsNone(view.server)


class MockBinLoader:
    """A payload server that answers the way GoldHEN's BinLoader does.

    It greets a new client with a banner (GoldHEN's own text), swallows
    everything the client sends and can reply afterwards. ``banner=""`` is the
    common real case: BinLoader connects and simply says nothing.
    """

    def __init__(self, banner: str = "GoldHEN BinLoader v2.4b18.3\n", reply: str = "") -> None:
        self.banner = banner
        self.reply = reply
        self.received = b""
        self.connections = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(3.0)
            if self.banner:
                conn.sendall(self.banner.encode("utf-8"))
            chunks = []
            while True:
                try:
                    chunk = conn.recv(65536)
                except (socket.timeout, OSError):
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            self.received += b"".join(chunks)
            if self.reply:
                conn.sendall(self.reply.encode("utf-8"))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


class TestPayloaderClient(unittest.TestCase):
    """The GoldHEN payload server client, against a stand-in server."""

    def setUp(self) -> None:
        self.mock = MockBinLoader()

    def tearDown(self) -> None:
        self.mock.close()

    def make(self) -> "m.Payloader":
        return m.Payloader("127.0.0.1", self.mock.port, timeout=3)

    def test_connect_reads_the_banner_and_stays_open(self) -> None:
        payloader = self.make()
        banner = payloader.connect()
        self.assertIn("GoldHEN", banner)
        self.assertTrue(payloader.connected)
        self.assertIn("GoldHEN", payloader.banner_summary())
        payloader.close()
        self.assertFalse(payloader.connected)

    def test_a_silent_server_is_still_a_working_payloader(self) -> None:
        self.mock.close()
        self.mock = MockBinLoader(banner="")     # BinLoader usually says nothing
        payloader = self.make()
        self.assertEqual(payloader.connect(), "")
        self.assertTrue(payloader.connected, "silence is not an error")
        self.assertEqual(payloader.banner_summary(), "payload server ready")
        payloader.close()

    def test_probe_describes_and_disconnects(self) -> None:
        payloader = self.make()
        self.assertIn("GoldHEN", payloader.probe())
        self.assertFalse(payloader.connected, "a probe must not leave the socket open")

    def test_nothing_listening_is_a_friendly_error(self) -> None:
        spare = socket.socket()
        spare.bind(("127.0.0.1", 0))
        dead_port = spare.getsockname()[1]
        spare.close()                             # nothing listens on it now
        payloader = m.Payloader("127.0.0.1", dead_port, timeout=2)
        with self.assertRaises(m.PayloaderError) as caught:
            payloader.connect()
        message = str(caught.exception)
        self.assertIn("Nothing is listening", message)
        self.assertIn("BinLoader", message)
        self.assertFalse(payloader.connected)

    def test_a_payload_reaches_the_console_byte_for_byte(self) -> None:
        self.mock.reply = "OK"
        data = bytes(range(256)) * 300            # 76 KB: more than one chunk
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "installer.bin"
            payload.write_bytes(data)
            seen = []
            payloader = self.make()
            payloader.connect()
            reply = payloader.send_file(payload, progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(reply, "OK")
        self.assertEqual(self.mock.received, data)
        self.assertEqual(seen[-1], (len(data), len(data)))
        self.assertEqual(seen[-1][0], seen[-1][1])
        self.assertFalse(payloader.connected, "binloader is done with the connection after a payload")

    def test_sending_without_a_connection_is_an_error(self) -> None:
        payloader = self.make()
        with self.assertRaises(m.PayloaderError):
            payloader.send_bytes(b"\x00")


class TestPayloaderSettings(unittest.TestCase):
    """An address in the settings is the opt-in - nothing else to press."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._real_config_file = m.CONFIG_FILE
        m.CONFIG_FILE = Path(self._tmp.name) / "ps4_manager_config.json"

    def tearDown(self) -> None:
        m.CONFIG_FILE = self._real_config_file
        self._tmp.cleanup()

    def test_an_empty_address_means_off(self) -> None:
        for host in ("", "   ", "\t"):
            self.assertFalse(m.Config(payloader_host=host).payloader_enabled, repr(host))
        self.assertTrue(m.Config(payloader_host=" 192.168.1.50 ").payloader_enabled)

    def test_the_address_and_port_survive_a_restart(self) -> None:
        m.Config(host="192.168.1.50", payloader_host=" 192.168.1.50 ", payloader_port=9020).save()
        again = m.Config.load()
        self.assertEqual(again.payloader_host, " 192.168.1.50 ")   # load keeps the file's value
        self.assertEqual(again.payloader_port, 9020)
        self.assertTrue(again.payloader_enabled)
        self.assertEqual(again.payloader_target(), "192.168.1.50:9020")
        # an old config file has no payloader keys at all: GoldHEN's default applies
        Path(m.CONFIG_FILE).write_text(json.dumps({"host": "192.168.1.50"}), encoding="utf-8")
        fresh = m.Config.load()
        self.assertEqual(fresh.payloader_port, m.DEFAULT_PAYLOAD_PORT)
        self.assertEqual(m.DEFAULT_PAYLOAD_PORT, 9090)
        self.assertFalse(fresh.payloader_enabled)

    def test_a_hand_edited_port_is_repaired(self) -> None:
        for broken in (0, -5, "nonsense"):
            config = m.Config(payloader_host="192.168.1.50", payloader_port=broken)  # type: ignore[arg-type]
            self.assertEqual(config.payloader_port_number(), m.DEFAULT_PAYLOAD_PORT, repr(broken))
            self.assertEqual(config.payloader_target(), "192.168.1.50:9090")
            self.assertEqual(config.make_payloader().port, 9090)


class FakeFtp:
    """Stand-in for the console's FTP connection; these tests are about the payloader."""

    def __init__(self) -> None:
        self.connected_calls = 0
        self.close_calls = 0

    def connect(self) -> None:
        self.connected_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class FakeConfig(m.Config):
    """Config that hands out a FakeFtp, so no console is needed here.

    The FTP stand-in is a class attribute on purpose: an instance attribute
    would land in Config.__dict__, and saving the settings writes that dict as
    JSON - a function in there is not serialisable.
    """

    fake_ftp: "FakeFtp"

    def make_ftp(self, logger=None) -> "FakeFtp":      # type: ignore[override]
        return FakeConfig.fake_ftp


class TestPayloaderConnectFlow(unittest.TestCase):
    """One Connect press opens FTP *and* the payload server - no second button."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._real_config_file = m.CONFIG_FILE
        m.CONFIG_FILE = Path(self._tmp.name) / "ps4_manager_config.json"
        self.mock = MockBinLoader()
        self.ftp = FakeFtp()
        FakeConfig.fake_ftp = self.ftp
        self.app = m.Ps4ManagerApp(FakeConfig(host="192.168.1.50", auto_connect=False))
        self.app.geometry("1080x700+40+40")
        self.app.withdraw()
        self.pump(0.4)          # let the app's own startup status line land first

    def tearDown(self) -> None:
        try:
            self.app.worker.stop()
            self.app.destroy()
        except Exception:
            pass
        self.mock.close()
        m.CONFIG_FILE = self._real_config_file
        self._tmp.cleanup()

    def pump(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.app.update()
            time.sleep(0.03)

    def connect(self, host: str = "", port: int = 0) -> None:
        self.app.config.payloader_host = host
        if port:
            self.app.config.payloader_port = port
        self.app.connect_async(load=False, announce=False)
        self.pump(3.0)

    def test_one_connect_brings_up_the_ftp_and_the_payload_server(self) -> None:
        self.connect("127.0.0.1", self.mock.port)
        self.assertEqual(self.ftp.connected_calls, 1)
        self.assertIs(self.app.ftp, self.ftp)
        self.assertIsNotNone(self.app.payloader)
        self.assertTrue(self.app.payloader.connected)
        self.assertEqual(self.app.pay_pill.label.cget("text"), "Payload ready")
        self.assertIn("payload server ready", self.app.status_label.cget("text").lower())
        self.assertIn("Payload server ready", self.app.log_text.get("1.0", "end"))
        self.assertEqual(self.mock.connections, 1)
        # Disconnect must close both, and say so
        self.app.disconnect()
        self.pump(0.3)
        self.assertIsNone(self.app.payloader)
        self.assertIsNone(self.app.ftp)
        self.assertEqual(self.ftp.close_calls, 1)
        self.assertEqual(self.app.pay_pill.label.cget("text"), "Payload off")

    def test_a_dead_payload_server_never_costs_the_ftp_session(self) -> None:
        spare = socket.socket()
        spare.bind(("127.0.0.1", 0))
        dead_port = spare.getsockname()[1]
        spare.close()
        self.connect("127.0.0.1", dead_port)
        self.assertIs(self.app.ftp, self.ftp, "the FTP side must still be up")
        self.assertIsNone(self.app.payloader)
        self.assertEqual(self.app.pay_pill.label.cget("text"), "Payload no reply")
        status = self.app.status_label.cget("text").lower()
        self.assertIn("ftp only", status)
        self.assertIn("nothing is listening", status)

    def test_without_an_address_the_payloader_is_never_touched(self) -> None:
        self.connect("")
        self.assertIs(self.app.ftp, self.ftp)
        self.assertIsNone(self.app.payloader)
        self.assertEqual(self.mock.connections, 0, "no address saved -> no payloader traffic")
        self.assertEqual(self.app.pay_pill.label.cget("text"), "Payload off")

    def test_the_settings_card_drives_all_of_it(self) -> None:
        view = self.app.views["settings"]
        view.vars["host"].set("192.168.1.50")
        view.vars["payloader_host"].set("")
        view._refresh_payloader_hint()
        self.assertIn("FTP only", view.payload_result.cget("text"))
        # the copy button saves the user from typing the same address twice
        view._copy_host_to_payloader()
        self.assertEqual(view.vars["payloader_host"].get(), "192.168.1.50")
        self.assertIn("filled in for the payload server", view.payload_result.cget("text"))
        view._refresh_payloader_hint()        # what the card says once it settles
        self.assertIn("also open the payload server", view.payload_result.cget("text"))
        # it is saved as a real number, not as the text that was typed
        view.vars["payloader_host"].set("127.0.0.1")
        view.vars["payloader_port"].set(str(self.mock.port))
        self.assertTrue(view.save(quiet=True))
        stored = json.loads(Path(m.CONFIG_FILE).read_text(encoding="utf-8"))
        self.assertEqual(stored["payloader_host"], "127.0.0.1")
        self.assertEqual(stored["payloader_port"], self.mock.port)
        self.assertNotIn("payloader_enabled", stored, "enabled is derived from the address, not stored")
        self.assertTrue(self.app.config.payloader_enabled)
        # Test payload server probes what is typed right now
        view.test_payloader()
        self.pump(3.0)
        self.assertIn("GoldHEN", view.payload_result.cget("text"))
        self.assertEqual(self.app.worker.busy, 0, "the busy indicator must be cleared")
        self.assertEqual(self.mock.connections, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
