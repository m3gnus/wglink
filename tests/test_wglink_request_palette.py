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


def test_the_palette_buttons_run_after_the_pages_message_returns(monkeypatch, tmp_path: Path) -> None:
    fixture = _following(monkeypatch, tmp_path, "WGLink_palette_buttons")
    module = fixture.module
    palette = _palette(fixture)
    palette.load()
    raised: list[bool] = []
    monkeypatch.setattr(module.wglink_launcher, "bring_wg_forward", lambda: raised.append(True) or False)
    opened: list[bool] = []
    monkeypatch.setattr(module, "_open_sources_and_mesh", lambda: opened.append(True))
    sent_inside: list[int] = []
    real_send = palette.sendInfoToHTML

    def guarded_send(action, data):
        sent_inside.append(dispatching[0])
        return real_send(action, data)

    dispatching = [0]
    real_dispatch = palette._send

    def dispatch(action, data=""):
        dispatching[0] += 1
        try:
            return real_dispatch(action, data)
        finally:
            dispatching[0] -= 1

    object.__setattr__(palette, "sendInfoToHTML", guarded_send)
    object.__setattr__(palette, "_send", dispatch)

    palette.click("openWg")
    assert raised == []  # nothing ran inside the page's own message
    _deliver_followups(module, fixture.app)
    assert raised == [True]
    assert palette.shown["notes"][-1] == "Switch to Waveguide Generator."
    assert sent_inside and all(depth == 0 for depth in sent_inside)

    palette.click("openSourcesMesh")
    _deliver_followups(module, fixture.app)
    assert opened == [True]

    executed: list[str] = []
    fixture.ui.commandDefinitions.items["hornlab_wglink_solve"] = types.SimpleNamespace(
        isValid=True, execute=lambda: executed.append("solve"))
    palette.click("sendAgain")
    _deliver_followups(module, fixture.app)
    assert executed == ["solve"]
    assert module._force_new_request[0] == "solve"


def test_requests_reopens_the_palette_without_running_a_command(monkeypatch, tmp_path: Path) -> None:
    fixture = _following(monkeypatch, tmp_path, "WGLink_palette_reopen")
    module = fixture.module
    palette = _palette(fixture)
    palette.close()
    added: list[object] = []
    command = types.SimpleNamespace(
        commandInputs=types.SimpleNamespace(), execute=types.SimpleNamespace(add=added.append))

    module.CommandCreatedHandler("requests").notify(types.SimpleNamespace(command=command))

    assert palette.isVisible is True
    assert added == []  # no execute: nothing changes, nothing to undo
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
    records.append(dict(submitted, receipt={"outcome": "accepted", "reason": None}, quiet=True))
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
    question = wglink_requests.repeat_question(base)
    assert wglink_protocol.user_text_violations(question, names=(name,)) == []


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


# -- no dead ends: every state still under way has a way on --------------------


def _submitted(tmp_path: Path, *, confirmed: bool = True, acknowledgement: bool = True,
               request_status: bool = True, kind: str = SOLVE) -> dict:
    (tmp_path / wglink_inbox.SOLVE_REQUESTS_DIRECTORY).mkdir(exist_ok=True)
    (tmp_path / wglink_inbox.CAPABILITIES_FILENAME).write_text(json.dumps({
        "schemaVersion": 1, "solveCommandDelivery": 4,
        **({"solveAcknowledgement": 1} if acknowledgement else {}),
        **({"requestStatus": 1} if request_status else {})}))
    request = wglink_requests.new_request("req-1", kind, "Horn A", now=1.0)
    return wglink_requests.submitted(request, manifest_sha256="sha256:x", confirmed=confirmed,
                                     acknowledgement=acknowledgement,
                                     request_status=request_status, now=1.0)


def _ids(shown: dict) -> list[str]:
    return [action["id"] for action in shown["actions"]]


