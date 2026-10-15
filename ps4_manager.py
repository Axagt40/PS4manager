#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PS4 Manager
===========

A tiny, modern-looking desktop app for a jailbroken PS4, talking to the FTP
server GoldHEN runs on it (port 2121, anonymous login). Today it manages plugins
and files; it is meant to grow more PS4 tools over time.

For plugins it downloads ``/data/GoldHEN/plugins.ini`` into memory, shows
every plugin (default section + per-game sections) and lets you:

  * toggle plugins on/off with a click (writes ``=true`` / ``=false``)
  * add plugins, either for every game (``[default]``) or for one game
    (a title id section, picked from a list or typed in)
  * remove plugins, browse the plugins folder, upload/download/delete files
  * keep automatic local backups and restore them

Everything is standard library only - there is nothing to pip install.
The only thing that can be missing on some Linux installs is Python's own
``tkinter`` module, and the script offers to install that for you on first
run (``--no-install`` disables that).

Run it with::

    python3 ps4_manager.py

Notes for real hardware:
  * GoldHEN's FTP server must be enabled in the GoldHEN settings menu.
  * Default credentials are user ``anonymous`` with an empty password.
  * plugins.ini lives in ``/data/GoldHEN/plugins.ini`` and plugins live in
    ``/data/GoldHEN/plugins/``.
  * GoldHEN only reads plugins.ini at (GoldHEN) startup / game launch, so
    restart the game (or GoldHEN) after changing things.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ftplib
import functools
import io
import json
import os
try:
    import pkg_manager
except ImportError as _missing:
    # The PKG manager's back end ships beside this file. If somebody copies
    # only ps4_manager.py, say what is wrong instead of dumping a traceback.
    raise SystemExit(
        "PS4 Manager cannot start: pkg_manager.py is missing.\n"
        "It belongs in the same folder as ps4_manager.py (the PKG manager's "
        "back end).\nCopy it from the project folder on GitHub "
        "(github.com/Axagt40/PS4manager) and start the app again."
    ) from _missing
import platform
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

APP_NAME = "PS4 Manager"
APP_TAGLINE = "Tools for a jailbroken PS4 over FTP"
APP_VERSION = "1.2.2"
DEFAULT_PORT = 2121
# GoldHEN's payload server ("BinLoader"). 9090 is the default; GoldHEN can also
# listen on 9020/9021, which is why the port is configurable.
DEFAULT_PAYLOAD_PORT = 9090
DEFAULT_INI = "/data/GoldHEN/plugins.ini"
DEFAULT_PLUGINS_DIR = "/data/GoldHEN/plugins"
DEFAULT_SECTION = "default"
# Where installed titles (and their param.sfo with the real game name) live.
APPMETA_DIRS = ("/system_data/priv/appmeta", "/user/appmeta")
TITLE_ID_RE = re.compile(r"^[A-Z]{4}[0-9]{5}$")
PLUGIN_EXTS = (".prx", ".sprx", ".bin", ".elf")

# ---------------------------------------------------------------------------
# tkinter import (kept importable even when tkinter is missing, so that the
# parsing/back-end code can be unit tested on headless machines)
# ---------------------------------------------------------------------------


class _StubModule:
    """Placeholder that hands out dummy classes so definitions below import."""

    def __init__(self, name: str, error: BaseException):
        self.__name__ = name
        self._error = error

    def __getattr__(self, item: str):
        return type(item, (), {})


TK_IMPORT_ERROR: Optional[BaseException] = None
try:  # pragma: no cover - depends on the machine
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox, ttk

    TK_OK = True
except Exception as _exc:  # pragma: no cover
    TK_IMPORT_ERROR = _exc
    TK_OK = False
    tk = _StubModule("tkinter", _exc)
    tkfont = _StubModule("tkinter.font", _exc)
    filedialog = _StubModule("tkinter.filedialog", _exc)
    messagebox = _StubModule("tkinter.messagebox", _exc)
    ttk = _StubModule("tkinter.ttk", _exc)


# ---------------------------------------------------------------------------
# Dependency bootstrap
# ---------------------------------------------------------------------------


def _run_cmd(cmd: Sequence[str], sudo: bool = False) -> Tuple[bool, str]:
    """Run an install command, echoing its output. Returns (ok, output)."""
    if sudo:
        sudo_exe = shutil.which("sudo")
        if sudo_exe:
            cmd = [sudo_exe] + list(cmd)
        elif os.geteuid() != 0:  # type: ignore[attr-defined]
            return False, "root or sudo is required"
    try:
        proc = subprocess.run(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=900,
        )
    except FileNotFoundError:
        return False, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return False, f"timed out: {' '.join(cmd)}"
    out = (proc.stdout or "").strip()
    return proc.returncode == 0, out


def tkinter_install_commands() -> List[Tuple[str, List[str], bool]]:
    """Return candidate (description, command, needs_root) install recipes."""
    system = platform.system()
    recipes: List[Tuple[str, List[str], bool]] = []
    if system == "Linux":
        if shutil.which("apt-get"):
            recipes.append(
                ("apt: python3-tk", ["apt-get", "install", "-y", "python3-tk"], True)
            )
        if shutil.which("dnf"):
            recipes.append(
                ("dnf: python3-tkinter", ["dnf", "install", "-y", "python3-tkinter"], True)
            )
        if shutil.which("yum"):
            recipes.append(
                ("yum: python3-tkinter", ["yum", "install", "-y", "python3-tkinter"], True)
            )
        if shutil.which("zypper"):
            recipes.append(
                ("zypper: python3-tk", ["zypper", "--non-interactive", "install", "python3-tk"], True)
            )
        if shutil.which("pacman"):
            recipes.append(("pacman: tk", ["pacman", "-S", "--noconfirm", "tk"], True))
        if shutil.which("apk"):
            recipes.append(("apk: py3-tkinter", ["apk", "add", "py3-tkinter"], True))
    elif system == "Darwin" and shutil.which("brew"):
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        recipes.append((f"brew: python-tk@{version}", ["brew", "install", f"python-tk@{version}"], False))
        recipes.append(("brew: python-tk", ["brew", "install", "python-tk"], False))
    return recipes


def tkinter_help_text() -> str:
    system = platform.system()
    lines = [f"Python's tkinter module is not available ({TK_IMPORT_ERROR})."]
    if system == "Windows":
        lines += [
            "Re-run the Python installer, choose 'Modify', and make sure",
            "'tcl/tk and IDLE' is ticked. Then start this script again.",
        ]
    elif system == "Darwin":
        lines += [
            "Homebrew Python:   brew install python-tk",
            "python.org Python: reinstall it and keep 'tcl/tk' enabled.",
        ]
    else:
        lines += [
            "Debian/Ubuntu: sudo apt-get install -y python3-tk",
            "Fedora:        sudo dnf install -y python3-tkinter",
            "Arch:          sudo pacman -S tk",
        ]
    return "\n".join(lines)


