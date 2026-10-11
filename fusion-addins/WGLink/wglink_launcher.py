"""Installed WG admission, independent of Fusion and its geometry objects."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from typing import Callable, Mapping
import urllib.request

if __package__:
    from . import wglink_inbox
else:
    import wglink_inbox


# Two seconds for discovery, 69 for admission, three for graceful cancellation,
# one for reaping after kill: at most 75 seconds of bounded waits.
ENSURE_TIMEOUT = 69.0


class LauncherError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def launcher_argv(ipc: Path) -> list[str] | None:
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
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(item, str) and item and "\x00" not in item for item in argv)
            or not Path(argv[0]).is_absolute()):
        return None
    return list(argv)


def _same_folder(value: object, folder: Path) -> bool:
    if not isinstance(value, str) or not Path(value).is_absolute():
        return False
    try:
        return Path(value).resolve() == folder.resolve() or os.path.samefile(value, folder)
    except (OSError, ValueError):
        return False


def collecting(ipc: Path, data_dir: Path) -> bool:
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
            health = json.loads(response.read(65537).decode("utf-8"))
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
    """Invoke only the advertised entry, once; cancellation addresses our child."""

    command = list(argv) + ["--ensure-backend"]
    if cancelled.is_set():
        raise LauncherError("WG backend startup was cancelled.")
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
                callback: Callable, dispatch: Callable) -> None:
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

        def work() -> None:
            error = None
            try:
                if not collecting(ipc, data_dir):
                    ensure_backend(argv, data_dir, override, self._cancelled)
            except Exception as exc:  # worker boundary: all failures reach the UI
                error = str(exc)

            def deliver() -> None:
                with self._lock:
                    callbacks, self._callbacks = self._callbacks, []
                if not self._cancelled.is_set():
                    for done in callbacks:
                        done(error)

            if not self._cancelled.is_set():
                dispatch(deliver)

        self.thread = threading.Thread(target=work, name="WGLinkBackendAdmission", daemon=True)
        try:
            self.thread.start()
        except RuntimeError as exc:
            with self._lock:
                self._callbacks.clear()
            raise LauncherError(f"Could not start WG's admission worker: {exc}.") from exc

    def close(self) -> None:
        self._cancelled.set()