@pytest.mark.parametrize("acknowledgement", [True, False], ids=["receipts", "old-wg"])
def test_an_unconfirmed_write_is_never_read_as_picked_up(tmp_path: Path, acknowledgement: bool) -> None:
    request = _submitted(tmp_path, confirmed=False, acknowledgement=acknowledgement,
                         request_status=acknowledgement)

    for now in (wglink_inbox.ACK_WAIT_SECONDS + 2, wglink_requests.QUIET_SECONDS + 5):
        request = wglink_requests.poll(request, tmp_path, now=now)
        shown = wglink_requests.view(request, now=now)
        assert request["local"] == wglink_requests.UNCONFIRMED
        assert "picked up" in shown["headline"] and "has not" in shown["headline"]
        assert shown["terminal"] is False and _ids(shown) == ["openWg", "sendAgain"]
    tracker = wglink_requests.RequestTracker()
    tracker.begin(request)
    assert tracker.poll_interval(now=wglink_requests.QUIET_SECONDS + 5) == wglink_requests.POLL_QUIET_SECONDS
    assert tracker.poll_interval(now=wglink_requests.FOLLOW_LIMIT_SECONDS + 5) is None


def test_taken_but_unconfirmed_offers_solve_again(tmp_path: Path) -> None:
    request = wglink_requests.poll(_submitted(tmp_path), tmp_path, now=wglink_inbox.ACK_WAIT_SECONDS + 2)

    shown = wglink_requests.view(request, now=wglink_inbox.ACK_WAIT_SECONDS + 2)
    assert request["local"] == wglink_requests.TAKEN_UNCONFIRMED
    assert _ids(shown) == ["openWg", "sendAgain"] and shown["busy"] is False


@pytest.mark.parametrize("held", [
    {"receipt": {"outcome": "accepted", "reason": None}},
    {"status": {"state": "preparing"}}, {"status": {"state": "queued"}},
    {"status": {"state": "solving"}}, {"status": {"state": "somethingNewer"}},
], ids=["accepted-no-status", "preparing", "queued", "solving", "unknown-state"])
def test_a_request_wg_stops_reporting_on_goes_quiet_with_a_way_on(tmp_path: Path, held: dict) -> None:
    request = _submitted(tmp_path)
    if "status" in held:
        request["status"] = wglink_protocol.validate_request_status({
            "contract": "wg-request-status", "schemaVersion": 1, "seq": 1,
            "requestId": "req-1", **held["status"]})
    else:
        request.update(held)
    early = wglink_requests.view(wglink_requests.poll(request, tmp_path, now=5.0), now=5.0)
    assert early["terminal"] is False and "for a while" not in early["headline"]

    quiet = wglink_requests.poll(request, tmp_path, now=1.0 + wglink_requests.QUIET_SECONDS)

    shown = wglink_requests.view(quiet, now=1.0 + wglink_requests.QUIET_SECONDS)
    assert shown["headline"] == "Waveguide Generator has not reported on Horn A for a while."
    assert _ids(shown) == ["openWg", "sendAgain"] and shown["busy"] is False
    tracker = wglink_requests.RequestTracker()
    tracker.begin(quiet)
    assert tracker.poll_interval(now=1.0 + wglink_requests.QUIET_SECONDS) == wglink_requests.POLL_QUIET_SECONDS


def _write_status(tmp_path: Path, seq: int, state: str = "solving") -> None:
    folder = tmp_path / wglink_inbox.ACK_DIRECTORY
    folder.mkdir(exist_ok=True)
    (folder / "req-1.status.json").write_text(json.dumps({
        "contract": "wg-request-status", "schemaVersion": 1, "seq": seq,
        "requestId": "req-1", "documentName": "Horn A", "state": state}))