def ensure_tkinter(auto_install: bool = True) -> bool:
    """Make sure tkinter is importable, installing it when we can."""
    global TK_OK, tkfont, ttk, filedialog, messagebox, tk
    if TK_OK:
        return True

    print(f"[deps] {tkinter_help_text()}\n")
    if not auto_install:
        return False

    recipes = tkinter_install_commands()
    if not recipes:
        print("[deps] No known package manager found - please install tkinter manually.")
        return False

    for description, cmd, needs_root in recipes:
        print(f"[deps] Trying to install tkinter via {description} ...")
        ok, out = _run_cmd(cmd, sudo=needs_root)
        if out:
            print("\n".join("       " + ln for ln in out.splitlines()[-12:]))
        if not ok:
            continue
        # The module may now be importable in a fresh interpreter.
        probe = subprocess.run(
            [sys.executable, "-c", "import tkinter, sys; sys.stdout.write(str(tkinter.TkVersion))"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if probe.returncode == 0:
            print(f"[deps] tkinter {probe.stdout.strip()} installed. Restarting ...")
            try:
                os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
            except OSError:
                # Could not replace ourselves - at least load it in-process.
                import tkinter as _tk  # noqa: F401
                import tkinter.font as _tkfont  # noqa: F401
                from tkinter import filedialog as _fd, messagebox as _mb, ttk as _ttk  # noqa: F401

                tk, tkfont, ttk = _tk, _tkfont, _ttk
                filedialog, messagebox = _fd, _mb
                TK_OK = True
                return True
        print(f"[deps] {description} finished but tkinter is still missing.")

    print("[deps] Automatic install failed, please install manually.")
    return False


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def app_dir() -> Path:
    """Folder for config, backups and caches - next to the script (or the exe)."""
    if getattr(sys, "frozen", False):  # PyInstaller style bundle
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return base
    except OSError:
        fallback = Path.home() / ".config" / "ps4_plugin_manager"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


CONFIG_FILE = app_dir() / "ps4_manager_config.json"
BACKUP_DIR = app_dir() / "backups"
CACHE_FILE = app_dir() / "title_cache.json"


def fmt_size(num: Optional[int]) -> str:
    if num is None:
        return "-"
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def fmt_time(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def is_title_id(text: str) -> bool:
    return bool(TITLE_ID_RE.match((text or "").strip().upper()))


def norm_title_id(text: str) -> str:
    return (text or "").strip().upper().replace(" ", "")


def join_remote(directory: str, name: str) -> str:
    directory = (directory or "/").rstrip("/")
    if not directory:
        directory = "/"
    return f"{directory}/{name}" if directory != "/" else f"/{name}"


def parent_remote(path: str) -> str:
    path = (path or "/").rstrip("/")
    if not path:
        return "/"
    idx = path.rfind("/")
    if idx <= 0:
        return "/"
    return path[:idx]


def round_rect_points(x1: float, y1: float, x2: float, y2: float, r: float) -> List[float]:
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]


def draw_round_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    return canvas.create_polygon(round_rect_points(x1, y1, x2, y2, r), smooth=True, splinesteps=12, **kwargs)


def widget_bg(widget, default: str = "#14161a") -> str:
    try:
        return widget.cget("bg")
    except Exception:
        return default


# ---------------------------------------------------------------------------
# plugins.ini model
# ---------------------------------------------------------------------------


@dataclass
class PluginEntry:
    """One plugin line: ``/path/to/plugin.prx`` or ``/path/to/plugin.prx=true``."""

    path: str
    value: Optional[str] = None          # None -> bare path (always enabled)
    orig_value: Optional[str] = None     # remembered non-boolean value
    style: str = "bare"                  # "bare" (no '=value') or "flag" ('=true'/'=false')
    kind: str = "plugin"                 # "plugin" or "raw" (unknown lines)
    raw_line: str = ""                   # exact original text for raw lines

    @property
    def enabled(self) -> bool:
        if self.value is None:
            return True
        return self.value.strip().lower() != "false"

    @property
    def togglable(self) -> bool:
        if self.kind != "plugin":
            return False
        if self.value is None:
            return True
        return self.value.strip().lower() in ("true", "false")

    def set_enabled(self, enabled: bool) -> None:
        """Flip the entry, keeping whichever notation the file already uses."""
        if not enabled:
            self.value = "false"
            return
        if self.orig_value:
            self.value = self.orig_value
        elif self.style == "bare":
            self.value = None
        else:
            self.value = "true"

    def dumps(self) -> str:
        if self.kind == "raw" and self.raw_line:
            return self.raw_line
        return self.path if self.value is None else f"{self.path}={self.value}"


@dataclass
class IniSection:
    name: str
    items: List[Any] = field(default_factory=list)  # PluginEntry | str (comment)

    @property
    def entries(self) -> List[PluginEntry]:
        return [i for i in self.items if isinstance(i, PluginEntry)]

    def find(self, path: str) -> Optional[PluginEntry]:
        wanted = path.strip()
        for entry in self.entries:
            if entry.path.strip() == wanted:
                return entry
        return None


class PluginsIni:
    """Parse/serialise GoldHEN's plugins.ini while preserving comments & order."""

    SECTION_RE = re.compile(r"^\[(.*)\]\s*$")

    def __init__(self) -> None:
        self.items: List[Any] = []  # str (comment/blank) | IniSection

    # -- parsing ----------------------------------------------------------
    @classmethod
    def parse(cls, text: str) -> "PluginsIni":
        doc = cls()
        current: Optional[IniSection] = None
        for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped:
                if current is None:
                    doc.items.append("")
                else:
                    current.items.append("")
                continue
            if stripped.startswith(";") or stripped.startswith("#"):
                (doc.items if current is None else current.items).append(line)
                continue
            m = cls.SECTION_RE.match(stripped)
            if m:
                current = IniSection(m.group(1).strip())
                doc.items.append(current)
                continue
            entry = cls._parse_entry(stripped)
            if current is None:
                # A plugin outside any section: GoldHEN treats these as global,
                # so fold it into the implicit default section.
                current = doc.default_section()
                current.items.append(entry)
            else:
                current.items.append(entry)
        return doc

    @staticmethod
    def _parse_entry(line: str) -> PluginEntry:
        if "=" in line:
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if "/" in key or key.lower().endswith(PLUGIN_EXTS) or key.startswith("\\"):
                if value.lower() in ("true", "false"):
                    return PluginEntry(path=key, value=value, style="flag", kind="plugin")
                return PluginEntry(path=key, value=value, orig_value=value, style="flag", kind="plugin")
            return PluginEntry(path=key, value=value, orig_value=value, kind="raw", raw_line=line)
        return PluginEntry(path=line, value=None, style="bare", kind="plugin")

    # -- serialising ------------------------------------------------------
    def dumps(self) -> str:
        lines: List[str] = []
        for item in self.items:
            if isinstance(item, IniSection):
                lines.append(f"[{item.name}]")
                for sub in item.items:
                    lines.append(sub if isinstance(sub, str) else sub.dumps())
            else:
                lines.append(item)
        text = "\n".join(lines).rstrip("\n") + "\n"
        return text

    # -- structure helpers ------------------------------------------------
    def sections(self) -> List[IniSection]:
        return [i for i in self.items if isinstance(i, IniSection)]

    def section_names(self) -> List[str]:
        return [s.name for s in self.sections()]

    def find_section(self, name: str) -> Optional[IniSection]:
        wanted = (name or "").strip().lower()
        for section in self.sections():
            if section.name.strip().lower() == wanted:
                return section
        return None

    def default_section(self, create: bool = True) -> Optional[IniSection]:
        section = self.find_section(DEFAULT_SECTION)
        if section is None and create:
            section = IniSection(DEFAULT_SECTION)
            # Keep [default] as the very first section, like GoldHEN's docs.
            insert_at = 0
            while insert_at < len(self.items) and isinstance(self.items[insert_at], str):
                insert_at += 1
            self.items.insert(insert_at, section)
        return section

    def get_or_create_section(self, name: str) -> IniSection:
        name = (name or DEFAULT_SECTION).strip()
        if name.lower() == DEFAULT_SECTION:
            section = self.default_section(create=True)
            assert section is not None
            return section
        section = self.find_section(name)
        if section is None:
            section = IniSection(name)
            self.items.append(section)
        return section

    def remove_section(self, name: str) -> bool:
        for idx, item in enumerate(self.items):
            if isinstance(item, IniSection) and item.name == name:
                del self.items[idx]
                return True
        return False

    def add(self, section_name: str, path: str, enabled: bool = True) -> Tuple[IniSection, PluginEntry]:
        section = self.get_or_create_section(section_name)
        existing = section.find(path)
        if existing is not None:
            existing.set_enabled(enabled)
            return section, existing
        entry = PluginEntry(path=path, value="true" if enabled else "false", style="flag")
        section.items.append(entry)
        return section, entry

    def titles(self) -> List[str]:
        return sorted({s.name for s in self.sections() if is_title_id(s.name)})

    def stats(self) -> Tuple[int, int]:
        entries = [e for s in self.sections() for e in s.entries if e.kind == "plugin"]
        enabled = sum(1 for e in entries if e.enabled)
        return enabled, len(entries)


# ---------------------------------------------------------------------------
# SFO parsing (to show real game names)
# ---------------------------------------------------------------------------


def parse_sfo(data: bytes) -> Dict[str, Any]:
    """Minimal param.sfo parser - returns the key/value pairs it understands."""
    out: Dict[str, Any] = {}
    if len(data) < 20 or data[:4] != b"\x00PSF":
        return out
    try:
        _version, key_off, data_off, count = struct.unpack_from("<IIII", data, 4)
    except struct.error:
        return out

    def read_cstr(offset: int) -> str:
        end = data.find(b"\x00", offset)
        if end < 0:
            end = len(data)
        return data[offset:end].decode("utf-8", "replace")

    for i in range(min(count, 4096)):
        pos = 20 + i * 16
        if pos + 16 > len(data):
            break
        try:
            koff, fmt, _dlen, _dmax, doff = struct.unpack_from("<HHIII", data, pos)
        except struct.error:
            break
        key = read_cstr(key_off + koff)
        base = data_off + doff
        if fmt == 0x0204:  # UTF-8 string
            out[key] = read_cstr(base)
        elif fmt == 0x0404:  # UTF-8 special (key = value)
            out[key] = read_cstr(base)
        elif fmt == 0x0004 and base + 4 <= len(data):
            out[key] = struct.unpack_from("<I", data, base)[0]
    return out


def title_name_from_json(text: str) -> str:
    try:
        payload = json.loads(text)
    except Exception:
        return ""
    if isinstance(payload, dict):
        for key in ("titleName", "title_name", "TITLE", "title"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


# ---------------------------------------------------------------------------
# FTP listing parsing
# ---------------------------------------------------------------------------


@dataclass
class RemoteEntry:
    name: str
    path: str
    is_dir: bool
    size: Optional[int] = None
    mtime: Optional[float] = None


_LIST_RE = re.compile(
    r"^(?P<perms>[-dlbcps][rwxstST-]{9}[.+]?)\s+"
    r"(?P<links>\d+)\s+"
    r"(?P<owner>\S+)\s+"
    r"(?P<group>\S+)\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<mon>[A-Za-z]{3})\s+"
    r"(?P<day>\d{1,2})\s+"
    r"(?P<year_or_time>\d{2}:\d{2}|\d{4})\s+"
    r"(?P<name>.+)$"
)

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def parse_list_line(line: str, directory: str = "/") -> Optional[RemoteEntry]:
    """Parse a single ``ls -l`` style FTP LIST line (handles odd PS4 output)."""
    line = (line or "").rstrip("\r\n")
    if not line.strip() or line.lower().startswith("total"):
        return None
    match = _LIST_RE.match(line.strip())
    if not match:
        # Fallback: name only listings (some servers send bare names).
        name = line.strip()
        if name in (".", "..") or "/" in name:
            return None
        return RemoteEntry(name=name, path=join_remote(directory, name), is_dir=False)
    name = match.group("name")
    if name in (".", ".."):
        return None
    # Strip a symlink target ("name -> /somewhere").
    if " -> " in name:
        name = name.split(" -> ", 1)[0].strip()
    perms = match.group("perms")
    is_dir = perms.startswith("d")
    stem = perms.lstrip("-dlbcps").rstrip(".")
    if "t" in stem and not is_dir and perms.startswith("-"):
        perms = "d" + perms[1:]  # some servers mark dirs as "-...t"
        is_dir = True
    size = None
    try:
        size = int(match.group("size"))
    except ValueError:
        size = None
    mtime = None
    try:
        month = _MONTHS.get(match.group("mon")[:3].title(), 1)
        day = int(match.group("day"))
        year_part = match.group("year_or_time")
        if ":" in year_part:
            hour, minute = (int(x) for x in year_part.split(":"))
            now = datetime.now()
            year = now.year
            candidate = datetime(year, month, day, hour, minute)
            if candidate > now.replace(year=now.year + 1, month=1, day=1):
                candidate = candidate.replace(year=year - 1)
            mtime = candidate.timestamp()
        else:
            mtime = datetime(int(year_part), month, day).timestamp()
    except Exception:
        mtime = None
    return RemoteEntry(name=name, path=join_remote(directory, name), is_dir=is_dir, size=size, mtime=mtime)


# ---------------------------------------------------------------------------
# FTP layer
# ---------------------------------------------------------------------------


class FtpError(Exception):
    pass


def synchronized(method: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize access to the shared FTP connection.

    Several worker threads use the same Ps4Ftp object (uploads, downloads,
    listings, the background game-name lookups). Without this, a hiccup in one
    thread reconnects - i.e. closes the socket - underneath another thread that
    is mid-upload, and the file silently fails with "Not connected to the PS4."
    The lock is reentrant, so composite operations can call the single-command
    helpers inside their own critical section.
    """

    @functools.wraps(method)
    def wrapper(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class Ps4Ftp:
    """Thin, forgiving wrapper around ftplib for GoldHEN's FTP server."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        user: str = "anonymous",
        password: str = "",
        passive: bool = True,
        timeout: float = 15.0,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.user = user or "anonymous"
        self.password = password or ""
        self.passive = passive
        self.timeout = timeout
        self.logger = logger
        self._ftp: Optional[ftplib.FTP] = None
        # one connection, many threads - see the synchronized() decorator
        self._lock = threading.RLock()

    # -- connection -------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._ftp is not None

    @synchronized
    def connect(self) -> None:
        self.close()
        ftp = ftplib.FTP()
        try:
            ftp.connect(self.host, self.port, timeout=self.timeout)
        except (OSError, ftplib.all_errors) as exc:
            raise FtpError(self._friendly_connect_error(exc)) from exc
        ftp.encoding = "utf-8"
        try:
            if self.user.lower() == "anonymous" and not self.password:
                try:
                    ftp.login()
                except ftplib.error_perm:
                    ftp.login(self.user, self.password)
            else:
                ftp.login(self.user, self.password)
        except ftplib.all_errors as exc:
            self.close()
            raise FtpError(f"Login failed for user '{self.user}': {exc}") from exc
        ftp.set_pasv(self.passive)
        self._ftp = ftp

    def _friendly_connect_error(self, exc: BaseException) -> str:
        if isinstance(exc, socket.timeout):
            return f"Timed out connecting to {self.host}:{self.port}."
        text = str(exc)
        if "refused" in text.lower():
            return (
                f"Nothing is listening on {self.host}:{self.port}.\n"
                "Start the FTP server in the GoldHEN settings menu on your PS4."
            )
        return f"Could not connect to {self.host}:{self.port}: {exc}"

    @synchronized
    def close(self) -> None:
        ftp, self._ftp = self._ftp, None
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass

    def _log(self, message: str) -> None:
        if self.logger:
            try:
                self.logger(message)
            except Exception:
                pass

    @synchronized
    def _run(self, func: Callable[[], Any], retry: bool = True) -> Any:
        if self._ftp is None:
            raise FtpError("Not connected to the PS4.")
        try:
            return func()
        except ftplib.error_perm as exc:
            raise FtpError(self._explain(exc)) from exc
        except (ftplib.error_temp, EOFError, OSError) as exc:
            self._log(f"FTP hiccup ({exc}); reconnecting ...")
            self.close()
            if not retry:
                raise FtpError(str(exc)) from exc
            try:
                self.connect()
            except FtpError:
                raise FtpError(f"Connection lost: {exc}") from exc
            try:
                return func()
            except ftplib.error_perm as exc2:
                raise FtpError(self._explain(exc2)) from exc2
            except ftplib.all_errors as exc2:
                self.close()
                raise FtpError(f"Connection lost: {exc2}") from exc2

    @staticmethod
    def _explain(exc: BaseException) -> str:
        text = str(exc)
        if "No such file" in text or "not found" in text.lower() or text.startswith("550"):
            return f"PS4 said: {text}"
        return text

    # -- basic operations -------------------------------------------------
    def pwd(self) -> str:
        return self._run(lambda: self._ftp.pwd())  # type: ignore[union-attr]

    def size(self, path: str) -> Optional[int]:
        try:
            return int(self._run(lambda: self._ftp.size(path)))  # type: ignore[union-attr]
        except FtpError:
            return None

    @synchronized
    def exists(self, path: str) -> Tuple[bool, bool]:
        """Returns (exists, is_dir).

        GoldHEN's FTP answers SIZE for folders too (it reports the directory
        size), so a plain SIZE probe cannot tell files from folders. Listing the
        parent instead is exact, and we only fall back to probing when that
        fails.
        """
        target = (path or "/").rstrip("/")
        if not target:
            return True, True
        parent = parent_remote(target)
        name = target.split("/")[-1]
        try:
            for entry in self.listdir(parent):
                if entry.name == name:
                    return True, entry.is_dir
        except FtpError:
            pass
        try:
            current = self.pwd()
        except FtpError:
            current = "/"
        try:
            self._run(lambda: self._ftp.cwd(target))  # type: ignore[union-attr]
            try:
                self._run(lambda: self._ftp.cwd(current))  # type: ignore[union-attr]
            except FtpError:
                pass
            return True, True
        except FtpError:
            pass
        try:
            self._run(lambda: self._ftp.size(target))  # type: ignore[union-attr]
            return True, False
        except FtpError:
            return False, False

    @synchronized
    def listdir(self, path: str = "/", with_details: bool = True) -> List[RemoteEntry]:
        if not with_details:
            names = self._run(lambda: self._ftp.nlst(path))  # type: ignore[union-attr]
            out: List[RemoteEntry] = []
            for name in names or []:
                base = name.rstrip("/").split("/")[-1]
                if base in (".", "..", ""):
                    continue
                out.append(RemoteEntry(name=base, path=join_remote(path, base), is_dir=False))
            return out
        try:
            entries: List[RemoteEntry] = []

            def collect() -> None:
                nonlocal entries
                entries = []
                for name, facts in self._ftp.mlsd(path):  # type: ignore[union-attr]
                    if name in (".", ".."):
                        continue
                    kind = (facts.get("type") or "file").lower()
                    mtime = None
                    modify = facts.get("modify")
                    if modify:
                        for pattern in ("%Y%m%d%H%M%S", "%Y%m%d%H%M"):
                            try:
                                mtime = datetime.strptime(modify[: len(pattern)], pattern).timestamp()
                                break
                            except ValueError:
                                continue
                    size = None
                    try:
                        size = int(facts.get("size") or 0)
                    except ValueError:
                        size = None
                    entries.append(
                        RemoteEntry(
                            name=name,
                            path=join_remote(path, name),
                            is_dir=kind in ("dir", "cdir", "pdir"),
                            size=size,
                            mtime=mtime,
                        )
                    )

            try:
                self._run(collect)
                if entries:
                    return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))
            except FtpError:
                pass
            # Fallback to LIST parsing for servers without MLSD.
            lines: List[str] = []
            self._run(lambda: self._ftp.retrlines(f"LIST {path}", lines.append))  # type: ignore[union-attr]
            out = []
            for line in lines:
                entry = parse_list_line(line, path)
                if entry is not None:
                    out.append(entry)
            return sorted(out, key=lambda e: (not e.is_dir, e.name.lower()))
        except FtpError:
            raise
        except ftplib.all_errors as exc:
            raise FtpError(str(exc)) from exc

    def read_bytes(self, path: str, progress: Optional[Callable[[int, int], None]] = None) -> bytes:
        sink = bytearray()

        def chunk(data: bytes) -> None:
            sink.extend(data)
            if progress:
                progress(len(sink), 0)

        self._run(lambda: self._ftp.retrbinary(f"RETR {path}", chunk, blocksize=65536))  # type: ignore[union-attr]
        return bytes(sink)

    def read_text(self, path: str) -> str:
        return self.read_bytes(path).decode("utf-8", "replace")

    def _stor_bytes(self, path: str, data: bytes, progress: Optional[Callable[[int, int], None]] = None) -> None:
        total = len(data)

        handle = io.BytesIO(data)

        def push(chunk: bytes) -> None:
            handle.write(chunk)
            if progress:
                progress(handle.tell(), total)

        try:
            self._run(lambda: self._ftp.storbinary(f"STOR {path}", handle, blocksize=65536, callback=push))  # type: ignore[union-attr]
        finally:
            handle.close()

    @synchronized
    def write_text(self, path: str, text: str, progress: Optional[Callable[[int, int], None]] = None) -> None:
        """Write a file safely: upload next to it, then rename over the original."""
        data = text.encode("utf-8")
        tmp = f"{path}.tmp"
        self._stor_bytes(tmp, data, progress)
        try:
            self.rename(tmp, path)
            return
        except FtpError:
            pass
        try:
            self.delete(path)
        except FtpError:
            pass
        try:
            self.rename(tmp, path)
        except FtpError:
            self._stor_bytes(path, data, progress)
            try:
                self.delete(tmp)
            except FtpError:
                pass

    def upload(self, local_path: Path, remote_path: str, progress: Optional[Callable[[int, int], None]] = None) -> None:
        total = local_path.stat().st_size
        with open(local_path, "rb") as handle:
            self._run(
                lambda: self._ftp.storbinary(  # type: ignore[union-attr]
                    f"STOR {remote_path}",
                    handle,
                    blocksize=65536,
                    callback=(lambda chunk: progress(handle.tell(), total)) if progress else None,
                )
            )

    def download(self, remote_path: str, local_path: Path, progress: Optional[Callable[[int, int], None]] = None) -> None:
        tmp = local_path.with_suffix(local_path.suffix + ".part")
        with open(tmp, "wb") as handle:
            def sink(chunk: bytes) -> None:
                handle.write(chunk)
                if progress:
                    progress(handle.tell(), 0)

            self._run(
                lambda: self._ftp.retrbinary(  # type: ignore[union-attr]
                    f"RETR {remote_path}", sink, blocksize=65536
                )
            )
        tmp.replace(local_path)

    def mkdir(self, path: str) -> None:
        self._run(lambda: self._ftp.mkd(path))  # type: ignore[union-attr]

    @synchronized
    def ensure_dir(self, path: str) -> None:
        parts = [p for p in (path or "").split("/") if p]
        current = ""
        for part in parts:
            current = f"{current}/{part}"
            exists, is_dir = self.exists(current)
            if exists and is_dir:
                continue
            if exists:
                raise FtpError(f"{current} exists but is not a folder.")
            self.mkdir(current)

    @synchronized
    def delete(self, path: str) -> None:
        exists, is_dir = self.exists(path)
        if is_dir:
            self._run(lambda: self._ftp.rmd(path))  # type: ignore[union-attr]
        else:
            self._run(lambda: self._ftp.delete(path))  # type: ignore[union-attr]

    def rename(self, src: str, dst: str) -> None:
        self._run(lambda: self._ftp.rename(src, dst))  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Payloader: GoldHEN's payload server ("BinLoader")
# ---------------------------------------------------------------------------


class PayloaderError(Exception):
    pass


class Payloader:
    """Talk to GoldHEN's payload server on the console.

    BinLoader greets a fresh connection with a short banner; whatever bytes
    arrive after that are taken as a payload and executed by GoldHEN. This
    wrapper knows how to connect, say what answered and push one file - it
    never invents a payload, so the app can use the server without shipping
    somebody else's binary.
    """

    BANNER_TIMEOUT = 1.2     # binloader is usually silent; never wait long here
    REPLY_TIMEOUT = 15.0
    CHUNK = 65536

    def __init__(self, host: str, port: int = DEFAULT_PAYLOAD_PORT, timeout: float = 6.0,
                 logger: Optional[Callable[[str], None]] = None) -> None:
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self.logger = logger
        self.sock: Optional[socket.socket] = None
        self.banner = ""

    @property
    def connected(self) -> bool:
        return self.sock is not None

    # -- connection -------------------------------------------------------
    def connect(self) -> str:
        """Open the connection and read the greeting; returns it (may be empty)."""
        self.close()
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise PayloaderError(self._friendly_error(exc)) from exc
        self.sock = sock
        self.banner = self._read_banner(sock)
        self._log(f"Payload server {self.host}:{self.port} answered"
                  + (f": {self.one_line(self.banner)}" if self.banner else " (no banner)."))
        return self.banner

    def probe(self) -> str:
        """Connect, describe what answered and disconnect again."""
        self.connect()
        try:
            return self.banner_summary()
        finally:
            self.close()

    def banner_summary(self) -> str:
        """Short human text, e.g. 'GoldHEN BinLoader v2.4b18.3' or 'payload server ready'."""
        for line in self.one_line(self.banner).splitlines():
            if "goldhen" in line.lower() or "binloader" in line.lower():
                return line[:48]
        first = self.one_line(self.banner)
        return first[:48] if first else "payload server ready"

    @staticmethod
    def one_line(text: str) -> str:
        """Collapse whitespace so a banner can go into a single log line."""
        return " ".join(str(text or "").split())

    def _read_banner(self, sock: socket.socket) -> str:
        chunks: List[bytes] = []
        sock.settimeout(self.BANNER_TIMEOUT)
        while sum(len(c) for c in chunks) < 4096:
            try:
                chunk = sock.recv(1024)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:     # one complete greeting line is enough
                break
        return b"".join(chunks).decode("utf-8", "replace").strip()

    def close(self) -> None:
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _friendly_error(self, exc: BaseException) -> str:
        if isinstance(exc, socket.timeout):
            return f"Timed out talking to the payload server at {self.host}:{self.port}."
        if "refused" in str(exc).lower():
            return (f"Nothing is listening on {self.host}:{self.port}.\n"
                    "Turn on the payload server (BinLoader) in GoldHEN's settings on the PS4.")
        return f"Could not reach the payload server at {self.host}:{self.port}: {exc}"

    def _log(self, message: str) -> None:
        if self.logger:
            try:
                self.logger(message)
            except Exception:
                pass

    # -- payloads ---------------------------------------------------------
    def send_file(self, path: Any, progress: Optional[Callable[[int, int], None]] = None) -> str:
        """Push one payload file; returns the console's reply (often empty)."""
        return self.send_bytes(Path(path).read_bytes(), progress=progress)

    def send_bytes(self, data: bytes, progress: Optional[Callable[[int, int], None]] = None) -> str:
        """Push payload bytes and read the answer. BinLoader closes on success."""
        if self.sock is None:
            raise PayloaderError("Not connected to the payload server.")
        sock = self.sock
        total, sent = len(data), 0
        reply = ""
        try:
            sock.settimeout(max(self.timeout, 30.0))
            for start in range(0, total, self.CHUNK):
                chunk = data[start:start + self.CHUNK]
                sock.sendall(chunk)
                sent += len(chunk)
                if progress:
                    progress(sent, total)
            try:
                sock.shutdown(socket.SHUT_WR)   # tell the console the payload ends here
            except OSError:
                pass
            reply = self._read_reply(sock)
        except OSError as exc:
            raise PayloaderError(f"Sending the payload failed: {exc}") from exc
        finally:
            self.close()
        self._log(f"Payload sent ({sent} byte(s))"
                  + (f"; console replied: {self.one_line(reply)}" if reply else "."))
        return reply

    def _read_reply(self, sock: socket.socket) -> str:
        chunks: List[bytes] = []
        sock.settimeout(self.REPLY_TIMEOUT)
        while sum(len(c) for c in chunks) < 4096:
            try:
                chunk = sock.recv(1024)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace").strip()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class Config:
    host: str = ""
    port: int = DEFAULT_PORT
    user: str = "anonymous"
    password: str = ""
    remote_ini: str = DEFAULT_INI
    remote_plugins_dir: str = DEFAULT_PLUGINS_DIR
    passive: bool = True
    timeout: int = 20
    auto_connect: bool = True
    auto_apply: bool = True
    keep_backups: int = 25
    pkg_dir: str = pkg_manager.DEFAULT_PKG_DIR
    # GoldHEN's payload server. An address here is the opt-in: the manager then
    # connects to it together with FTP, and an empty address leaves it alone.
    payloader_host: str = ""
    payloader_port: int = DEFAULT_PAYLOAD_PORT
    window_geometry: str = ""
    recent_hosts: List[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        # resolved at call time so tests (and future --config) can point it elsewhere
        path = CONFIG_FILE if path is None else Path(path)
        config = cls()
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return config
        valid = {f.name: f.type for f in fields(cls)}  # type: ignore[arg-type]
        for key, value in (data or {}).items():
            if key not in valid:
                continue
            default = getattr(config, key)
            try:
                if isinstance(default, bool):
                    setattr(config, key, bool(value))
                elif isinstance(default, int):
                    setattr(config, key, int(value))
                elif isinstance(default, list):
                    setattr(config, key, list(value))
                else:
                    setattr(config, key, str(value))
            except (TypeError, ValueError):
                continue
        return config

    def save(self, path: Optional[Path] = None) -> None:
        # resolved at call time: CONFIG_FILE may be repointed (tests, portable setups)
        target = CONFIG_FILE if path is None else Path(path)
        try:
            target.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"Could not save config to {target}: {exc}") from exc

    def remember_host(self, host: str) -> None:
        host = (host or "").strip()
        if not host:
            return
        self.recent_hosts = [host] + [h for h in self.recent_hosts if h != host]
        self.recent_hosts = self.recent_hosts[:10]

    def make_ftp(self, logger: Optional[Callable[[str], None]] = None) -> Ps4Ftp:
        return Ps4Ftp(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            passive=self.passive,
            timeout=max(3, int(self.timeout)),
            logger=logger,
        )

    # -- payload server ---------------------------------------------------
    @property
    def payloader_enabled(self) -> bool:
        """An address in the settings *is* the opt-in - there is no second button."""
        return bool(str(self.payloader_host or "").strip())

    def payloader_target(self) -> str:
        return f"{str(self.payloader_host).strip()}:{self.payloader_port_number()}"

    def payloader_port_number(self) -> int:
        """A hand-edited config may hold 0 or nonsense - fall back to GoldHEN's default."""
        try:
            port = int(self.payloader_port)
        except (TypeError, ValueError):
            return DEFAULT_PAYLOAD_PORT
        return port if port > 0 else DEFAULT_PAYLOAD_PORT

    def make_payloader(self, logger: Optional[Callable[[str], None]] = None) -> Payloader:
        return Payloader(
            host=str(self.payloader_host).strip(),
            port=self.payloader_port_number(),
            timeout=max(3.0, min(float(self.timeout), 30.0)),
            logger=logger,
        )


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def write_backup(text: str, suffix: str = "") -> Optional[Path]:
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        path = BACKUP_DIR / f"plugins-{timestamp()}{suffix}.ini"
        path.write_text(text, encoding="utf-8")
        return path
    except OSError:
        return None


def list_backups() -> List[Path]:
    try:
        files = [p for p in BACKUP_DIR.iterdir() if p.suffix == ".ini" and p.is_file()]
    except OSError:
        return []
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def prune_backups(keep: int) -> None:
    if keep <= 0:
        return
    for path in list_backups()[keep:]:
        try:
            path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Network helpers: find the PS4 on the LAN
# ---------------------------------------------------------------------------


def local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def scan_for_ftp(port: int = DEFAULT_PORT, timeout: float = 0.5, workers: int = 64) -> List[str]:
    """Look for hosts on the local /24 with an open FTP port (e.g. a PS4)."""
    base_ip = local_ip()
    if not base_ip:
        return []
    prefix = ".".join(base_ip.split(".")[:3])
    hosts = [f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" != base_ip]

    def probe(host: str) -> Optional[str]:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return host
        except OSError:
            return None

    found: List[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(probe, hosts):
            if result:
                found.append(result)
    return found


# ---------------------------------------------------------------------------
# Theme & fonts
# ---------------------------------------------------------------------------


class Theme:
    bg = "#12141a"
    surface = "#191c23"
    surface2 = "#22262f"
    surface3 = "#2b303b"
    border = "#2a2f3a"
    text = "#e7ebf2"
    text_dim = "#9aa3b5"
    muted = "#6f7889"
    accent = "#4f8cff"
    accent_hi = "#6aa0ff"
    accent_lo = "#3c72d9"
    good = "#3ddc97"
    warn = "#ffb84d"
    bad = "#ff6b6b"
    sel = "#252c3a"

    font_ui: Any = ("TkDefaultFont", 10)
    font_small: Any = ("TkDefaultFont", 9)
    font_h1: Any = ("TkDefaultFont", 15, "bold")
    font_h2: Any = ("TkDefaultFont", 11, "bold")
    font_bold: Any = ("TkDefaultFont", 10, "bold")
    font_mono: Any = ("TkFixedFont", 9)


def init_fonts() -> None:
    if not TK_OK:
        return
    families = set(tkfont.families())
    ui_candidates = ["Inter", "Segoe UI Variable Text", "Segoe UI", "SF Pro Text",
                    "Helvetica Neue", "Ubuntu", "Noto Sans", "DejaVu Sans", "Arial"]
    mono_candidates = ["JetBrains Mono", "Cascadia Mono", "Consolas", "SF Mono",
                       "Menlo", "DejaVu Sans Mono", "Liberation Mono", "Courier New"]
    ui = next((f for f in ui_candidates if f in families), "TkDefaultFont")
    mono = next((f for f in mono_candidates if f in families), "TkFixedFont")
    Theme.font_ui = (ui, 10)
    Theme.font_small = (ui, 9)
    Theme.font_h1 = (ui, 15, "bold")
    Theme.font_h2 = (ui, 11, "bold")
    Theme.font_bold = (ui, 10, "bold")
    Theme.font_mono = (mono, 9)


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


class CButton(tk.Canvas):
    """Flat, rounded, hover-aware button drawn on a canvas."""

    VARIANTS = {
        "primary": dict(fill=Theme.accent, hover=Theme.accent_hi, press=Theme.accent_lo,
                        fg="#ffffff", outline=Theme.accent),
        "secondary": dict(fill=Theme.surface2, hover=Theme.surface3, press=Theme.surface2,
                          fg=Theme.text, outline=Theme.border),
        "ghost": dict(fill=None, hover=Theme.surface2, press=Theme.surface3,
                      fg=Theme.text_dim, outline=None),
        "danger": dict(fill=Theme.surface2, hover="#3a2126", press="#2c1a1e",
                       fg=Theme.bad, outline=Theme.border),
        "nav": dict(fill=None, hover=Theme.surface, press=Theme.surface2,
                    fg=Theme.text_dim, outline=None),
    }

    def __init__(
        self,
        master,
        text: str = "",
        command: Optional[Callable[[], None]] = None,
        variant: str = "secondary",
        bg: Optional[str] = None,
        height: int = 34,
        radius: int = 9,
        padx: int = 16,
        font: Any = None,
        width: Optional[int] = None,
        anchor: str = "center",
    ) -> None:
        self._font = font or Theme.font_ui
        self._padx = padx
        self._bg = bg or widget_bg(master, Theme.bg)
        self._text = text
        self._variant = variant
        self._enabled = True
        self._active = False
        self._height = height
        self._radius = radius
        self._command = command
        self._anchor = anchor
        measured = tkfont.Font(font=self._font).measure(text) if TK_OK else 80
        self._width = width or measured + padx * 2
        super().__init__(
            master, width=self._width, height=height, bg=self._bg,
            highlightthickness=0, bd=0, cursor="hand2", takefocus=0,
        )
        self._draw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    # -- drawing ----------------------------------------------------------
    def _palette(self) -> Dict[str, Any]:
        return self.VARIANTS.get(self._variant, self.VARIANTS["secondary"])

    def _draw(self, state: str = "normal") -> None:
        self.delete("all")
        palette = self._palette()
        fill = palette["fill"]
        outline = palette["outline"]
        fg = palette["fg"]
        if self._active and self._variant == "nav":
            fill = Theme.surface
            fg = Theme.text
        if not self._enabled:
            fg = Theme.muted
        elif state == "hover" and palette["hover"]:
            fill = palette["hover"]
        elif state == "press" and palette["press"]:
            fill = palette["press"]
        if fill:
            draw_round_rect(
                self, 0.5, 0.5, self._width - 0.5, self._height - 0.5, self._radius,
                fill=fill, outline=outline or fill, width=1,
            )
        x = 12 if self._anchor == "w" else self._width / 2
        self.create_text(
            x, self._height / 2, text=self._text, fill=fg, font=self._font,
            anchor="w" if self._anchor == "w" else "center",
        )

    # -- events -----------------------------------------------------------
    def _on_enter(self, _event=None) -> None:
        if self._enabled:
            self._draw("hover")
            self.configure(cursor="hand2")

    def _on_leave(self, _event=None) -> None:
        if self._enabled:
            self._draw()

    def _on_press(self, _event=None) -> None:
        if self._enabled:
            self._draw("press")

    def _on_release(self, event=None) -> None:
        if not self._enabled:
            return
        inside = event is None or (0 <= event.x <= self._width and 0 <= event.y <= self._height)
        self._draw("hover" if inside else "normal")
        if inside and self._command:
            self._command()

    # -- api --------------------------------------------------------------
    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        if not self._width:
            self._width = tkfont.Font(font=self._font).measure(text) + self._padx * 2
            self.configure(width=self._width)
        self._draw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self._draw()

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        self._draw()

    def set_variant(self, variant: str) -> None:
        self._variant = variant
        self._draw()

    def flash(self, variant: str, ms: int = 900) -> None:
        original = self._variant
        self.set_variant(variant)
        self.after(ms, lambda: self.set_variant(original))


class CCheck(tk.Canvas):
    """Checkbox with a label, drawn by hand for a consistent look."""

    def __init__(
        self,
        master,
        text: str = "",
        variable: Optional[Any] = None,
        command: Optional[Callable[[bool], None]] = None,
        bg: Optional[str] = None,
        font: Any = None,
        size: int = 18,
        gap: int = 9,
    ) -> None:
        self._bg = bg or widget_bg(master, Theme.bg)
        self._font = font or Theme.font_ui
        self._size = size
        self.variable = variable if variable is not None else tk.BooleanVar(value=False)
        self._command = command
        self._enabled = True
        measured = tkfont.Font(font=self._font).measure(text) if TK_OK else 100
        self._box_h = max(size, 20) + 6
        super().__init__(
            master, width=size + gap + measured + 2, height=self._box_h,
            bg=self._bg, highlightthickness=0, bd=0, cursor="hand2", takefocus=0,
        )
        self._text = text
        self._gap = gap
        self._draw()
        self.bind("<Button-1>", self._on_click)

    def _draw(self) -> None:
        self.delete("all")
        size, gap = self._size, self._gap
        top = (self._box_h - size) / 2
        checked = bool(self.variable.get())
        fill = Theme.accent if checked else Theme.surface2
        outline = Theme.accent if checked else Theme.border
        if not self._enabled:
            fill, outline = Theme.surface, Theme.border
        draw_round_rect(self, 1, top + 1, size - 1, top + size - 1, 5, fill=fill, outline=outline, width=1.5)
        if checked:
            tick = Theme.text if not self._enabled else "#ffffff"
            self.create_line(
                size * 0.26, top + size * 0.52, size * 0.44, top + size * 0.70,
                size * 0.76, top + size * 0.30, fill=tick, width=2,
                capstyle="round", joinstyle="round",
            )
        self.create_text(
            size + gap, self._box_h / 2, text=self._text, anchor="w",
            fill=Theme.text if self._enabled else Theme.muted, font=self._font,
        )

    def _on_click(self, _event=None) -> None:
        if not self._enabled:
            return
        new_value = not bool(self.variable.get())
        self.variable.set(new_value)
        self._draw()
        if self._command:
            self._command(new_value)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self._draw()

    def refresh(self) -> None:
        self._draw()

    def selected(self) -> bool:
        return bool(self.variable.get())


class CSwitch(tk.Canvas):
    """iOS-style toggle switch."""

    def __init__(
        self,
        master,
        variable: Optional[Any] = None,
        command: Optional[Callable[[bool], None]] = None,
        bg: Optional[str] = None,
        width: int = 44,
        height: int = 24,
    ) -> None:
        self._bg = bg or widget_bg(master, Theme.bg)
        self.variable = variable if variable is not None else tk.BooleanVar(value=False)
        self._command = command
        self._track_w, self._track_h = width, height
        self._enabled = True
        super().__init__(master, width=width, height=height, bg=self._bg,
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=0)
        self._draw()
        self.bind("<Button-1>", self._on_click)

    def _draw(self) -> None:
        self.delete("all")
        on = bool(self.variable.get())
        track = Theme.accent if on else Theme.surface3
        if not self._enabled:
            track = Theme.surface2
        width, height = self._track_w, self._track_h
        draw_round_rect(self, 1, 1, width - 1, height - 1, height / 2 - 1,
                        fill=track, outline=track)
        knob_r = height / 2 - 3
        cx = (width - height / 2) if on else (height / 2)
        fill = "#ffffff" if self._enabled else Theme.muted
        self.create_oval(cx - knob_r, height / 2 - knob_r, cx + knob_r, height / 2 + knob_r,
                         fill=fill, outline=fill)

    def _on_click(self, _event=None) -> None:
        if not self._enabled:
            return
        new_value = not bool(self.variable.get())
        self.variable.set(new_value)
        self._draw()
        if self._command:
            self._command(new_value)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._draw()

    def refresh(self) -> None:
        self._draw()


def make_entry(master, textvariable=None, width: int = 24, font=None, show: str = "", bg: Optional[str] = None) -> tk.Entry:
    return tk.Entry(
        master,
        textvariable=textvariable,
        width=width,
        font=font or Theme.font_ui,
        show=show,
        bg=bg or Theme.surface2,
        fg=Theme.text,
        insertbackground=Theme.accent,
        disabledbackground=Theme.surface,
        disabledforeground=Theme.muted,
        relief="flat",
        highlightthickness=1,
        highlightbackground=Theme.border,
        highlightcolor=Theme.accent,
    )


class VScrollFrame(tk.Frame):
    """A vertically scrollable frame."""

    def __init__(self, master, bg: Optional[str] = None, **kwargs) -> None:
        bg = bg or Theme.bg
        super().__init__(master, bg=bg, **kwargs)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", lambda _e: self._bind_wheel(True))
        self.canvas.bind("<Leave>", lambda _e: self._bind_wheel(False))

    def _on_inner(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self, active: bool) -> None:
        if active:
            self.canvas.bind_all("<MouseWheel>", self._on_wheel)
            self.canvas.bind_all("<Button-4>", self._on_wheel)
            self.canvas.bind_all("<Button-5>", self._on_wheel)
        else:
            self.canvas.unbind_all("<MouseWheel>")
            self.canvas.unbind_all("<Button-4>")
            self.canvas.unbind_all("<Button-5>")

    def _on_wheel(self, event) -> None:
        delta = 0
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        elif getattr(event, "delta", 0):
            delta = -1 if event.delta > 0 else 1
        if delta:
            self.canvas.yview_scroll(delta * 3, "units")


class StatusPill(tk.Frame):
    """Little coloured dot + text used in the header."""

    def __init__(self, master, bg: str = Theme.surface) -> None:
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, width=12, height=12, bg=bg, highlightthickness=0, bd=0)
        self.canvas.pack(side="left", pady=(1, 0))
        self.dot = self.canvas.create_oval(3, 3, 10, 10, fill=Theme.muted, outline=Theme.muted)
        self.label = tk.Label(self, text="Offline", bg=bg, fg=Theme.text_dim, font=Theme.font_ui)
        self.label.pack(side="left", padx=(7, 0))

    def set(self, text: str, color: str) -> None:
        self.label.configure(text=text)
        self.canvas.itemconfigure(self.dot, fill=color, outline=color)


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------


class Worker:
    """Run blocking work on threads, deliver results back on the UI thread."""

    def __init__(self, root) -> None:
        self.root = root
        self.queue: "queue.Queue[Tuple[Optional[Callable], Any]]" = queue.Queue()
        self.busy = 0
        self._after_id: Optional[str] = None
        self._stopped = False
        self._poll()

    def stop(self) -> None:
        """Stop the UI poll - call before destroying the window, otherwise Tk
        complains about the already-scheduled callback (invalid command name)."""
        self._stopped = True
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def submit(self, func: Callable[[], Any], on_done: Optional[Callable[[Any], None]] = None,
               on_error: Optional[Callable[[BaseException], None]] = None,
               name: str = "worker") -> None:
        def runner() -> None:
            try:
                result = func()
            except BaseException as exc:  # noqa: BLE001 - surfaced to the UI
                self.post(on_error, exc)
            else:
                self.post(on_done, result)

        threading.Thread(target=runner, name=name, daemon=True).start()

    def post(self, callback: Optional[Callable], value: Any = None) -> None:
        """Thread-safe: queue a callback to run on the UI thread."""
        if callback is not None:
            self.queue.put((callback, value))

    def progress(self, callback: Callable[[int, int], None]) -> Callable[[int, int], None]:
        return lambda done, total: self.post(callback, (done, total))

    def _poll(self) -> None:
        while True:
            try:
                callback, value = self.queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback(value)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        if self._stopped:
            return
        try:
            self._after_id = self.root.after(40, self._poll)
        except Exception:
            self._after_id = None


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------


class ModalDialog(tk.Toplevel):
    def __init__(self, app, title: str, width: int, height: int, resizable: bool = False) -> None:
        super().__init__(app)
        self.app = app
        self.title(title)
        self.configure(bg=Theme.bg)
        self.resizable(resizable, resizable)
        self.transient(app)
        self.result: Any = None
        self._closed = False
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())
        self.update_idletasks()
        root_x, root_y = app.winfo_rootx(), app.winfo_rooty()
        root_w, root_h = app.winfo_width(), app.winfo_height()
        x = root_x + max(0, (root_w - width) // 2)
        y = root_y + max(0, (root_h - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def close(self, result: Any = None) -> None:
        if self._closed:
            return
        self._closed = True
        self.result = result
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def show(self) -> Any:
        self.wait_window(self)
        return self.result


class AddPluginDialog(ModalDialog):
    """Add a plugin either to [default] or to a single title id section."""

    def __init__(self, app, remote_files: Optional[List[RemoteEntry]] = None) -> None:
        super().__init__(app, "Add plugin", 620, 560)
        self.grab_set()
        self.remote_files = remote_files or []

        tk.Label(self, text="Add plugin", bg=Theme.bg, fg=Theme.text, font=Theme.font_h1).pack(
            anchor="w", padx=24, pady=(20, 2)
        )
        tk.Label(
            self,
            text="Choose where the plugin should load, then point to the plugin file on your PS4.",
            bg=Theme.bg, fg=Theme.text_dim, font=Theme.font_small,
        ).pack(anchor="w", padx=24, pady=(0, 14))

        # -- scope ------------------------------------------------------
        scope = tk.Frame(self, bg=Theme.bg)
        scope.pack(fill="x", padx=24)
        tk.Label(scope, text="1.  WHERE SHOULD IT LOAD?", bg=Theme.bg, fg=Theme.muted,
                 font=Theme.font_small).pack(anchor="w")
        self.var_default = tk.BooleanVar(value=True)
        self.var_game = tk.BooleanVar(value=False)
        row = tk.Frame(scope, bg=Theme.bg)
        row.pack(anchor="w", pady=(8, 0))
        self.check_default = CCheck(
            row, "Default — every game", variable=self.var_default, bg=Theme.bg,
            command=lambda _v: self._set_scope("default"),
        )
        self.check_default.pack(side="left")
        self.check_game = CCheck(
            row, "Game specific — one title", variable=self.var_game, bg=Theme.bg,
            command=lambda _v: self._set_scope("game"),
        )
        self.check_game.pack(side="left", padx=(24, 0))

        # -- game picker -------------------------------------------------
        self.game_box = tk.Frame(self, bg=Theme.surface, highlightthickness=1,
                                 highlightbackground=Theme.border)
        self.game_box.pack(fill="x", padx=24, pady=(10, 0), ipady=10)
        tk.Label(self.game_box, text="Title ID", bg=Theme.surface, fg=Theme.text_dim,
                 font=Theme.font_small).pack(anchor="w", padx=14, pady=(2, 4))
        entry_row = tk.Frame(self.game_box, bg=Theme.surface)
        entry_row.pack(fill="x", padx=14)
        self.var_title = tk.StringVar(value="")
        self.title_entry = make_entry(entry_row, self.var_title, width=18, font=Theme.font_mono)
        self.title_entry.pack(side="left", ipady=5)
        self.var_title.trace_add("write", lambda *_: self._validate_title())
        self.pick_button = CButton(entry_row, "Pick installed game", command=self._open_title_picker,
                                   variant="secondary", bg=Theme.surface, height=30)
        self.pick_button.pack(side="left", padx=(10, 0))
        self.title_hint = tk.Label(self.game_box, text="", bg=Theme.surface, fg=Theme.muted,
                                   font=Theme.font_small)
        self.title_hint.pack(anchor="w", padx=14, pady=(6, 0))

        # -- plugin path --------------------------------------------------
        path_head = tk.Frame(self, bg=Theme.bg)
        path_head.pack(fill="x", padx=24, pady=(16, 0))
        tk.Label(path_head, text="2.  PLUGIN FILE ON THE PS4", bg=Theme.bg, fg=Theme.muted,
                 font=Theme.font_small).pack(anchor="w")
        self.var_path = tk.StringVar(value="")
        self.var_path.trace_add("write", lambda *_: self._validate_path())
        path_row = tk.Frame(self, bg=Theme.bg)
        path_row.pack(fill="x", padx=24, pady=(8, 0))
        self.path_entry = make_entry(path_row, self.var_path, width=48, font=Theme.font_mono)
        self.path_entry.pack(side="left", fill="x", expand=True, ipady=5)
        CButton(path_row, "Browse PS4", command=self._open_remote_picker,
                bg=Theme.bg, height=30).pack(side="left", padx=(8, 0))
        CButton(path_row, "Upload from PC", command=self._upload_from_pc,
                bg=Theme.bg, height=30).pack(side="left", padx=(8, 0))

        known = [e for e in self.remote_files if not e.is_dir]
        if known:
            quick = tk.Frame(self, bg=Theme.bg)
            quick.pack(fill="x", padx=24, pady=(8, 0))
            tk.Label(quick, text="In plugins folder:", bg=Theme.bg, fg=Theme.muted,
                     font=Theme.font_small).pack(side="left")
            self.quick_var = tk.StringVar(value=known[0].name)
            combo = ttk.Combobox(quick, textvariable=self.quick_var, state="readonly",
                                 values=[e.name for e in known], width=34, font=Theme.font_small)
            combo.pack(side="left", padx=(8, 0))
            combo.bind("<<ComboboxSelected>>", lambda _e: self._use_quick_pick())
            self._files_by_name = {e.name: e for e in known}
        else:
            self._files_by_name = {}
            tk.Label(self, text="(the plugins folder could not be listed — type a path instead)",
                     bg=Theme.bg, fg=Theme.muted, font=Theme.font_small).pack(anchor="w", padx=24, pady=(8, 0))

        self.path_hint = tk.Label(self, text="", bg=Theme.bg, fg=Theme.muted, font=Theme.font_small,
                                  justify="left", anchor="w")
        self.path_hint.pack(fill="x", padx=24, pady=(8, 0))

        # -- enabled + buttons -------------------------------------------
        self.var_enabled = tk.BooleanVar(value=True)
        CCheck(self, "Enable it right away", variable=self.var_enabled, bg=Theme.bg).pack(
            anchor="w", padx=24, pady=(12, 0)
        )

        self.error_label = tk.Label(self, text="", bg=Theme.bg, fg=Theme.bad, font=Theme.font_small,
                                    justify="left", anchor="w", wraplength=560)
        self.error_label.pack(fill="x", padx=24, pady=(10, 0))

        footer = tk.Frame(self, bg=Theme.bg)
        footer.pack(side="bottom", fill="x", padx=24, pady=18)
        self.add_button = CButton(footer, "Add plugin", command=self._submit, variant="primary")
        self.add_button.pack(side="right")
        CButton(footer, "Cancel", command=self.close, variant="ghost").pack(side="right", padx=(0, 8))

        self._set_scope("default")
        self.title_entry.focus_set()
        if self.remote_files:
            self._use_quick_pick()

    # -- scope handling ---------------------------------------------------
    def _set_scope(self, scope: str) -> None:
        if scope == "game":
            self.var_game.set(True)
            self.var_default.set(False)
        else:
            self.var_default.set(True)
            self.var_game.set(False)
        self.check_default.refresh()
        self.check_game.refresh()
        state = "normal" if scope == "game" else "disabled"
        for child in (self.title_entry,):
            child.configure(state=state)
        self.game_box.configure(highlightbackground=Theme.border if scope != "game" else Theme.accent)
        self.pick_button.set_enabled(scope == "game")
        if scope == "game":
            self._validate_title()
        else:
            self.title_hint.configure(text="Loads for every game ([default] section).", fg=Theme.muted)
            self.title_entry.configure(bg=Theme.surface2)

    def _validate_title(self) -> bool:
        if not self.var_game.get():
            return True
        value = norm_title_id(self.var_title.get())
        if not value:
            self.title_hint.configure(text="Pick a game or type a title id, e.g. CUSA12345.", fg=Theme.muted)
            return False
        if not is_title_id(value):
            self.title_hint.configure(
                text="That does not look like a title id (4 letters + 5 digits, e.g. CUSA12345).",
                fg=Theme.warn,
            )
            return False
        name = self.app.titles.get(value, "")
        known = value in self.app.installed_titles
        if name:
            self.title_hint.configure(text=f"{value} · {name}" + ("" if known else "  (not installed)"),
                                      fg=Theme.good)
        elif known:
            self.title_hint.configure(text=f"{value} (installed)", fg=Theme.good)
        elif not self.app.titles_available and self.app.ftp is not None:
            self.title_hint.configure(
                text=f"{value} — the PS4 would not hand over its game list, adding anyway.", fg=Theme.muted
            )
        else:
            self.title_hint.configure(text=f"{value} — that game is not installed, adding anyway.",
                                      fg=Theme.warn)
        return True

    def _open_title_picker(self) -> None:
        picked = TitlePickerDialog(self.app).show()
        if picked:
            self.var_title.set(picked)
            self._set_scope("game")

    # -- plugin file handling ---------------------------------------------
    def _validate_path(self) -> bool:
        value = self.var_path.get().strip()
        if not value:
            self.path_hint.configure(text="")
            return False
        if not value.startswith("/"):
            self.path_hint.configure(text="Use the full PS4 path, starting with /", fg=Theme.warn)
            return False
        name = value.split("/")[-1]
        in_folder = any(e.name == name for e in self.remote_files if not e.is_dir)
        if in_folder:
            self.path_hint.configure(text="Found in the plugins folder.", fg=Theme.good)
        else:
            self.path_hint.configure(
                text="Heads up: that file was not found in the plugins folder listing.",
                fg=Theme.muted,
            )
        return True

    def _use_quick_pick(self) -> None:
        entry = self._files_by_name.get(self.quick_var.get())
        if entry is not None:
            self.var_path.set(entry.path)

    def _open_remote_picker(self) -> None:
        dialog = RemoteBrowserDialog(
            self.app,
            start=self.app.config.remote_plugins_dir or DEFAULT_PLUGINS_DIR,
            mode="file",
            prompt="Pick a plugin file",
        )
        picked = dialog.show()
        if picked:
            self.var_path.set(picked)

    def _upload_from_pc(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Choose a plugin to upload to the PS4",
            filetypes=[("GoldHEN plugins", "*.prx *.sprx *.bin *.elf"), ("All files", "*.*")],
        )
        if not path:
            return
        remote_dir = self.app.config.remote_plugins_dir or DEFAULT_PLUGINS_DIR
        remote_path = join_remote(remote_dir, Path(path).name)

        def work() -> str:
            ftp = self.app.require_ftp()
            ftp.ensure_dir(remote_dir)
            ftp.upload(Path(path), remote_path, progress=self.app.worker.progress(self.app.on_progress))
            return remote_path

        self.add_button.set_enabled(False)
        self.error_label.configure(text="Uploading ...", fg=Theme.text_dim)

        def done(result: str) -> None:
            self.add_button.set_enabled(True)
            self.error_label.configure(text="")
            self.var_path.set(result)
            self.app.set_status(f"Uploaded {Path(path).name}", "good")

        def failed(exc: BaseException) -> None:
            self.add_button.set_enabled(True)
            self.error_label.configure(text=f"Upload failed: {exc}", fg=Theme.bad)

        self.app.worker.submit(work, done, failed, name="upload-plugin")

    # -- submit -----------------------------------------------------------
    def _submit(self) -> None:
        path = self.var_path.get().strip()
        if not path:
            self.error_label.configure(text="Pick a plugin file first.", fg=Theme.bad)
            return
        if not path.startswith("/"):
            self.error_label.configure(text="The plugin path must start with '/' (PS4 path).", fg=Theme.bad)
            return
        section = DEFAULT_SECTION
        if self.var_game.get():
            title = norm_title_id(self.var_title.get())
            if not is_title_id(title):
                self.error_label.configure(
                    text="Enter a valid title id (4 letters + 5 digits) or tick 'Default'.", fg=Theme.bad
                )
                return
            section = title
        elif not self.var_default.get():
            self.error_label.configure(text="Choose 'Default' or 'Game specific'.", fg=Theme.bad)
            return
        self.close({"section": section, "path": path, "enabled": bool(self.var_enabled.get())})


class TitlePickerDialog(ModalDialog):
    """Pick an installed game (title id) from /user/appmeta."""

    def __init__(self, app) -> None:
        super().__init__(app, "Pick a game", 560, 520, resizable=True)
        self.grab_set()
        self.titles = sorted(app.installed_titles, key=lambda t: (app.titles.get(t, t) or t).lower())

        header = tk.Frame(self, bg=Theme.bg)
        header.pack(fill="x", padx=20, pady=(18, 8))
        tk.Label(header, text="Installed games", bg=Theme.bg, fg=Theme.text, font=Theme.font_h2).pack(anchor="w")
        self.status = tk.Label(header, text="", bg=Theme.bg, fg=Theme.muted, font=Theme.font_small)
        self.status.pack(anchor="w", pady=(2, 0))

        search_row = tk.Frame(self, bg=Theme.bg)
        search_row.pack(fill="x", padx=20, pady=(4, 8))
        self.var_search = tk.StringVar()
        self.var_search.trace_add("write", lambda *_: self.refresh())
        entry = make_entry(search_row, self.var_search, width=40)
        entry.pack(side="left", fill="x", expand=True, ipady=5)
        entry.focus_set()

        body = tk.Frame(self, bg=Theme.surface, highlightthickness=1, highlightbackground=Theme.border)
        body.pack(fill="both", expand=True, padx=20)
        self.tree = ttk.Treeview(body, columns=("name",), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Title ID")
        self.tree.heading("name", text="Game")
        self.tree.column("#0", width=150, minwidth=110)
        self.tree.column("name", width=320)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda _e: self._choose())
        self.tree.bind("<Return>", lambda _e: self._choose())

        footer = tk.Frame(self, bg=Theme.bg)
        footer.pack(fill="x", padx=20, pady=16)
        CButton(footer, "Use selected", command=self._choose, variant="primary").pack(side="right")
        CButton(footer, "Cancel", command=self.close, variant="ghost").pack(side="right", padx=(0, 8))

        self.refresh()
        if not self.titles:
            self.status.configure(text="No games found — connect to your PS4, or type the title id by hand.",
                                  fg=Theme.warn)

    def refresh(self) -> None:
        needle = self.var_search.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        count = 0
        for title in self.titles:
            name = self.app.titles.get(title, "")
            if needle and needle not in title.lower() and needle not in (name or "").lower():
                continue
            self.tree.insert("", "end", iid=title, text=title, values=(name or "-",))
            count += 1
        if not self.status.cget("text") or "Found" in self.status.cget("text"):
            self.status.configure(text=f"Found {count} game(s)", fg=Theme.muted)

    def _choose(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        self.close(selection[0])


class RemoteBrowserDialog(ModalDialog):
    """Browse the PS4 file system and pick a file or folder."""

    def __init__(self, app, start: str, mode: str = "file", prompt: str = "Pick a file") -> None:
        super().__init__(app, prompt, 640, 520, resizable=True)
        self.grab_set()
        self.mode = mode
        tk.Label(self, text=prompt, bg=Theme.bg, fg=Theme.text, font=Theme.font_h2).pack(
            anchor="w", padx=20, pady=(16, 8)
        )
        self.browser = RemoteBrowser(
            self, app, on_open_file=lambda path: self.close(path),
            on_pick=self._pick, select_mode=mode, compact=True,
        )
        self.browser.pack(fill="both", expand=True, padx=20)
        footer = tk.Frame(self, bg=Theme.bg)
        footer.pack(fill="x", padx=20, pady=16)
        self.use_folder_button = CButton(footer, "Use this folder", command=self._use_folder)
        self.use_folder_button.pack(side="left")
        CButton(footer, "Cancel", command=self.close, variant="ghost").pack(side="right")
        self.browser.go(start)

    def _pick(self, path: str) -> None:
        self.close(path)

    def _use_folder(self) -> None:
        self.close(self.browser.current_path)


class BackupsDialog(ModalDialog):
    def __init__(self, app) -> None:
        super().__init__(app, "Backups", 620, 460, resizable=True)
        self.grab_set()
        tk.Label(self, text="Automatic backups", bg=Theme.bg, fg=Theme.text, font=Theme.font_h2).pack(
            anchor="w", padx=20, pady=(16, 2)
        )
        tk.Label(self, text=f"Stored in {BACKUP_DIR}", bg=Theme.bg, fg=Theme.muted,
                 font=Theme.font_small).pack(anchor="w", padx=20, pady=(0, 10))
        body = tk.Frame(self, bg=Theme.surface, highlightthickness=1, highlightbackground=Theme.border)
        body.pack(fill="both", expand=True, padx=20)
        self.tree = ttk.Treeview(body, columns=("when", "size", "info"), show="tree headings",
                                 selectmode="browse")
        self.tree.heading("#0", text="File")
        self.tree.heading("when", text="Saved")
        self.tree.heading("info", text="Contents")
        self.tree.column("#0", width=250)
        self.tree.column("when", width=130)
        self.tree.column("info", width=160)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda _e: self.restore())

        footer = tk.Frame(self, bg=Theme.bg)
        footer.pack(fill="x", padx=20, pady=16)
        CButton(footer, "Restore to PS4", command=self.restore, variant="primary").pack(side="left")
        CButton(footer, "Load into editor", command=self.load_local).pack(side="left", padx=(8, 0))
        CButton(footer, "Save as ...", command=self.save_as).pack(side="left", padx=(8, 0))
        CButton(footer, "Close", command=self.close, variant="ghost").pack(side="right")
        self.refresh()

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for path in list_backups():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                doc = PluginsIni.parse(text)
                enabled, total = doc.stats()
                info = f"{enabled} on / {total} plugins"
            except OSError:
                info = "unreadable"
            try:
                stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                stamp = "-"
            self.tree.insert("", "end", iid=str(path), text=path.name, values=(stamp, info))

    def _selected(self) -> Optional[Path]:
        selection = self.tree.selection()
        return Path(selection[0]) if selection else None

    def restore(self) -> None:
        path = self._selected()
        if not path:
            return
        if not messagebox.askyesno(
            "Restore backup",
            f"Upload {path.name} to {self.app.config.remote_ini} on the PS4?",
            parent=self,
        ):
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            messagebox.showerror("Restore failed", str(exc), parent=self)
            return
        self.app.upload_ini_text(text, note=f"restored {path.name}")
        self.close()

    def load_local(self) -> None:
        path = self._selected()
        if not path:
            return
        self.app.load_local_path(path)
        self.close()

    def save_as(self) -> None:
        path = self._selected()
        if not path:
            return
        target = filedialog.asksaveasfilename(parent=self, initialfile=path.name,
                                             defaultextension=".ini",
                                             filetypes=[("plugins.ini", "*.ini"), ("All files", "*.*")])
        if not target:
            return
        shutil.copyfile(path, target)
        self.app.set_status(f"Saved a copy to {target}", "good")
        self.close()


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class PluginsView(tk.Frame):
    """The main plugin tool: list, toggle, add, remove."""

    def __init__(self, app, master) -> None:
        super().__init__(master, bg=Theme.bg)
        self.app = app
        self._rows: Dict[str, Tuple[str, int]] = {}

        head = tk.Frame(self, bg=Theme.bg)
        head.pack(fill="x", padx=24, pady=(20, 0))
        # The tab strip is packed *before* the title block on purpose. Tk hands out
        # space in packing order, and the long path under the title grows without
        # bound - so without this the strip is what gets squeezed, and "PKG
        # manager" is the first tab to shrink away in a narrow window.
        right = tk.Frame(head, bg=Theme.bg)
        right.pack(side="right")
        # the file browser and the PKG manager are part of the plugin tool: switch here
        self.app.make_tool_tabs(right, "plugins").pack(side="right", padx=(14, 0))
        self.dirty_label = tk.Label(right, text="", bg=Theme.bg, fg=Theme.warn, font=Theme.font_small)
        self.dirty_label.pack(side="left", padx=(0, 12))
        self.save_button = CButton(right, "Save to PS4", command=self.app.apply_async, variant="primary")
        self.save_button.pack(side="left")

        left = tk.Frame(head, bg=Theme.bg)
        left.pack(side="left")
        tk.Label(left, text="GoldHEN plugins", bg=Theme.bg, fg=Theme.text, font=Theme.font_h1).pack(anchor="w")
        self.subtitle = tk.Label(left, text=self.app.ini_path_label(), bg=Theme.bg, fg=Theme.muted,
                                 font=Theme.font_small)
        self.subtitle.pack(anchor="w", pady=(2, 0))

        toolbar = tk.Frame(self, bg=Theme.bg)
        toolbar.pack(fill="x", padx=24, pady=(14, 0))
        self.banner = tk.Label(toolbar, text="Tip: click the State column to toggle a plugin.",
                               bg=Theme.bg, fg=Theme.muted, font=Theme.font_small,
                               justify="left", anchor="w")
        self.banner.pack(side="left")

        actions = tk.Frame(self, bg=Theme.bg)
        actions.pack(fill="x", padx=24, pady=(10, 8))
        CButton(actions, "+  Add plugin", command=self.app.open_add_dialog, variant="secondary").pack(side="left")
        CButton(actions, "Remove", command=self.remove_selected, variant="danger").pack(side="left", padx=(8, 0))
        CButton(actions, "Enable all", command=lambda: self.set_all(True)).pack(side="left", padx=(8, 0))
        CButton(actions, "Disable all", command=lambda: self.set_all(False)).pack(side="left", padx=(8, 0))
        CButton(actions, "Reload", command=self.app.reload_async).pack(side="left", padx=(8, 0))
        CButton(actions, "Backups", command=lambda: BackupsDialog(self.app).show()).pack(side="right")
        CButton(actions, "Open local ...", command=self.app.open_local_async).pack(side="right", padx=(8, 0))
        CButton(actions, "Save copy ...", command=self.save_copy).pack(side="right", padx=(8, 0))

        body = tk.Frame(self, bg=Theme.surface, highlightthickness=1, highlightbackground=Theme.border)
        body.pack(fill="both", expand=True, padx=24, pady=(0, 6))
        self.tree = ttk.Treeview(body, columns=("state", "path"), show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Plugin", anchor="w")
        self.tree.heading("state", text="State", anchor="w")
        self.tree.heading("path", text="Path on PS4", anchor="w")
        self.tree.column("#0", width=300, minwidth=200, stretch=True)
        self.tree.column("state", width=140, minwidth=110, stretch=False)
        self.tree.column("path", width=420, minwidth=180, stretch=True)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("section", foreground=Theme.text_dim, font=Theme.font_bold)
        self.tree.tag_configure("on", foreground=Theme.text)
        self.tree.tag_configure("off", foreground=Theme.muted)
        self.tree.tag_configure("missing", foreground=Theme.warn)
        self.tree.tag_configure("empty", foreground=Theme.muted)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<space>", lambda _e: self.toggle_selected())
        self.tree.bind("<Delete>", lambda _e: self.remove_selected())

        self.menu = tk.Menu(self, tearoff=0, bg=Theme.surface2, fg=Theme.text,
                            activebackground=Theme.accent, activeforeground="#ffffff",
                            bd=0, font=Theme.font_ui)
        self.menu.add_command(label="Enable", command=lambda: self.set_selected(True))
        self.menu.add_command(label="Disable", command=lambda: self.set_selected(False))
        self.menu.add_separator()
        self.menu.add_command(label="Remove from plugins.ini", command=self.remove_selected)
        self.menu.add_command(label="Download to this PC", command=self.download_selected)
        self.menu.add_command(label="Copy path", command=self.copy_selected_path)
        self.menu.add_separator()
        self.menu.add_command(label="Enable all in section", command=lambda: self.section_set(True))
        self.menu.add_command(label="Disable all in section", command=lambda: self.section_set(False))

        self.footer = tk.Label(self, text="", bg=Theme.bg, fg=Theme.muted, font=Theme.font_small,
                               justify="left", anchor="w")
        self.footer.pack(fill="x", padx=24, pady=(0, 14))

        self.rebuild()

    # -- rendering --------------------------------------------------------
    def display_name(self, entry: PluginEntry) -> str:
        name = entry.path.rstrip("/").split("/")[-1] or entry.path
        if entry.kind == "plugin" and self.app.entry_missing(entry):
            return f"{name}   ⚠ not found on PS4"
        return name

    def state_text(self, entry: PluginEntry) -> str:
        if entry.kind != "plugin":
            return entry.value or ""
        return "● Enabled" if entry.enabled else "○ Disabled"

    def row_tag(self, section: IniSection, entry: PluginEntry) -> str:
        if entry.kind == "plugin" and self.app.entry_missing(entry):
            return "missing"
        return "on" if entry.enabled else "off"

    def rebuild(self) -> None:
        tree = self.tree
        tree.delete(*tree.get_children())
        self._rows.clear()
        doc = self.app.ini
        sections = doc.sections()
        if not sections:
            tree.insert("", "end", iid="empty", text="  No plugins yet — click “+ Add plugin” to create the first one.",
                        values=("", ""), tags=("empty",))
            self.update_footer()
            return
        for section in sections:
            sid = f"sec::{section.name}"
            label = self.app.section_label(section.name)
            entries = section.entries
            on = sum(1 for e in entries if e.enabled)
            tree.insert(
                "", "end", iid=sid, text=f"  {label}",
                values=(f"{on}/{len(entries)} enabled", ""), open=True, tags=("section",),
            )
            if not entries:
                tree.insert(sid, "end", iid=f"none::{section.name}",
                            text="   (no plugins in this section)", values=("", ""), tags=("empty",))
            for index, entry in enumerate(section.items):
                if not isinstance(entry, PluginEntry):
                    continue
                iid = f"pl::{section.name}::{index}"
                tree.insert(
                    sid, "end", iid=iid, text="   " + self.display_name(entry),
                    values=(self.state_text(entry), entry.path), tags=(self.row_tag(section, entry),),
                )
                self._rows[iid] = (section.name, index)
        self.update_footer()
        self.app.update_dirty_label()

    def _entry_at(self, iid: str) -> Optional[Tuple[IniSection, int, PluginEntry]]:
        info = self._rows.get(iid)
        if not info:
            return None
        section = self.app.ini.find_section(info[0])
        if section is None:
            return None
        index = info[1]
        if not 0 <= index < len(section.items):
            return None
        item = section.items[index]
        if not isinstance(item, PluginEntry):
            return None
        return section, index, item

    def refresh_row(self, iid: str) -> None:
        found = self._entry_at(iid)
        if found is None:
            self.rebuild()
            return
        section, _index, entry = found
        self.tree.item(iid, text="   " + self.display_name(entry),
                       values=(self.state_text(entry), entry.path),
                       tags=(self.row_tag(section, entry),))
        self.update_section_row(section)
        self.update_footer()

    def update_section_row(self, section: IniSection) -> None:
        sid = f"sec::{section.name}"
        if not self.tree.exists(sid):
            return
        entries = section.entries
        on = sum(1 for e in entries if e.enabled)
        self.tree.item(sid, values=(f"{on}/{len(entries)} enabled", ""))

    def update_footer(self) -> None:
        enabled, total = self.app.ini.stats()
        missing = sum(1 for s in self.app.ini.sections() for e in s.entries
                      if e.kind == "plugin" and self.app.entry_missing(e))
        parts = [f"{total} plugin{'s' if total != 1 else ''}", f"{enabled} enabled", f"{total - enabled} disabled"]
        if missing:
            parts.append(f"{missing} missing file{'s' if missing != 1 else ''}")
        parts.append("click the State column (or press space) to toggle")
        self.footer.configure(text="   ·   ".join(parts))
        self.subtitle.configure(text=self.app.ini_path_label())
        if self.app.ftp is None and self.app.local_path is None:
            self.banner.configure(
                text="Not connected — connect to your PS4, or use 'Open local ...' to edit a plugins.ini offline.",
                fg=Theme.warn,
            )
        elif self.app.local_path is not None and not self.app.ini_remote:
            self.banner.configure(
                text=f"Offline editing {self.app.local_path.name} — connect to push changes to the PS4.",
                fg=Theme.muted,
            )
        else:
            self.banner.configure(
                text="GoldHEN reads plugins.ini at startup, so restart GoldHEN (or the game) after changes.",
                fg=Theme.muted,
            )

    # -- interactions -----------------------------------------------------
    def _on_click(self, event) -> str:
        iid = self.tree.identify_row(event.y)
        region = self.tree.identify_region(event.x, event.y)
        if not iid or region not in ("cell", "tree"):
            return ""
        column = self.tree.identify_column(event.x)
        if iid in self._rows:
            if column in ("#0", "#1"):
                self.toggle_row(iid)
                return "break"
        elif iid.startswith("sec::"):
            if column == "#1":
                name = iid.split("::", 1)[1]
                self._section_bulk(name)
                return "break"
        return ""

    def _on_double_click(self, event) -> str:
        iid = self.tree.identify_row(event.y)
        if iid in self._rows:
            self.toggle_row(iid)
            return "break"
        return ""

    def _on_right_click(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _section_bulk(self, name: str, value: Optional[bool] = None) -> None:
        section = self.app.ini.find_section(name)
        if section is None:
            return
        current = [e for e in section.entries if e.kind == "plugin"]
        if not current or not value:
            value = not all(e.enabled for e in current) if current else True
        changed = False
        for entry in current:
            if entry.enabled != value:
                entry.set_enabled(value)
                changed = True
        if changed:
            self.rebuild()
            self.app.mark_dirty()

    def toggle_row(self, iid: str) -> None:
        found = self._entry_at(iid)
        if found is None:
            return
        _section, _index, entry = found
        if not entry.togglable:
            self.app.set_status("That line cannot be toggled - edit it by hand if needed.", "warn")
            return
        entry.set_enabled(not entry.enabled)
        self.refresh_row(iid)
        self.app.mark_dirty()

    def selected_entries(self) -> List[Tuple[str, int, PluginEntry]]:
        out: List[Tuple[str, int, PluginEntry]] = []
        for iid in self.tree.selection():
            found = self._entry_at(iid)
            if found is None:
                continue
            section, index, entry = found
            out.append((section.name, index, entry))
        return out

    def set_selected(self, value: bool) -> None:
        changed = False
        for iid in list(self.tree.selection()):
            found = self._entry_at(iid)
            if found is None or found[2].enabled == value:
                continue
            found[2].set_enabled(value)
            self.refresh_row(iid)
            changed = True
        if changed:
            self.app.mark_dirty()

    def toggle_selected(self) -> None:
        changed = False
        for iid in list(self.tree.selection()):
            found = self._entry_at(iid)
            if found is None or not found[2].togglable:
                continue
            found[2].set_enabled(not found[2].enabled)
            self.refresh_row(iid)
            changed = True
        if changed:
            self.app.mark_dirty()

    def section_set(self, value: bool) -> None:
        for section, _index, _entry in self.selected_entries():
            self._section_bulk(section, value)
            return

    def set_all(self, value: bool) -> None:
        changed = False
        for section in self.app.ini.sections():
            for entry in section.entries:
                if entry.kind == "plugin" and entry.enabled != value:
                    entry.set_enabled(value)
                    changed = True
        if changed:
            self.rebuild()
            self.app.mark_dirty()

    def remove_selected(self) -> None:
        selection = self.selected_entries()
        if not selection:
            self.app.set_status("Select one or more plugins to remove.", "warn")
            return
        names = ", ".join(self.display_name(e).split("   ")[0] for _s, _i, e in selection[:4])
        more = "" if len(selection) <= 4 else f" and {len(selection) - 4} more"
        if not messagebox.askyesno(
            "Remove plugins",
            f"Remove {names}{more} from plugins.ini?\n\n"
            "(Only the entry is removed - the plugin file stays on your PS4.)",
            parent=self,
        ):
            return
        # remove from the end so indices stay valid
        for section, index, _entry in sorted(selection, key=lambda item: (item[0], -item[1])):
            target = self.app.ini.find_section(section)
            if target and 0 <= index < len(target.items):
                del target.items[index]
        self.rebuild()
        self.app.mark_dirty()

    def download_selected(self) -> None:
        selection = self.selected_entries()
        if not selection:
            return
        _section, _index, entry = selection[0]
        self.app.download_remote_file(entry.path)

    def copy_selected_path(self) -> None:
        selection = self.selected_entries()
        if not selection:
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(e.path for _s, _i, e in selection))
        self.app.set_status("Path copied to the clipboard.", "good")

    def save_copy(self) -> None:
        target = filedialog.asksaveasfilename(
            parent=self, initialfile="plugins.ini", defaultextension=".ini",
            filetypes=[("plugins.ini", "*.ini"), ("Text", "*.txt"), ("All files", "*.*")],
        )
        if not target:
            return
        try:
            Path(target).write_text(self.app.ini.dumps(), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Could not save", str(exc), parent=self)
            return
        self.app.set_status(f"Saved a copy to {target}", "good")


class RemoteBrowser(tk.Frame):
    """File browser for the PS4 file system (used by the Files view + pickers)."""

    def __init__(self, master, app, on_open_file=None, on_pick=None,
                 select_mode: str = "file", compact: bool = False) -> None:
        super().__init__(master, bg=Theme.bg)
        self.app = app
        self.on_open_file = on_open_file
        self.on_pick = on_pick
        self.select_mode = select_mode
        self.current_path = "/"
        self.entries: List[RemoteEntry] = []

        bar = tk.Frame(self, bg=Theme.bg)
        bar.pack(fill="x", pady=(0, 8))
        self.var_path = tk.StringVar(value="/")
        entry = make_entry(bar, self.var_path, width=48, font=Theme.font_mono)
        entry.pack(side="left", fill="x", expand=True, ipady=5)
        entry.bind("<Return>", lambda _e: self.go(self.var_path.get()))
        if not compact:
            CButton(bar, "Up", command=self.go_up, bg=Theme.bg, height=30).pack(side="left", padx=(8, 0))
        CButton(bar, "Go", command=lambda: self.go(self.var_path.get()), bg=Theme.bg, height=30).pack(
            side="left", padx=(8, 0)
        )
        CButton(bar, "Refresh", command=lambda: self.go(self.current_path), bg=Theme.bg, height=30).pack(
            side="left", padx=(8, 0)
        )

        body = tk.Frame(self, bg=Theme.surface, highlightthickness=1, highlightbackground=Theme.border)
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=("size", "when"), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Name", anchor="w")
        self.tree.heading("size", text="Size", anchor="w")
        self.tree.heading("when", text="Modified", anchor="w")
        self.tree.column("#0", width=340, minwidth=180)
        self.tree.column("size", width=100, stretch=False)
        self.tree.column("when", width=150, stretch=False)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("dir", foreground=Theme.accent)
        self.tree.tag_configure("file", foreground=Theme.text)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Return>", self._on_double_click)

    # -- navigation -------------------------------------------------------
    def go(self, path: str) -> None:
        path = (path or "/").strip() or "/"
        if not path.startswith("/"):
            path = "/" + path
        self.app.set_status(f"Listing {path} ...", "muted")
        self.app.list_remote(
            path,
            on_done=lambda entries, target=path: self._fill(target, entries),
            on_error=lambda exc: self.app.set_status(f"Listing failed: {exc}", "bad"),
        )

    def _fill(self, path: str, entries: List[RemoteEntry]) -> None:
        self.current_path = path
        self.var_path.set(path)
        self.entries = entries or []
        self.tree.delete(*self.tree.get_children())
        for entry in self.entries:
            self.tree.insert(
                "", "end", iid=entry.path,
                text=("📁 " if entry.is_dir else "   ") + entry.name,
                values=("-" if entry.is_dir else fmt_size(entry.size), fmt_time(entry.mtime)),
                tags=("dir" if entry.is_dir else "file",),
            )
        self.app.set_status(f"{len(self.entries)} item(s) in {self.current_path}", "muted")

    def go_up(self) -> None:
        self.go(parent_remote(self.current_path))

    def selected(self) -> Optional[RemoteEntry]:
        selection = self.tree.selection()
        if not selection:
            return None
        target = selection[0]
        for entry in self.entries:
            if entry.path == target:
                return entry
        return None

    def _on_double_click(self, _event=None) -> str:
        entry = self.selected()
        if entry is None:
            return ""
        if entry.is_dir:
            self.go(entry.path)
            return "break"
        if self.on_pick:
            self.on_pick(entry.path)
        elif self.on_open_file:
            self.on_open_file(entry.path)
        return "break"


class FilesView(tk.Frame):
    """Browse/upload/download/delete plugin files on the PS4."""

    def __init__(self, app, master) -> None:
        super().__init__(master, bg=Theme.bg)
        self.app = app
        head = tk.Frame(self, bg=Theme.bg)
        head.pack(fill="x", padx=24, pady=(20, 12))
        # tabs first, title last - see the note in PluginsView above
        self.app.make_tool_tabs(head, "files").pack(side="right")
        CButton(head, "Jump to plugins folder", command=self.jump, bg=Theme.bg).pack(side="right", padx=(0, 8))
        left = tk.Frame(head, bg=Theme.bg)
        left.pack(side="left")
        tk.Label(left, text="Plugin files", bg=Theme.bg, fg=Theme.text, font=Theme.font_h1).pack(anchor="w")
        tk.Label(left, text=f"Folders and files on the PS4 — start folder: {app.config.remote_plugins_dir}",
                 bg=Theme.bg, fg=Theme.muted, font=Theme.font_small).pack(anchor="w", pady=(2, 0))
        self.browser = RemoteBrowser(self, app, on_open_file=self.download_file)
        self.browser.pack(fill="both", expand=True, padx=24)

        actions = tk.Frame(self, bg=Theme.bg)
        actions.pack(fill="x", padx=24, pady=14)
        CButton(actions, "Upload files ...", command=self.upload_files, variant="primary").pack(side="left")
        CButton(actions, "Download", command=lambda: self.download_file(None)).pack(side="left", padx=(8, 0))
        CButton(actions, "Delete", command=self.delete_selected, variant="danger").pack(side="left", padx=(8, 0))
        CButton(actions, "New folder", command=self.new_folder).pack(side="left", padx=(8, 0))
        CButton(actions, "⋮⋮ path", command=self.copy_path, variant="ghost").pack(side="right")

    def jump(self) -> None:
        self.browser.go(self.app.config.remote_plugins_dir or DEFAULT_PLUGINS_DIR)

    def refresh(self) -> None:
        if self.app.ftp is None:
            return
        self.browser.go(self.browser.current_path if self.browser.entries else
                        (self.app.config.remote_plugins_dir or DEFAULT_PLUGINS_DIR))

    def upload_files(self) -> None:
        if self.app.ftp is None:
            self.app.set_status("Connect to your PS4 first.", "warn")
            return
        paths = filedialog.askopenfilenames(
            parent=self, title="Upload to " + self.browser.current_path,
            filetypes=[("GoldHEN plugins", "*.prx *.sprx *.bin *.elf"), ("All files", "*.*")],
        )
        if not paths:
            return
        self.app.upload_files([Path(p) for p in paths], self.browser.current_path)

    def download_file(self, remote_path: Optional[str]) -> None:
        if self.app.ftp is None:
            self.app.set_status("Connect to your PS4 first.", "warn")
            return
        if remote_path is None:
            entry = self.browser.selected()
            if entry is None or entry.is_dir:
                self.app.set_status("Select a file to download.", "warn")
                return
            remote_path = entry.path
        self.app.download_remote_file(remote_path)

    def delete_selected(self) -> None:
        entry = self.browser.selected()
        if entry is None:
            self.app.set_status("Select something to delete.", "warn")
            return
        if not messagebox.askyesno("Delete", f"Really delete {entry.path} from the PS4?", parent=self):
            return
        self.app.delete_remote(entry.path, refresh=lambda: self.browser.go(self.browser.current_path))

    def new_folder(self) -> None:
        if self.app.ftp is None:
            self.app.set_status("Connect to your PS4 first.", "warn")
            return
        dialog = _TextPrompt(self.app, "New folder", "Folder name")
        name = dialog.show()
        if not name:
            return
        target = join_remote(self.browser.current_path, name)
        app = self.app
        app.begin_busy(f"Creating {name} ...")

        def work() -> str:
            app.require_ftp().mkdir(target)
            return target

        def done(_created: str) -> None:
            app.end_busy()
            app.set_status(f"Created {target}", "good")
            self.browser.go(self.browser.current_path)

        def failed(exc: BaseException) -> None:
            app.end_busy()
            app.set_status(f"Could not create folder: {exc}", "bad")

        app.worker.submit(work, done, failed, name="mkdir")

    def copy_path(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.browser.current_path)
        self.app.set_status("Folder path copied.", "good")


class _TextPrompt(ModalDialog):
    def __init__(self, app, title: str, label: str, initial: str = "") -> None:
        super().__init__(app, title, 420, 190)
        self.grab_set()
        tk.Label(self, text=label, bg=Theme.bg, fg=Theme.text, font=Theme.font_ui).pack(
            anchor="w", padx=20, pady=(22, 8)
        )
        self.var = tk.StringVar(value=initial)
        entry = make_entry(self, self.var, width=36)
        entry.pack(fill="x", padx=20, ipady=6)
        entry.focus_set()
        entry.bind("<Return>", lambda _e: self.close(self.var.get().strip()))
        footer = tk.Frame(self, bg=Theme.bg)
        footer.pack(fill="x", padx=20, pady=18)
        CButton(footer, "OK", command=lambda: self.close(self.var.get().strip()), variant="primary").pack(side="right")
        CButton(footer, "Cancel", command=self.close, variant="ghost").pack(side="right", padx=(0, 8))


PKG_SETUP_HELP = """\
Nothing is listening on {host}:12800 - and that port is the only way to install a
package on the console without touching the package installer in its menus.

What has to run on the PS4
  "Remote Package Installer" (by flatz, title id FLTZ00003). It is a tiny
  homebrew app: it listens on 12800, takes a package URL from this PC and hands
  the download to the PS4's own downloader, which installs the package and
  shows the usual progress notification.

  1. Get its .pkg from a homebrew store - "Copy link" below copies the PKG-Zone
     page for it. flatz's GitHub carries the source only, and building it needs
     the private PS4 SDK, so a store build is the practical route.
  2. Install that one .pkg on the PS4, once - Itemzflow's PKG Installer or
     GoldHEN's package installer can both do it without a PC.
  3. Start "Remote Package Installer" from the home screen, and leave it in the
     foreground until a transfer has actually started: the console suspends a
     background app, and that stops the download.
  4. Back here, press "Check console".

Alternative - GoldHEN's payload server
  Turn on BinLoader / the payload server in GoldHEN's settings (ports 9090,
  9020, 9021) and use a PC tool that pushes its own installer payload; the
  service it leaves running on 12800 is the same API this app speaks. This app
  ships no third-party payload on purpose, so the app above is the one to use.
  Save its address under Settings -> "Payload server (optional)" and the
  manager connects to it together with FTP, the header showing whether it
  answered. GoldHEN's BinLoader is experimental and on some builds stops
  listening after one connection - if the pill says "Payload no reply", toggle
  BinLoader on the console again.

Without that service the rest of this page still works
  "Send a .pkg ..." moves packages onto the console over FTP and deletes them
  again, and the installed-titles list reads the console's own database - handy
  together with the console's own installers (Itemzflow, FPKGi), or to keep a
  package on the console for later.
"""


class PkgSetupDialog(ModalDialog):
    """The one-time console-side setup, spelled out."""

    def __init__(self, app, host: str) -> None:
        super().__init__(app, "Console installer - setup", 660, 470)
        self.app = app
        self.host = host
        self.grab_set()
        tk.Label(self, text="One-time setup on the PS4", bg=Theme.bg, fg=Theme.text,
                 font=Theme.font_h2).pack(anchor="w", padx=22, pady=(18, 8))
        body = tk.Text(self, bg=Theme.surface, fg=Theme.text_dim, font=Theme.font_small,
                       relief="flat", wrap="word", bd=0, highlightthickness=0)
        body.pack(fill="both", expand=True, padx=22)
        body.insert("1.0", PKG_SETUP_HELP.format(host=host or "your PS4"))
        body.configure(state="disabled")
        footer = tk.Frame(self, bg=Theme.bg)
        footer.pack(fill="x", padx=22, pady=(14, 16))
        CButton(footer, "Copy the download link", command=self._copy_link, bg=Theme.bg).pack(side="left")
        CButton(footer, "Copy the source link", command=lambda: self._copy(pkg_manager.RPI_SOURCE_URL),
                bg=Theme.bg).pack(side="left", padx=(8, 0))
        CButton(footer, "Close", command=self.close, variant="primary", bg=Theme.bg).pack(side="right")

    def _copy_link(self) -> None:
        self._copy(pkg_manager.RPI_STORE_URL)

    def _copy(self, link: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(link)
        self.app.set_status("Remote Package Installer link copied to the clipboard.", "good")


class PkgManagerView(tk.Frame):
    """Install, remove and move .pkg files - its own page beside the plugin tool.

    Everything the console has to do is done by the console: it downloads the
    package from this PC (direct link or a local file served over HTTP) and
    installs it itself, so no package is ever copied to the console by hand and
    no menu on the PS4 has to be touched.
    """

    POLL_MS = 1500

    def __init__(self, app, master) -> None:
        super().__init__(master, bg=Theme.bg)
        self.app = app
        self.server: Optional[pkg_manager.LocalPkgServer] = None
        self.task: Optional[pkg_manager.InstallTask] = None
        self.installer: Optional[pkg_manager.RemoteInstaller] = None
        self.local_pkg: Optional[Path] = None
        self._poll_id: Optional[str] = None
        self._checked = False
        self.console_rows: Dict[str, RemoteEntry] = {}
        self.installed_rows: Dict[str, RemoteEntry] = {}

        head = tk.Frame(self, bg=Theme.bg)
        head.pack(fill="x", padx=24, pady=(20, 0))
        # tabs first, title last - see the note in PluginsView above
        right = tk.Frame(head, bg=Theme.bg)
        right.pack(side="right")
        self.service_pill = StatusPill(right, bg=Theme.bg)
        self.service_pill.pack(side="right")
        left = tk.Frame(head, bg=Theme.bg)
        left.pack(side="left")
        tk.Label(left, text="PKG manager", bg=Theme.bg, fg=Theme.text,
                 font=Theme.font_h1).pack(anchor="w")
        self.subtitle = tk.Label(
            left, text="Install a .pkg from a link or straight from this PC - the PS4 downloads it itself.",
            bg=Theme.bg, fg=Theme.muted, font=Theme.font_small)
        self.subtitle.pack(anchor="w", pady=(2, 0))

        body = VScrollFrame(self, bg=Theme.bg)
        body.pack(fill="both", expand=True, padx=24, pady=(14, 14))
        inner = body.inner

        # -- console installer service -------------------------------------
        service = self._card(inner, "Console installer",
                             "Installing has to be done by the console. It needs one small homebrew "
                             "service (port 12800) running - everything below is one-time setup.")
        row = tk.Frame(service, bg=Theme.surface)
        row.pack(fill="x", padx=14, pady=(0, 12))
        self.service_label = tk.Label(row, text="Not checked yet.", bg=Theme.surface, fg=Theme.text_dim,
                                      font=Theme.font_small, justify="left", anchor="w", wraplength=620)
        self.service_label.pack(side="left", fill="x", expand=True)
        CButton(row, "Check console", command=self.check_service, bg=Theme.surface, height=30).pack(side="left", padx=(8, 0))
        CButton(row, "How to set it up", command=self.show_setup, bg=Theme.surface, height=30).pack(side="left", padx=(8, 0))

        # -- install --------------------------------------------------------
        install = self._card(inner, "Install a package",
                             "A direct link is downloaded by the PS4 itself. A file on this PC is "
                             "streamed to the console over HTTP while it installs - nothing is copied to "
                             "the console over FTP first, and no menu on the PS4 is used.")
        row = tk.Frame(install, bg=Theme.surface)
        row.pack(fill="x", padx=14)
        tk.Label(row, text="Direct link", bg=Theme.surface, fg=Theme.text_dim, font=Theme.font_small,
                 width=12, anchor="w").pack(side="left")
        self.var_url = tk.StringVar()
        entry = make_entry(row, self.var_url, width=54, font=Theme.font_mono, bg=Theme.surface2)
        entry.pack(side="left", fill="x", expand=True, ipady=4)
        entry.bind("<Return>", lambda _e: self.install_from_link())
        CButton(row, "Install link", command=self.install_from_link, variant="primary",
                bg=Theme.surface, height=30).pack(side="left", padx=(8, 0))

        row = tk.Frame(install, bg=Theme.surface)
        row.pack(fill="x", padx=14, pady=(8, 0))
        tk.Label(row, text="From this PC", bg=Theme.surface, fg=Theme.text_dim, font=Theme.font_small,
                 width=12, anchor="w").pack(side="left")
        self.local_label = tk.Label(row, text="no file chosen", bg=Theme.surface2, fg=Theme.muted,
                                    font=Theme.font_mono, anchor="w", padx=8, pady=4)
        self.local_label.pack(side="left", fill="x", expand=True)
        CButton(row, "Choose ...", command=self.choose_local, bg=Theme.surface, height=30).pack(side="left", padx=(8, 0))
        self.local_button = CButton(row, "Install from this PC", command=self.install_from_pc,
                                    variant="primary", bg=Theme.surface, height=30)
        self.local_button.pack(side="left", padx=(8, 0))

        self.var_parts = tk.BooleanVar(value=True)
        CCheck(install, "Send every piece when the package is split (name_0.pkg, name_1.pkg, ...)",
               variable=self.var_parts, bg=Theme.surface).pack(anchor="w", padx=14, pady=(10, 6))

        progress_row = tk.Frame(install, bg=Theme.surface)
        progress_row.pack(fill="x", padx=14, pady=(0, 12))
        self.progress = ttk.Progressbar(progress_row, mode="determinate", maximum=100, length=380,
                                        style="PS4.Horizontal.TProgressbar")
        self.progress.pack(side="left")
        self.progress_label = tk.Label(progress_row, text="Idle.", bg=Theme.surface, fg=Theme.muted,
                                       font=Theme.font_small, anchor="w")
        self.progress_label.pack(side="left", padx=(12, 0))
        self.stop_button = CButton(progress_row, "Stop", command=self.stop_install, variant="danger",
                                   bg=Theme.surface, height=28)
        self.stop_button.pack(side="right")
        self.stop_button.set_enabled(False)

        # -- package files on the console -----------------------------------
        files = self._card(inner, "Packages on the PS4",
                           "Files that live on the console, so a package can be installed later without "
                           "the PC, or kept as a backup of what you sent.")
        self.console_tree = self._tree(files, (("size", "Size", 110), ("where", "Folder", 240)))
        row = tk.Frame(files, bg=Theme.surface)
        row.pack(fill="x", padx=14, pady=(10, 12))
        CButton(row, "Send a .pkg ...", command=self.send_pkgs_to_console, variant="primary",
                bg=Theme.surface).pack(side="left")
        CButton(row, "Delete from PS4", command=self.delete_console_pkgs, variant="danger",
                bg=Theme.surface).pack(side="left", padx=(8, 0))
        CButton(row, "Refresh", command=self.refresh_console_pkgs, bg=Theme.surface).pack(side="left", padx=(8, 0))
        self.console_hint = tk.Label(row, text="", bg=Theme.surface, fg=Theme.muted, font=Theme.font_small)
        self.console_hint.pack(side="right")

        # -- installed titles -----------------------------------------------
        apps = self._card(inner, "Installed on the PS4",
                          "Uninstalling also goes through the console service, so it works without touch"
                          "ing the console itself. Saved data stays unless the console removes it.")
        self.apps_tree = self._tree(apps)
        row = tk.Frame(apps, bg=Theme.surface)
        row.pack(fill="x", padx=14, pady=(10, 12))
        self.uninstall_button = CButton(row, "Uninstall selected", command=self.uninstall_selected,
                                        variant="danger", bg=Theme.surface)
        self.uninstall_button.pack(side="left")
        CButton(row, "Refresh", command=self.refresh_installed, bg=Theme.surface).pack(side="left", padx=(8, 0))
        self.apps_hint = tk.Label(row, text="", bg=Theme.surface, fg=Theme.muted, font=Theme.font_small)
        self.apps_hint.pack(side="right")

    # -- small builders -----------------------------------------------------
    def _card(self, parent, title: str, hint: str = "") -> tk.Frame:
        card = tk.Frame(parent, bg=Theme.surface, highlightthickness=1,
                        highlightbackground=Theme.border)
        card.pack(fill="x", pady=(0, 14))
        tk.Label(card, text=title, bg=Theme.surface, fg=Theme.text,
                 font=Theme.font_h2).pack(anchor="w", padx=14, pady=(12, 0))
        if hint:
            tk.Label(card, text=hint, bg=Theme.surface, fg=Theme.muted, font=Theme.font_small,
                     justify="left", anchor="w", wraplength=880).pack(anchor="w", padx=14, pady=(3, 10))
        return card

    def _tree(self, parent, columns: Sequence[Tuple[str, str, int]] = ()) -> ttk.Treeview:
        """A short, scrollable list. Extra columns are optional - with none this
        is a plain name column."""
        frame = tk.Frame(parent, bg=Theme.surface, height=170)
        frame.pack(fill="x", padx=14)
        frame.pack_propagate(False)
        keys = tuple(c[0] for c in columns)
        tree = ttk.Treeview(frame, columns=keys,
                            show="tree headings" if keys else "tree",
                            selectmode="extended", height=7)
        tree.heading("#0", text="Name" if keys else "Title", anchor="w")
        tree.column("#0", width=360, minwidth=200, stretch=True)
        for key, label, width in columns:
            tree.heading(key, text=label, anchor="w")
            tree.column(key, width=width, minwidth=80, stretch=False)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scroll.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        tree.tag_configure("empty", foreground=Theme.muted)
        tree.tag_configure("ok", foreground=Theme.text)
        return tree

    # -- helpers -----------------------------------------------------------
    def console_host(self) -> str:
        return (self.app.config.host or "").strip()

    def pkg_dir(self) -> str:
        return (self.app.config.pkg_dir or pkg_manager.DEFAULT_PKG_DIR).rstrip("/") or "/"

    def bg_log(self, message: str) -> None:
        """Log from a worker thread.

        The HTTP client and the file server run on worker threads, and touching
        a widget from one of those stalls for a second per call (Tkinter makes
        the caller wait for the UI thread to reach its main loop).  Hop back to
        the UI thread through the app's worker queue instead.
        """
        self.app.worker.post(self.app.log, message)

    def make_installer(self) -> Optional[pkg_manager.RemoteInstaller]:
        host = self.console_host()
        if not host:
            self.app.set_status("Connect to your PS4 first - the PKG manager needs its address.", "warn")
            return None
        return pkg_manager.RemoteInstaller(host, logger=self.bg_log)

    def require_ftp(self):
        if self.app.ftp is None:
            self.app.set_status("Connect to your PS4 first.", "warn")
            return None
        return self.app.ftp

    # -- what happens when the page opens ----------------------------------
    def on_shown(self) -> None:
        if not self._checked:
            self._checked = True
            self.check_service(quiet=True)
        if self.app.ftp is not None:
            self.refresh_console_pkgs()
            self.refresh_installed()

    def shutdown(self) -> None:
        self._cancel_poll()
        self._stop_server()

    # -- installer service -------------------------------------------------
    def check_service(self, quiet: bool = False) -> None:
        installer = self.make_installer()
        if installer is None:
            return
        self.service_pill.set("Checking ...", Theme.warn)
        self.service_label.configure(text="Looking for the installer service on "
                                          f"{installer.host}:{installer.port} ...", fg=Theme.text_dim)

        def work() -> bool:
            return installer.probe()

        def done(ready: bool) -> None:
            self.installer = installer if ready else None
            if ready:
                self.service_pill.set("Installer ready", Theme.good)
                self.service_label.configure(
                    text=f"Remote Package Installer is running on {installer.host}:{installer.port} - "
                         "packages can be installed and removed from here.", fg=Theme.text)
                self.uninstall_button.set_enabled(True)
                self.apps_hint.configure(text="")
            else:
                self.service_pill.set("Installer not running", Theme.warn)
                self.service_label.configure(
                    text=f"Nothing answers on {installer.host}:{installer.port}. Press \"How to set it up\" "
                         "for the one-time install, then \"Check console\" again. Everything else on this "
                         "tab (sending files, listing, deleting) works right now.", fg=Theme.warn)
                self.uninstall_button.set_enabled(False)
                self.apps_hint.configure(text="Uninstalling needs the installer service.")
            if not quiet:
                self.app.set_status("Console installer ready." if ready
                                    else "No installer service on the console yet - see the setup notes.",
                                    "good" if ready else "warn")

        def failed(exc: BaseException) -> None:
            self.service_pill.set("Installer not running", Theme.warn)
            self.service_label.configure(text=f"Could not check the console: {exc}", fg=Theme.warn)

        self.app.worker.submit(work, done, failed, name="pkg-probe")

    def show_setup(self) -> None:
        PkgSetupDialog(self.app, self.console_host()).show()

    # -- installing ---------------------------------------------------------
    def choose_local(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self, title="Choose a package to install on the PS4",
            filetypes=[("PS4 packages", "*.pkg"), ("All files", "*.*")])
        if not chosen:
            return
        self.local_pkg = Path(chosen)
        self.show_local_file()

    def show_local_file(self) -> None:
        if self.local_pkg is None:
            self.local_label.configure(text="no file chosen", fg=Theme.muted)
            return
        try:
            size = self.local_pkg.stat().st_size
        except OSError:
            size = None
        self.local_label.configure(text=f"{self.local_pkg.name}   ({fmt_size(size)})", fg=Theme.text)

    def install_from_link(self) -> None:
        url = (self.var_url.get() or "").strip()
        if not url:
            self.app.set_status("Paste a http(s) link to a .pkg (or a .json manifest) first.", "warn")
            return
        if not url.lower().startswith(("http://", "https://")):
            self.app.set_status("The link has to start with http:// or https://", "warn")
            return
        installer = self.make_installer()
        if installer is None:
            return
        if not installer.probe():
            self.warn_no_service()
            return
        self.installer = installer
        self.app.begin_busy("Asking the PS4 to download the package ...")

        def work() -> pkg_manager.InstallTask:
            if pkg_manager.looks_like_manifest(url):
                return installer.install_manifest(url)
            return installer.install_direct([url])

        def done(task: pkg_manager.InstallTask) -> None:
            self.app.end_busy()
            self.start_tracking(task, source=url)

        def failed(exc: BaseException) -> None:
            self.app.end_busy()
            self.install_failed(exc)

        self.app.worker.submit(work, done, failed, name="pkg-install-link")

    def install_from_pc(self) -> None:
        path = self.local_pkg
        if path is None:
            self.choose_local()
            path = self.local_pkg
            if path is None:
                return
        if not path.is_file():
            self.app.set_status(f"{path} is no longer there.", "bad")
            return
        installer = self.make_installer()
        if installer is None:
            return
        if not installer.probe():
            self.warn_no_service()
            return
        host = installer.host
        ip = pkg_manager.local_ip_for(host)
        if not ip:
            self.app.set_status("This PC has no network route to the console - is the LAN up?", "bad")
            return
        files = pkg_manager.split_package_parts(path) if self.var_parts.get() else [path]
        server = pkg_manager.LocalPkgServer(files, ip, allow_from=host, logger=self.bg_log)
        self.installer = installer
        self.app.begin_busy(f"Streaming {path.name} to the PS4 ...")

        def work() -> pkg_manager.InstallTask:
            server.start()
            urls = server.urls()
            self.app.log("Serving " + ", ".join(urls))
            return installer.install_direct(urls)

        def done(task: pkg_manager.InstallTask) -> None:
            self.app.end_busy()
            self.server = server
            self.start_tracking(task, source=f"{path.name} via {ip}:{server.port}")

        def failed(exc: BaseException) -> None:
            self.app.end_busy()
            server.stop()
            self.install_failed(exc)

        self.app.worker.submit(work, done, failed, name="pkg-install-local")

    def warn_no_service(self) -> None:
        host = self.console_host()
        self.service_pill.set("Installer not running", Theme.warn)
        self.app.set_status(f"Nothing answers on {host}:{pkg_manager.RPI_PORT} - press "
                            "\"How to set it up\" in the PKG manager for the one-time install.", "warn")

    def install_failed(self, exc: BaseException) -> None:
        self.app.set_status(f"Install failed: {exc}", "bad")
        self.progress_label.configure(text="Failed.", fg=Theme.bad)
        self.progress["value"] = 0
        self.stop_button.set_enabled(False)

    # -- tracking -----------------------------------------------------------
    def start_tracking(self, task: pkg_manager.InstallTask, source: str = "") -> None:
        self.task = task
        self.progress["value"] = 0
        self.progress_label.configure(text=f"Task {task.task_id} started ...", fg=Theme.text_dim)
        self.stop_button.set_enabled(True)
        title = task.title or "package"
        self.app.set_status(f"Installing {title} - the PS4 is downloading it now.", "muted")
        self.app.log(f"Install task {task.task_id} ({title}) from {source}")
        self._schedule_poll()

    def _schedule_poll(self, delay: Optional[int] = None) -> None:
        self._cancel_poll()
        self._poll_id = self.after(self.POLL_MS if delay is None else delay, self._poll)

    def _cancel_poll(self) -> None:
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None

    def _poll(self) -> None:
        self._poll_id = None
        installer, task = self.installer, self.task
        if installer is None or task is None:
            return

        def work() -> pkg_manager.TaskProgress:
            return installer.progress(task.task_id)

        def done(state: pkg_manager.TaskProgress) -> None:
            self.progress["value"] = max(0.0, min(100.0, state.percent))
            self.progress_label.configure(text=state.detail or state.state, fg=Theme.text_dim)
            if state.failed:
                self.app.set_status(f"Install failed: {state.detail}", "bad")
                self._finish_tracking()
                return
            if state.done:
                name = task.title or "Package"
                self.progress["value"] = 100
                self.progress_label.configure(text="Installed.", fg=Theme.good)
                self.app.set_status(f"{name} installed on the PS4.", "good")
                self.app.log(f"Install task {task.task_id} finished: {name}")
                self._finish_tracking()
                self.refresh_installed(quiet=True)
                return
            self._schedule_poll()

        def failed(exc: BaseException) -> None:
            # a hiccup while polling must not abort a running install
            self.app.log(f"Progress check failed: {exc}")
            self._schedule_poll(self.POLL_MS * 2)

        self.app.worker.submit(work, done, failed, name="pkg-progress")

    def _finish_tracking(self) -> None:
        self._cancel_poll()
        self.task = None
        self.stop_button.set_enabled(False)
        self._stop_server()

    def stop_install(self) -> None:
        installer, task = self.installer, self.task
        if installer is None or task is None:
            self._finish_tracking()
            return
        self.app.set_status(f"Stopping install task {task.task_id} ...", "warn")

        def work() -> None:
            installer.task_action("stop", task.task_id)

        def done(_result: Any) -> None:
            self.app.set_status("Install stopped. The console keeps what it already downloaded.", "warn")
            self.progress_label.configure(text="Stopped.", fg=Theme.warn)
            self._finish_tracking()

        def failed(exc: BaseException) -> None:
            self.app.set_status(f"Could not stop the task: {exc}", "bad")
            self._finish_tracking()

        self.app.worker.submit(work, done, failed, name="pkg-stop")

    def _stop_server(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            server.stop()

    # -- files on the console -----------------------------------------------
    def refresh_console_pkgs(self, quiet: bool = False) -> None:
        # Deliberately not require_ftp(): listing is a side job here and must
        # never overwrite a status message that matters (an install finishing).
        ftp = self.app.ftp
        if ftp is None:
            if not quiet:
                self.console_hint.configure(text="connect to your PS4 to list its package files")
            return
        directory = self.pkg_dir()

        def work() -> List[RemoteEntry]:
            try:
                ftp.ensure_dir(directory)
            except Exception:
                pass
            return pkg_manager.pkg_entries(ftp.listdir(directory))

        def done(entries: List[RemoteEntry]) -> None:
            self.console_rows.clear()
            self.console_tree.delete(*self.console_tree.get_children())
            if not entries:
                self.console_tree.insert("", "end", text=f"  (no .pkg files in {directory})",
                                         values=("", ""), tags=("empty",))
            for entry in entries:
                iid = f"pkg::{entry.path}"
                self.console_rows[iid] = entry
                self.console_tree.insert("", "end", iid=iid, text="   " + entry.name,
                                         values=(fmt_size(entry.size), directory), tags=("ok",))
            total = sum(e.size or 0 for e in entries)
            self.console_hint.configure(text=f"{len(entries)} file(s), {fmt_size(total)}" if entries else "")

        def failed(exc: BaseException) -> None:
            self.app.set_status(f"Could not list {directory}: {exc}", "bad")

        self.app.worker.submit(work, done, failed, name="pkg-list")

    def send_pkgs_to_console(self) -> None:
        ftp = self.require_ftp()
        if ftp is None:
            return
        chosen = filedialog.askopenfilenames(
            parent=self, title=f"Send to {self.pkg_dir()} on the PS4",
            filetypes=[("PS4 packages", "*.pkg"), ("All files", "*.*")])
        if not chosen:
            return
        directory = self.pkg_dir()
        paths = [Path(p) for p in chosen]
        self.app.begin_busy(f"Sending {len(paths)} file(s) to the PS4 ...")

        def work() -> List[str]:
            ftp.ensure_dir(directory)
            sent: List[str] = []
            for path in paths:
                remote = join_remote(directory, path.name)
                ftp.upload(path, remote, progress=self.app.worker.progress(self.app.on_progress))
                sent.append(remote)
            return sent

        def done(sent: List[str]) -> None:
            self.app.end_busy()
            self.app.set_status(f"Sent {len(sent)} file(s) to {directory} on the PS4.", "good")
            self.app.log("Sent: " + ", ".join(sent))
            self.refresh_console_pkgs()

        def failed(exc: BaseException) -> None:
            self.app.end_busy()
            self.app.set_status(f"Send failed: {exc}", "bad")

        self.app.worker.submit(work, done, failed, name="pkg-send")

    def delete_console_pkgs(self) -> None:
        ftp = self.require_ftp()
        if ftp is None:
            return
        selected = [self.console_rows.get(iid) for iid in self.console_tree.selection()]
        selected = [entry for entry in selected if entry is not None]
        if not selected:
            self.app.set_status("Select the package file(s) you want to delete from the PS4.", "warn")
            return
        names = ", ".join(e.name for e in selected[:3])
        more = "" if len(selected) <= 3 else f" and {len(selected) - 3} more"
        if not messagebox.askyesno(
                "Delete from the PS4",
                f"Delete {names}{more} from the console?\n\n"
                "This only removes the stored package file - a game that is already installed stays installed.",
                parent=self):
            return
        self.app.begin_busy("Deleting ...")

        def work() -> int:
            for entry in selected:
                ftp.delete(entry.path)
            return len(selected)

        def done(count: int) -> None:
            self.app.end_busy()
            self.app.set_status(f"Deleted {count} package file(s) from the PS4.", "good")
            self.refresh_console_pkgs()

        def failed(exc: BaseException) -> None:
            self.app.end_busy()
            self.app.set_status(f"Delete failed: {exc}", "bad")

        self.app.worker.submit(work, done, failed, name="pkg-delete")

    # -- installed titles ---------------------------------------------------
    def refresh_installed(self, quiet: bool = False) -> None:
        ftp = self.app.ftp
        if ftp is None:
            if not quiet:
                self.apps_hint.configure(text="connect to your PS4 to list what is installed")
            return

        def work() -> List[RemoteEntry]:
            # The two appmeta directories are usually the same content through
            # different mounts, so collect by name instead of by listing.
            found: Dict[str, RemoteEntry] = {}
            for directory in APPMETA_DIRS:
                try:
                    entries = ftp.listdir(directory)
                except Exception:
                    continue
                for entry in entries:
                    if not entry.is_dir or not is_title_id(entry.name):
                        continue
                    if entry.name.upper().startswith("NPXS"):  # system apps
                        continue
                    found.setdefault(entry.name, entry)
            return [found[name] for name in sorted(found)]

        def done(entries: List[RemoteEntry]) -> None:
            self.installed_rows.clear()
            self.apps_tree.delete(*self.apps_tree.get_children())
            if not entries:
                self.apps_tree.insert("", "end", text="  (nothing found)", tags=("empty",))
            for entry in entries:
                iid = f"app::{entry.name}"
                self.installed_rows[iid] = entry
                title = self.app.titles.get(entry.name.upper(), "")
                label = f"{entry.name}   {title}" if title else entry.name
                self.apps_tree.insert("", "end", iid=iid, text="   " + label, tags=("ok",))
            self.apps_hint.configure(text=f"{len(entries)} title(s)" if entries else "")

        def failed(exc: BaseException) -> None:
            self.app.set_status(f"Could not read the installed titles: {exc}", "bad")

        self.app.worker.submit(work, done, failed, name="pkg-installed")

    def uninstall_selected(self) -> None:
        installer = self.installer or self.make_installer()
        if installer is None:
            return
        selected = [self.installed_rows.get(iid) for iid in self.apps_tree.selection()]
        selected = [entry for entry in selected if entry is not None]
        if not selected:
            self.app.set_status("Select the title you want to remove from the PS4.", "warn")
            return
        title_id = norm_title_id(selected[0].name)
        title = self.app.titles.get(title_id, "")
        if not messagebox.askyesno(
                "Uninstall from the PS4",
                f"Uninstall {title_id}{' (' + title + ')' if title else ''} from the console?\n\n"
                "The application is removed from the PS4. Saved data is left alone.",
                parent=self):
            return
        if not installer.probe():
            self.warn_no_service()
            return
        self.installer = installer
        self.app.begin_busy(f"Uninstalling {title_id} ...")

        def work() -> str:
            installer.uninstall("game", title_id)
            return title_id

        def done(removed: str) -> None:
            self.app.end_busy()
            self.app.set_status(f"Uninstalled {removed} from the PS4.", "good")
            self.refresh_installed()

        def failed(exc: BaseException) -> None:
            self.app.end_busy()
            self.app.set_status(f"Uninstall failed: {exc}", "bad")

        self.app.worker.submit(work, done, failed, name="pkg-uninstall")


class SettingsView(VScrollFrame):
    """FTP credentials + behaviour settings."""

    def __init__(self, app, master) -> None:
        super().__init__(master, bg=Theme.bg)
        self.app = app
        self.container = self.inner
        self.vars: Dict[str, Any] = {}

        header = tk.Frame(self.container, bg=Theme.bg)
        header.pack(fill="x", padx=24, pady=(20, 4))
        tk.Label(header, text="Settings", bg=Theme.bg, fg=Theme.text, font=Theme.font_h1).pack(anchor="w")
        tk.Label(header, text="Saved automatically - next time you start the app it connects for you.",
                 bg=Theme.bg, fg=Theme.muted, font=Theme.font_small).pack(anchor="w", pady=(2, 0))

        self._connection_card()
        self._payloader_card()
        self._saved_card()
        self._behaviour_card()
        self._about_card()

        footer = tk.Frame(self.container, bg=Theme.bg)
        footer.pack(fill="x", padx=24, pady=(4, 24))
        CButton(footer, "Save & connect", command=self.save_and_connect, variant="primary").pack(side="left")
        CButton(footer, "Save", command=self.save).pack(side="left", padx=(8, 0))
        CButton(footer, "Test connection", command=self.test_connection).pack(side="left", padx=(8, 0))
        CButton(footer, "Find my PS4 on the network", command=self.scan_network).pack(side="left", padx=(8, 0))
        self.result = tk.Label(self.container, text="", bg=Theme.bg, fg=Theme.text_dim,
                               font=Theme.font_small, justify="left", anchor="w")
        self.result.pack(fill="x", padx=24, pady=(0, 24))

        self.load_from_config()
        # keep the "next start" line in sync while the fields are edited
        for key in ("host", "port", "user", "password", "auto_connect"):
            var = self.vars.get(key)
            if var is not None:
                var.trace_add("write", lambda *_a: self._refresh_saved())
        self._refresh_saved()
        for key in ("payloader_host", "payloader_port"):
            var = self.vars.get(key)
            if var is not None:
                var.trace_add("write", lambda *_a: self._refresh_payloader_hint())
        self._refresh_payloader_hint()
    def _card(self, title: str, subtitle: str = "") -> tk.Frame:
        card = tk.Frame(self.container, bg=Theme.surface, highlightthickness=1,
                        highlightbackground=Theme.border)
        card.pack(fill="x", padx=24, pady=(12, 0))
        inner = tk.Frame(card, bg=Theme.surface)
        inner.pack(fill="x", padx=18, pady=16)
        tk.Label(inner, text=title, bg=Theme.surface, fg=Theme.text, font=Theme.font_h2).pack(anchor="w")
        if subtitle:
            tk.Label(inner, text=subtitle, bg=Theme.surface, fg=Theme.muted, font=Theme.font_small).pack(
                anchor="w", pady=(2, 0)
            )
        body = tk.Frame(inner, bg=Theme.surface)
        body.pack(fill="x", pady=(12, 0))
        return body

    def _row(self, body: tk.Frame, label: str, hint: str = "") -> tk.Frame:
        # Grid, not pack: the label column must keep a 190 px minimum width
        # *without* pinning the frame's height. A frame with width=190 and
        # pack_propagate(False) collapses to 1 px tall, which squashed every
        # label and hint in this view into an unreadable 1 px strip.
        row = tk.Frame(body, bg=Theme.surface)
        row.pack(fill="x", pady=5)
        row.columnconfigure(0, minsize=190)
        row.columnconfigure(1, weight=1)
        left = tk.Frame(row, bg=Theme.surface)
        left.grid(row=0, column=0, sticky="nw")
        tk.Label(left, text=label, bg=Theme.surface, fg=Theme.text_dim, font=Theme.font_ui).pack(anchor="w")
        if hint:
            tk.Label(left, text=hint, bg=Theme.surface, fg=Theme.muted, font=Theme.font_small,
                     wraplength=180, justify="left").pack(anchor="w")
        right = tk.Frame(row, bg=Theme.surface)
        right.grid(row=0, column=1, sticky="nw")
        return right

    def _entry_row(self, body, key: str, label: str, default: str = "", hint: str = "",
                   width: int = 26, mono: bool = False) -> tk.Entry:
        right = self._row(body, label, hint)
        var = tk.StringVar(value=default)
        self.vars[key] = var
        entry = make_entry(right, var, width=width, font=Theme.font_mono if mono else Theme.font_ui)
        entry.pack(side="left", ipady=5)
        return entry

    def _switch_row(self, body, key: str, label: str, hint: str = "", default: bool = False) -> CSwitch:
        right = self._row(body, label, hint)
        var = tk.BooleanVar(value=default)
        self.vars[key] = var
        switch = CSwitch(right, variable=var, bg=Theme.surface)
        switch.pack(side="left")
        return switch

    def _connection_card(self) -> None:
        body = self._card("PS4 connection",
                          "GoldHEN's FTP server: enable it in GoldHEN settings, default port 2121.")
        self.host_entry = self._entry_row(body, "host", "PS4 IP address", hint="e.g. 192.168.1.50", width=22)
        self._entry_row(body, "port", "FTP port", default=str(DEFAULT_PORT), width=10)
        self._entry_row(body, "user", "Username", default="anonymous", width=22)
        self.password_entry = self._entry_row(body, "password", "Password", hint="anonymous needs none", width=22)
        self.password_entry.configure(show="•")
        toggle_row = self._row(body, "")
        CButton(toggle_row, "Show / hide password", command=self._toggle_password, bg=Theme.surface,
                height=30).pack(side="left")
        self._switch_row(body, "passive", "Passive mode", hint="keep it on unless FTP fails", default=True)
        self._entry_row(body, "timeout", "Timeout (seconds)", default="20", width=8)
        self._entry_row(body, "remote_ini", "plugins.ini path", default=DEFAULT_INI, width=40, mono=True)
        self._entry_row(body, "remote_plugins_dir", "Plugins folder", default=DEFAULT_PLUGINS_DIR,
                        width=40, mono=True)
        hint = tk.Label(body, text="A Windows-style path like /data/GoldHEN/plugins.ini is correct — the PS4 "
                                   "sees it as the root of the file system.",
                        bg=Theme.surface, fg=Theme.muted, font=Theme.font_small, justify="left",
                        wraplength=460)
        hint.pack(anchor="w", pady=(6, 0))

    def _payloader_card(self) -> None:
        """GoldHEN's payload server: an address here means "connect to it too"."""
        body = self._card(
            "Payload server (optional)",
            "GoldHEN's payload server - BinLoader - is switched on in GoldHEN's settings, not "
            "here. Save its address and the manager connects to it together with FTP: one "
            "Connect button, nothing extra to press. Empty means the manager stays on FTP only.")
        self._entry_row(body, "payloader_host", "PS4 address", hint="empty = payloader off", width=22)
        self._entry_row(body, "payloader_port", "Payload port", default=str(DEFAULT_PAYLOAD_PORT),
                        hint="9090 in GoldHEN unless you changed it", width=10)
        actions = self._row(body, "")
        CButton(actions, "Use the PS4 address above", command=self._copy_host_to_payloader,
                bg=Theme.surface, height=30).pack(side="left")
        CButton(actions, "Test payload server", command=self.test_payloader, bg=Theme.surface,
                height=30).pack(side="left", padx=(8, 0))
        self.payload_result = tk.Label(body, text="", bg=Theme.surface, fg=Theme.text_dim,
                                       font=Theme.font_small, justify="left", anchor="w",
                                       wraplength=560)
        self.payload_result.pack(anchor="w", pady=(8, 0))

    def _refresh_payloader_hint(self) -> None:
        """Plain words about whether the payload server will be used, and why."""
        host = str(self.vars["payloader_host"].get() or "").strip()
        port = str(self.vars["payloader_port"].get() or "").strip() or str(DEFAULT_PAYLOAD_PORT)
        if host:
            self.payload_result.configure(
                text=f"✔  Connect will also open the payload server at {host}:{port}.", fg=Theme.good)
        else:
            self.payload_result.configure(
                text="No payloader address saved - the manager connects over FTP only.", fg=Theme.muted)

    def _copy_host_to_payloader(self) -> None:
        host = str(self.vars["host"].get() or "").strip()
        if not host:
            self.payload_result.configure(text="Fill in the PS4 address at the top first.", fg=Theme.warn)
            return
        self.vars["payloader_host"].set(host)
        self.payload_result.configure(
            text=f"{host} filled in for the payload server - press Save settings to keep it.",
            fg=Theme.text_dim)

    def _typed_payloader(self) -> Optional[Payloader]:
        """A client built from what is typed right now, not from the saved settings."""
        host = str(self.vars["payloader_host"].get() or "").strip()
        if not host:
            return None
        try:
            port = int(str(self.vars["payloader_port"].get() or "").strip() or DEFAULT_PAYLOAD_PORT)
        except ValueError:
            port = DEFAULT_PAYLOAD_PORT
        timeout = float(self.app.config.timeout or 20)
        return Payloader(host=host, port=max(1, port), timeout=max(3.0, min(timeout, 30.0)),
                         logger=self.app.bg_log)

    def test_payloader(self) -> None:
        payloader = self._typed_payloader()
        if payloader is None:
            self.payload_result.configure(text="Enter the console's address in the payloader card - "
                                               "the payload server runs on the PS4.", fg=Theme.warn)
            return
        self.payload_result.configure(text=f"Talking to {payloader.host}:{payloader.port} ...",
                                      fg=Theme.text_dim)
        self.app.begin_busy("Testing the payload server ...")

        def work() -> str:
            return payloader.probe()

        def done(summary: str) -> None:
            self.app.end_busy()
            self.payload_result.configure(
                text=f"✔  Payload server answered at {payloader.host}:{payloader.port}: {summary}. "
                     "Connect picks it up together with FTP.", fg=Theme.good)
            self.app.set_status("Payload server reachable.", "good")

        def failed(exc: BaseException) -> None:
            self.app.end_busy()
            self.payload_result.configure(text=f"✗ {exc}", fg=Theme.bad)
            self.app.set_status("Payload server did not answer.", "warn")

        self.app.worker.submit(work, done, failed, name="test-payloader")

    def _saved_card(self) -> None:
        """Store the FTP details so the next launch connects on its own."""
        body = self._card("Saved PS4",
                          "Save these FTP details once - every later start connects by itself, "
                          "no typing and no FileZilla.")
        self._switch_row(body, "auto_connect", "Connect automatically on startup",
                         hint="uses the saved address, port and login below", default=True)
        self.saved_label = tk.Label(body, text="", bg=Theme.surface, fg=Theme.text_dim,
                                    font=Theme.font_ui, justify="left", anchor="w", wraplength=560)
        self.saved_label.pack(anchor="w", pady=(8, 0))
        self.recent_row = tk.Frame(body, bg=Theme.surface)
        self.recent_row.pack(anchor="w", pady=(8, 0))
        actions = tk.Frame(body, bg=Theme.surface)
        actions.pack(anchor="w", pady=(10, 0))
        CButton(actions, "Save settings", command=self.save, variant="primary", height=30).pack(side="left")
        CButton(actions, "Forget saved PS4", command=self.forget_saved, variant="danger",
                bg=Theme.surface, height=30).pack(side="left", padx=(8, 0))
        self.saved_hint = tk.Label(body, text="", bg=Theme.surface, fg=Theme.muted,
                                   font=Theme.font_small, justify="left", anchor="w", wraplength=560)
        self.saved_hint.pack(anchor="w", pady=(8, 0))
        self._recent_shown: List[str] = []

    def _refresh_saved(self) -> None:
        """Say what the next start will do, and offer previously used hosts."""
        config = self.app.config
        host = str(self.vars["host"].get() or "").strip()
        port = str(self.vars["port"].get() or "").strip() or str(DEFAULT_PORT)
        user = str(self.vars["user"].get() or "").strip() or "anonymous"
        auto = bool(self.vars["auto_connect"].get())
        if host and auto:
            self.saved_label.configure(
                text=f"✔  Next start: connects automatically to {host}:{port} as {user}.",
                fg=Theme.good)
        elif host:
            self.saved_label.configure(
                text=f"Address {host}:{port} is saved, but 'Connect automatically on startup' is off - "
                     "the app will start offline.", fg=Theme.warn)
        else:
            self.saved_label.configure(
                text="Nothing saved yet: fill in the address above and press Save settings once.",
                fg=Theme.warn)
        recent = [h for h in config.recent_hosts if h][:3]
        if recent != self._recent_shown:
            for child in self.recent_row.winfo_children():
                child.destroy()
            if recent:
                tk.Label(self.recent_row, text="Saved PS4s:", bg=Theme.surface, fg=Theme.text_dim,
                         font=Theme.font_small).pack(side="left")
                for candidate in recent:
                    CButton(self.recent_row, candidate, variant="ghost", bg=Theme.surface, height=26,
                            command=lambda c=candidate: self._use_saved_host(c)).pack(side="left",
                                                                                      padx=(6, 0))
            self._recent_shown = recent
        if str(self.vars["password"].get() or ""):
            self.saved_hint.configure(text="The password is stored as plain text in "
                                           "ps4_manager_config.json (anonymous logins need none).")
        else:
            self.saved_hint.configure(text="")

    def _use_saved_host(self, host: str) -> None:
        self.vars["host"].set(host)
        self.result.configure(text=f"{host} filled in - press Save settings to keep it.", fg=Theme.text_dim)

    def forget_saved(self) -> None:
        """Delete the stored FTP details so nothing is remembered."""
        config = self.app.config
        old_host = str(self.vars["host"].get() or "").strip()
        self.vars["host"].set("")
        self.vars["password"].set("")
        config.host = ""
        config.password = ""
        if old_host:
            config.recent_hosts = [h for h in config.recent_hosts if h != old_host]
        try:
            config.save()
        except RuntimeError as exc:
            self.result.configure(text=str(exc), fg=Theme.bad)
            return
        self.app.config = config
        self._refresh_saved()
        self.result.configure(text="Saved PS4 forgotten - the manager will ask for the address again.",
                              fg=Theme.text_dim)
        self.app.set_status("Saved PS4 forgotten.", "muted")

    def _behaviour_card(self) -> None:
        body = self._card("Behaviour")
        self._switch_row(body, "auto_apply", "Save changes automatically",
                         hint="upload plugins.ini right after each change", default=True)
        self._entry_row(body, "keep_backups", "Backups to keep", default="25", width=8)
        CButton(body, "Open backups folder", command=self.open_backups, bg=Theme.surface, height=30).pack(
            anchor="w", pady=(8, 0)
        )

    def _about_card(self) -> None:
        body = self._card("About")
        for text in (
            f"{APP_NAME} {APP_VERSION} — standard library only, no pip packages needed.",
            f"Config file:  {CONFIG_FILE}",
            f"Backups:      {BACKUP_DIR}",
            "GoldHEN reads plugins.ini at startup, so restart GoldHEN (or the game) after changes.",
        ):
            tk.Label(body, text=text, bg=Theme.surface, fg=Theme.text_dim, font=Theme.font_small,
                     justify="left", anchor="w", wraplength=560).pack(anchor="w", pady=1)

    # -- data -------------------------------------------------------------
    def load_from_config(self) -> None:
        config = self.app.config
        for key, var in self.vars.items():
            value = getattr(config, key, "")
            if isinstance(var, tk.BooleanVar):
                var.set(bool(value))
            else:
                var.set(str(value))
        self._refresh_saved()

    def _toggle_password(self) -> None:
        self.password_entry.configure(show="" if self.password_entry.cget("show") else "•")

    def collect(self) -> Config:
        config = self.app.config
        for key, var in self.vars.items():
            raw = var.get()
            if isinstance(var, tk.BooleanVar):
                setattr(config, key, bool(raw))
            elif key in ("port", "timeout", "keep_backups", "payloader_port"):
                try:
                    setattr(config, key, int(str(raw).strip() or 0))
                except ValueError:
                    self.app.log(f"Ignoring invalid value for {key}: {raw!r}")
            else:
                setattr(config, key, str(raw).strip())
        if config.port <= 0:
            config.port = DEFAULT_PORT
        if config.payloader_port <= 0:
            config.payloader_port = DEFAULT_PAYLOAD_PORT
        if config.timeout < 3:
            config.timeout = 5
        return config

    def save(self, quiet: bool = False) -> bool:
        config = self.collect()
        if not config.host:
            self.result.configure(text="Please enter your PS4's IP address first.", fg=Theme.warn)
            return False
        config.remember_host(config.host)   # feeds the "Saved PS4s" quick pick
        try:
            config.save()
        except RuntimeError as exc:
            self.result.configure(text=str(exc), fg=Theme.bad)
            return False
        self.app.config = config
        self._refresh_saved()
        if not quiet:
            if config.host and config.auto_connect:
                message = (f"Saved {config.host}:{config.port} - the manager will connect "
                           "automatically from now on.")
            else:
                message = f"Saved to {CONFIG_FILE}"
            self.result.configure(text=message, fg=Theme.good)
            self.app.set_status("Settings saved.", "good")
        return True

    def save_and_connect(self) -> None:
        if not self.save(quiet=True):
            return
        self.result.configure(text=f"Saved. Connecting to {self.app.config.host} ...", fg=Theme.text_dim)
        self.app.connect_async(load=True, announce=True)

    def test_connection(self) -> None:
        if not self.save(quiet=True):
            return
        config = self.app.config
        self.result.configure(text=f"Testing {config.host}:{config.port} ...", fg=Theme.text_dim)

        def work() -> str:
            # bg_log, not app.log: Tk is off limits in a worker thread
            ftp = config.make_ftp(self.app.bg_log)
            ftp.connect()
            try:
                lines = [f"✓ Connected to {config.host}:{config.port}."]
                exists, _isdir = ftp.exists(config.remote_ini)
                if exists:
                    doc = PluginsIni.parse(ftp.read_text(config.remote_ini))
                    enabled, total = doc.stats()
                    lines.append(f"✓ {config.remote_ini}: {total} plugin(s), {enabled} enabled, "
                                 f"{len(doc.sections())} section(s).")
                else:
                    lines.append(f"! {config.remote_ini} does not exist yet — it will be created when you save.")
                exists_dir, is_dir = ftp.exists(config.remote_plugins_dir)
                if exists_dir and is_dir:
                    files = [e for e in ftp.listdir(config.remote_plugins_dir) if not e.is_dir]
                    lines.append(f"✓ {config.remote_plugins_dir}: {len(files)} file(s).")
                else:
                    lines.append(f"! {config.remote_plugins_dir} was not found.")
                if config.payloader_enabled:
                    try:
                        summary = config.make_payloader(self.app.bg_log).probe()
                        lines.append(f"✓ Payload server {config.payloader_target()}: {summary} "
                                     "- it connects together with FTP.")
                    except PayloaderError as exc:
                        lines.append(f"! Payload server {config.payloader_target()}: "
                                     f"{' '.join(str(exc).split())}")
                return "\n".join(lines)
            finally:
                ftp.close()

        self.app.begin_busy("Testing connection ...")
        self.app.worker.submit(
            work,
            lambda text: (self.app.end_busy(), self.result.configure(text=text, fg=Theme.text_dim),
                          self.app.set_status("Connection test finished.", "good")),
            lambda exc: (self.app.end_busy(), self.result.configure(text=f"✗ {exc}", fg=Theme.bad),
                         self.app.set_status(f"Connection failed: {exc}", "bad")),
            name="test-connection",
        )

    def scan_network(self) -> None:
        self.result.configure(text="Scanning your network for a PS4 (looking for port "
                                   f"{self.app.config.port}) ...", fg=Theme.text_dim)
        app = self.app
        app.begin_busy("Scanning network ...")

        def work() -> List[str]:
            return scan_for_ftp(port=max(1, int(app.config.port)), timeout=0.6)

        def done(found: List[str]) -> None:
            app.end_busy()
            if not found:
                self.result.configure(text="No FTP server found on your local network. Is GoldHEN's FTP "
                                           "enabled and the console on the same WiFi?", fg=Theme.warn)
                return
            if len(found) == 1:
                self.vars["host"].set(found[0])
                self.result.configure(text=f"Found a device at {found[0]} — press 'Save & connect'.",
                                      fg=Theme.good)
                return
            self.result.configure(text="Found: " + ", ".join(found), fg=Theme.good)
            self.vars["host"].set(found[0])

        app.worker.submit(work, done, lambda exc: (app.end_busy(),
                                                   self.result.configure(text=str(exc), fg=Theme.bad)),
                          name="scan")

    def open_backups(self) -> None:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if platform.system() == "Windows":
                os.startfile(str(BACKUP_DIR))  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(BACKUP_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(BACKUP_DIR)])
        except Exception as exc:  # noqa: BLE001
            self.result.configure(text=f"Could not open {BACKUP_DIR}: {exc}", fg=Theme.warn)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class Ps4ManagerApp(tk.Tk):
    def __init__(self, config: Config, args: Optional[argparse.Namespace] = None) -> None:
        super().__init__()
        self.config = config
        self.args = args
        self.ftp: Optional[Ps4Ftp] = None
        # GoldHEN's payload server, when an address is saved for it (see Config)
        self.payloader: Optional[Payloader] = None
        self.ini = PluginsIni()
        self.ini_remote = True
        self.local_path: Optional[Path] = None
        self.dirty = False
        self.remote_entries: List[RemoteEntry] = []
        self.installed_titles: List[str] = []
        self.titles_available = False
        self.titles: Dict[str, str] = {}
        self._titles_resolved: set = set()
        self.last_remote_text: str = ""
        self._apply_job: Optional[str] = None

        self.title(APP_NAME)
        self.configure(bg=Theme.bg)
        init_fonts()
        self._init_ttk_style()
        self.option_add("*TCombobox*Listbox.background", Theme.surface2)
        self.option_add("*TCombobox*Listbox.foreground", Theme.text)
        self.option_add("*TCombobox*Listbox.selectBackground", Theme.accent)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.option_add("*TCombobox*Listbox.font", Theme.font_ui)

        geometry = config.window_geometry or "1080x700"
        self.geometry(geometry)
        config.window_geometry = geometry   # so a save from this session keeps it
        self.minsize(900, 580)
        self.worker = Worker(self)
        self.tool_tabs: List[Tuple[str, CButton]] = []
        self.tab_strips: List[tk.Frame] = []

        self.load_title_cache()
        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Control-s>", lambda _e: self.apply_async())
        self.bind("<Control-r>", lambda _e: self.reload_async())
        self.bind("<F5>", lambda _e: self.reload_async())

        self.after(120, self._startup)

    # -- layout -----------------------------------------------------------
    def _init_ttk_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Treeview", background=Theme.surface, fieldbackground=Theme.surface, foreground=Theme.text,
            rowheight=30, borderwidth=0, relief="flat", font=Theme.font_ui,
        )
        style.map("Treeview", background=[("selected", Theme.sel)], foreground=[("selected", Theme.text)])
        style.configure(
            "Treeview.Heading", background=Theme.surface, foreground=Theme.muted, relief="flat",
            font=Theme.font_small, padding=(8, 7), borderwidth=0,
        )
        style.map("Treeview.Heading", background=[("active", Theme.surface2)])
        style.configure(
            "Vertical.TScrollbar", background=Theme.surface2, troughcolor=Theme.bg,
            bordercolor=Theme.bg, arrowcolor=Theme.muted, relief="flat", borderwidth=0,
            lightcolor=Theme.surface2, darkcolor=Theme.surface2, width=11,
        )
        style.map("Vertical.TScrollbar", background=[("active", Theme.surface3)])
        style.configure("TCombobox", fieldbackground=Theme.surface2, background=Theme.surface2,
                        foreground=Theme.text, arrowcolor=Theme.text_dim, bordercolor=Theme.border,
                        lightcolor=Theme.surface2, darkcolor=Theme.surface2, borderwidth=1,
                        relief="flat", padding=4)
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", Theme.surface2), ("disabled", Theme.surface)],
            foreground=[("disabled", Theme.muted)],
            bordercolor=[("focus", Theme.accent)],
            arrowcolor=[("active", Theme.text)],
        )
        style.configure("PS4.Horizontal.TProgressbar", background=Theme.accent, troughcolor=Theme.surface2,
                        bordercolor=Theme.surface2, lightcolor=Theme.accent, darkcolor=Theme.accent,
                        borderwidth=0)
        style.configure("TSeparator", background=Theme.border)

    def _build_layout(self) -> None:
        header = tk.Frame(self, bg=Theme.surface, height=62)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        tk.Frame(self, bg=Theme.border, height=1).pack(fill="x", side="top")

        brand = tk.Frame(header, bg=Theme.surface)
        brand.pack(side="left", padx=20)
        logo = tk.Canvas(brand, width=26, height=26, bg=Theme.surface, highlightthickness=0, bd=0)
        logo.pack(side="left")
        draw_round_rect(logo, 1, 1, 25, 25, 8, fill=Theme.accent, outline=Theme.accent)
        logo.create_text(13, 13, text="PS", fill="#ffffff", font=(Theme.font_bold[0], 9, "bold"))
        titles = tk.Frame(brand, bg=Theme.surface)
        titles.pack(side="left", padx=(10, 0))
        tk.Label(titles, text=APP_NAME, bg=Theme.surface, fg=Theme.text, font=Theme.font_h2).pack(anchor="w")
        tk.Label(titles, text=APP_TAGLINE, bg=Theme.surface, fg=Theme.muted,
                 font=Theme.font_small).pack(anchor="w")

        right = tk.Frame(header, bg=Theme.surface)
        right.pack(side="right", padx=20)
        self.connect_button = CButton(right, "Connect", command=self.toggle_connection, variant="primary",
                                      bg=Theme.surface, width=110)
        self.connect_button.pack(side="right")
        self.pill = StatusPill(right, bg=Theme.surface)
        self.pill.pack(side="right", padx=(0, 14))
        # second pill: the payload server's state, so one Connect press reports both
        self.pay_pill = StatusPill(right, bg=Theme.surface)
        self.pay_pill.pack(side="right", padx=(0, 14))
        self.pay_pill.set("Payload off", Theme.muted)

        middle = tk.Frame(self, bg=Theme.bg)
        middle.pack(fill="both", expand=True)
        sidebar = tk.Frame(middle, bg=Theme.bg, width=196)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Frame(middle, bg=Theme.border, width=1).pack(side="left", fill="y")

        self.content = tk.Frame(middle, bg=Theme.bg)
        self.content.pack(side="left", fill="both", expand=True)

        self.nav_buttons: Dict[str, CButton] = {}
        for index, (key, label) in enumerate((("plugins", "Plugin tool"), ("pkgs", "PKG manager"),
                                              ("settings", "Settings"))):
            button = CButton(sidebar, "   " + label, command=lambda k=key: self.show_view(k),
                             variant="nav", bg=Theme.bg, height=38, width=180, anchor="w", padx=14)
            button.pack(padx=8, pady=(12 if index == 0 else 4, 0))
            self.nav_buttons[key] = button

        version = tk.Label(sidebar, text=f"v{APP_VERSION}\nno pip installs needed", bg=Theme.bg,
                           fg=Theme.muted, font=Theme.font_small, justify="left")
        version.pack(side="bottom", anchor="w", padx=16, pady=14)

        tk.Frame(self, bg=Theme.border, height=1).pack(fill="x", side="bottom")
        status = tk.Frame(self, bg=Theme.surface, height=34)
        status.pack(fill="x", side="bottom")
        status.pack_propagate(False)
        self.status_label = tk.Label(status, text="Ready.", bg=Theme.surface, fg=Theme.text_dim,
                                     font=Theme.font_small, anchor="w")
        self.status_label.pack(side="left", padx=16)
        self.progress = ttk.Progressbar(status, mode="determinate", length=160,
                                        style="PS4.Horizontal.TProgressbar")
        self.log_button = CButton(status, "Show log", command=self.toggle_log, variant="ghost",
                                 bg=Theme.surface, height=26, width=88)
        self.log_button.pack(side="right", padx=(0, 12))
        self.log_visible = False
        self.log_frame = tk.Frame(self, bg=Theme.bg)
        self.log_text = tk.Text(self.log_frame, height=8, bg=Theme.surface, fg=Theme.text_dim,
                                font=Theme.font_mono, relief="flat", wrap="none", bd=0,
                                highlightthickness=0, insertbackground=Theme.text)
        self.log_scroll = ttk.Scrollbar(self.log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=self.log_scroll.set, state="disabled")
        self.log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

        self.views: Dict[str, tk.Frame] = {
            "plugins": PluginsView(self, self.content),
            "files": FilesView(self, self.content),
            "pkgs": PkgManagerView(self, self.content),
            "settings": SettingsView(self, self.content),
        }
        for view in self.views.values():
            view.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.current_view = "plugins"
        self.show_view("plugins")
        self.update_dirty_label()

    def make_tool_tabs(self, parent, active: str) -> tk.Frame:
        """'Plugins | Plugin files' switcher - both inside the plugin tool.

        The PKG manager is *not* a tab: it has its own entry in the sidebar beside
        the plugin tool and the settings, where it is a full page of its own.
        """
        strip = tk.Frame(parent, bg=Theme.bg)
        self.tab_strips.append(strip)      # so the layout can be checked (see the tests)
        for key, label in (("plugins", "Plugins"), ("files", "Plugin files")):
            button = CButton(strip, label, command=lambda k=key: self.show_view(k),
                             variant="nav", bg=Theme.bg, height=30, width=112)
            button.pack(side="left", padx=(4, 0))
            button.set_active(key == active)
            self.tool_tabs.append((key, button))
        return strip

    def show_view(self, key: str) -> None:
        self.current_view = key
        for name, button in self.nav_buttons.items():
            # the file browser is a tab of the Plugin tool, so keep that nav
            # button lit while it is open (the PKG manager has its own entry)
            inside_plugin_tool = key == "files"
            button.set_active(name == key or (name == "plugins" and inside_plugin_tool))
        for tab_key, tab_button in self.tool_tabs:
            tab_button.set_active(tab_key == key)
        view = self.views.get(key)
        if view is None:
            return
        view.lift()
        if key == "files":
            files_view = self.views["files"]
            if isinstance(files_view, FilesView) and self.ftp is not None and not files_view.browser.entries:
                files_view.jump()
        if key == "settings" and isinstance(view, SettingsView):
            view.load_from_config()
        if key == "pkgs" and isinstance(view, PkgManagerView):
            view.on_shown()

    # -- status / logging -------------------------------------------------
    def set_status(self, message: str, kind: str = "info") -> None:
        colors = {"info": Theme.text_dim, "good": Theme.good, "warn": Theme.warn,
                  "bad": Theme.bad, "muted": Theme.muted}
        self.status_label.configure(text=message, fg=colors.get(kind, Theme.text_dim))
        if kind in ("bad", "warn"):
            self.log(message)

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}\n"
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line)
            if int(self.log_text.index("end-1c").split(".")[0]) > 600:
                self.log_text.delete("1.0", "100.0")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except Exception:
            pass

    def bg_log(self, message: str) -> None:
        """Log from a worker thread: App.log() touches Tk, which only the UI thread may do."""
        self.worker.post(self.log, message)

    def toggle_log(self) -> None:
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.log_frame.pack(fill="x", side="bottom")
            self.log_button.set_text("Hide log")
        else:
            self.log_frame.pack_forget()
            self.log_button.set_text("Show log")

    def begin_busy(self, message: str = "Working ...") -> None:
        self.worker.busy += 1
        self.set_status(message, "muted")
        self.progress.pack(side="right", padx=(0, 12), before=self.log_button)
        if self.progress["mode"] != "indeterminate":
            self.progress.configure(mode="indeterminate", maximum=100)
            self.progress.start(18)

    def end_busy(self) -> None:
        self.worker.busy = max(0, self.worker.busy - 1)
        if self.worker.busy == 0:
            self.progress.stop()
            self.progress.pack_forget()

    def on_progress(self, payload: Tuple[int, int]) -> None:
        done, total = payload
        if total:
            self.progress.configure(mode="determinate", maximum=100)
            self.progress["value"] = min(100, done * 100 / max(1, total))
            self.set_status(f"Transferring … {fmt_size(done)} / {fmt_size(total)}", "muted")
        else:
            self.set_status(f"Transferring … {fmt_size(done)}", "muted")

    # -- titles -----------------------------------------------------------
    def load_title_cache(self) -> None:
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            self.titles = {str(k): str(v) for k, v in (data.get("names") or {}).items()}
        except Exception:
            self.titles = {}

    def save_title_cache(self) -> None:
        try:
            CACHE_FILE.write_text(json.dumps({"names": self.titles, "saved": timestamp()}, indent=2),
                                  encoding="utf-8")
        except OSError:
            pass

    def section_label(self, name: str) -> str:
        if name.strip().lower() == DEFAULT_SECTION:
            return "Default — every game"
        pretty = self.titles.get(name.upper())
        return f"{name} — {pretty}" if pretty else name

    def entry_missing(self, entry: PluginEntry) -> bool:
        path = entry.path
        directory = parent_remote(path)
        plugins_dir = (self.config.remote_plugins_dir or DEFAULT_PLUGINS_DIR).rstrip("/")
        if directory.rstrip("/") != plugins_dir:
            return False
        if not self.remote_entries:
            return False
        names = {e.name for e in self.remote_entries}
        return path.split("/")[-1] not in names

    def entry_exists_remote(self, path: str) -> bool:
        return any(e.path == path or e.name == path.split("/")[-1] for e in self.remote_entries)

    def ini_path_label(self) -> str:
        if not self.ini_remote and self.local_path:
            return f"Local file: {self.local_path}"
        target = f"{self.config.host or 'PS4'}:{self.config.port}{self.config.remote_ini}"
        return f"FTP: {target}"

    # -- connection -------------------------------------------------------
    def _startup(self) -> None:
        if self.args is not None and getattr(self.args, "local", None):
            self.load_local_path(Path(self.args.local))
            return
        if self.config.host and self.config.auto_connect:
            self.connect_async(load=True, announce=False)
        else:
            self.set_status("Enter your PS4's address in Settings and press Save settings - "
                            "the manager then connects by itself at every start.", "warn")
            self.show_view("settings")

    def toggle_connection(self) -> None:
        if self.ftp is not None:
            self.disconnect()
            return
        if not self.config.host:
            # the header button should pick up whatever is typed in Settings
            view = self.views.get("settings")
            if isinstance(view, SettingsView):
                view.save(quiet=True)
        if not self.config.host:
            self.set_status("No PS4 address yet — fill it in and press Save settings.", "warn")
            self.show_view("settings")
        else:
            self.connect_async(load=True, announce=True)

    def disconnect(self) -> None:
        if self.ftp is not None:
            self.ftp.close()
        if self.payloader is not None:
            self.payloader.close()
        self.payloader = None
        self.ftp = None
        self.remote_entries = []
        self.ini_remote = False
        self.pill.set("Offline", Theme.muted)
        self.connect_button.set_text("Connect")
        self.connect_button.set_variant("primary")
        self.refresh_payloader_pill()
        self.set_status("Disconnected.", "muted")
        self.log("Disconnected from the PS4.")
        self.views["plugins"].rebuild()

    def refresh_payloader_pill(self) -> None:
        """One pill says it all: payload server off, ready, or not answering."""
        if not self.config.payloader_enabled:
            self.pay_pill.set("Payload off", Theme.muted)
        elif self.payloader is not None and self.payloader.connected:
            self.pay_pill.set("Payload ready", Theme.good)
        elif self.ftp is not None:
            self.pay_pill.set("Payload no reply", Theme.bad)
        else:
            self.pay_pill.set("Payload off", Theme.muted)

    def connect_async(self, load: bool = True, announce: bool = True) -> None:
        if not self.config.host:
            self.show_view("settings")
            return
        config = self.config
        self.pill.set("Connecting ...", Theme.warn)
        if config.payloader_enabled:
            self.pay_pill.set("Payload checking", Theme.warn)
        self.connect_button.set_enabled(False)
        self.begin_busy(f"Connecting to {config.host}:{config.port} ...")
        self.log(f"Connecting to {config.host}:{config.port} as {config.user}")
        if config.payloader_enabled:
            self.log(f"Also connecting to the payload server at {config.payloader_target()}")

        def work() -> Tuple[Ps4Ftp, Optional[Payloader], str]:
            # bg_log, not self.log: Tk may only be touched from the UI thread
            ftp = config.make_ftp(self.bg_log)
            ftp.connect()
            payloader: Optional[Payloader] = None
            payload_error = ""
            if config.payloader_enabled:
                # a payload server that stays silent must never cost the FTP session
                try:
                    payloader = config.make_payloader(self.bg_log)
                    payloader.connect()
                except PayloaderError as exc:
                    payloader = None
                    payload_error = " ".join(str(exc).split())
            return ftp, payloader, payload_error

        def done(result: Tuple[Ps4Ftp, Optional[Payloader], str]) -> None:
            ftp, payloader, payload_error = result
            self.end_busy()
            self.connect_button.set_enabled(True)
            self.ftp = ftp
            self.payloader = payloader
            self.pill.set(f"Connected · {config.host}:{config.port}", Theme.good)
            self.connect_button.set_text("Disconnect")
            self.connect_button.set_variant("secondary")
            self.log(f"Connected to {config.host}:{config.port}")
            if payloader is not None:
                self.log(f"Payload server ready: {payloader.banner_summary()}")
                self.set_status(f"Connected to {config.host} - payload server ready "
                                f"({payloader.banner_summary()}).", "good")
            elif payload_error:
                self.set_status(f"Connected to {config.host} - FTP only, {payload_error}", "warn")
            else:
                self.set_status(f"Connected to {config.host}.", "good")
            self.refresh_payloader_pill()
            if load:
                self.load_async()

        def failed(exc: BaseException) -> None:
            self.end_busy()
            self.connect_button.set_enabled(True)
            self.ftp = None
            self.payloader = None
            self.pill.set("Connection failed", Theme.bad)
            self.connect_button.set_text("Connect")
            self.refresh_payloader_pill()
            self.set_status(str(exc).splitlines()[0] if str(exc) else "Connection failed.", "bad")
            self.log(f"Connection failed: {exc}")
            if announce:
                messagebox.showerror("Connection failed", str(exc), parent=self)

        self.worker.submit(work, done, failed, name="connect")

    # -- remote helpers ---------------------------------------------------
    def require_ftp(self) -> Ps4Ftp:
        if self.ftp is None:
            raise FtpError("Not connected to the PS4.")
        return self.ftp

    def list_remote(self, path: str, on_done: Callable[[List[RemoteEntry]], None],
                    on_error: Callable[[BaseException], None]) -> None:
        if self.ftp is None:
            on_error(FtpError("Not connected to the PS4."))
            return
        ftp = self.ftp

        def work() -> List[RemoteEntry]:
            return ftp.listdir(path)

        self.begin_busy(f"Listing {path} ...")
        self.worker.submit(work, lambda entries: (self.end_busy(), on_done(entries)),
                           lambda exc: (self.end_busy(), on_error(exc)), name="list")

    def reload_async(self) -> None:
        if self.ftp is None and not self.local_path:
            self.set_status("Connect to your PS4 first.", "warn")
            return
        if self.dirty and not messagebox.askyesno(
            "Reload", "You have unsaved changes. Reload and throw them away?", parent=self
        ):
            return
        if self.ftp is None:
            self.load_local_path(self.local_path)  # type: ignore[arg-type]
        else:
            self.load_async()

    def load_async(self) -> None:
        if self.ftp is None:
            return
        ftp = self.ftp
        path = self.config.remote_ini
        self.log(f"Reading {path}")
        self.begin_busy(f"Reading {path} ...")

        def work() -> Dict[str, Any]:
            exists, _isdir = ftp.exists(path)
            text = ftp.read_text(path) if exists else ""
            entries: List[RemoteEntry] = []
            try:
                entries = ftp.listdir(self.config.remote_plugins_dir)
            except FtpError as exc:
                self.worker.post(self.log, f"Could not list plugins folder: {exc}")
            app_titles: List[str] = []
            listed_any = False
            for directory in APPMETA_DIRS:
                try:
                    found = ftp.listdir(directory)
                except FtpError:
                    continue
                listed_any = True
                app_titles.extend(e.name for e in found if e.is_dir and is_title_id(e.name))
            if not listed_any:
                self.worker.post(self.log, "Could not read the list of installed games.")
            return {"exists": exists, "text": text, "entries": entries,
                    "titles": sorted(set(app_titles)), "listed": listed_any}

        def done(payload: Dict[str, Any]) -> None:
            self.end_busy()
            self.ini_remote = True
            self.local_path = None
            self.last_remote_text = payload["text"]
            if payload["exists"]:
                self.ini = PluginsIni.parse(payload["text"])
                self.set_status(f"Loaded {self.config.remote_ini}.", "good")
            else:
                self.ini = PluginsIni.parse("")
                self.set_status(
                    f"{self.config.remote_ini} was not found — a fresh one will be created when you save.",
                    "warn",
                )
            self.remote_entries = payload["entries"]
            self.installed_titles = payload["titles"]
            self.titles_available = bool(payload["listed"])
            self.dirty = False
            self.views["plugins"].rebuild()
            self.log(f"Loaded {len(self.ini.sections())} section(s).")
            self.resolve_titles_async(titles=self.ini.titles() + self.installed_titles)

        self.worker.submit(work, done, lambda exc: (self.end_busy(), self.set_status(str(exc), "bad"),
                                                    self.log(f"Load failed: {exc}")), name="load")

    def resolve_titles_async(self, titles: Optional[List[str]] = None) -> None:
        """Fetch real game names from /user/appmeta (in the background, cached)."""
        if self.ftp is None:
            return
        pending = [t for t in (titles or self.installed_titles) if t not in self._titles_resolved]
        pending = sorted(set(pending))
        if not pending:
            return
        self._titles_resolved.update(pending)
        ftp = self.ftp

        def work() -> Dict[str, str]:
            found: Dict[str, str] = {}
            for title in pending:
                name = ""
                for candidate in (
                    f"/system_data/priv/appmeta/{title}/param.sfo",
                    f"/user/appmeta/{title}/param.sfo",
                    f"/user/appmeta/{title}/param.json",
                    f"/system_data/priv/appmeta/{title}/param.json",
                ):
                    try:
                        if candidate.endswith(".json"):
                            name = title_name_from_json(ftp.read_text(candidate))
                        else:
                            info = parse_sfo(ftp.read_bytes(candidate))
                            name = str(info.get("TITLE") or "").strip()
                    except FtpError:
                        name = ""
                    if name:
                        break
                found[title] = name
            return found

        def done(found: Dict[str, str]) -> None:
            changed = False
            for title, name in found.items():
                if name and self.titles.get(title) != name:
                    self.titles[title] = name
                    changed = True
            if changed:
                self.save_title_cache()
                self.views["plugins"].rebuild()

        self.worker.submit(work, done, lambda exc: self.log(f"Title lookup failed: {exc}"), name="titles")

    # -- local files ------------------------------------------------------
    def open_local_async(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="Open a plugins.ini",
            filetypes=[("plugins.ini", "*.ini"), ("Text", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.load_local_path(Path(path))

    def load_local_path(self, path: Path) -> None:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.set_status(f"Could not read {path}: {exc}", "bad")
            return
        self.ini = PluginsIni.parse(text)
        self.local_path = Path(path)
        self.ini_remote = False
        self.dirty = False
        self.views["plugins"].rebuild()
        self.set_status(f"Opened {path} (offline editing).", "good")
        self.log(f"Opened local file {path}")

    # -- mutations --------------------------------------------------------
    def mark_dirty(self) -> None:
        self.dirty = True
        self.update_dirty_label()
        if self.config.auto_apply and self.ftp is not None and self.ini_remote:
            if self._apply_job:
                try:
                    self.after_cancel(self._apply_job)
                except Exception:
                    pass
            self._apply_job = self.after(700, self.apply_async)

    def update_dirty_label(self) -> None:
        view = self.views.get("plugins") if hasattr(self, "views") else None
        if not isinstance(view, PluginsView):
            return
        if self.dirty:
            view.dirty_label.configure(text="● Unsaved changes", fg=Theme.warn)
        elif self.ftp is not None and self.ini_remote:
            view.dirty_label.configure(text="✓ Synced with PS4", fg=Theme.good)
        elif not self.ini_remote and self.local_path:
            view.dirty_label.configure(text="Editing a local file", fg=Theme.muted)
        else:
            view.dirty_label.configure(text="", fg=Theme.muted)
        view.subtitle.configure(text=self.ini_path_label())

    def open_add_dialog(self) -> None:
        dialog = AddPluginDialog(self, remote_files=self.remote_entries)
        result = dialog.show()
        if not result:
            return
        section_name = result["section"]
        path = result["path"]
        section = self.ini.get_or_create_section(section_name)
        if section.find(path) is not None:
            messagebox.showinfo("Already there",
                                f"{path} is already listed in [{section_name}].", parent=self)
            return
        self.ini.add(section_name, path, enabled=bool(result["enabled"]))
        self.views["plugins"].rebuild()
        self.mark_dirty()
        self.set_status(f"Added {path.split('/')[-1]} to [{section_name}].", "good")
        self.log(f"Added {path} to [{section_name}] (enabled={result['enabled']})")

    def apply_async(self) -> None:
        self._apply_job = None
        if self.ftp is None:
            if self.local_path:
                target = filedialog.asksaveasfilename(parent=self, initialfile=self.local_path.name,
                                                      defaultextension=".ini",
                                                      filetypes=[("plugins.ini", "*.ini")])
                if target:
                    Path(target).write_text(self.ini.dumps(), encoding="utf-8")
                    self.dirty = False
                    self.update_dirty_label()
                    self.set_status(f"Saved to {target}", "good")
                return
            self.set_status("Connect to your PS4 to save, or use 'Save copy ...'.", "warn")
            return
        self.save_to_ps4()

    def save_to_ps4(self) -> None:
        text = self.ini.dumps()
        if self.last_remote_text:
            write_backup(self.last_remote_text, "-before")
        backup = write_backup(text)
        self.upload_ini_text(text, note=f"saved to {self.config.remote_ini}")
        if backup:
            prune_backups(self.config.keep_backups)

    def upload_ini_text(self, text: str, note: str = "saved", on_done: Optional[Callable[[], None]] = None) -> None:
        if self.ftp is None:
            self.set_status("Not connected to the PS4.", "warn")
            return
        ftp = self.ftp
        path = self.config.remote_ini
        self.begin_busy(f"Uploading {path} ...")
        self.log(f"Uploading {len(text)} bytes to {path}")

        def work() -> None:
            directory = parent_remote(path)
            exists, is_dir = ftp.exists(directory)
            if not is_dir:
                ftp.ensure_dir(directory)
            ftp.write_text(path, text, progress=self.worker.progress(self.on_progress))

        def done(_result: Any) -> None:
            self.end_busy()
            self.last_remote_text = text
            self.dirty = False
            self.update_dirty_label()
            self.ini_remote = True
            self.set_status(f"Saved {path} ({note}).", "good")
            self.log(f"Saved {path}: {note}")
            if on_done:
                on_done()

        def failed(exc: BaseException) -> None:
            self.end_busy()
            self.dirty = True
            self.update_dirty_label()
            self.set_status(f"Save failed: {exc}", "bad")
            self.log(f"Save failed: {exc}")

        self.worker.submit(work, done, failed, name="save-ini")

    def upload_files(self, paths: List[Path], remote_dir: str) -> None:
        if not paths:
            return
        ftp = self.require_ftp()
        app = self
        self.begin_busy(f"Uploading {len(paths)} file(s) ...")

        def work() -> List[str]:
            ftp.ensure_dir(remote_dir)
            uploaded: List[str] = []
            for path in paths:
                remote = join_remote(remote_dir, path.name)
                ftp.upload(path, remote, progress=app.worker.progress(app.on_progress))
                uploaded.append(remote)
            return uploaded

        def finished(remote_paths: List[str]) -> None:
            app.end_busy()
            app.set_status(f"Uploaded {len(remote_paths)} file(s).", "good")
            app.log("Uploaded: " + ", ".join(remote_paths))
            files_view = app.views.get("files")
            if isinstance(files_view, FilesView):
                files_view.browser.go(remote_dir)
            app.refresh_remote_entries()

        def failed(exc: BaseException) -> None:
            app.end_busy()
            app.set_status(f"Upload failed: {exc}", "bad")

        self.worker.submit(work, finished, failed, name="upload")

    def download_remote_file(self, remote_path: str) -> None:
        target = filedialog.asksaveasfilename(parent=self, initialfile=Path(remote_path).name,
                                              title="Save file to your PC")
        if not target:
            return
        ftp = self.require_ftp()
        app = self
        self.begin_busy(f"Downloading {Path(remote_path).name} ...")

        def work() -> Path:
            destination = Path(target)
            ftp.download(remote_path, destination, progress=app.worker.progress(app.on_progress))
            return destination

        def done(destination: Path) -> None:
            app.end_busy()
            app.set_status(f"Downloaded to {destination}", "good")
            app.log(f"Downloaded {remote_path} -> {destination}")

        def failed(exc: BaseException) -> None:
            app.end_busy()
            app.set_status(f"Download failed: {exc}", "bad")

        self.worker.submit(work, done, failed, name="download")

    def delete_remote(self, remote_path: str, refresh: Optional[Callable[[], None]] = None) -> None:
        ftp = self.require_ftp()
        app = self
        self.begin_busy(f"Deleting {remote_path} ...")

        def work() -> None:
            ftp.delete(remote_path)

        def done(_result: Any) -> None:
            app.end_busy()
            app.set_status(f"Deleted {remote_path}", "good")
            app.log(f"Deleted {remote_path}")
            if refresh:
                refresh()
            app.refresh_remote_entries()

        def failed(exc: BaseException) -> None:
            app.end_busy()
            app.set_status(f"Delete failed: {exc}", "bad")

        self.worker.submit(work, done, failed, name="delete")

    def refresh_remote_entries(self) -> None:
        if self.ftp is None:
            return
        ftp = self.ftp
        directory = self.config.remote_plugins_dir

        def work() -> List[RemoteEntry]:
            return ftp.listdir(directory)

        def done(entries: List[RemoteEntry]) -> None:
            self.remote_entries = entries
            self.views["plugins"].rebuild()

        self.worker.submit(work, done, lambda exc: self.log(f"Refresh failed: {exc}"), name="refresh")

    # -- window -----------------------------------------------------------
    def on_close(self) -> None:
        # Stop serving packages and stop polling the console before the widgets go
        pkg_view = self.views.get("pkgs")
        if isinstance(pkg_view, PkgManagerView):
            pkg_view.shutdown()
        # Remember the FTP details even if Save was never pressed: the whole point
        # of the "Saved PS4" card is never having to type the address twice.
        view = self.views.get("settings")
        if isinstance(view, SettingsView):
            try:
                view.save(quiet=True)
            except Exception:
                pass
        try:
            self.config.window_geometry = self.geometry()
            self.config.save()
        except Exception:
            pass
        if self.dirty and self.ini_remote and self.ftp is not None:
            if messagebox.askyesno("Unsaved changes", "Save your plugin changes to the PS4 before quitting?",
                                   parent=self):
                self.save_to_ps4()
        if self.ftp is not None:
            self.ftp.close()
        self.worker.stop()
        self.destroy()


# ---------------------------------------------------------------------------
# Self test (headless-ish smoke test, used by the test suite / CI)
# ---------------------------------------------------------------------------


SAMPLE_INI = """\
[default]
/data/GoldHEN/plugins/example_plugin.prx=false

[CUSA12345]
/data/GoldHEN/plugins/example_game_plugin.prx=true
/data/GoldHEN/plugins/second_example.sprx=true
/data/GoldHEN/plugins/third_example.prx=false
"""


def selftest() -> int:
    """Build every view/dialog with sample data and exit - a smoke test."""
    if not ensure_tkinter(auto_install=False):
        print("selftest: tkinter unavailable")
        return 1
    config = Config(host="127.0.0.1", auto_connect=False)
    app = Ps4ManagerApp(config)
    app.update()
    app.ini = PluginsIni.parse(SAMPLE_INI)
    app.ini_remote = False
    app.titles = {"CUSA12345": "Example Game"}
    app.installed_titles = ["CUSA12345", "CUSA12346"]
    app.views["plugins"].rebuild()
    for key in ("plugins", "files", "settings"):
        app.show_view(key)
        app.update()
    # dialogs
    for factory in (
        lambda: AddPluginDialog(app, remote_files=[
            RemoteEntry("example_plugin.prx", "/data/GoldHEN/plugins/example_plugin.prx", False, 4096),
        ]),
        lambda: TitlePickerDialog(app),
        lambda: RemoteBrowserDialog(app, start="/data/GoldHEN", mode="file"),
        lambda: BackupsDialog(app),
        lambda: _TextPrompt(app, "New folder", "Folder name"),
    ):
        dialog = factory()
        dialog.update()
        dialog.update_idletasks()
        if isinstance(dialog, AddPluginDialog):
            dialog.var_game.set(True)
            dialog._set_scope("game")
            dialog.var_title.set("CUSA12345")
            dialog._validate_title()
            dialog.update()
        dialog.close()
        app.update()
    # toggle + add + remove through the UI layer
    view = app.views["plugins"]
    assert isinstance(view, PluginsView)
    first = next(iid for iid in view._rows)
    view.toggle_row(first)
    app.update()
    app.ini.add("CUSA12346", "/data/GoldHEN/plugins/selftest_plugin.prx", enabled=True)
    view.rebuild()
    view.set_all(False)
    view.set_all(True)
    view.tree.selection_set(list(view._rows)[0])
    view.toggle_selected()
    app.update()
    text = app.ini.dumps()
    assert "[default]" in text and "CUSA12346" in text, text
    app.on_progress((512, 1024))
    app.set_status("selftest ok", "good")
    app.log("selftest finished")
    app.update()
    print("selftest: OK")
    print(text)
    app.destroy()
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} - {APP_TAGLINE}")
    parser.add_argument("--host", help="PS4 IP address (overrides the saved config)")
    parser.add_argument("--port", type=int, help="FTP port (default 2121)")
    parser.add_argument("--ini", help="path of plugins.ini on the PS4")
    parser.add_argument("--local", help="open a local plugins.ini and edit it offline")
    parser.add_argument("--no-connect", action="store_true", help="start without connecting")
    parser.add_argument("--no-install", action="store_true",
                        help="never try to install missing dependencies automatically")
    parser.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    return parser.parse_args(argv)


def check_display() -> Optional[str]:
    if platform.system() in ("Linux", "FreeBSD") and not os.environ.get("DISPLAY") \
            and not os.environ.get("WAYLAND_DISPLAY"):
        return ("No graphical display found (DISPLAY is not set).\n"
                "Run this on the machine's desktop session, or forward X11 "
                "(ssh -X user@host) before starting the app.")
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    if args.selftest:
        return selftest()

    if not ensure_tkinter(auto_install=not args.no_install):
        return 1

    display_error = check_display()
    if display_error:
        print(f"[error] {display_error}")
        return 1

    config = Config.load()
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port
    if args.ini:
        config.remote_ini = args.ini
    if args.no_connect:
        config.auto_connect = False
    if config.host:
        config.remember_host(config.host)

    try:
        app = Ps4ManagerApp(config, args)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] Could not start the GUI: {exc}")
        traceback.print_exc()
        return 1

    try:
        app.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            config.save()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
