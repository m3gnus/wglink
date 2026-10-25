"""The dev-only in-Fusion test runner, without Fusion.

Live scenarios (sources-mesh-baseline) can only be proven in Fusion; these
cover the runner's request/result files, its error handling, the dev-sync
gate and the reload scenario.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import types

from test_wglink_addin_lifecycle import _Definitions, _Panels, _UI, _load_instance

ADDIN_DIR = Path(__file__).resolve().parents[1] / "fusion-addins" / "WGLink"


def test_runner_runs_scenarios_over_ticks_and_records_failures(tmp_path, monkeypatch):
    # The runner and its filesystem writer require no Fusion imports.
    monkeypatch.syspath_prepend(str(ADDIN_DIR))
    dev_tests = importlib.import_module("wglink_dev_tests")
    log = []
    context = types.SimpleNamespace(app=lambda: types.SimpleNamespace(
        version="2.0.test", activeDocument=types.SimpleNamespace(name="Horn")))

    def broken(run):
        yield
        raise RuntimeError("boom")

    def failing(run):
        run.check("always false", False, "detail")
        yield

    scenarios = {**dev_tests.scenarios(context), "broken": broken, "failing": failing}
    assert set(dev_tests.scenarios(context)) == {"noop"}  # no add-in context: noop only
    runner = dev_tests.Runner(tmp_path, scenarios, log.append)
    requests, results = dev_tests.folders(tmp_path)
    assert not runner.pending()
    for request_id, scenario in (("a", "noop"), ("b", "missing"), ("c", "broken"), ("d", "failing")):
        dev_tests.write_json(requests / f"{request_id}.json", {"id": request_id, "scenario": scenario})
    for _ in range(12):
        runner.tick()
    outcome = {path.stem: json.loads(path.read_text()) for path in results.glob("*.json")}
    assert outcome["a"]["status"] == "passed" and outcome["a"]["fusionVersion"] == "2.0.test"
    assert outcome["b"]["status"] == "error" and "unknown scenario" in outcome["b"]["error"]
    assert outcome["c"]["status"] == "error" and "boom" in outcome["c"]["error"]
    assert outcome["d"]["status"] == "failed" and "always false" in outcome["d"]["error"]
    assert not list(requests.glob("*.json")) and not runner.pending()


def _started(monkeypatch, tmp_path, name, enabled):
    panels, definitions = _Panels(), _Definitions(reserve_ids=False)
    module = _load_instance(monkeypatch, name, _UI(panels, definitions))
    monkeypatch.setattr(module, "_dev_tests_enabled", lambda: enabled)
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: tmp_path)
    app = module._app()
    module.run(None)
    return module, app, panels


def test_the_runner_starts_only_in_a_dev_sync_and_rides_the_follow_up_event(monkeypatch, tmp_path):
    counts = []
    for enabled in (False, True):
        module, app, panels = _started(monkeypatch, tmp_path, f"WGLink_dev_tests_{enabled}", enabled)
        try:
            counts.append(panels.itemById(module.PANEL_ID).controls.count)
            runner = module._dev_tests.get("runner")
            assert (runner is not None) is enabled
            # No event or thread of its own: one of WGLink's one-shot timers.
            assert module.FOLLOWUP_EVENT_ID in app.events and not [event for event in app.events if "dev" in event]
            polls = [timer for timer in module._timers if timer.function is module._poll_dev_tests]
            assert bool(polls) is enabled
            if enabled:
                dev_tests = importlib.import_module(f"{module._registration_package_name}.wglink_dev_tests")
                requests, results = dev_tests.folders(tmp_path)
                dev_tests.write_json(requests / "n.json", {"id": "n", "scenario": "noop"})
                fired = len(app.fired)
                module._poll_dev_tests()  # what the timer does: queue a tick, raise the event
                assert len(app.fired) == fired + 1 and runner.tick in module._followups
                for _ in range(3):
                    module._run_followups()
                    module._poll_dev_tests()
                assert json.loads((results / "n.json").read_text())["status"] == "passed"
        finally:
            module.stop(None)
        assert module._dev_tests == {}
        assert all(not timer.is_alive() for timer in module._timers)
    assert counts[0] == counts[1]  # the runner adds no command to the panel
    # The repository checkout never carries the marker, so CI never starts it.
    fresh = _load_instance(monkeypatch, "WGLink_dev_tests_gate", _UI(_Panels(), _Definitions(reserve_ids=False)))
    assert fresh._dev_tests_enabled() is False


def test_reload_refreshes_the_scenarios_in_place(monkeypatch, tmp_path):
    module, _app, _panels = _started(monkeypatch, tmp_path, "WGLink_dev_tests_reload", True)
    try:
        runner = module._dev_tests["runner"]
        old = runner.scenarios
        assert set(old) == {"noop", "reload", "sources-mesh-baseline"}
        dev_tests = importlib.import_module(f"{module._registration_package_name}.wglink_dev_tests")
        requests, results = dev_tests.folders(tmp_path)
        dev_tests.write_json(requests / "r.json", {"id": "r", "scenario": "reload",
                                                       "modules": ["wglink_mesh_dialog"]})
        for _ in range(4):
            runner.tick()
        result = json.loads((results / "r.json").read_text())
        assert result["status"] == "passed", result
        assert result["reloaded"] == ["wglink_mesh_dialog", "wglink_dev_tests"]
        # Reload keeps ordinary module dependencies, without injecting a host
        # or letting a helper reach into arbitrary entry-module globals.
        assert not hasattr(module.wglink_mesh_dialog, "host")
        state = module.wglink_mesh_dialog.MeshDialogState(module._mesh_dialog_services())
        assert state.services.command_name == module.COMMANDS["source"][1]
        assert runner.scenarios is not old and set(runner.scenarios) == set(old)
    finally:
        module.stop(None)
