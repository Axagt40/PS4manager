"""PKG Manager -- install, remove and move .pkg files on a GoldHEN PS4.

Three things live here, none of which need a pip install:

* ``RemoteInstaller``  - the client for the console-side service that actually
  installs packages.  Two exist in the scene and share one port, 12800:

    - **Remote Package Installer** (flatz) - a small homebrew app you install
      on the console once and leave running.  Its HTTP API is documented by the
      author, and this client speaks it: ``/api/install`` with
      ``{"type": "direct", "packages": [url, ...]}`` (also supports a merged
      single file and split packages), ``/api/is_exists``,
      ``/api/uninstall_game`` / ``_patch`` / ``_ac`` / ``_theme``,
      ``/api/get_task_progress`` and the task control calls.
    - the installer payload some PC tools push through GoldHEN's BinLoader.
      Same port, same three install/uninstall endpoints - but this app does not
      ship a third-party payload, so the app tells you to use the first one.

  The console's downloader (``bgft``) fetches the package from *us*: it does a
  plain HTTP GET, plus a ranged GET to read the package header before the
  download starts.  That is why the file goes over HTTP and not over FTP -
  and why :class:`LocalPkgServer` must answer Range requests.

* ``LocalPkgServer``   - serves the chosen ``.pkg`` (or the pieces of one) to
  the console, on the LAN interface that can reach it, behind a random path and
  restricted to the console's address.  Files are exposed under an opaque name
  because a URL that needs escaping breaks the manifest the console builds.

* small helpers        - split-package detection, ``.json`` manifest detection,
  title ids out of file names.

Nothing in here touches tkinter, so it is all unit-testable.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import re
import secrets
import socket
import socketserver
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RPI_PORT = 12800
DEFAULT_PKG_DIR = "/data/pkg"
DEFAULT_SERVE_PORT = 9898
# The console-side installer this app talks to (documented API, and by far the
# fastest of the two options - see PKG_SETUP_HELP in ps4_manager.py).
# flatz's GitHub holds the source only; the installable build of the app lives
# in the homebrew stores under title id FLTZ00003.
RPI_STORE_URL = "https://pkg-zone.com/details/FLTZ00003"
RPI_SOURCE_URL = "https://github.com/flatz/ps4_remote_pkg_installer"

# The console writes hex literals (0x...) into its JSON replies, which is not
# valid JSON - turn them into plain numbers before parsing.
_HEX_LITERAL = re.compile(r"\b0x[0-9a-fA-F]+\b")
_SPLIT_PART = re.compile(r"^(?P<base>.+?)_(?P<part>\d+)\.pkg$", re.IGNORECASE)
# The trailing guard is "not another digit" rather than a word boundary: in a
# real file name the id is followed by "_00", and "_" is a word character.
_TITLE_ID = re.compile(r"\b([A-Z]{4}\d{5})(?!\d)")


class InstallerError(RuntimeError):
    """The console answered, but refused / was unhappy with the request."""


class InstallerUnavailable(InstallerError):
    """Nothing is listening on the installer port (the one-time setup is missing)."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def local_ip_for(host: str) -> str:
    """The local address the console would reach us on ('' when offline)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host or "8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def looks_like_manifest(url: str) -> bool:
    """A CDN/reference package: RPI downloads the JSON itself and follows it."""
    return urllib.parse.urlparse(url.strip()).path.lower().endswith(".json")


def title_id_from_name(name: str) -> Optional[str]:
    """Pull a PS4 title id (CUSA12345, and homebrew ids like ABCD00001) out of a name."""
    match = _TITLE_ID.search((name or "").upper())
    return match.group(1) if match else None


def split_package_parts(path: Path) -> List[Path]:
    """All pieces of a split package, in order.

    A merged upload is one file; a split one is ``name_0.pkg``, ``name_1.pkg``
    ... and the installer needs every piece, in order, in one request.
    """
    path = Path(path)
    match = _SPLIT_PART.match(path.name)
    if not match or not path.is_file():
        return [path]
    base, folder = match.group("base"), path.parent
    parts: List[Tuple[int, Path]] = []
    try:
        candidates = list(folder.iterdir())
    except OSError:
        return [path]
    for candidate in candidates:
        found = _SPLIT_PART.match(candidate.name)
        if found and found.group("base").lower() == base.lower() and candidate.is_file():
            parts.append((int(found.group("part")), candidate))
    if not parts:
        return [path]
    parts.sort(key=lambda item: item[0])
    first = parts[0][0]
    if [index for index, _ in parts] != list(range(first, first + len(parts))):
        # a hole in the sequence - only send the file the user actually asked for
        return [path]
    return [item for _, item in parts]


def _decode_reply(body: str) -> dict:
    """The console's JSON has hex literals and is sometimes not valid JSON."""
    text = _HEX_LITERAL.sub(lambda match: str(int(match.group(0), 16)), body or "")
    try:
        data = json.loads(text)
    except ValueError:
        raise InstallerError(f"The PS4 sent something that is not JSON: {body[:160]!r}")
    if not isinstance(data, dict):
        raise InstallerError(f"Unexpected reply from the PS4: {body[:160]!r}")
    return data


