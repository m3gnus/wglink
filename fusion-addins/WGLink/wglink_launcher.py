"""Installed WG admission, independent of Fusion and its geometry objects."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Mapping
import urllib.request

if __package__:
    from . import wglink_inbox
else:
    import wglink_inbox


# Three seconds for discovery, 69 for admission, three for graceful cancellation,
# one for reaping after kill: at most 76 seconds of bounded waits.
ENSURE_TIMEOUT = 69.0
DISCOVERY_TIMEOUT = 3.0
INSTALL_MARKER = "wglink_install.json"
DEVELOPER_MARKER = "wglink_dev.json"
_INSTALL_MARKER_FIELDS = frozenset({
    "schema", "managedBy", "waveguideGeneratorRoot",
    "waveguideGeneratorVersion", "sourceCommit", "addinVersion",
})
_NATIVE_SIGNATURE = b"WG_NATIVE_ENSURE_BACKEND_V1"
_MAX_NATIVE_ENTRY_BYTES = 64 * 1024 * 1024


class LauncherError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def launcher_argv(ipc: Path, *, strict: bool = False) -> list[str] | None:
    """Read only the additive, explicitly enabled installed-launcher field."""

    try:
        payload = json.loads((ipc / wglink_inbox.CAPABILITIES_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or type(payload.get("schemaVersion")) is not int or payload["schemaVersion"] != 1:
        return None
    launcher = payload.get("launcher")
    if not isinstance(launcher, dict) or launcher.get("ensureBackend") is not True:
        return None
    argv = launcher.get("argv")
    reason = _launcher_error(argv)
    if reason is not None:
        if strict:
            raise LauncherError(f"Invalid WG installed launcher: {reason}. Start WG manually, then try again.")
        return None
    return list(argv)


def installed_marker_argv(addin_dir: Path) -> list[str] | None:
    """Derive the managing bundle's entry without changing its install record."""

    try:
        if addin_dir.is_symlink() or not addin_dir.is_dir():
            return None
        developer = addin_dir / DEVELOPER_MARKER
        if developer.exists() or developer.is_symlink():
            return None
        marker = addin_dir / INSTALL_MARKER
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 65536:
            return None
        with marker.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            return None
        payload = json.loads(raw.decode("utf-8"))
        if (not isinstance(payload, dict) or set(payload) != _INSTALL_MARKER_FIELDS
                or type(payload.get("schema")) is not int or payload["schema"] != 1
                or payload.get("managedBy") != "waveguide-generator"):
            return None
        value = payload.get("waveguideGeneratorRoot")
        if not isinstance(value, str) or not value or "\x00" in value:
            return None
        root = Path(value)
        if not root.is_absolute() or root.name != "app" or not root.is_dir():
            return None
        if sys.platform == "win32":
            entry = root.parent / "Waveguide Generator.exe"
        elif sys.platform == "darwin":
            if root.parent.name != "Resources" or root.parent.parent.name != "Contents":
                return None
            entry = root.parent.parent / "MacOS" / "Waveguide Generator"
        else:
            return None  # Fusion has no Linux managed install.
        argv = [str(entry)]
        return argv if _launcher_error(argv) is None else None
    except (OSError, ValueError, RecursionError):
        return None


def _launcher_error(argv: object) -> str | None:
    if not isinstance(argv, list) or len(argv) != 1:
        return "argv must contain exactly one installed entry, without extra arguments"
    if not isinstance(argv[0], str) or not argv[0] or "\x00" in argv[0]:
        return "the installed entry must be a non-empty path"
    entry = Path(argv[0])
    if not entry.is_absolute():
        return "the installed entry must be an absolute path"
    if not entry.is_file():
        return "the installed entry is missing or is not a regular file"
    if sys.platform == "darwin":
        contents = entry.parent.parent
        if (entry.name != "Waveguide Generator" or entry.parent.name != "MacOS"
                or contents.name != "Contents" or not contents.parent.name.endswith(".app")):
            return "expected an .app/Contents/MacOS/Waveguide Generator entry"
        app = contents / "Resources" / "app"
    elif sys.platform == "win32":
        if entry.name != "Waveguide Generator.exe":
            return "expected the installed Waveguide Generator.exe entry"
        app = entry.parent / "app"
        try:
            if (entry.parent / "Waveguide Generator.ensure-backend-v1").read_bytes() != b"WG_NATIVE_ENSURE_BACKEND_V1":
                return "the native ensure-backend marker has invalid contents"
        except OSError:
            return "the native ensure-backend marker is missing or unreadable"
        try:
            if entry.stat().st_size > _MAX_NATIVE_ENTRY_BYTES:
                return "the native installed entry exceeds 64 MiB"
            with entry.open("rb") as stream:
                image = stream.read(_MAX_NATIVE_ENTRY_BYTES + 1)
            if len(image) > _MAX_NATIVE_ENTRY_BYTES:
                return "the native installed entry exceeds 64 MiB"
            if _NATIVE_SIGNATURE not in image:
                return "the native ensure-backend signature is missing from the installed entry"
        except OSError:
            return "the native installed entry is unreadable"
    else:
        if entry.name not in {"waveguide-generator", "launch-wg.sh"}:
            return "expected the installed waveguide-generator or launch-wg.sh entry"
        app = entry.parent / "app"
    if not app.is_dir():
        return "the installed entry's sibling app directory is missing"
    return None


