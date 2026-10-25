"""The request palette: one Send or Solve, followed from WG's status files.

WG writes ``<ipc>/.wg-solve-acks/<id>.status.json`` (docs/REQUEST-STATUS.md);
the add-in reads it off Fusion's main thread, hands each change to the main
thread through its follow-up event, and the palette shows WG's own words
(``wglink_protocol.request_status_text``). The fakes in ``fusion_fakes`` hold
the add-in to Fusion's rules: Fusion objects only on the main thread, a page
that hears nothing before it has loaded, and an event that may not deliver.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import types

import pytest

from fusion_fakes import FakeHTMLPalette, FlakyEvents, MainThreadOnly
from test_wglink_acknowledgement import _ack_payload, _advertise_acks, _claim, _write_ack
from test_wglink_addin_lifecycle import _load_instance
from test_wglink_transfer import SNAPSHOT, SOLVE, _deliver_followups, _inbox, _run, _transfer


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
import wglink_inbox  # noqa: E402
import wglink_protocol  # noqa: E402
import wglink_requests  # noqa: E402


def _advertise_status(ipc: Path, value: object = 1) -> None:
    path = ipc / wglink_inbox.CAPABILITIES_FILENAME
    payload = json.loads(path.read_text())
    payload["requestStatus"] = value
    path.write_text(json.dumps(payload))


def _following(monkeypatch, tmp_path: Path, name: str, operation: str = "solve", **options):
    """A Send or Solve to a WG that writes receipts and request status."""

    fixture = _transfer(monkeypatch, tmp_path, name, **options)
    _advertise_acks(fixture.ipc)
    _advertise_status(fixture.ipc)
    _run(fixture.module, operation)
    [request] = _inbox(fixture.ipc)
    fixture.request = request
    fixture.request_id = request["operationId"]
    return fixture


def _status(fixture, state: str, seq: int, request_id: str | None = None, **extra) -> None:
    rid = request_id or fixture.request_id
    record = {
        "contract": "wg-request-status", "schemaVersion": 1, "seq": seq,
        "requestId": rid, "operationId": rid, "kind": fixture.request["kind"],
        "manifestSha256": fixture.request["manifestSha256"], "documentName": "Horn A",
        "updatedAt": "2026-10-25T10:00:00Z", "state": state, **extra,
    }
    folder = fixture.ipc / wglink_inbox.ACK_DIRECTORY
    folder.mkdir(exist_ok=True)
    (folder / f"{rid}.status.json").write_text(json.dumps(record))


def _palette(fixture) -> FakeHTMLPalette:
    return fixture.ui.palettes.items["hornlab_wglink_requests"]


def _tick(fixture) -> dict:
    """Run the due look, deliver the main-thread work; what the palette shows."""

    due = list(fixture.timers)
    fixture.timers.clear()
    for _delay, function in due:
        function()
    _deliver_followups(fixture.module, fixture.app)
    palette = _palette(fixture)
    if not palette.loaded:
        palette.load()
    return palette.shown


def test_a_solve_moves_through_every_state_in_one_palette(monkeypatch, tmp_path: Path) -> None:
    fixture = _following(monkeypatch, tmp_path, "WGLink_palette_states")
    _claim(fixture, fixture.request_id)
    _write_ack(fixture.ipc, fixture.request_id, _ack_payload(
        fixture.request_id, "accepted", kind=SOLVE, manifestSha256=fixture.request["manifestSha256"]))

    shown = _tick(fixture)
    assert shown["headline"] == "Horn A · Preparing the simulation…"
    assert shown["busy"] is True and shown["terminal"] is False

    steps = [
        ("preparing", {}, "Horn A · Preparing the simulation…"),
        ("queued", {}, "Horn A · Waiting for the solver to start."),
        ("solving", {"progress": {"fraction": 0.25, "frequency": {"done": 12, "total": 36}}},
         "Horn A · Solving · 12 of 36 frequencies"),
        ("solved", {"result": {"jobId": "job-89", "runNumber": 89}, "notice": "fullModel"},
         "Horn A · Solved (run 89)."),
    ]
    for seq, (state, extra, headline) in enumerate(steps, start=1):
        _status(fixture, state, seq, **extra)
        shown = _tick(fixture)
        assert shown["headline"] == headline
        assert fixture.module._recent_outcomes[-1]["outcome"] == state
        if state == "solving":
            assert shown["progress"] == 0.25 and shown["busy"] is False

    assert shown["terminal"] is True
    assert shown["detail"] == wglink_protocol.request_status_text(
        {"state": "solved", "notice": "fullModel", "result": {"jobId": "j"}})["detail"]
    assert [action["id"] for action in shown["actions"]] == ["showResults"]
    assert shown["diagnostics"]["jobId"] == "job-89"
    # One palette for the whole request, no message box, and nothing more to look at.
    assert len(fixture.ui.palettes.added) == 1
    assert fixture.ui.messages == []
    assert fixture.timers == []


def test_wg_needing_the_user_opens_a_closed_palette(monkeypatch, tmp_path: Path) -> None:
    fixture = _following(monkeypatch, tmp_path, "WGLink_palette_needs_you")
    palette = _palette(fixture)
    palette.load()
    palette.close()
    assert fixture.module._requests.visible is False

    _status(fixture, "solving", 1)
    _tick(fixture)
    assert palette.isVisible is False  # routine news does not reopen it
    [(delay, _function)] = fixture.timers
    assert delay == wglink_requests.POLL_HIDDEN_SECONDS

    _status(fixture, "needsYou", 2, prompt={
        "code": "mesh_too_large", "nextStep": "This model meshes to 31,000 triangles.",
        "options": [{"id": "solve", "label": "Solve anyway"}, {"id": "coarser", "label": "Use a coarser mesh"}],
    })
    shown = _tick(fixture)

    assert palette.isVisible is True
    assert shown["headline"] == "Horn A · Waveguide Generator needs you."
    assert shown["detail"] == "This model meshes to 31,000 triangles."
    assert shown["options"] == ["Solve anyway", "Use a coarser mesh"]
    assert [action["id"] for action in shown["actions"]] == ["openWg", "openSourcesMesh"]
    assert fixture.ui.messages == []  # never a modal
    assert fixture.module._requests.visible is True
    _tick(fixture)
    [(delay, _function)] = fixture.timers
    assert delay == wglink_requests.POLL_VISIBLE_SECONDS


def test_the_page_hears_nothing_before_it_loads_and_is_answered_in_full(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _following(monkeypatch, tmp_path, "WGLink_palette_handshake")
    palette = _palette(fixture)
    assert palette.first_paint["headline"].startswith("Horn A · Solve · Preparing the export")
    _status(fixture, "queued", 1)
    due = list(fixture.timers)
    fixture.timers.clear()
    for _delay, function in due:
        function()
    _deliver_followups(fixture.module, fixture.app)
    assert palette.received == []  # nothing was sent to a page that could not hear it

    # Had it been sent, Fusion would have dropped it: the handshake recovers.
    fixture.module._requests_palette_ready = True
    fixture.module._show_request_palette()
    assert len(palette.dropped) == 1 and palette.received == []

    answered = palette.load()
    assert answered["headline"] == "Horn A · Waiting for the solver to start."
    _status(fixture, "solving", 2)
    assert _tick(fixture)["headline"] == "Horn A · Solving…"


def test_a_modal_holds_the_update_and_the_palette_catches_up(monkeypatch, tmp_path: Path) -> None:
    fixture = _following(monkeypatch, tmp_path, "WGLink_palette_modal")
    module = fixture.module
    palette = _palette(fixture)
    palette.load()
    monkeypatch.setattr(module, "_followup_event", object())
    monkeypatch.setattr(module, "_followup_app", fixture.app)
    module._command_busy = True  # a modal is up

    _status(fixture, "queued", 1)
    shown = _tick(fixture)
    assert shown["headline"].startswith("Horn A · Sent to")
    assert len(module._followups) == 1

    fixture.app.fired.clear()
    module._release_command_busy()  # the modal closed
    assert module.FOLLOWUP_EVENT_ID in fixture.app.fired
    _deliver_followups(module, fixture.app)
    assert palette.shown["headline"] == "Horn A · Waiting for the solver to start."


def test_an_event_fusion_did_not_deliver_is_raised_again(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_palette_flaky_event")
    module = fixture.module
    events = FlakyEvents(False, RuntimeError("event queue torn down"), True, False)
    monkeypatch.setattr(module, "_followup_event", object())
    monkeypatch.setattr(module, "_followup_app", types.SimpleNamespace(fireCustomEvent=events))
    module._followups.append(lambda: None)  # a job waits for the main thread

    module._raise_followup_event()
    [(delay, again)] = fixture.timers
    assert delay == module.FOLLOWUP_RETRY_SECONDS
    fixture.timers.clear()
    again()  # raises inside: still not delivered
    [(_delay, again)] = fixture.timers
    fixture.timers.clear()
    again()

    assert events.fired == [module.FOLLOWUP_EVENT_ID] * 3
    assert fixture.timers == []  # delivered: nothing more

    # Nothing waiting: a False answer is not chased.
    module._followups.clear()
    module._raise_followup_event()
    assert fixture.timers == []


def test_a_failed_look_or_render_never_ends_the_following(monkeypatch, tmp_path: Path) -> None:
    fixture = _following(monkeypatch, tmp_path, "WGLink_palette_survives")
    module = fixture.module
    real_poll = module.wglink_requests.poll
    calls: list[int] = []

    def failing_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("folder went away")
        return real_poll(*args, **kwargs)

    monkeypatch.setattr(module.wglink_requests, "poll", failing_once)
    _tick(fixture)
    assert len(fixture.timers) == 1  # re-armed after the failed look

    real_show = module._show_request_palette
    monkeypatch.setattr(module, "_show_request_palette",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("render")))
    _status(fixture, "queued", 1)
    _tick(fixture)
    assert len(fixture.timers) == 1
    monkeypatch.setattr(module, "_show_request_palette", real_show)

    _status(fixture, "solving", 2)
    assert _tick(fixture)["headline"] == "Horn A · Solving…"


def test_the_look_off_the_main_thread_touches_nothing_of_fusion(monkeypatch, tmp_path: Path) -> None:
    fixture = _following(monkeypatch, tmp_path, "WGLink_palette_thread")
    module = fixture.module
    MainThreadOnly.VIOLATIONS.clear()
    main = threading.get_ident()
    off_main: list[str] = []

    def guarded(name, real):
        def call(*args, **kwargs):
            if threading.get_ident() != main:
                off_main.append(name)
            return real(*args, **kwargs)
        return call

    monkeypatch.setattr(module, "_app", guarded("_app", module._app))
    monkeypatch.setattr(module, "_ui", guarded("_ui", module._ui))
    monkeypatch.setattr(module, "_log", guarded("_log", module._log))
    monkeypatch.setattr(module.adsk.core.Application, "get", guarded("Application.get", module.adsk.core.Application.get))
    fired: list[int] = []
    monkeypatch.setattr(module, "_followup_event", object())
    # fireCustomEvent is the one Fusion call that is safe from any thread.
    monkeypatch.setattr(module, "_followup_app", types.SimpleNamespace(
        fireCustomEvent=lambda *_a: fired.append(threading.get_ident()) or True))
    _status(fixture, "solving", 1)
    [(_delay, look)] = fixture.timers
    fixture.timers.clear()

    worker = threading.Thread(target=look)
    worker.start()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert off_main == [] and MainThreadOnly.VIOLATIONS == []
    assert fired and fired[0] != main
    assert module._requests.current()["status"]["state"] == "solving"
    _deliver_followups(module, fixture.app)  # the render, on the main thread
    assert off_main == []


def test_stop_cancels_and_joins_every_look(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_palette_stop", real_timers=True)
    module = fixture.module
    _advertise_acks(fixture.ipc)
    _advertise_status(fixture.ipc)
    module.run(None)
    _run(module, "solve")
    timers = list(module._timers)
    assert timers and all(timer.is_alive() for timer in timers)

    module.stop(None)

    assert not any(timer.is_alive() for timer in timers)
    assert module._timers == [] and module._requests.current() is None


def test_a_wg_that_writes_no_status_ends_at_follow_it_in_wg(monkeypatch, tmp_path: Path) -> None:
    """WG 0.3.5 writes receipts but no status: a status file there is not read."""

    fixture = _transfer(monkeypatch, tmp_path, "WGLink_palette_old_wg")
    _advertise_acks(fixture.ipc)
    _run(fixture.module, "solve")
    [request] = _inbox(fixture.ipc)
    fixture.request, fixture.request_id = request, request["operationId"]
    _claim(fixture, fixture.request_id)
    _status(fixture, "solving", 1)
    _write_ack(fixture.ipc, fixture.request_id, _ack_payload(
        fixture.request_id, "accepted", kind=SOLVE, manifestSha256=request["manifestSha256"]))

    shown = _tick(fixture)

    assert shown["headline"] == "Horn A · Accepted. Follow progress in Waveguide Generator."
    assert shown["terminal"] is True and fixture.timers == []
    assert fixture.module._requests.current()["status"] is None


def test_a_capability_file_that_goes_missing_does_not_downgrade_the_request(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _following(monkeypatch, tmp_path, "WGLink_palette_capability_race")
    (fixture.ipc / wglink_inbox.CAPABILITIES_FILENAME).unlink()
    _status(fixture, "queued", 1)

    assert _tick(fixture)["headline"] == "Horn A · Waiting for the solver to start."


def test_an_older_write_is_ignored(monkeypatch, tmp_path: Path) -> None:
    fixture = _following(monkeypatch, tmp_path, "WGLink_palette_seq")
    _status(fixture, "solving", 5)
    assert _tick(fixture)["headline"] == "Horn A · Solving…"
    _status(fixture, "queued", 4)
    assert _tick(fixture)["headline"] == "Horn A · Solving…"


def test_a_late_status_for_an_earlier_request_never_changes_the_current_one(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _following(monkeypatch, tmp_path, "WGLink_palette_two_requests")
    first = fixture.request_id
    fixture.module._force_new_request = True
    _run(fixture.module, "solve")
    second = fixture.module._requests.current()["requestId"]
    assert second != first

    _status(fixture, "solved", 9, request_id=first, result={"jobId": "job-1", "runNumber": 1})
    shown = _tick(fixture)

    assert shown["diagnostics"]["requestId"] == second
    assert "Solved" not in shown["headline"]
    # And an update computed for the first request is refused outright.
    stale = dict(fixture.module._requests.current(), requestId=first)
    assert fixture.module._requests.replace(stale)[1] is False


def test_a_restart_resumes_the_request_without_sending_it_again(monkeypatch, tmp_path: Path) -> None:
    fixture = _following(monkeypatch, tmp_path, "WGLink_palette_restart_a")
    _claim(fixture, fixture.request_id)
    _status(fixture, "solving", 1)
    _tick(fixture)
    fixture.module.stop(None)

    second = _load_instance(monkeypatch, "WGLink_palette_restart_b", fixture.ui, fixture.app)
    monkeypatch.setattr(second.wglink_workspace, "ipc_folder", lambda **_kwargs: fixture.ipc)
    monkeypatch.setattr(second.wglink_workspace, "workspace_root", lambda: tmp_path / "workspace")
    timers: list[tuple[float, object]] = []
    monkeypatch.setattr(second, "_start_timer", lambda delay, function: timers.append((delay, function)))
    second.run(None)
    try:
        resumed = second._requests.current()
        assert resumed["requestId"] == fixture.request_id
        assert [delay for delay, _function in timers] == [0.0]
        palette = fixture.ui.palettes.items["hornlab_wglink_requests"]
        assert palette.isVisible is True
        assert palette.load()["headline"] == "Horn A · Solving…"
        _status(fixture, "solved", 2, result={"jobId": "job-7", "runNumber": 7})
        for _delay, function in timers:
            function()
        _deliver_followups(second, fixture.app)
        assert palette.shown["headline"] == "Horn A · Solved (run 7)."
        # Nothing was submitted again.
        assert _inbox(fixture.ipc) == []
    finally:
        second.stop(None)


def test_a_request_whose_export_never_finished_reads_not_sent_after_a_restart(tmp_path: Path) -> None:
    record = wglink_requests.new_request("req-1", SOLVE, "Horn A")
    wglink_requests.save(tmp_path, record)

    loaded = wglink_requests.load(tmp_path)

    assert loaded["local"] == wglink_requests.NOT_SENT
    assert wglink_requests.terminal(loaded)
    assert wglink_requests.view(loaded)["headline"] == "Horn A was not sent to Waveguide Generator."


def test_the_palette_buttons(monkeypatch, tmp_path: Path) -> None:
    fixture = _following(monkeypatch, tmp_path, "WGLink_palette_buttons")
    module = fixture.module
    palette = _palette(fixture)
    palette.load()
    raised: list[bool] = []
    monkeypatch.setattr(module.wglink_launcher, "bring_wg_forward", lambda: raised.append(True) or False)
    scheduled: list[object] = []
    monkeypatch.setattr(module, "_schedule_followup", scheduled.append)

    palette.click("openWg")
    assert raised == [True]
    assert palette.shown["notes"][-1] == "Switch to Waveguide Generator."

    palette.click("openSourcesMesh")
    assert scheduled == [module._open_sources_and_mesh]

    executed: list[str] = []
    fixture.ui.commandDefinitions.items["hornlab_wglink_solve"] = types.SimpleNamespace(
        isValid=True, execute=lambda: executed.append("solve"))
    palette.click("sendAgain")
    assert module._force_new_request is True
    scheduled[-1]()
    assert executed == ["solve"]


def test_requests_reopens_the_palette(monkeypatch, tmp_path: Path) -> None:
    fixture = _following(monkeypatch, tmp_path, "WGLink_palette_reopen")
    module = fixture.module
    palette = _palette(fixture)
    palette.close()
    added: list[object] = []
    command = types.SimpleNamespace(
        commandInputs=types.SimpleNamespace(), execute=types.SimpleNamespace(add=added.append))

    module.CommandCreatedHandler("requests").notify(types.SimpleNamespace(command=command))
    [handler] = added
    handler.notify(types.SimpleNamespace(command=command))

    assert palette.isVisible is True
    assert fixture.ui.messages == []


@pytest.mark.parametrize("kind", [SNAPSHOT, SOLVE])
@pytest.mark.parametrize("name", ["Horn A", "Gate horn", None])
def test_every_local_line_is_user_text(kind: str, name: str | None) -> None:
    base = wglink_requests.new_request("req-1", kind, name)
    submitted = wglink_requests.submitted(
        base, manifest_sha256="sha256:x", acknowledgement=True, request_status=True, confirmed=True)
    records = [base, wglink_requests.not_sent(base), submitted,
               dict(submitted, receipt={"outcome": "accepted", "reason": None},
                    capabilities={"acknowledgement": True, "requestStatus": False})]
    records += [dict(submitted, local=local) for local in (
        wglink_requests.UNCONFIRMED, wglink_requests.NOT_TAKEN, wglink_requests.TAKEN,
        wglink_requests.TAKEN_UNCONFIRMED)]
    for record in records:
        shown = wglink_requests.view(record)
        for text in [shown["headline"], shown["detail"], *shown["notes"], *shown["options"],
                     *(action["label"] for action in shown["actions"])]:
            if text:
                assert wglink_protocol.user_text_violations(text, names=(name,)) == [], text
        assert {action["id"] for action in shown["actions"]} <= set(wglink_requests.ACTION_IDS)


def test_a_status_file_being_replaced_is_read_again_once(monkeypatch, tmp_path: Path) -> None:
    rid = "req-1"
    folder = tmp_path / wglink_inbox.ACK_DIRECTORY
    folder.mkdir()
    (folder / f"{rid}.status.json").write_text(json.dumps({
        "contract": "wg-request-status", "schemaVersion": 1, "seq": 1, "requestId": rid,
        "state": "queued"}))
    real = Path.read_text
    attempts: list[int] = []

    def held_once(self, *args, **kwargs):
        if self.name.endswith(".status.json"):
            attempts.append(1)
            if len(attempts) == 1:
                raise PermissionError("held by the writer")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(wglink_inbox, "STATUS_READ_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(Path, "read_text", held_once)

    assert wglink_inbox.read_request_status(tmp_path, rid)["state"] == "queued"
    assert len(attempts) == 2
    assert wglink_inbox.read_request_status(tmp_path, "req-2") is None
    assert wglink_inbox.read_request_status(tmp_path, "../escape") is None


@pytest.mark.parametrize("value", [None, 0, True, "1"], ids=["absent", "zero", "bool", "string"])
def test_only_an_advertised_request_status_is_read(tmp_path: Path, value: object) -> None:
    (tmp_path / wglink_inbox.CAPABILITIES_FILENAME).write_text(json.dumps(
        {"schemaVersion": 1, "solveCommandDelivery": 4, **({} if value is None else {"requestStatus": value})}))
    assert wglink_inbox.wg_reports_request_status(tmp_path) is False
    _advertise_status(tmp_path, 1)
    assert wglink_inbox.wg_reports_request_status(tmp_path) is True
