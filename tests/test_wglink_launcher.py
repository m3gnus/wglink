"""Installed launcher admission and the Fusion main-thread continuation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import types
import threading

import pytest

from test_wglink_transfer import _transfer, _run, _inbox

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fusion-addins" / "WGLink"))
import wglink_launcher
import wglink_workspace


def _success(root):
    return {"ok": True, "base_url": "http://127.0.0.1:3100", "pid": 42,
            "data_dir": str(root), "version": "test", "commit": "test", "extra": "ignored"}


def _script(tmp_path, body):
    script = tmp_path / "fake launcher.py"
    script.write_text("#!" + sys.executable + "\nimport json, sys, time, os\nfrom pathlib import Path\n" + body)
    script.chmod(0o755)
    return [sys.executable, str(script)] if os.name == "nt" else [str(script)]


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


def _checked_fixture(monkeypatch, tmp_path, name, *, advertised=4):
    monkeypatch.delenv("WG2_DATA_DIR", raising=False)
    fixture = _transfer(monkeypatch, tmp_path, name, advertised=advertised)
    # Model the production IPC layout so the data root cannot be confused
    # with the separately selected CAD workspace.
    ipc = tmp_path / "data" / "ipc" / "wglink"
    ipc.mkdir(parents=True)
    (ipc / "wg-capabilities.json").write_bytes((fixture.ipc / "wg-capabilities.json").read_bytes())
    fixture.ipc = ipc
    monkeypatch.setattr(fixture.module.wglink_workspace, "ipc_folder", lambda **kwargs: ipc)
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
    assert "Start WG manually" in text and "BackendFailure: specific reason" in text
    assert f"exit {code}" in text


@pytest.mark.parametrize("output,code", [("", 0), ("", 5), ("not json", 0), ("{}\n{}", 0), ("[]", 0)])
def test_native_and_invalid_json_failures_do_not_export(monkeypatch, tmp_path, output, code):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_invalid")
    _advertise_launcher(fixture, _script(tmp_path, f"sys.stdout.write({output!r})\nsys.exit({code})\n"))
    _run(fixture.module, "solve")
    _finish(fixture)
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    assert "JSON" in fixture.ui.messages[-1][1]


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
    monkeypatch.setattr(launcher, "ensure_backend", lambda *args: real_ensure(*args, timeout=0.1))
    _run(fixture.module, "send")
    _finish(fixture)
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    assert "Timed out" in fixture.ui.messages[-1][1]
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
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert size == 65537
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
    assert "Start Waveguide Generator" in fixture.ui.messages[-1][1]
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
    assert "different WG data folder" in fixture.ui.messages[-1][1]


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


def test_missing_installed_entry_is_a_visible_spawn_failure(monkeypatch, tmp_path):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_start_missing_entry")
    _advertise_launcher(fixture, [str(tmp_path / "missing installed launcher")])
    _run(fixture.module, "send")
    _finish(fixture)
    assert fixture.exports == [] and _inbox(fixture.ipc) == []
    assert "Could not run WG's installed launcher" in fixture.ui.messages[-1][1]
    assert "Start WG manually" in fixture.ui.messages[-1][1]


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
    assert "not collecting" in fixture.ui.messages[-1][1]


@pytest.mark.parametrize("operation", ["insert", "update"])
def test_insert_update_never_admit_backend(monkeypatch, tmp_path, operation):
    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_no_admission_" + operation)
    _advertise_launcher(fixture, _script(tmp_path, "raise AssertionError('must not launch')\n"))
    monkeypatch.setattr(fixture.module._backend_admission, "request", lambda *a, **k: pytest.fail("admission"))
    if operation == "insert":
        monkeypatch.setattr(fixture.module, "_choose_bundle", lambda *a: None)
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
    monkeypatch.setattr(launcher, "ensure_backend", lambda *args: real_ensure(*args, timeout=0.1))
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _run(fixture.module, "solve")
        _finish(fixture)
        assert foreign.poll() is None
        assert fixture.exports == [] and _inbox(fixture.ipc) == []
    finally:
        foreign.terminate()
        foreign.wait(timeout=5)