# ---------------------------------------------------------------------------
# Remote installer client
# ---------------------------------------------------------------------------


@dataclass
class InstallTask:
    task_id: int
    title: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class TaskProgress:
    """One reading of an install task.

    The console reports three phases: ``preparing`` (it reads the package
    header), ``downloading`` (it pulls our bytes) and ``installing`` (local
    copy).  ``percent`` is the phase we are in, ``overall`` blends them.
    """

    state: str = "unknown"
    percent: float = 0.0
    transferred: int = 0
    total: int = 0
    detail: str = ""
    error: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.state == "done"

    @property
    def failed(self) -> bool:
        return self.error != 0 or self.state == "error"


class RemoteInstaller:
    """Client for the console-side installer service on port 12800."""

    def __init__(self, host: str, port: int = RPI_PORT, timeout: float = 10.0,
                 logger: Optional[Callable[[str], None]] = None) -> None:
        self.host = (host or "").strip()
        self.port = int(port)
        self.timeout = float(timeout)
        self.logger = logger

    # -- plumbing ---------------------------------------------------------
    def _log(self, message: str) -> None:
        if self.logger:
            try:
                self.logger(message)
            except Exception:
                pass

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _post(self, path: str, payload: Optional[dict] = None) -> dict:
        url = f"{self.url}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method="POST" if data is not None else "GET",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:  # the service is there and said no
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:160]
            except Exception:
                pass
            raise InstallerError(f"{path}: PS4 answered HTTP {exc.code} {detail}".strip()) from exc
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise InstallerUnavailable(
                f"No installer service on {self.host}:{self.port} "
                f"({type(exc).__name__}). Remote Package Installer is not running."
            ) from exc
        reply = _decode_reply(body)
        self._log(f"{path} -> {json.dumps(reply)[:200]}")
        return reply

    # -- capability -------------------------------------------------------
    def probe(self) -> bool:
        """True when something that speaks the installer API is listening.

        Uses ``is_exists`` because it is read-only and cheap - a title id that
        can never exist, so the answer is always ``false``.
        """
        try:
            reply = self._post("/api/is_exists", {"title_id": "NONE00000"})
        except InstallerError:
            return False
        return str(reply.get("status", "")).lower() == "success"

    # -- install ----------------------------------------------------------
    def install_direct(self, urls: Sequence[str]) -> InstallTask:
        urls = [u for u in urls if u]
        if not urls:
            raise InstallerError("No package URL to install.")
        reply = self._post("/api/install", {"type": "direct", "packages": list(urls)})
        return self._task_from(reply)

    def install_manifest(self, manifest_url: str) -> InstallTask:
        if not manifest_url:
            raise InstallerError("No manifest URL to install.")
        reply = self._post("/api/install", {"type": "ref_pkg_url", "url": manifest_url})
        return self._task_from(reply)

    @staticmethod
    def _task_from(reply: dict) -> InstallTask:
        status = str(reply.get("status", "")).lower()
        task_id = reply.get("task_id")
        if status != "success" or task_id is None:
            raise InstallerError(
                "The PS4 refused the install request: "
                f"{reply.get('error', reply.get('message', json.dumps(reply)[:160]))}"
            )
        return InstallTask(task_id=int(task_id), title=str(reply.get("title", "")), raw=reply)

    def progress(self, task_id: int) -> TaskProgress:
        """Read one task.

        Three phases, in order: ``preparing`` (the console reads the package
        header off our server), ``downloading`` (``transferred_total`` climbs
        towards ``length_total``) and ``installing`` (``local_copy_percent``,
        what the PS4 shows as "copying").  Any of the counters can be missing,
        so decide from whatever is there.
        """
        reply = self._post("/api/get_task_progress", {"task_id": int(task_id)})
        error = int(reply.get("error", 0) or 0)
        preparing = float(reply.get("preparing_percent", 0) or 0)
        total = int(reply.get("length_total", 0) or 0)
        transferred = int(reply.get("transferred_total", 0) or 0)
        copying = float(reply.get("local_copy_percent", 0) or 0)
        download_pct = (transferred * 100.0 / total) if total else 0.0
        got_everything = bool(total) and transferred >= total
        if error:
            state, percent = "error", 0.0
            detail = f"PS4 error 0x{error:08x}"
        elif got_everything and copying >= 100:
            state, percent = "done", 100.0
            detail = "Installed."
        elif copied_everything(total, transferred, copying, got_everything):
            state, percent = "installing", copying
            detail = f"Installing on the PS4 — {copying:.0f}%"
        elif download_pct > 0:
            state, percent = "downloading", download_pct
            detail = (f"Downloading to the PS4 — {download_pct:.0f}% "
                      f"({transferred // 1048576} / {total // 1048576} MB)")
        elif copying > 0:
            state, percent = "installing", copying
            detail = f"Installing on the PS4 — {copying:.0f}%"
        else:
            state, percent = "preparing", preparing
            detail = f"Preparing — {preparing:.0f}%"
        return TaskProgress(state=state, percent=percent, transferred=transferred, total=total,
                            detail=detail, error=error, raw=reply)

    def task_action(self, action: str, task_id: int) -> dict:
        """``start`` / ``stop`` / ``pause`` / ``resume`` / ``unregister``."""
        return self._post(f"/api/{action}_task", {"task_id": int(task_id)})

    # -- uninstall --------------------------------------------------------
    UNINSTALL_TARGETS = {
        "game": ("title_id", "/api/uninstall_game"),
        "patch": ("title_id", "/api/uninstall_patch"),
        "ac": ("content_id", "/api/uninstall_ac"),
        "theme": ("content_id", "/api/uninstall_theme"),
    }

    def uninstall(self, kind: str, value: str) -> dict:
        if kind not in self.UNINSTALL_TARGETS:
            raise InstallerError(f"Unknown uninstall target: {kind}")
        field_name, path = self.UNINSTALL_TARGETS[kind]
        reply = self._post(path, {field_name: value})
        if str(reply.get("status", "")).lower() != "success":
            raise InstallerError(
                f"The PS4 refused to uninstall {value}: "
                f"{reply.get('error', json.dumps(reply)[:160])}"
            )
        return reply

    def is_exists(self, title_id: str) -> Tuple[bool, Optional[int]]:
        reply = self._post("/api/is_exists", {"title_id": title_id})
        exists = str(reply.get("exists", "")).lower() == "true"
        size = reply.get("size")
        return exists, (int(size) if size is not None else None)