def test_a_clean_shutdown_is_quiet_at_once_and_a_restart_is_not(tmp_path: Path) -> None:
    request = _submitted(tmp_path)
    _write_status(tmp_path, 1)
    request = wglink_requests.poll(request, tmp_path, now=10.0)
    capabilities = tmp_path / wglink_inbox.CAPABILITIES_FILENAME
    running = json.loads(capabilities.read_text())
    capabilities.write_text(json.dumps({**running, wglink_protocol.CAPABILITY_STOPPED_AT: "2026-10-25T10:00:00Z"}))

    stopped = wglink_requests.poll(request, tmp_path, now=12.0)
    assert stopped["quiet"] is True
    assert "for a while" in wglink_requests.view(stopped, now=12.0)["headline"]

    capabilities.write_text(json.dumps(running))  # WG started again ...
    _write_status(tmp_path, 2)  # ... and rewrote every unfinished status at once
    back = wglink_requests.poll(stopped, tmp_path, now=20.0)
    assert back["quiet"] is False
    assert wglink_requests.view(back, now=20.0)["headline"] == "Horn A · Solving…"


def test_a_crashed_wg_is_quiet_after_two_missed_rewrites(tmp_path: Path) -> None:
    request = _submitted(tmp_path)
    _write_status(tmp_path, 1)
    request = wglink_requests.poll(request, tmp_path, now=10.0)

    # The capability file stays, unmarked: a crash. Only the missed rewrites tell.
    late = wglink_requests.poll(request, tmp_path, now=10.0 + wglink_requests.QUIET_WITHOUT_WG_SECONDS)
    assert late["quiet"] is False
    gone = wglink_requests.poll(late, tmp_path, now=11.0 + wglink_requests.QUIET_WITHOUT_WG_SECONDS)
    assert gone["quiet"] is True


def test_the_stopped_marker_is_read_once_in_the_protocol(tmp_path: Path) -> None:
    assert wglink_protocol.capabilities_stopped({"stoppedAt": "2026-10-25T10:00:00Z"}) is True
    for value in ({}, {"stoppedAt": ""}, {"stoppedAt": 1}, None, "x"):
        assert wglink_protocol.capabilities_stopped(value) is False
    assert wglink_inbox.wg_stopped(tmp_path) is False  # no file


def test_wgs_rhythm_keeps_a_long_solve_from_going_quiet(tmp_path: Path) -> None:
    """WG rewrites an unfinished request every minute with a higher seq."""

    request = _submitted(tmp_path)
    folder = tmp_path / wglink_inbox.ACK_DIRECTORY
    folder.mkdir()
    for minute in range(1, 61):  # an hour of solving, one frequency an hour
        (folder / "req-1.status.json").write_text(json.dumps({
            "contract": "wg-request-status", "schemaVersion": 1, "seq": minute,
            "requestId": "req-1", "documentName": "Horn A", "state": "solving"}))
        now = 1.0 + 60.0 * minute
        request = wglink_requests.poll(request, tmp_path, now=now)
        assert request["quiet"] is False
        assert "for a while" not in wglink_requests.view(request, now=now)["headline"]


def test_an_equal_seq_is_taken_but_is_not_news(tmp_path: Path) -> None:
    request = _submitted(tmp_path)
    folder = tmp_path / wglink_inbox.ACK_DIRECTORY
    folder.mkdir()
    path = folder / "req-1.status.json"
    path.write_text(json.dumps({"contract": "wg-request-status", "schemaVersion": 1, "seq": 3,
                                "requestId": "req-1", "documentName": "Horn A", "state": "queued"}))
    first = wglink_requests.poll(request, tmp_path, now=10.0)
    path.write_text(json.dumps({"contract": "wg-request-status", "schemaVersion": 1, "seq": 3,
                                "requestId": "req-1", "documentName": "Horn A", "state": "solving"}))

    same = wglink_requests.poll(first, tmp_path, now=50.0)

    assert same["status"]["state"] == "solving"
    assert same["heardAt"] == 10.0