def _same_folder(value: object, folder: Path) -> bool:
    if not isinstance(value, str) or not Path(value).is_absolute():
        return False
    try:
        return Path(value).resolve() == folder.resolve() or os.path.samefile(value, folder)
    except (OSError, ValueError):
        return False


def collecting(ipc: Path, data_dir: Path, cancelled: threading.Event | None = None,
               *, timeout: float = DISCOVERY_TIMEOUT) -> bool:
    """Bound all discovery, including a trickling response, by one deadline.

    Network I/O runs on a daemon probe so cancellation can release admission
    even while urllib is waiting for headers or a response body.
    """

    cancelled = cancelled if cancelled is not None else threading.Event()
    if cancelled.is_set():
        return False
    deadline = time.monotonic() + min(timeout, DISCOVERY_TIMEOUT)
    finished = threading.Event()
    result = []

    def probe() -> None:
        try:
            result.append(_collecting_probe(ipc, data_dir, cancelled, deadline))
        except Exception:  # failed discovery cannot establish a current owner
            result.append(False)
        finally:
            finished.set()

    threading.Thread(target=probe, name="WGLinkOwnerProbe", daemon=True).start()
    while not cancelled.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if finished.wait(min(0.05, remaining)):
            return bool(result and result[0]) and not cancelled.is_set()
    return False


def _collecting_probe(ipc: Path, data_dir: Path, cancelled: threading.Event,
                      deadline: float) -> bool:
    """Capabilities plus a current owner, never the age of a startup-only file.

    Mirrors WG's read-only owner discovery: locks/server.pid supplies pid/port;
    /health must confirm both the pid and exact data folder. No locks or writes.
    """

    try:
        wglink_inbox.require_wg_delivery(ipc)
    except wglink_inbox.WgNotCollectingError:
        return False
    try:
        owner = json.loads((data_dir / "locks" / "server.pid").read_text(encoding="utf-8"))
        if not isinstance(owner, dict):
            return False
        pid, port = owner.get("pid"), owner.get("port")
        if type(pid) is not int or pid <= 0 or type(port) is not int or not 0 < port < 65536:
            return False
        # Local admission must never pass through an inherited HTTP proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=2.0) as response:
            # read1 returns available bytes rather than waiting for the entire
            # body, allowing a trickling peer to be stopped at the deadline.
            read = getattr(response, "read1", response.read)
            body = bytearray()
            while not cancelled.is_set() and time.monotonic() < deadline:
                chunk = read(65537 - len(body))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > 65536:
                    return False
            if cancelled.is_set() or time.monotonic() >= deadline:
                return False
            health = json.loads(body.decode("utf-8"))
        return (isinstance(health, dict) and type(health.get("pid")) is int
                and health["pid"] == pid and _same_folder(health.get("data_dir"), data_dir))
    except (OSError, ValueError):
        return False


def parse_response(output: str, code: int, data_dir: Path) -> Mapping[str, object]:
    lines = output.splitlines()
    try:
        if len(lines) != 1:
            raise ValueError("expected exactly one JSON line")
        payload = json.loads(lines[0])
        if not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
            raise ValueError("expected an object with a boolean ok field")
        if payload["ok"] is False:
            error = payload.get("error")
            if (not isinstance(error, dict) or not isinstance(error.get("type"), str)
                    or not isinstance(error.get("message"), str)):
                raise ValueError("invalid error object")
            raise LauncherError(f"{error['type']}: {error['message']} (exit {code}).")
        if code != 0:
            raise ValueError(f"success response with exit {code}")
        if (type(payload.get("pid")) is not int or payload["pid"] <= 0
                or not all(isinstance(payload.get(key), str) for key in ("base_url", "version", "commit"))
                or not payload["base_url"]):
            raise ValueError("incomplete success object")
        if not _same_folder(payload.get("data_dir"), data_dir):
            raise LauncherError("The launcher returned a different WG data folder.")
        return payload
    except (ValueError, TypeError) as exc:
        raise LauncherError(f"Invalid or missing launcher JSON (exit {code}): {exc}.") from exc