def copied_everything(total: int, transferred: int, copying: float, got_everything: bool) -> bool:
    """True while the console is copying the downloaded package into place."""
    if copying <= 0:
        return False
    if total and not got_everything:
        return False  # still downloading, whatever the copy counter says
    return copying < 100


# ---------------------------------------------------------------------------
# Serving the package to the console
# ---------------------------------------------------------------------------


class _PkgServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS lookup done while binding.

    ``http.server``'s ``server_bind()`` calls ``socket.getfqdn()`` - a blocking
    DNS round trip.  On a LAN without a resolver that stalls the whole install
    by seconds before a single byte moves, and the host name it computes is
    never used here.
    """

    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host or "ps4manager"
        self.server_port = port


class _PkgRequestHandler(http.server.BaseHTTPRequestHandler):
    server_version = "PS4ManagerPkg"

    # -- helpers ----------------------------------------------------------
    def _files(self) -> Dict[str, Path]:
        return self.server.files_by_key  # type: ignore[attr-defined]

    def _allowed(self) -> bool:
        allowed = self.server.allow_from  # type: ignore[attr-defined]
        return not allowed or self.client_address[0] in allowed

    def _describe(self) -> str:
        return self.path.split("?", 1)[0]

    def _key_for(self, path: str) -> Optional[Path]:
        token = getattr(self.server, "token", "")
        clean = urllib.parse.unquote(self._describe()).lstrip("/")
        if token and clean.startswith(token + "/"):
            clean = clean[len(token) + 1:]
        return self._files().get(clean)

    def _range(self, size: int) -> Tuple[int, int, bool]:
        header = (self.headers.get("Range") or "").strip()
        if not header.startswith("bytes="):
            return 0, max(0, size - 1), False
        spec = header[len("bytes="):].split(",")[0].strip()
        first, _, last = spec.partition("-")
        try:
            start = int(first) if first else 0
            end = int(last) if last else size - 1
        except ValueError:
            return 0, max(0, size - 1), False
        if start >= size:
            return 0, 0, False
        end = min(end, size - 1)
        if end < start:
            end = size - 1
        return start, end, True

    # -- verbs -------------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802 - http.server naming
        self._serve(head_only=True)

    def do_GET(self) -> None:  # noqa: N802
        self._serve(head_only=False)

    def _serve(self, head_only: bool) -> None:
        if not self._allowed():
            self.send_error(403, "Forbidden")
            return
        target = self._key_for(self.path)
        if target is None:
            self.send_error(404, "Not Found")
            return
        try:
            size = target.stat().st_size
        except OSError:
            self.send_error(404, "Not Found")
            return
        start, end, partial = self._range(size)
        length = max(0, end - start + 1)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return
        sent = 0
        try:
            with open(target, "rb") as handle:
                handle.seek(start)
                while sent < length:
                    chunk = handle.read(min(262144, length - sent))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    self.server.note_progress(target.name, sent)  # type: ignore[attr-defined]
        except (BrokenPipeError, ConnectionResetError, OSError):
            # the console closes the socket when it has what it needs
            pass

    def log_message(self, fmt: str, *args) -> None:  # silence the default stderr spam
        pass


class LocalPkgServer:
    """Hand the console a URL for a file that is sitting on this PC.

    The console does the downloading, so the package never has to be copied to
    the console over FTP first - and this server lives only as long as the
    install does.
    """

    def __init__(self, files: Sequence[Path], ip: str, port: int = DEFAULT_SERVE_PORT,
                 allow_from: str = "", logger: Optional[Callable[[str], None]] = None) -> None:
        self.paths = [Path(p) for p in files]
        self.ip = ip
        self.port = int(port)
        self.allow_from = {allow_from} if allow_from else set()
        self.logger = logger
        self.token = secrets.token_hex(4)
        self._server: Optional[http.server.ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.files_by_key: Dict[str, Path] = {}
        self._key_by_path: Dict[str, str] = {}
        self._progress: Dict[str, int] = {}
        self._lock = threading.Lock()
        for path in self.paths:
            resolved = str(Path(path).resolve())
            # an opaque, space-free name on purpose: the console copies the URL
            # into a manifest it builds by hand, so nothing may need escaping
            digest = hashlib.md5(resolved.encode("utf-8")).hexdigest()[:16]
            key = f"{digest}.pkg"
            self.files_by_key[key] = Path(path)
            self._key_by_path[resolved] = key

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._server is not None:
            return
        try:
            server = _PkgServer(("0.0.0.0", self.port), _PkgRequestHandler)
        except OSError:
            # the preferred port is taken - take any free one
            server = _PkgServer(("0.0.0.0", 0), _PkgRequestHandler)
        self.port = server.server_address[1]
        server.files_by_key = self.files_by_key  # type: ignore[attr-defined]
        server.allow_from = self.allow_from  # type: ignore[attr-defined]
        server.token = self.token  # type: ignore[attr-defined]
        server.note_progress = self._note_progress  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="pkg-http", daemon=True)
        self._thread.start()
        if self.logger:
            self.logger(f"Serving {len(self.paths)} package file(s) on {self.ip}:{self.port}")

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    @property
    def running(self) -> bool:
        return self._server is not None

    # -- urls + progress ---------------------------------------------------
    def url_for(self, path: Path) -> str:
        key = self._key_by_path.get(str(Path(path).resolve()))
        if key is None:
            raise KeyError(str(path))
        return f"http://{self.ip}:{self.port}/{self.token}/{key}"

    def urls(self) -> List[str]:
        return [self.url_for(path) for path in self.paths]

    def _note_progress(self, name: str, sent: int) -> None:
        with self._lock:
            self._progress[name] = max(sent, self._progress.get(name, 0))

    def served(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._progress)

    def served_total(self) -> int:
        with self._lock:
            return sum(self._progress.values())

    def expected_total(self) -> int:
        total = 0
        for path in self.paths:
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total


# ---------------------------------------------------------------------------
# Console-side package folder (over the app's existing FTP connection)
# ---------------------------------------------------------------------------


def is_pkg_file(entry) -> bool:
    """True for a .pkg (or a piece of one, or a leftover .part/.resume file)."""
    name = getattr(entry, "name", "") or ""
    return name.lower().endswith((".pkg", ".pkg.part", ".pkg.resume"))


def pkg_entries(entries: Iterator) -> List:
    return [entry for entry in entries if is_pkg_file(entry)]