def test_news_after_quiet_moves_the_request_on(tmp_path: Path) -> None:
    request = _submitted(tmp_path)
    request["receipt"] = {"outcome": "accepted", "reason": None}
    quiet = wglink_requests.poll(request, tmp_path, now=1.0 + wglink_requests.QUIET_SECONDS)
    folder = tmp_path / wglink_inbox.ACK_DIRECTORY
    folder.mkdir()
    (folder / "req-1.status.json").write_text(json.dumps({
        "contract": "wg-request-status", "schemaVersion": 1, "seq": 2, "requestId": "req-1",
        "documentName": "Horn A", "state": "solving"}))

    moving = wglink_requests.poll(quiet, tmp_path, now=2.0 + wglink_requests.QUIET_SECONDS)

    assert moving["quiet"] is False
    assert wglink_requests.view(moving, now=2.0 + wglink_requests.QUIET_SECONDS)["headline"] == "Horn A · Solving…"


def test_wg_needing_the_user_is_never_busy_or_quiet(tmp_path: Path) -> None:
    request = _submitted(tmp_path)
    request["status"] = wglink_protocol.validate_request_status({
        "contract": "wg-request-status", "schemaVersion": 1, "seq": 1, "requestId": "req-1",
        "state": "needsYou", "prompt": {"code": "mesh_too_large", "nextStep": "Too many triangles."}})

    later = wglink_requests.poll(request, tmp_path, now=1.0 + 3 * wglink_requests.QUIET_SECONDS)

    shown = wglink_requests.view(later)
    assert later["quiet"] is False and shown["busy"] is False


def test_wgs_try_again_after_could_not_finish_is_seen_for_an_hour(tmp_path: Path) -> None:
    request = _submitted(tmp_path)
    folder = tmp_path / wglink_inbox.ACK_DIRECTORY
    folder.mkdir()

    def write(seq: int, state: str, **extra) -> None:
        (folder / "req-1.status.json").write_text(json.dumps({
            "contract": "wg-request-status", "schemaVersion": 1, "seq": seq, "requestId": "req-1",
            "documentName": "Horn A", "state": state, **extra}))

    write(1, "couldNotFinish", phase="preparing", failure={"recorded": True})
    stopped = wglink_requests.poll(request, tmp_path, now=10.0)
    assert wglink_requests.view(stopped)["terminal"] is True
    tracker = wglink_requests.RequestTracker()
    tracker.begin(stopped)
    assert tracker.poll_interval(now=20.0) == wglink_requests.POLL_QUIET_SECONDS

    write(2, "preparing")
    again = wglink_requests.poll(stopped, tmp_path, now=600.0)
    assert wglink_requests.view(again, now=600.0)["headline"] == "Horn A · Preparing the simulation…"

    write(3, "couldNotFinish", phase="preparing", failure={"recorded": True})
    final = wglink_requests.poll(again, tmp_path, now=700.0)
    write(4, "preparing")
    assert wglink_requests.poll(final, tmp_path, now=700.0 + wglink_requests.RETRY_WATCH_SECONDS + 1) == final
    tracker.begin(final)
    assert tracker.poll_interval(now=700.0 + wglink_requests.RETRY_WATCH_SECONDS + 1) is None


def test_an_old_record_is_not_resumed(tmp_path: Path) -> None:
    request = _submitted(tmp_path)
    wglink_requests.save(tmp_path, request)

    assert wglink_requests.load(tmp_path, now=2.0)["requestId"] == "req-1"
    assert wglink_requests.load(tmp_path, now=2.0 + wglink_requests.RECORD_MAX_AGE_SECONDS) is None


