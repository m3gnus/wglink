"""Installed launcher admission and the Fusion main-thread continuation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import types
import threading
import time

import pytest

from test_wglink_transfer import _transfer, _run, _inbox

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fusion-addins" / "WGLink"))
import wglink_launcher
import wglink_workspace


def _success(root):
    return {"ok": True, "base_url": "http://127.0.0.1:3100", "pid": 42,
            "data_dir": str(root), "version": "test", "commit": "test", "extra": "ignored"}


def _installed_entry(tmp_path, platform=None, basename=None):
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        contents = tmp_path / "WG.app" / "Contents"
        (contents / "Resources" / "app").mkdir(parents=True, exist_ok=True)
        folder = contents / "MacOS"
        name = "Waveguide Generator"
    else:
        folder = tmp_path / "installed"
        (folder / "app").mkdir(parents=True, exist_ok=True)
        name = "Waveguide Generator.exe" if platform == "win32" else "waveguide-generator"
        if platform == "win32":
            (folder / "Waveguide Generator.ensure-backend-v1").write_bytes(b"WG_NATIVE_ENSURE_BACKEND_V1")
    folder.mkdir(parents=True, exist_ok=True)
    entry = folder / (basename or name)
    entry.touch()
    if platform == "win32":
        entry.write_bytes(b"native image WG_NATIVE_ENSURE_BACKEND_V1")
    return entry


def _script(tmp_path, body):
    if os.name == "nt":
        pytest.skip("real subprocess fixtures require a native Windows installed launcher")
    script = _installed_entry(tmp_path)
    script.write_text("#!" + sys.executable + "\nimport json, sys, time, os\nfrom pathlib import Path\n" + body)
    script.chmod(0o755)
    return [str(script)]


def _advertise_launcher(fixture, argv):
    path = fixture.ipc / "wg-capabilities.json"
    payload = json.loads(path.read_text())
    payload["launcher"] = {"argv": argv, "ensureBackend": True, "unknown": 7}
    path.write_text(json.dumps(payload))
    return path, payload


def _finish(fixture):
    worker = fixture.module._backend_admission.thread
    assert worker is not None
    worker.join(timeout=5)
    assert not worker.is_alive()
    fixture.module.FollowupEventHandler().notify(None)


def _checked_fixture(monkeypatch, tmp_path, name, *, advertised=4, preserve_addin_dir=False):
    monkeypatch.delenv("WG2_DATA_DIR", raising=False)
    fixture = _transfer(monkeypatch, tmp_path, name, advertised=advertised)
    # Model the production IPC layout so the data root cannot be confused
    # with the separately selected CAD workspace.
    ipc = tmp_path / "data" / "ipc" / "wglink"
    ipc.mkdir(parents=True)
    (ipc / "wg-capabilities.json").write_bytes((fixture.ipc / "wg-capabilities.json").read_bytes())
    fixture.ipc = ipc
    monkeypatch.setattr(fixture.module.wglink_workspace, "ipc_folder", lambda **kwargs: ipc)
    real_data_dir = fixture.module.wglink_workspace.data_dir
    monkeypatch.setattr(fixture.module.wglink_workspace, "data_dir",
                        lambda **kwargs: real_data_dir(**kwargs) if kwargs else ipc.parent.parent)
    if not preserve_addin_dir:
        addin = tmp_path / "WGLink"
        addin.mkdir()
        monkeypatch.setattr(fixture.module.wglink_core, "ADDIN_DIR", addin)
        monkeypatch.setattr(fixture.module, "__file__", str(addin / "WGLink.py"))
    main = threading.get_ident()
    app_type = type(fixture.app)

    class ThreadCheckedApp(app_type):
        def __getattribute__(self, key):
            if key != "fireCustomEvent":
                assert threading.get_ident() == main, "Fusion property accessed from worker: " + key
            return super().__getattribute__(key)

        def fireCustomEvent(self, event_id, _payload=""):
            # Fusion explicitly permits this custom-event bridge from workers.
            object.__getattribute__(self, "fired").append(event_id)
            return True

    fixture.app.__class__ = ThreadCheckedApp
    real_app = fixture.module._app

    def checked_app():
        assert threading.get_ident() == main, "Application.get from worker"
        return real_app()

    monkeypatch.setattr(fixture.module, "_app", checked_app)
    real_events = fixture.module.adsk.doEvents

    def do_events():
        assert threading.get_ident() == main, "doEvents from worker"
        real_events()

    monkeypatch.setattr(fixture.module.adsk, "doEvents", do_events)
    fixture.module._start_followups(fixture.app)
    calls = []
    real_send = fixture.module.wglink_send.send

    def send(app, options, **kwargs):
        assert threading.get_ident() == main, "geometry export from worker"
        calls.append(dict(options))
        return real_send(app, options, **kwargs)

    monkeypatch.setattr(fixture.module.wglink_send, "send", send)
    real_message = fixture.ui.messageBox

    def message(*args):
        assert threading.get_ident() == main, "UI from worker"
        return real_message(*args)

    monkeypatch.setattr(fixture.ui, "messageBox", message)
    fixture.exports = calls
    return fixture


@pytest.mark.parametrize("operation", ["send", "solve"])
@pytest.mark.parametrize("advertised", [4, None])
def test_ok_resumes_original_command_only_on_custom_event(monkeypatch, tmp_path, operation, advertised):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_ok", advertised=advertised)
    args_log = tmp_path / "argv.json"
    path = fixture.ipc / "wg-capabilities.json"
    ready = json.loads(path.read_text())
    ready["solveCommandDelivery"] = 4
    body = (f"Path({str(args_log)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            f"Path({str(path)!r}).write_text({json.dumps(ready)!r})\n"
            f"print({json.dumps(_success(fixture.ipc.parent.parent))!r})\n")
    _advertise_launcher(fixture, _script(tmp_path, body))
    # A dead owner's old advertisement still has all delivery fields.
    os.utime(path, (1, 1))
    _run(fixture.module, operation)
    fixture.module._backend_admission.thread.join(timeout=5)
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    _finish(fixture)
    assert len(fixture.exports) == 1
    assert _inbox(fixture.ipc)[0]["kind"] == ("prepare_and_solve" if operation == "solve" else "receive_snapshot")
    assert args_log.exists()
    assert json.loads(args_log.read_text()) == ["--ensure-backend"]
    assert fixture.module.FOLLOWUP_EVENT_ID in fixture.app.fired


@pytest.mark.parametrize("operation", ["send", "solve"])
@pytest.mark.parametrize("code", [3, 4, 5])
def test_json_failure_preserves_reason_and_writes_nothing(monkeypatch, tmp_path, operation, code):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_error")
    output = json.dumps({"ok": False, "error": {"type": "BackendFailure", "message": "specific reason"}})
    _advertise_launcher(fixture, _script(tmp_path, f"print({output!r})\nsys.exit({code})\n"))
    _run(fixture.module, operation)
    _finish(fixture)
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    text = fixture.ui.messages[-1][1]
    assert "Start Waveguide Generator manually" in text
    detail = "\n".join(fixture.ui.text_palette.written)
    assert "BackendFailure: specific reason" in detail
    assert f"exit {code}" in detail


@pytest.mark.parametrize("output,code", [("", 0), ("", 5), ("not json", 0), ("{}\n{}", 0), ("[]", 0)])
def test_native_and_invalid_json_failures_do_not_export(monkeypatch, tmp_path, output, code):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_invalid")
    _advertise_launcher(fixture, _script(tmp_path, f"sys.stdout.write({output!r})\nsys.exit({code})\n"))
    _run(fixture.module, "solve")
    _finish(fixture)
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    assert "JSON" in "\n".join(fixture.ui.text_palette.written)


def test_timeout_terminates_only_launcher_and_does_not_export(monkeypatch, tmp_path):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_timeout")
    launcher = fixture.module.wglink_launcher
    _advertise_launcher(fixture, _script(tmp_path, "time.sleep(30)\n"))
    real_ensure = launcher.ensure_backend
    real_popen = launcher.subprocess.Popen
    children = []

    def popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher, "ensure_backend", lambda *args, **kwargs: real_ensure(*args, timeout=0.1))
    _run(fixture.module, "send")
    _finish(fixture)
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    assert "Timed out" in "\n".join(fixture.ui.text_palette.written)
    assert len(children) == 1 and children[0].poll() is not None


def test_concurrent_presses_join_one_start_and_each_resume_once(monkeypatch, tmp_path):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_join")
    count = tmp_path / "starts.txt"
    release = tmp_path / "release"
    body = (f"with Path({str(count)!r}).open('a') as f: f.write('start\\n')\n"
            f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
            f"print({json.dumps(_success(fixture.ipc.parent.parent))!r})\n")
    _advertise_launcher(fixture, _script(tmp_path, body))
    _run(fixture.module, "send")
    worker = fixture.module._backend_admission.thread
    _run(fixture.module, "solve")
    assert fixture.module._backend_admission.thread is worker
    assert fixture.exports == []
    release.touch()
    _finish(fixture)
    assert count.read_text() == "start\n"
    assert len(fixture.exports) == 2
    requests = _inbox(fixture.ipc)
    assert {r["kind"] for r in requests} == {"receive_snapshot", "prepare_and_solve"}
    assert len({r["commandId"] for r in requests}) == 2


def _owner_health(monkeypatch, launcher, root, *, pid=42, reported_root=None):
    locks = root / "locks"
    locks.mkdir(exist_ok=True)
    (locks / "server.pid").write_text(json.dumps({"pid": 42, "port": 3100}))
    health = json.dumps({"pid": pid, "data_dir": str(reported_root or root)}).encode()
    calls = []

    class Response:
        read_once = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert 0 < size <= 65537
            if self.read_once:
                return b""
            self.read_once = True
            return health

    def open_health(url, timeout):
        assert url == "http://127.0.0.1:3100/health" and timeout == 2.0
        calls.append(url)
        return Response()

    monkeypatch.setattr(launcher.urllib.request, "build_opener",
                        lambda *args: types.SimpleNamespace(open=open_health))
    return calls


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_already_collecting_even_with_old_capabilities_does_not_spawn(monkeypatch, tmp_path, operation):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_collecting")
    argv = _script(tmp_path, "raise AssertionError('must not launch')\n")
    path, _ = _advertise_launcher(fixture, argv)
    os.utime(path, (1, 1))
    calls = _owner_health(monkeypatch, fixture.module.wglink_launcher, fixture.ipc.parent.parent)
    monkeypatch.setattr(fixture.module.wglink_launcher.subprocess, "Popen",
                        lambda *args, **kwargs: pytest.fail("collecting WG spawned a launcher"))
    _run(fixture.module, operation)
    _finish(fixture)
    assert len(fixture.exports) == 1 and len(_inbox(fixture.ipc)) == 1
    assert calls == ["http://127.0.0.1:3100/health"]


@pytest.mark.parametrize("pid,other_folder", [(7, False), (42, True)])
def test_owner_discovery_requires_pid_and_same_folder(monkeypatch, tmp_path, pid, other_folder):
    ipc = tmp_path / "ipc" / "wglink"
    ipc.mkdir(parents=True)
    (ipc / "wg-capabilities.json").write_text(json.dumps({"schemaVersion": 1, "solveCommandDelivery": 4, "fusionRequestDelivery": 3}))
    _owner_health(monkeypatch, wglink_launcher, tmp_path, pid=pid,
                  reported_root=tmp_path / "other" if other_folder else None)
    assert not wglink_launcher.collecting(ipc, tmp_path)


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_no_launcher_keeps_start_manually_behavior(monkeypatch, tmp_path, operation):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_no_launcher", advertised=None)
    monkeypatch.setattr(fixture.module.wglink_launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("spawn"))
    _run(fixture.module, operation)
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    assert "Open Waveguide Generator" in fixture.ui.messages[-1][1]
    assert fixture.module._backend_admission.thread is None


@pytest.mark.parametrize("launcher", [None, {}, {"argv": ["relative"], "ensureBackend": True},
                                     {"argv": [sys.executable], "ensureBackend": 1},
                                     {"argv": [], "ensureBackend": True}])
def test_unknown_or_invalid_launcher_is_ignored(tmp_path, launcher):
    (tmp_path / "wg-capabilities.json").write_text(json.dumps({"schemaVersion": 1, "launcher": launcher}))
    assert wglink_launcher.launcher_argv(tmp_path) is None


def test_non_default_folder_is_the_only_explicit_launcher_override(monkeypatch, tmp_path):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_override")
    root = fixture.ipc.parent.parent
    monkeypatch.setenv("WG2_DATA_DIR", str(root))
    args_log = tmp_path / "args.json"
    body = f"Path({str(args_log)!r}).write_text(json.dumps(sys.argv[1:]))\nprint({json.dumps(_success(root))!r})\n"
    _advertise_launcher(fixture, _script(tmp_path, body))
    _run(fixture.module, "solve")
    _finish(fixture)
    assert json.loads(args_log.read_text()) == ["--ensure-backend", "--data-dir", str(root.absolute())]
    default_env = {key: value for key, value in os.environ.items() if key != "WG2_DATA_DIR"}
    default = wglink_workspace.data_dir(environ=default_env)
    if default is not None:
        monkeypatch.setenv("WG2_DATA_DIR", str(default))
        assert wglink_workspace.launcher_data_dir() is None
    monkeypatch.delenv("WG2_DATA_DIR")
    assert wglink_workspace.launcher_data_dir() is None


def test_wrong_folder_success_is_refused_without_retry(monkeypatch, tmp_path):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_wrong_folder")
    _advertise_launcher(fixture, _script(tmp_path, f"print({json.dumps(_success(tmp_path / 'other'))!r})\n"))
    _run(fixture.module, "solve")
    _finish(fixture)
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    assert "different WG data folder" in "\n".join(fixture.ui.text_palette.written)


def test_document_switch_while_starting_does_not_export(monkeypatch, tmp_path):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_document_switch")
    _advertise_launcher(fixture, _script(tmp_path, f"print({json.dumps(_success(fixture.ipc.parent.parent))!r})\n"))
    _run(fixture.module, "solve")
    fixture.app.activeDocument = object()
    _finish(fixture)
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    assert "document changed" in fixture.ui.messages[-1][1]


def test_stop_cancels_pending_start_and_drops_continuation(monkeypatch, tmp_path):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_stop")
    _advertise_launcher(fixture, _script(tmp_path, "time.sleep(30)\n"))
    _run(fixture.module, "solve")
    worker = fixture.module._backend_admission.thread
    fixture.module._stop_followups(fixture.app)
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    assert fixture.module._followups == []


def test_missing_installed_entry_is_a_visible_validation_failure(monkeypatch, tmp_path):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_missing_entry")
    _advertise_launcher(fixture, [str(tmp_path / "missing installed launcher")])
    monkeypatch.setattr(fixture.module.wglink_launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("spawn"))
    _run(fixture.module, "send")
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    assert fixture.module._backend_admission.thread is None
    assert "missing or is not a regular file" in "\n".join(fixture.ui.text_palette.written)
    assert "Start Waveguide Generator manually" in fixture.ui.messages[-1][1]


def test_unsupported_delivery_with_launcher_still_refuses_without_spawn(monkeypatch, tmp_path):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_outdated", advertised=3)
    _advertise_launcher(fixture, _script(tmp_path, "raise AssertionError('must not launch')\n"))
    _run(fixture.module, "solve")
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    assert fixture.module._backend_admission.thread is None
    assert "Update Waveguide Generator" in fixture.ui.messages[-1][1]


def test_success_with_disabled_consumer_is_refused_before_export(monkeypatch, tmp_path):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_disabled", advertised=None)
    _advertise_launcher(fixture, _script(tmp_path, f"print({json.dumps(_success(fixture.ipc.parent.parent))!r})\n"))
    _run(fixture.module, "send")
    _finish(fixture)
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    assert "not ready to receive this model" in fixture.ui.messages[-1][1]


@pytest.mark.parametrize("operation", ["insert", "update"])
def test_insert_update_never_admit_backend(monkeypatch, tmp_path, operation):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_no_admission_" + operation)
    _advertise_launcher(fixture, _script(tmp_path, "raise AssertionError('must not launch')\n"))
    monkeypatch.setattr(fixture.module._backend_admission, "request", lambda *a, **k: pytest.fail("admission"))
    if operation == "insert":
        monkeypatch.setattr(fixture.module, "_choose_bundle", lambda *a, **k: None)
    else:
        monkeypatch.setattr(fixture.module.wglink_core, "update", lambda *a: {})
    _run(fixture.module, operation)
    assert fixture.module._backend_admission.thread is None


def test_command_inputs_are_not_retained_after_execute(monkeypatch, tmp_path):
    from test_wglink_addin_lifecycle import _dialog_inputs
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_inputs")
    _advertise_launcher(fixture, _script(tmp_path, f"print({json.dumps(_success(fixture.ipc.parent.parent))!r})\n"))

    class Command:
        valid = True

        @property
        def commandInputs(self):
            assert self.valid, "expired CommandInputs accessed"
            return _dialog_inputs()

    command = Command()
    fixture.module.CommandExecuteHandler("solve").notify(types.SimpleNamespace(command=command))
    command.valid = False
    _finish(fixture)
    assert len(fixture.exports) == 1 and len(_inbox(fixture.ipc)) == 1


@pytest.mark.parametrize("payload,code", [({"ok": True}, 0), ({"ok": False}, 5),
                                          ({"ok": 1}, 0), (None, 0)])
def test_incomplete_json_is_a_launcher_failure(tmp_path, payload, code):
    with pytest.raises(wglink_launcher.LauncherError, match="JSON"):
        wglink_launcher.parse_response(json.dumps(payload), code, tmp_path)


def test_success_with_nonzero_exit_is_a_launcher_failure(tmp_path):
    with pytest.raises(wglink_launcher.LauncherError, match="exit 5"):
        wglink_launcher.parse_response(json.dumps(_success(tmp_path)), 5, tmp_path)


def test_cancelled_admission_never_spawns(monkeypatch, tmp_path):
    cancelled = threading.Event()
    cancelled.set()
    monkeypatch.setattr(wglink_launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("spawn"))
    with pytest.raises(wglink_launcher.LauncherError, match="cancelled"):
        wglink_launcher.ensure_backend([sys.executable], tmp_path, None, cancelled)


def test_launcher_timeout_leaves_foreign_process_alive(monkeypatch, tmp_path):
    import subprocess
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_foreign")
    launcher = fixture.module.wglink_launcher
    _advertise_launcher(fixture, _script(tmp_path, "time.sleep(30)\n"))
    real_ensure = launcher.ensure_backend
    monkeypatch.setattr(launcher, "ensure_backend", lambda *args, **kwargs: real_ensure(*args, timeout=0.1))
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _run(fixture.module, "solve")
        _finish(fixture)
        assert foreign.poll() is None
        assert fixture.exports == [] and _inbox(fixture.ipc) == []
    finally:
        foreign.terminate()
        foreign.wait(timeout=5)


@pytest.mark.parametrize("operation", ["send", "solve"])
@pytest.mark.parametrize("invalid,reason", [
    ("shell", "exactly one"), ("extra", "exactly one"),
    ("basename", "expected"), ("relative", "absolute"),
    ("directory", "regular file"), ("app", "app directory"),
])
def test_invalid_advertised_launcher_refuses_before_export_or_spawn(monkeypatch, tmp_path, operation, invalid, reason):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_invalid_entry")
    entry = _installed_entry(tmp_path)
    argv = [str(entry)]
    if invalid == "shell":
        argv = ["/bin/sh", "-c", "exit 0"]
    elif invalid == "extra":
        argv.append("extra")
    elif invalid == "basename":
        wrong = entry.with_name("wrong-entry")
        entry.rename(wrong)
        argv = [str(wrong)]
    elif invalid == "relative":
        argv = [entry.name]
    elif invalid == "directory":
        entry.unlink()
        entry.mkdir()
    elif invalid == "app":
        app = entry.parent.parent / "Resources" / "app" if sys.platform == "darwin" else entry.parent / "app"
        app.rmdir()
    _advertise_launcher(fixture, argv)
    monkeypatch.setattr(fixture.module.wglink_launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("spawn"))
    _run(fixture.module, operation)
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    assert fixture.module._backend_admission.thread is None
    assert "Invalid WG installed launcher" in "\n".join(fixture.ui.text_palette.written)
    assert reason in "\n".join(fixture.ui.text_palette.written)
    assert wglink_launcher.launcher_argv(fixture.ipc) is None


@pytest.mark.parametrize("platform,basename", [
    ("darwin", None), ("win32", None), ("linux", "waveguide-generator"), ("linux", "launch-wg.sh"),
])
def test_installed_layout_validation(monkeypatch, tmp_path, platform, basename):
    entry = _installed_entry(tmp_path, platform, basename)
    monkeypatch.setattr(wglink_launcher.sys, "platform", platform)
    (tmp_path / "wg-capabilities.json").write_text(json.dumps({
        "schemaVersion": 1, "launcher": {"argv": [str(entry)], "ensureBackend": True}}))
    assert wglink_launcher.launcher_argv(tmp_path, strict=True) == [str(entry)]


def test_macos_entry_requires_app_bundle_shape(monkeypatch, tmp_path):
    entry = _installed_entry(tmp_path, "darwin")
    bundle = tmp_path / "WG.app"
    bundle.rename(tmp_path / "WG")
    entry = tmp_path / "WG" / entry.relative_to(bundle)
    monkeypatch.setattr(wglink_launcher.sys, "platform", "darwin")
    assert ".app/Contents/MacOS" in wglink_launcher._launcher_error([str(entry)])


def test_spawn_boundary_revalidates_installed_entry(monkeypatch, tmp_path):
    entry = _installed_entry(tmp_path)
    entry.unlink()
    monkeypatch.setattr(wglink_launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("spawn"))
    with pytest.raises(wglink_launcher.LauncherError, match="missing or is not a regular file"):
        wglink_launcher.ensure_backend([str(entry)], tmp_path, None, threading.Event())


@pytest.mark.parametrize("marker", [None, b"wrong", b"WG_NATIVE_ENSURE_BACKEND_V1\n"])
def test_windows_marker_is_required_exactly(monkeypatch, tmp_path, marker):
    entry = _installed_entry(tmp_path, "win32")
    path = entry.parent / "Waveguide Generator.ensure-backend-v1"
    if marker is None:
        path.unlink()
    else:
        path.write_bytes(marker)
    monkeypatch.setattr(wglink_launcher.sys, "platform", "win32")
    assert "marker" in wglink_launcher._launcher_error([str(entry)])


@pytest.mark.parametrize("grew_after_stat", [False, True])
def test_windows_setup_marker_read_is_bounded(monkeypatch, tmp_path, grew_after_stat):
    import io

    entry = _installed_entry(tmp_path, "win32")
    marker = entry.parent / "Waveguide Generator.ensure-backend-v1"
    if not grew_after_stat:
        marker.write_bytes(wglink_launcher._NATIVE_SIGNATURE + b" " * 65536)
    real_open = Path.open
    reads = []

    class GrowingMarker(io.BytesIO):
        def read(self, size=-1):
            assert size == len(wglink_launcher._NATIVE_SIGNATURE) + 1
            reads.append(size)
            return super().read(size)

    def open_marker(path, *args, **kwargs):
        if path == marker:
            assert grew_after_stat, "oversized setup marker was opened"
            return GrowingMarker(wglink_launcher._NATIVE_SIGNATURE + b" " * 65536)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_marker)
    monkeypatch.setattr(wglink_launcher.sys, "platform", "win32")
    assert "marker has invalid contents" in wglink_launcher._launcher_error([str(entry)])
    assert reads == ([len(wglink_launcher._NATIVE_SIGNATURE) + 1] if grew_after_stat else [])


def _install_marker(addin, entry, platform):
    root = (entry.parent.parent / "Resources" / "app"
            if platform == "darwin" else entry.parent / "app")
    payload = {"schema": 1, "managedBy": "waveguide-generator",
               "waveguideGeneratorRoot": str(root), "waveguideGeneratorVersion": "test",
               "sourceCommit": "a" * 40, "addinVersion": "0.2.1"}
    path = addin / wglink_launcher.INSTALL_MARKER
    path.write_text(json.dumps(payload))
    return path, payload


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_install_marker_derives_only_fusion_platform_entries(monkeypatch, tmp_path, platform):
    entry = _installed_entry(tmp_path, platform)
    addin = tmp_path / "WGLink"
    addin.mkdir()
    _install_marker(addin, entry, platform)
    monkeypatch.setattr(wglink_launcher.sys, "platform", platform)
    assert wglink_launcher.installed_marker_argv(addin) == (
        None if platform == "linux" else [str(entry)])


@pytest.mark.parametrize("invalid", [
    "absent", "malformed", "non-object", "extra-field", "missing-field", "schema", "bool-schema",
    "owner", "relative", "non-string", "nul", "unnormalized-root", "oversize", "encoding", "nested-json", "symlink",
    "developer", "dangling-developer", "checkout", "missing-entry", "missing-root",
    "wrong-shape", "missing-native-marker", "missing-signature", "non-dir-addin", "symlink-addin",
])
def test_invalid_install_marker_keeps_manual_start_message(monkeypatch, tmp_path, invalid):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_invalid_marker")
    (fixture.ipc / "wg-capabilities.json").unlink()
    addin = fixture.module.wglink_core.ADDIN_DIR
    entry = _installed_entry(tmp_path, "win32")
    marker, payload = _install_marker(addin, entry, "win32")
    if invalid == "absent":
        marker.unlink()
    elif invalid in {"malformed", "non-object", "oversize", "encoding", "nested-json"}:
        marker.write_bytes({"malformed": b"{", "non-object": b"[]",
                            "oversize": b" " * 65537, "encoding": b"\xff",
                            "nested-json": b"[" * 2000 + b"]" * 2000}[invalid])
    elif invalid == "symlink":
        target = tmp_path / "marker.json"
        marker.rename(target)
        marker.symlink_to(target)
    elif invalid in {"developer", "dangling-developer"}:
        developer = addin / wglink_launcher.DEVELOPER_MARKER
        if invalid == "developer":
            developer.write_text("{}")
        else:
            developer.symlink_to(tmp_path / "absent-dev-marker")
    elif invalid == "missing-entry":
        entry.unlink()
    elif invalid == "missing-root":
        (entry.parent / "app").rmdir()
    elif invalid == "wrong-shape":
        entry.rename(entry.with_name("other.exe"))
    elif invalid == "missing-native-marker":
        (entry.parent / "Waveguide Generator.ensure-backend-v1").unlink()
    elif invalid == "missing-signature":
        entry.write_bytes(b"older native image")
    elif invalid == "non-dir-addin":
        marker.unlink()
        addin.rmdir()
        addin.touch()
    elif invalid == "symlink-addin":
        target = tmp_path / "managed-addin"
        addin.rename(target)
        addin.symlink_to(target, target_is_directory=True)
    else:
        if invalid == "extra-field":
            payload["launcher"] = str(entry)
        elif invalid == "missing-field":
            payload.pop("addinVersion")
        elif invalid in {"schema", "bool-schema"}:
            payload["schema"] = 2 if invalid == "schema" else True
        elif invalid == "owner":
            payload["managedBy"] = "external"
        else:
            value = {"relative": "installed/app", "non-string": 7, "nul": "bad\x00root",
                     "unnormalized-root": str(entry.parent / "app" / ".." / "app"),
                     "checkout": str(tmp_path / "checkout")}[invalid]
            if invalid == "checkout":
                Path(value).mkdir()
            payload["waveguideGeneratorRoot"] = value
        marker.write_text(json.dumps(payload))
    monkeypatch.setattr(wglink_launcher.sys, "platform", "win32")
    monkeypatch.setattr(wglink_launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("spawn"))
    assert wglink_launcher.installed_marker_argv(addin) is None
    _run(fixture.module, "send")
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    assert fixture.module._backend_admission.thread is None
    assert "Open Waveguide Generator" in fixture.ui.messages[-1][1]


@pytest.mark.parametrize("symlinked", [False, True])
def test_install_marker_uses_production_addin_folder(monkeypatch, tmp_path, symlinked):
    import test_wglink_addin_lifecycle as lifecycle

    source = Path(__file__).resolve().parents[1] / "fusion-addins" / "WGLink"
    managed = tmp_path / "managed" / "WGLink"
    shutil.copytree(source, managed, ignore=shutil.ignore_patterns("__pycache__"))
    addin = managed
    if symlinked:
        addin = tmp_path / "WGLink"
        addin.symlink_to(managed, target_is_directory=True)
    monkeypatch.setattr(lifecycle, "ADDIN", addin / "WGLink.py")
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_marker_production_folder",
                               preserve_addin_dir=True)
    entry = _installed_entry(tmp_path, "darwin")
    _install_marker(managed, entry, "darwin")
    launcher = fixture.module.wglink_launcher
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    capabilities = fixture.ipc / "wg-capabilities.json"
    ready = capabilities.read_bytes()
    capabilities.unlink()
    calls = []

    def ensure_backend(*args, **kwargs):
        calls.append(args)
        capabilities.write_bytes(ready)

    monkeypatch.setattr(launcher, "ensure_backend", ensure_backend)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("spawn"))
    # Package/core paths resolve, but admission must see the registered folder.
    assert fixture.module.ADDIN_DIR == managed
    assert fixture.module.wglink_core.ADDIN_DIR == managed
    assert launcher.installed_marker_argv(addin) == (
        None if symlinked else [str(entry)])
    marker_folders = []
    real_marker_argv = launcher.installed_marker_argv

    def marker_argv(folder):
        marker_folders.append(folder)
        return real_marker_argv(folder)

    monkeypatch.setattr(launcher, "installed_marker_argv", marker_argv)
    _run(fixture.module, "send")
    assert marker_folders == [addin]
    if symlinked:
        assert fixture.module._backend_admission.thread is None
        assert calls == [] and fixture.exports == [] and _inbox(fixture.ipc) == []
        assert "Open Waveguide Generator" in fixture.ui.messages[-1][1]
    else:
        _finish(fixture)
        assert len(calls) == 1 and calls[0][0] == [str(entry)]
        assert len(fixture.exports) == 1 and len(_inbox(fixture.ipc)) == 1


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_invalid_advertisement_and_marker_preserve_strict_error(monkeypatch, tmp_path, operation):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_invalid_advertisement_and_marker")
    entry = _installed_entry(tmp_path, "darwin")
    marker, _ = _install_marker(fixture.module.wglink_core.ADDIN_DIR, entry, "darwin")
    marker.write_text("{")
    _advertise_launcher(fixture, [str(tmp_path / "missing-entry")])
    launcher = fixture.module.wglink_launcher
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("spawn"))
    _run(fixture.module, operation)
    assert fixture.module._backend_admission.thread is None
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    assert fixture.ui.messages[-1][1] == (
        "Waveguide Generator could not start. Start Waveguide Generator manually, then try again.")
    assert "missing or is not a regular file" in "\n".join(fixture.ui.text_palette.written)


@pytest.mark.parametrize("platform", ["win32", "darwin"])
@pytest.mark.parametrize("operation", ["send", "solve"])
def test_already_collecting_with_marker_argv_does_not_spawn(monkeypatch, tmp_path, platform, operation):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_marker_collecting")
    entry = _installed_entry(tmp_path, platform)
    _install_marker(fixture.module.wglink_core.ADDIN_DIR, entry, platform)
    launcher = fixture.module.wglink_launcher
    monkeypatch.setattr(launcher.sys, "platform", platform)
    assert launcher.launcher_argv(fixture.ipc) is None
    assert launcher.installed_marker_argv(fixture.module.wglink_core.ADDIN_DIR) == [str(entry)]
    calls = _owner_health(monkeypatch, launcher, fixture.ipc.parent.parent)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("spawn"))
    _run(fixture.module, operation)
    _finish(fixture)
    assert len(fixture.exports) == 1 and len(_inbox(fixture.ipc)) == 1
    assert calls == ["http://127.0.0.1:3100/health"]


@pytest.mark.parametrize("oversize", [False, True])
def test_windows_exe_signature_guard_covers_advertisement_and_spawn(monkeypatch, tmp_path, oversize):
    entry = _installed_entry(tmp_path, "win32")
    if oversize:
        with entry.open("ab") as stream:
            stream.truncate(wglink_launcher._MAX_NATIVE_ENTRY_BYTES + 1)
    else:
        entry.write_bytes(b"old native image")
    monkeypatch.setattr(wglink_launcher.sys, "platform", "win32")
    (tmp_path / "wg-capabilities.json").write_text(json.dumps({
        "schemaVersion": 1, "launcher": {"argv": [str(entry)], "ensureBackend": True}}))
    reason = "64 MiB" if oversize else "signature"
    assert wglink_launcher.launcher_argv(tmp_path) is None
    with pytest.raises(wglink_launcher.LauncherError, match=reason):
        wglink_launcher.launcher_argv(tmp_path, strict=True)
    monkeypatch.setattr(wglink_launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("spawn"))
    with pytest.raises(wglink_launcher.LauncherError, match=reason):
        wglink_launcher.ensure_backend([str(entry)], tmp_path, None, threading.Event())


@pytest.mark.parametrize("advertisement", ["valid", "absent-launcher", "invalid"])
def test_capabilities_precedence_and_marker_rescue(monkeypatch, tmp_path, advertisement):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_marker_precedence")
    platform = "darwin"
    entry = _installed_entry(tmp_path / "managed", platform)
    _install_marker(fixture.module.wglink_core.ADDIN_DIR, entry, platform)
    advertised_entry = _installed_entry(tmp_path / "advertised", platform)
    if advertisement != "absent-launcher":
        _advertise_launcher(fixture, [str(advertised_entry if advertisement == "valid"
                                        else tmp_path / "moved-entry")])
    monkeypatch.setattr(wglink_launcher.sys, "platform", platform)
    calls = []
    monkeypatch.setattr(fixture.module.wglink_launcher, "ensure_backend", lambda *args, **kwargs: calls.append(args))
    _run(fixture.module, "send")
    _finish(fixture)
    assert len(calls) == 1
    assert calls[0][0] == [str(advertised_entry if advertisement == "valid" else entry)]
    assert len(fixture.exports) == 1 and len(_inbox(fixture.ipc)) == 1


@pytest.mark.parametrize("platform", ["win32", "darwin"])
@pytest.mark.parametrize("operation", ["send", "solve"])
@pytest.mark.parametrize("fresh", ["compatible", "outdated", "missing-feature", "override", "override-no-root"])
def test_marker_cold_start_without_ipc_resumes_only_after_fresh_gates(
        monkeypatch, tmp_path, platform, operation, fresh):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_marker_cold_start")
    entry = _installed_entry(tmp_path, platform)
    _install_marker(fixture.module.wglink_core.ADDIN_DIR, entry, platform)
    path = fixture.ipc / "wg-capabilities.json"
    ready = json.loads(path.read_text())
    if fresh == "outdated":
        ready["solveCommandDelivery"] = 3
    elif fresh == "missing-feature":
        ready.pop("automaticDomain")
    path.unlink()
    fixture.ipc.rmdir()
    fixture.ipc.parent.rmdir()
    fixture.ipc.parent.parent.rmdir()
    data = fixture.ipc.parent.parent
    settings = data / "cadlink_settings.json"
    monkeypatch.setattr(fixture.module.wglink_workspace, "workspace_root", lambda: (
        Path(json.loads(settings.read_text())["cadLinkPath"]) if settings.exists() else None))
    monkeypatch.setattr(fixture.module.wglink_workspace, "ipc_folder",
                        lambda **kwargs: fixture.ipc if fixture.ipc.is_dir() else None)
    monkeypatch.setattr(wglink_launcher.sys, "platform", platform)
    if fresh.startswith("override"):
        monkeypatch.setenv("WG2_DATA_DIR", str(data))
    assert not wglink_launcher.collecting(fixture.ipc, data)
    assert not data.exists()
    calls = []

    def popen(command, **kwargs):
        assert fixture.exports == [] and not data.exists()
        calls.append(command)
        fixture.ipc.mkdir(parents=True)
        path.write_text(json.dumps(ready))
        if fresh != "override-no-root":
            settings.write_text(json.dumps({"cadLinkPath": str(tmp_path / "workspace"),
                                            "captureDocument": False}))
        kwargs["stdout"].write(json.dumps(_success(data)).encode())
        return types.SimpleNamespace(poll=lambda: 0, returncode=0)

    monkeypatch.setattr(wglink_launcher.subprocess, "Popen", popen)
    _run(fixture.module, operation)
    fixture.module._backend_admission.thread.join(timeout=5)
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    _finish(fixture)
    expected = [str(entry), "--ensure-backend"]
    if fresh.startswith("override"):
        expected += ["--data-dir", str(data)]
    assert calls == [expected]
    if fresh in {"outdated", "missing-feature", "override-no-root"}:
        assert fixture.exports == [] and _inbox(fixture.ipc) == []
        assert {"outdated": "Update Waveguide Generator", "missing-feature": "Update Waveguide Generator",
                "override-no-root": "no selected WGLink folder"}[fresh] in fixture.ui.messages[-1][1]
    else:
        assert len(fixture.exports) == 1
        assert fixture.exports[0]["capture_document"] is False
        assert fixture.exports[0]["output_folder"] == str(tmp_path / "workspace" / "wgreturn")
        assert _inbox(fixture.ipc)[0]["kind"] == (
            "receive_snapshot" if operation == "send" else "prepare_and_solve")


def test_windows_without_appdata_cannot_admit_even_with_marker(monkeypatch, tmp_path):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_marker_no_appdata")
    entry = _installed_entry(tmp_path, "win32")
    _install_marker(fixture.module.wglink_core.ADDIN_DIR, entry, "win32")
    monkeypatch.setattr(wglink_launcher.sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    assert wglink_workspace.data_dir(system="Windows") is None
    monkeypatch.setattr(fixture.module.wglink_workspace, "data_dir", lambda: None)
    monkeypatch.setattr(fixture.module.wglink_workspace, "ipc_folder", lambda: None)
    monkeypatch.setattr(wglink_launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("spawn"))
    _run(fixture.module, "send")
    assert fixture.module._backend_admission.thread is None and fixture.exports == []
    assert "Open Waveguide Generator" in fixture.ui.messages[-1][1]


@pytest.mark.parametrize("cancel", [False, True])
def test_blocked_discovery_releases_admission_on_deadline_or_close(monkeypatch, tmp_path, cancel):
    entered, release = threading.Event(), threading.Event()
    launched = []

    def probe(*args):
        entered.set()
        release.wait(5)
        return False

    monkeypatch.setattr(wglink_launcher, "_collecting_probe", probe)
    monkeypatch.setattr(wglink_launcher, "DISCOVERY_TIMEOUT", 0.1)
    monkeypatch.setattr(wglink_launcher, "ensure_backend", lambda *a, **k: launched.append(True))
    admission = wglink_launcher.BackendAdmission()
    delivered = []
    admission.request(tmp_path, tmp_path, [], None, delivered.append, lambda job: job())
    try:
        assert entered.wait(1)
        if cancel:
            admission.close()
        admission.thread.join(timeout=0.5)
        assert not admission.thread.is_alive()
        assert launched == ([] if cancel else [True])
        assert delivered == ([] if cancel else [None])
    finally:
        release.set()
        admission.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_trickling_health_has_overall_deadline_and_cancellation(monkeypatch, tmp_path, cancel):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    entered, stop = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "10000")
            self.end_headers()
            entered.set()
            try:
                while not stop.is_set():
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    stop.wait(0.05)
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    ipc = tmp_path / "ipc" / "wglink"
    ipc.mkdir(parents=True)
    (ipc / "wg-capabilities.json").write_text(json.dumps({
        "schemaVersion": 1, "solveCommandDelivery": 4, "fusionRequestDelivery": 3}))
    locks = tmp_path / "locks"
    locks.mkdir()
    (locks / "server.pid").write_text(json.dumps({"pid": 42, "port": server.server_port}))
    launched, delivered = [], []
    monkeypatch.setattr(wglink_launcher, "ensure_backend", lambda *a, **k: launched.append(True))
    admission = wglink_launcher.BackendAdmission()
    began = time.monotonic()
    try:
        admission.request(ipc, tmp_path, [], None, delivered.append, lambda job: job())
        assert entered.wait(1)
        if cancel:
            admission.close()
            admission.thread.join(timeout=0.5)
        else:
            admission.thread.join(timeout=wglink_launcher.DISCOVERY_TIMEOUT + 0.8)
        assert not admission.thread.is_alive()
        assert time.monotonic() - began < (1.5 if cancel else wglink_launcher.DISCOVERY_TIMEOUT + 0.8)
        assert launched == ([] if cancel else [True])
        assert delivered == ([] if cancel else [None])
    finally:
        admission.close()
        stop.set()
        server.shutdown()
        server.server_close()
        serving.join(timeout=1)


def test_close_during_first_joined_continuation_skips_second(monkeypatch, tmp_path):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_close_joined")
    _advertise_launcher(fixture, [_installed_entry(tmp_path).as_posix()])
    monkeypatch.setattr(fixture.module.wglink_launcher, "collecting", lambda *a: True)
    _run(fixture.module, "send")
    fixture.module._backend_admission.thread.join(timeout=1)
    _run(fixture.module, "solve")
    real_send = fixture.module.wglink_send.send

    def send(*args, **kwargs):
        report = real_send(*args, **kwargs)
        fixture.module._backend_admission.close()
        return report

    monkeypatch.setattr(fixture.module.wglink_send, "send", send)
    _finish(fixture)
    assert len(fixture.exports) == 1
    assert _inbox(fixture.ipc) == []


@pytest.mark.parametrize("pump", [1, 2, 3])
def test_close_during_resume_event_pumping_prevents_export_or_submit(monkeypatch, tmp_path, pump):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_close_pump")
    _advertise_launcher(fixture, [_installed_entry(tmp_path).as_posix()])
    monkeypatch.setattr(fixture.module.wglink_launcher, "collecting", lambda *a: True)
    _run(fixture.module, "solve")
    calls = []

    def do_events():
        calls.append(True)
        if len(calls) == pump:
            fixture.module._backend_admission.close()

    monkeypatch.setattr(fixture.module.adsk, "doEvents", do_events)
    _finish(fixture)
    assert len(fixture.exports) == (1 if pump == 3 else 0)
    assert _inbox(fixture.ipc) == []


def test_dispatch_exception_is_reported_before_next_press_starts_worker(monkeypatch, tmp_path):
    monkeypatch.setattr(wglink_launcher, "collecting", lambda *a: True)
    admission = wglink_launcher.BackendAdmission()
    done = []

    def failed(job):
        raise RuntimeError("event bridge unavailable")

    admission.request(tmp_path, tmp_path, [], None, done.append, failed)
    first = admission.thread
    first.join(timeout=1)
    assert not first.is_alive() and admission._callbacks == [] and done == []
    with pytest.raises(wglink_launcher.LauncherError, match="event bridge unavailable"):
        admission.request(tmp_path, tmp_path, [], None, done.append, lambda job: job())
    assert admission.thread is first
    admission.request(tmp_path, tmp_path, [], None, done.append, lambda job: job())
    admission.thread.join(timeout=1)
    assert admission.thread is not first and done == [None]


def test_failed_completion_dispatch_drops_queued_start_status(monkeypatch, tmp_path):
    monkeypatch.setattr(wglink_launcher, "collecting", lambda *a: False)
    monkeypatch.setattr(wglink_launcher, "ensure_backend", lambda *a, **k: None)
    admission = wglink_launcher.BackendAdmission()
    queued, shown = [], []

    def dispatch(job):
        if queued:
            raise RuntimeError("event bridge failed")
        queued.append(job)

    admission.request(tmp_path, tmp_path, [], None, lambda error: pytest.fail("continuation"),
                      dispatch, lambda: shown.append(True))
    admission.thread.join(timeout=1)
    assert not admission.thread.is_alive() and admission._callbacks == []
    queued[0]()
    assert shown == []


def test_launch_status_is_modeless_and_only_shown_on_main_event(monkeypatch, tmp_path):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_status")
    _advertise_launcher(fixture, [str(_installed_entry(tmp_path))])
    launcher = fixture.module.wglink_launcher
    monkeypatch.setattr(launcher, "collecting", lambda *a: False)
    monkeypatch.setattr(launcher, "ensure_backend", lambda *a, **k: None)
    main = threading.get_ident()
    real_factory = fixture.ui.createProgressDialog

    def factory():
        assert threading.get_ident() == main
        return real_factory()

    monkeypatch.setattr(fixture.ui, "createProgressDialog", factory)
    _run(fixture.module, "send")
    fixture.module._backend_admission.thread.join(timeout=1)
    assert fixture.ui.progress_dialogs == [] and fixture.ui.messages == []
    _finish(fixture)
    dialog = fixture.ui.progress_dialogs[0]
    assert dialog.events[0][0] == "show"
    assert dialog.events[0][2] == "Starting Waveguide Generator…"
    assert dialog.isCancelButtonShown is False
    assert dialog.events[-1][0] == "hide"


def test_stop_hides_pending_launch_status(monkeypatch, tmp_path):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_stop_status")
    _advertise_launcher(fixture, [str(_installed_entry(tmp_path))])
    launcher = fixture.module.wglink_launcher
    entered = threading.Event()
    monkeypatch.setattr(launcher, "collecting", lambda *a: False)

    def ensure(argv, data_dir, override, cancelled, **kwargs):
        entered.set()
        cancelled.wait(2)

    monkeypatch.setattr(launcher, "ensure_backend", ensure)
    _run(fixture.module, "send")
    try:
        assert entered.wait(1)
        fixture.module.FollowupEventHandler().notify(None)
        dialog = fixture.ui.progress_dialogs[0]
        assert dialog.events[0][2] == "Starting Waveguide Generator…"
        fixture.module._stop_followups(fixture.app)
        fixture.module._backend_admission.thread.join(timeout=1)
        assert not fixture.module._backend_admission.thread.is_alive()
        assert dialog.events[-1][0] == "hide"
        assert fixture.exports == [] and _inbox(fixture.ipc) == []
    finally:
        fixture.module._backend_admission.close()


class _FakeUser32:
    """EnumWindows over ``(hwnd, pid, title, visible, iconic)`` rows."""

    def __init__(self, windows):
        self.windows = {row[0]: row for row in windows}
        self.raised: list[int] = []
        self.restored: list[int] = []

    def EnumWindows(self, visit, _lparam):
        for hwnd in self.windows:
            if not visit(hwnd, 0):
                break
        return True

    def IsWindowVisible(self, hwnd):
        return self.windows[hwnd][3]

    def GetWindowThreadProcessId(self, hwnd, pid):
        pid._obj.value = self.windows[hwnd][1]
        return 1

    def GetWindowTextLengthW(self, hwnd):
        return len(self.windows[hwnd][2])

    def GetWindowTextW(self, hwnd, buffer, _size):
        buffer.value = self.windows[hwnd][2]
        return len(buffer.value)

    def IsIconic(self, hwnd):
        return self.windows[hwnd][4]

    def ShowWindow(self, hwnd, _command):
        self.restored.append(hwnd)
        return True

    def SetForegroundWindow(self, hwnd):
        self.raised.append(hwnd)
        return True


def test_windows_raises_wgs_window_and_never_fusions_own_palette() -> None:
    own = os.getpid()
    user32 = _FakeUser32([
        (1, own, "Waveguide Generator", True, False),  # Fusion's palette
        (2, own + 1, "Waveguide Generator", False, False),  # hidden
        (3, own + 1, "Notes", True, False),
        (4, own + 1, "Horn A — Waveguide Generator", True, True),
    ])

    assert wglink_launcher.bring_wg_forward(platform="win32", user32=user32) is True
    assert user32.restored == [4]
    assert user32.raised == [4]


def test_windows_without_a_wg_window_asks_the_user_to_switch() -> None:
    user32 = _FakeUser32([(1, os.getpid(), "Waveguide Generator", True, False)])

    assert wglink_launcher.bring_wg_forward(platform="win32", user32=user32) is False
    assert user32.raised == []


def test_other_platforms_ask_the_user_to_switch() -> None:
    assert wglink_launcher.bring_wg_forward(platform="linux") is False