def ensure_backend(argv: list[str], data_dir: Path, override: Path | None,
                   cancelled: threading.Event, *, timeout: float = ENSURE_TIMEOUT) -> None:
    """Invoke only the validated installed entry, once; cancel only our child."""

    command = list(argv) + ["--ensure-backend"]
    if cancelled.is_set():
        raise LauncherError("WG backend startup was cancelled.")
    reason = _launcher_error(argv)
    if reason is not None:
        raise LauncherError(f"Invalid WG installed launcher: {reason}.")
    if override is not None:
        command += ["--data-dir", str(override)]
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    # Regular files avoid inherited stdout pipes keeping communicate() blocked
    # after the public launcher exits. Admission output is bounded when read.
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.DEVNULL, close_fds=True, **kwargs)
        except OSError as exc:
            raise LauncherError(f"Could not run WG's installed launcher: {exc}.") from exc
        deadline = time.monotonic() + min(timeout, ENSURE_TIMEOUT)
        try:
            while process.poll() is None:
                if cancelled.is_set():
                    raise LauncherError("WG backend startup was cancelled.")
                if time.monotonic() >= deadline:
                    raise LauncherError("Timed out waiting for WG's installed launcher.")
                cancelled.wait(min(0.05, max(0.0, deadline - time.monotonic())))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)
        output.seek(0)
        try:
            raw = output.read(65537)
            if len(raw) > 65536:
                raise ValueError("launcher response exceeds 64 KiB")
            parse_response(raw.decode("utf-8"), process.returncode, data_dir)
        except (UnicodeError, ValueError) as exc:
            raise LauncherError(f"Invalid launcher output: {exc}.") from exc


class BackendAdmission:
    """One worker; presses join until its main-thread completion is delivered."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._callbacks: list[Callable] = []
        self._folder: Path | None = None
        self._cancelled = threading.Event()
        self.thread: threading.Thread | None = None

    def request(self, ipc: Path, data_dir: Path, argv: list[str], override: Path | None,
                callback: Callable, dispatch: Callable, starting: Callable | None = None) -> None:
        with self._lock:
            if self._cancelled.is_set():
                raise LauncherError("WGLink is stopping; start WG manually.")
            if self._callbacks:
                if self._folder != data_dir.resolve():
                    raise LauncherError("WG startup is already waiting on another configured data folder.")
                self._callbacks.append(callback)
                return
            self._folder = data_dir.resolve()
            self._callbacks.append(callback)
            pending = self._callbacks

        def work() -> None:
            error = None
            try:
                if not collecting(ipc, data_dir, self._cancelled) and not self.cancelled:
                    if starting is not None:
                        def show_starting() -> None:
                            with self._lock:
                                if self._callbacks is not pending or not pending:
                                    return
                            if not self.cancelled:
                                starting()
                        dispatch(show_starting)
                    ensure_backend(argv, data_dir, override, self._cancelled)
            except Exception as exc:  # worker boundary: all failures reach the UI
                error = str(exc)

            def deliver() -> None:
                with self._lock:
                    if self._callbacks is not pending:
                        return
                    callbacks, self._callbacks = self._callbacks, []
                for done in callbacks:
                    if self.cancelled:
                        break
                    done(error)

            try:
                if not self.cancelled:
                    dispatch(deliver)
            except Exception:  # the event bridge may disappear during shutdown
                with self._lock:
                    if self._callbacks is pending:
                        self._callbacks = []

        self.thread = threading.Thread(target=work, name="WGLinkBackendAdmission", daemon=True)
        try:
            self.thread.start()
        except RuntimeError as exc:
            with self._lock:
                self._callbacks.clear()
            raise LauncherError(f"Could not start WG's admission worker: {exc}.") from exc

    def close(self) -> None:
        self._cancelled.set()
        with self._lock:
            self._callbacks.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()