def test_a_quiet_request_resumes_at_start_up_without_opening_the_palette(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _following(monkeypatch, tmp_path, "WGLink_palette_quiet_restart_a")
    _claim(fixture, fixture.request_id)
    _write_ack(fixture.ipc, fixture.request_id, _ack_payload(
        fixture.request_id, "accepted", kind=SOLVE, manifestSha256=fixture.request["manifestSha256"]))
    _tick(fixture)
    record = wglink_requests.load(fixture.ipc)
    record.update(quiet=True, heardAt=record["heardAt"] - wglink_requests.QUIET_SECONDS)
    wglink_requests.save(fixture.ipc, record)
    fixture.module.stop(None)
    fixture.ui.palettes.items.clear()

    second = _load_instance(monkeypatch, "WGLink_palette_quiet_restart_b", fixture.ui, fixture.app)
    monkeypatch.setattr(second.wglink_workspace, "ipc_folder", lambda **_kwargs: fixture.ipc)
    timers: list = []
    monkeypatch.setattr(second, "_start_timer", lambda delay, function: timers.append((delay, function)))
    second.run(None)
    try:
        assert second._requests.current()["requestId"] == fixture.request_id
        assert fixture.ui.palettes.items == {}  # not opened by itself
        assert [delay for delay, _function in timers] == [0.0]  # still looked at, slowly
    finally:
        second.stop(None)


@pytest.mark.parametrize(("state", "extra", "headline", "actions"), [
    ("couldNotFinish", {"phase": "solving", "failure": {"recorded": True}},
     "Waveguide Generator could not finish solving Horn A. This has been recorded for the developers.",
     ["openWg"]),
    ("cancelled", {"cancelReason": "replaced"}, "Horn A · Replaced by a newer solve.", ["openWg"]),
    ("refused", {"failure": {"recorded": False, "message": "Select at least one source face."}},
     "Waveguide Generator could not take Horn A.", ["openWg"]),
])
def test_wgs_endings_in_the_palette(monkeypatch, tmp_path: Path, state, extra, headline, actions) -> None:
    fixture = _following(monkeypatch, tmp_path, f"WGLink_palette_end_{state}")
    _status(fixture, state, 1, **extra)

    shown = _tick(fixture)

    assert shown["headline"] == headline and _ids(shown) == actions
    assert shown["terminal"] is True and shown["busy"] is False
    assert fixture.ui.messages == []
    assert fixture.module._recent_outcomes[-1]["outcome"] == state


@pytest.mark.parametrize("local", ["notTaken", "takenUnconfirmed"])
def test_the_add_ins_own_waits_in_the_palette(monkeypatch, tmp_path: Path, local: str) -> None:
    fixture = _following(monkeypatch, tmp_path, f"WGLink_palette_wait_{local}")
    if local == "notTaken":
        monkeypatch.setattr(fixture.module.wglink_inbox, "SOLVE_PICKUP_NOTICE_SECONDS", 0.0)
    else:
        _claim(fixture, fixture.request_id)
        monkeypatch.setattr(fixture.module.wglink_inbox, "ACK_WAIT_SECONDS", 0.0)

    shown = _tick(fixture)

    assert fixture.module._requests.current()["local"] == local
    assert _ids(shown) == ["openWg", "sendAgain"] and shown["terminal"] is False
    assert fixture.ui.messages == []


def test_a_request_that_was_not_sent_is_recorded(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_palette_not_sent")
    monkeypatch.setattr(fixture.module.wglink_send, "send",
                        lambda *_a, **_k: (_ for _ in ()).throw(fixture.module.wglink_core.WgLinkError("no")))

    _run(fixture.module, "solve")

    assert wglink_requests.load(fixture.ipc)["local"] == wglink_requests.NOT_SENT
    assert _palette(fixture).load()["headline"] == "Horn A was not sent to Waveguide Generator."


# -- the host: palettes that fail, reload, or already exist ----------------------


def test_without_html_palettes_there_is_no_dialog_held_open(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_palette_fallback")
    _advertise_acks(fixture.ipc)
    _advertise_status(fixture.ipc)
    monkeypatch.setattr(fixture.ui.palettes, "add",
                        lambda *_a: (_ for _ in ()).throw(RuntimeError("no HTML palettes")))
    _run(fixture.module, "solve")
    [request] = _inbox(fixture.ipc)
    fixture.request, fixture.request_id = request, request["operationId"]
    _status(fixture, "solving", 1)
    due = list(fixture.timers)
    fixture.timers.clear()
    for _delay, function in due:
        function()
    _deliver_followups(fixture.module, fixture.app)

    assert fixture.ui.progress_dialogs[1:] == []  # only the export's own, already hidden
    assert any("Horn A · Solving" in line for line in fixture.ui.text_palette.written)
    assert fixture.ui.messages == []

    _status(fixture, "needsYou", 2, prompt={"nextStep": "Choose a mesh.", "options": [
        {"id": "a", "label": "Solve anyway"}]})
    due = list(fixture.timers)
    fixture.timers.clear()
    for _delay, function in due:
        function()
    _deliver_followups(fixture.module, fixture.app)
    assert fixture.ui.messages == []  # never inside the follow-up that found it
    _deliver_followups(fixture.module, fixture.app)
    [(_title, text)] = fixture.ui.messages  # the one news worth a box
    assert "needs you" in text and "Solve anyway" in text


def test_a_reloaded_page_asks_again_and_gets_everything(monkeypatch, tmp_path: Path) -> None:
    fixture = _following(monkeypatch, tmp_path, "WGLink_palette_reload")
    palette = _palette(fixture)
    palette.load()
    palette.loaded = False  # Fusion is reloading the page
    _status(fixture, "queued", 1)
    due = list(fixture.timers)
    fixture.timers.clear()
    for _delay, function in due:
        function()
    _deliver_followups(fixture.module, fixture.app)
    assert palette.dropped  # sent while it could not hear

    assert palette.reload()["headline"] == "Horn A · Waiting for the solver to start."


def test_a_palette_left_by_an_earlier_registration_is_replaced(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_palette_duplicate")
    stale = fixture.ui.palettes.add("hornlab_wglink_requests", "Waveguide Generator", "file:///old", True)

    _run(fixture.module, "send")

    assert stale.isValid is False
    assert _palette(fixture) is not stale and _palette(fixture).isVisible is True


def test_stop_waits_for_a_look_in_progress_and_nothing_follows_it(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_palette_stop_mid_look", real_timers=True)
    module = fixture.module
    _advertise_acks(fixture.ipc)
    _advertise_status(fixture.ipc)
    module.run(None)
    entered, release = threading.Event(), threading.Event()
    real_poll = module.wglink_requests.poll

    def slow_poll(*args, **kwargs):
        entered.set()
        release.wait(5.0)
        return real_poll(*args, **kwargs)

    monkeypatch.setattr(module.wglink_requests, "poll", slow_poll)
    monkeypatch.setattr(module.wglink_requests, "POLL_VISIBLE_SECONDS", 0.01)
    _run(module, "solve")
    assert entered.wait(5.0)
    look = [timer for timer in module._timers if timer.is_alive()]
    fixture.app.fired.clear()
    threading.Timer(0.2, release.set).start()

    module.stop(None)

    assert not any(timer.is_alive() for timer in look)
    assert module._timers == [] and module._request_poll_armed is False
    assert module._start_timer(0.0, lambda: None) is None  # nothing re-arms behind stop()
    assert module.FOLLOWUP_EVENT_ID not in fixture.app.fired


def test_fusion_holds_events_while_a_modal_is_open(monkeypatch, tmp_path: Path) -> None:
    from fusion_fakes import FusionEventLoop

    fixture = _following(monkeypatch, tmp_path, "WGLink_palette_event_loop")
    module = fixture.module
    module.run(None)
    try:
        palette = _palette(fixture)
        palette.load()
        loop = FusionEventLoop(fixture.app)
        monkeypatch.setattr(module, "_followup_app", types.SimpleNamespace(fireCustomEvent=loop.fire))
        _status(fixture, "queued", 1)
        with loop.modal():
            due = list(fixture.timers)
            fixture.timers.clear()
            for _delay, function in due:
                function()
            assert loop.pump() == 0
            assert palette.shown["headline"].startswith("Horn A · Sent to")
        assert loop.pump() == 1
        assert palette.shown["headline"] == "Horn A · Waiting for the solver to start."
    finally:
        module.stop(None)
