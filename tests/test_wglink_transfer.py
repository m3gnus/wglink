"""The M1 transfer path: one request file for Send and Solve (contract C1-C6, C8).

Send and Solve write one file each into WG's request inbox,
``<ipc>/.wg-solve-requests/<operationId>.json``, and differ only in ``kind``.
There is no second path: no live outbox item is made for new work in either
gate state. The file's schema follows what WG advertises, so a schema-3
reader -- which ignores ``kind`` -- can never be handed a Send.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import time
import types

import pytest

from test_wglink_addin_lifecycle import (
    _Application,
    _Definitions,
    _Panels,
    _UI,
    _dialog_inputs,
    _load_instance,
    _per_request_folders,
)


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
import wglink_legacy_queue  # noqa: E402
import wglink_inbox  # noqa: E402
import wglink_handoff  # noqa: E402
import wglink_status  # noqa: E402


SNAPSHOT = wglink_inbox.KIND_RECEIVE_SNAPSHOT
SOLVE = wglink_inbox.KIND_PREPARE_AND_SOLVE
FIELDS = {
    "bundle_relative": "wgreturn/speaker.wgreturn",
    "manifest_sha256": "sha256:" + "a" * 64,
    "requested_at": "2026-09-21T10:00:00Z",
}


def _advertise(ipc: Path, value: object, *, live: bool = False) -> None:
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "fusionRequestDelivery": 3,
        "automaticDomain": 1,
        "meshSizing": 1, "documentUp": 1,
    }
    if value is not None:
        payload["solveCommandDelivery"] = value
    if live:
        payload["liveProtocol"] = 1
    (ipc / wglink_inbox.CAPABILITIES_FILENAME).write_text(json.dumps(payload))


def _inbox(ipc: Path) -> list[dict]:
    folder = ipc / wglink_inbox.SOLVE_REQUESTS_DIRECTORY
    if not folder.is_dir():
        return []
    return [
        json.loads(path.read_text())
        for path in sorted(folder.iterdir())
        if not path.name.startswith(".")
    ]


def _outbox(ipc: Path) -> list[dict]:
    items, _foreign = wglink_legacy_queue.LegacyQueue(ipc).scan()
    return items


# -- C2/C3: the writer and the versioning rule ---------------------------------


def test_both_delivery_checks_use_the_same_outdated_remedy(tmp_path: Path) -> None:
    _advertise(tmp_path, 2)
    payload = json.loads((tmp_path / wglink_inbox.CAPABILITIES_FILENAME).read_text())
    payload["fusionRequestDelivery"] = 2
    (tmp_path / wglink_inbox.CAPABILITIES_FILENAME).write_text(json.dumps(payload))
    for check in (wglink_inbox.require_wg_delivery, wglink_inbox.require_fusion_delivery):
        with pytest.raises(wglink_inbox.WgOutdatedError) as refusal:
            check(tmp_path)
        assert str(refusal.value) == wglink_inbox.WG_OUTDATED_MESSAGE


def test_a_send_for_a_wg_that_reads_schema_4_is_a_receive_snapshot_without_return_id(
    tmp_path: Path,
) -> None:
    _advertise(tmp_path, 4)

    path = wglink_inbox.write_wg_request(
        tmp_path, kind=SNAPSHOT, command_id="op-1", return_id="wgr_ignored", **FIELDS
    )

    assert path == tmp_path / ".wg-solve-requests" / "op-1.json"
    assert json.loads(path.read_text()) == {
        "schemaVersion": 4,
        "target": "waveguide-generator",
        "kind": "receive_snapshot",
        "commandId": "op-1",
        "operationId": "op-1",
        "bundlePath": "wgreturn/speaker.wgreturn",
        "manifestSha256": "sha256:" + "a" * 64,
        "requestedAt": "2026-09-21T10:00:00Z",
    }


@pytest.mark.parametrize("return_id", ["wgr_1", ""])
def test_a_solve_for_a_wg_that_reads_schema_4_carries_its_return_id_even_empty(
    tmp_path: Path, return_id: str
) -> None:
    _advertise(tmp_path, 4)

    path = wglink_inbox.write_wg_request(
        tmp_path, kind=SOLVE, command_id="op-2", return_id=return_id, **FIELDS
    )

    payload = json.loads(path.read_text())
    assert (payload["schemaVersion"], payload["kind"], payload["returnId"]) == (4, SOLVE, return_id)
    assert payload["commandId"] == payload["operationId"] == "op-2"


def test_a_solve_for_a_wg_that_reads_schema_3_is_refused(tmp_path: Path) -> None:
    _advertise(tmp_path, 3)
    with pytest.raises(wglink_inbox.WgOutdatedError, match="Update Waveguide Generator"):
        wglink_inbox.write_wg_request(
            tmp_path, kind=SOLVE, command_id="op-3", return_id="wgr_1", **FIELDS
        )
    assert _inbox(tmp_path) == []


def test_a_send_for_a_wg_that_reads_only_schema_3_is_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    """A schema-3 reader ignores ``kind``: a Send written for it would start a solve."""

    _advertise(tmp_path, 3)

    with pytest.raises(wglink_inbox.WgOutdatedError, match="Update Waveguide Generator"):
        wglink_inbox.write_wg_request(
            tmp_path, kind=SNAPSHOT, command_id="op-4", return_id=None, **FIELDS
        )

    assert _inbox(tmp_path) == []


@pytest.mark.parametrize(
    ("advertised", "error"),
    [
        ("no-file", wglink_inbox.WgNotCollectingError),
        (None, wglink_inbox.WgNotCollectingError),
        (2, wglink_inbox.WgOutdatedError),
    ],
    ids=["no-capabilities", "no-solve-delivery", "older-wg"],
)
@pytest.mark.parametrize("kind", [SNAPSHOT, SOLVE])
def test_a_wg_that_advertises_nothing_or_less_than_3_gets_nothing(
    tmp_path: Path, advertised: object, error: type, kind: str
) -> None:
    if advertised != "no-file":
        _advertise(tmp_path, advertised)

    with pytest.raises(error):
        wglink_inbox.write_wg_request(
            tmp_path, kind=kind, command_id="op-5", return_id="", **FIELDS
        )

    assert _inbox(tmp_path) == []


@pytest.mark.parametrize("advertised", ["no-file", None, 1, 2, 3, 4, 5, "4", True])
def test_no_send_is_ever_written_in_a_schema_a_reader_could_run_as_a_solve(
    tmp_path: Path, advertised: object
) -> None:
    """The versioning rule as one property, over every advertisement."""

    if advertised != "no-file":
        _advertise(tmp_path, advertised)
    try:
        wglink_inbox.write_wg_request(
            tmp_path, kind=SNAPSHOT, command_id="op-6", return_id=None, **FIELDS
        )
    except (wglink_inbox.WgOutdatedError, wglink_inbox.WgNotCollectingError):
        pass

    for payload in _inbox(tmp_path):
        assert payload["schemaVersion"] >= wglink_inbox.WG_REQUEST_SCHEMA_VERSION
        assert payload["kind"] == SNAPSHOT
        assert "returnId" not in payload


def test_the_inbox_schema_is_its_own_constant(tmp_path: Path) -> None:
    """The Fusion-bound reader and the heartbeat keep version 3 (C3)."""

    assert wglink_inbox.WG_REQUEST_SCHEMA_VERSION == 4
    assert wglink_handoff.REQUEST_SCHEMA_VERSION == 3
    assert wglink_status.DELIVERY_VERSION == 3


@pytest.mark.parametrize("command_id", ["", "../escape", "a" * 129, "has space"])
def test_an_id_that_is_not_a_plain_file_name_is_refused(tmp_path: Path, command_id: str) -> None:
    _advertise(tmp_path, 4)

    with pytest.raises(ValueError):
        wglink_inbox.write_wg_request(
            tmp_path, kind=SNAPSHOT, command_id=command_id, return_id=None, **FIELDS
        )


# -- the commands: one path, synchronous outcome -------------------------------


def _transfer(monkeypatch, tmp_path: Path, name: str, *, advertised: object = 4, live: bool = False,
              real_timers: bool = False):
    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    app.activeDocument = types.SimpleNamespace(name="Horn A", isModified=False)
    module = _load_instance(monkeypatch, name, ui, app)
    ipc, _bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _advertise(ipc, advertised, live=live)
    bundle = tmp_path / "workspace" / "wgreturn" / "speaker.wgreturn"
    bundle.mkdir(parents=True)
    (bundle / "wgreturn.json").write_bytes(b'{"return": "speaker"}')
    monkeypatch.setattr(
        module.wglink_send,
        "send",
        lambda _app, _options, **_kwargs: {
            "bundle_path": str(bundle),
            "return_id": "wgr_1",
            "scope": {"status": "clean"},
            "sources": [],
        },
    )
    timers: list[tuple[float, object]] = []
    if not real_timers:
        monkeypatch.setattr(
            module, "_start_timer", lambda delay, function: timers.append((delay, function))
        )
    monkeypatch.setattr(module, "_owns_active_ipc_lease", lambda: True)
    return types.SimpleNamespace(module=module, ui=ui, app=app, ipc=ipc, bundle=bundle, timers=timers)


def _run(module, operation: str) -> None:
    module.CommandExecuteHandler(operation).notify(
        types.SimpleNamespace(command=types.SimpleNamespace(commandInputs=_dialog_inputs()))
    )


@pytest.mark.parametrize(("operation", "kind"), [("send", SNAPSHOT), ("solve", SOLVE)])
def test_send_and_solve_write_one_request_differing_only_in_kind(
    monkeypatch, tmp_path: Path, operation: str, kind: str
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_one_path_{operation}")

    _run(fixture.module, operation)

    [payload] = _inbox(fixture.ipc)
    assert payload["kind"] == kind
    assert payload["schemaVersion"] == 4
    assert payload["bundlePath"] == "wgreturn/speaker.wgreturn"
    assert ("returnId" in payload) is (kind == SOLVE)
    assert _outbox(fixture.ipc) == []
    # No message box: the request palette follows the request from here.
    assert fixture.ui.messages == []
    shown = _palette(fixture).load()
    assert shown["headline"] == "Horn A · Sent to Waveguide Generator. Waiting for it to pick up the model."
    assert shown["diagnostics"]["requestId"] == payload["operationId"]
    assert fixture.module._request_trace["correlationId"] == payload["operationId"]


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_obsolete_endpoint_advertisements_do_not_change_the_file_path(
    monkeypatch, tmp_path: Path, operation: str
) -> None:
    """C1: whatever the gate says, new work is a file and never an outbox item."""

    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_live_one_path_{operation}", live=True)
    _run(fixture.module, operation)

    assert len(_inbox(fixture.ipc)) == 1
    assert _outbox(fixture.ipc) == []


def test_a_send_to_a_wg_that_reads_only_schema_3_is_refused_visibly(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_send_old", advertised=3)

    _run(fixture.module, "send")

    assert _inbox(fixture.ipc) == [] and _outbox(fixture.ipc) == []
    title, text = fixture.ui.messages[-1]
    assert title == "Waveguide Generator"
    assert "Update Waveguide Generator" in text
    assert fixture.timers == []


def test_a_solve_to_a_wg_that_reads_only_schema_3_is_refused(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_solve_old", advertised=3)
    _run(fixture.module, "solve")
    assert _inbox(fixture.ipc) == []
    assert "Update Waveguide Generator" in fixture.ui.messages[-1][1]


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_a_wg_not_collecting_requests_is_named_at_command_time(
    monkeypatch, tmp_path: Path, operation: str
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_not_collecting_{operation}", advertised=None)

    _run(fixture.module, operation)

    assert _inbox(fixture.ipc) == []
    title, text = fixture.ui.messages[-1]
    assert title == "Waveguide Generator"
    assert "not ready to receive this model" in text


def test_a_write_whose_outcome_is_unknown_is_retried_once_with_the_same_id(
    monkeypatch, tmp_path: Path
) -> None:
    """C5: the retry re-writes the same file; WG recovers it as the same operation."""

    fixture = _transfer(monkeypatch, tmp_path, "WGLink_retry_once")
    monkeypatch.setattr(fixture.module, "_retry_pause", lambda _seconds: None)
    real = fixture.module.wglink_inbox.publish_wg_request
    calls: list[tuple] = []

    def flaky(path, payload):
        calls.append((path, dict(payload)))
        if len(calls) == 1:
            real(path, payload)
            raise OSError("the rename reported an error after it landed")
        return real(path, payload)

    monkeypatch.setattr(fixture.module.wglink_inbox, "publish_wg_request", flaky)

    _run(fixture.module, "send")

    assert len(calls) == 2 and calls[0] == calls[1]
    [payload] = _inbox(fixture.ipc)
    assert payload == calls[0][1]
    assert "Sent to Waveguide Generator" in _palette(fixture).load()["headline"]


def test_a_write_that_keeps_failing_is_a_visible_refusal(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_write_fails")
    monkeypatch.setattr(fixture.module, "_retry_pause", lambda _seconds: None)
    calls: list[int] = []

    def failing(_path, _payload):
        calls.append(1)
        raise OSError("disk full")

    monkeypatch.setattr(fixture.module.wglink_inbox, "publish_wg_request", failing)

    _run(fixture.module, "solve")

    assert len(calls) == fixture.module.WG_REQUEST_WRITE_ATTEMPTS
    title, text = fixture.ui.messages[-1]
    assert title == "Waveguide Generator"
    assert "has not received it" in text and "writable" in text
    assert "disk full" in "\n".join(fixture.ui.text_palette.written)


def test_a_refusal_is_an_answer_and_is_never_retried(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_refusal_not_retried", advertised=2)
    calls: list[int] = []
    published: list[int] = []
    real = fixture.module.wglink_inbox.plan_wg_request

    def counting(ipc, **fields):
        calls.append(1)
        return real(ipc, **fields)

    monkeypatch.setattr(fixture.module.wglink_inbox, "plan_wg_request", counting)
    monkeypatch.setattr(
        fixture.module.wglink_inbox, "publish_wg_request", lambda *_a: published.append(1)
    )

    _run(fixture.module, "solve")

    assert published == []

    assert calls == []  # Refused before exporting or planning an inbox write.


def _press(module, operation: str, *, force_new: bool = False) -> None:
    """A press of OK in the Send or Solve dialog (``force_new``: the dialog the
    palette's "Send again" opened)."""

    handler = module.CommandExecuteHandler(operation)
    handler.force_new = force_new
    handler.notify(types.SimpleNamespace(command=types.SimpleNamespace(commandInputs=_dialog_inputs())))


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_a_repeat_press_of_the_same_model_asks_and_no_shows_progress(
    monkeypatch, tmp_path: Path, operation: str
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_repeat_no_{operation}")
    _press(fixture.module, operation)
    [first] = _inbox(fixture.ipc)
    _palette(fixture).close()
    fixture.ui.dialog_result = "no"

    _press(fixture.module, operation)

    [(title, question)] = fixture.ui.messages
    assert title == "Waveguide Generator"
    # WG has not picked it up: the question says so, not "already working on".
    assert "Horn A is still waiting for Waveguide Generator to pick it up." in question
    assert "Yes:" in question and "No: Show its progress." in question
    assert _inbox(fixture.ipc) == [first]  # nothing sent, nothing lost
    assert _palette(fixture).isVisible is True


def test_a_repeat_press_answered_yes_sends_again_and_takes_back_the_untaken_one(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_repeat_yes")
    monkeypatch.setattr(fixture.module, "_active_document_id", lambda: "fusion:doc-a")
    _press(fixture.module, "solve")
    [first] = _inbox(fixture.ipc)
    fixture.ui.dialog_result = "yes"

    _press(fixture.module, "solve")

    assert len(fixture.ui.messages) == 1  # asked once
    [second] = _inbox(fixture.ipc)  # the first, never picked up, was taken back
    assert second["operationId"] != first["operationId"]
    assert fixture.module._requests.current()["requestId"] == second["operationId"]


def test_a_press_without_a_question_box_still_sends(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_repeat_no_box")
    monkeypatch.setattr(fixture.module, "_active_document_id", lambda: "fusion:doc-a")
    _press(fixture.module, "send")
    monkeypatch.setattr(fixture.ui, "messageBox", None)

    _press(fixture.module, "send")

    assert fixture.module._requests.current()["requestId"] == _inbox(fixture.ipc)[-1]["operationId"]
    assert fixture.module._requests.current()["local"] == "submitted"


def test_the_palettes_send_again_does_not_ask(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_repeat_forced")
    monkeypatch.setattr(fixture.module, "_active_document_id", lambda: "fusion:doc-a")
    _press(fixture.module, "send")

    _press(fixture.module, "send", force_new=True)

    assert fixture.ui.messages == []
    assert len({payload["operationId"] for payload in _inbox(fixture.ipc)}) == 1
    assert fixture.module._requests.current()["requestId"] == _inbox(fixture.ipc)[0]["operationId"]


def test_wg_having_the_request_is_said_in_the_question(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_repeat_wg_has_it")
    fixture.ui.dialog_result = "no"
    _press(fixture.module, "solve")
    current = fixture.module._requests.current()
    fixture.module._requests.begin(dict(current, receipt={"outcome": "accepted", "reason": None},
                                        capabilities={"acknowledgement": True, "requestStatus": True}))

    _press(fixture.module, "solve")

    [(_title, question)] = fixture.ui.messages
    assert question.startswith("Waveguide Generator is already working on Horn A. Solve it again?")


def test_an_unnamed_document_never_takes_back_anothers_request(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_repeat_unnamed")
    monkeypatch.setattr(fixture.module, "_active_document_id", lambda: None)
    _press(fixture.module, "send")
    fixture.app.activeDocument = types.SimpleNamespace(name="Horn A", isModified=False)  # another unsaved "Horn A"

    _press(fixture.module, "send", force_new=True)

    assert len(_inbox(fixture.ipc)) == 2


def test_the_previous_request_is_taken_back_only_once_the_new_one_is_written(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_repeat_after_write")
    module = fixture.module
    monkeypatch.setattr(module, "_active_document_id", lambda: "fusion:doc-a")
    monkeypatch.setattr(module, "_retry_pause", lambda _seconds: None)
    _press(module, "solve")
    [first] = _inbox(fixture.ipc)
    seen: list[list[str]] = []
    real_publish = module.wglink_inbox.publish_wg_request

    def publish(path, payload):
        seen.append([item["operationId"] for item in _inbox(fixture.ipc)])
        return real_publish(path, payload)

    monkeypatch.setattr(module.wglink_inbox, "publish_wg_request", publish)
    _press(module, "solve", force_new=True)
    assert seen[0] == [first["operationId"]]  # still there while the new one was written
    [second] = _inbox(fixture.ipc)
    assert second["operationId"] != first["operationId"]

    # A write that cannot be confirmed takes nothing back.
    monkeypatch.setattr(module.wglink_inbox, "publish_wg_request",
                        lambda *_a: (_ for _ in ()).throw(module.wglink_inbox.WriteOutcomeUnknown("held")))
    _press(module, "solve", force_new=True)
    assert second in _inbox(fixture.ipc)


def _marked_model(app, role: str) -> None:
    face = types.SimpleNamespace(entityToken="face-7")
    mark = types.SimpleNamespace(parent=face, name="face_role", value=role)
    app.activeProduct = types.SimpleNamespace(
        objectType="adsk::fusion::Design",
        rootComponent=types.SimpleNamespace(bRepBodies=[], allOccurrences=[], attributes=None),
        findAttributes=lambda group, name: [mark] if group == "WGLink" else [])


def test_a_changed_source_mark_is_a_changed_model(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_repeat_marks")
    _marked_model(fixture.app, "HF")
    key = fixture.module._submission_key("solve", _dialog_inputs())
    assert fixture.module._submission_key("solve", _dialog_inputs()) == key  # stable
    _marked_model(fixture.app, "MF")

    assert fixture.module._submission_key("solve", _dialog_inputs()) != key


def test_send_again_is_spent_by_one_dialog_and_never_outlives_it(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_repeat_spent")
    module = fixture.module
    _press(module, "solve")
    executed: list[str] = []
    fixture.ui.commandDefinitions.items["hornlab_wglink_solve"] = types.SimpleNamespace(
        isValid=True, execute=lambda: executed.append("solve"))

    module._request_action("sendAgain")
    assert executed == ["solve"]
    assert module._take_force_new_request("send") is False  # another command spends it
    assert module._take_force_new_request("solve") is False  # and it is gone

    module._request_action("sendAgain")
    assert module._take_force_new_request("solve") is True
    assert module._take_force_new_request("solve") is False  # one dialog only

    # A dialog that was never opened leaves nothing behind.
    fixture.ui.commandDefinitions.items["hornlab_wglink_solve"] = types.SimpleNamespace(
        isValid=True, execute=lambda: (_ for _ in ()).throw(RuntimeError("busy")))
    module._request_action("sendAgain")
    assert module._force_new_request is None

    # Nor does one the user cancelled long ago.
    module._force_new_request = ("solve", time.monotonic() - 1.0)
    assert module._take_force_new_request("solve") is False


def test_a_press_after_the_request_finished_is_a_new_request(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_send_after_done")

    _press(fixture.module, "send")
    for path in (fixture.ipc / ".wg-solve-requests").iterdir():
        path.unlink()  # a WG without receipts took it: all it will ever say
    _fire_timers(fixture)
    _press(fixture.module, "send")

    assert fixture.ui.messages == []  # nothing to ask about
    assert len(_inbox(fixture.ipc)) == 1  # the second request, waiting
    assert fixture.module._requests.current()["local"] == "submitted"


def _moved_model(app, transform: list[float], *, parameter: str = "10 mm") -> None:
    body = types.SimpleNamespace(entityToken="body-1", revisionId="rev-1", isVisible=True)
    component = types.SimpleNamespace(bRepBodies=[body], attributes=None)
    occurrence = types.SimpleNamespace(
        component=component, fullPathName="Driver:1", isLightBulbOn=True,
        transform2=types.SimpleNamespace(asArray=lambda: list(transform)))
    root = types.SimpleNamespace(bRepBodies=[], allOccurrences=[occurrence], attributes=None)
    app.activeProduct = types.SimpleNamespace(
        objectType="adsk::fusion::Design", rootComponent=root,
        timeline=types.SimpleNamespace(count=7),
        allParameters=[types.SimpleNamespace(name="throat", expression=parameter, value=1.0)])


@pytest.mark.parametrize("change", ["revision", "visibility", "placement", "parameter"])
def test_repeat_detection_and_mesh_learning_both_observe_model_changes(
    monkeypatch, tmp_path: Path, change: str
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_shared_revision_{change}")
    module = fixture.module
    _moved_model(fixture.app, [1.0] * 16)
    design = fixture.app.activeProduct
    occurrence = design.rootComponent.allOccurrences[0]
    body = occurrence.component.bRepBodies[0]
    repeated = module._submission_key("solve", _dialog_inputs())
    learned = module.wglink_send.mesh_geometry_key(design, [body])
    assert learned is not None
    if change == "revision":
        body.revisionId = "rev-2"
    elif change == "visibility":
        body.isVisible = False
    elif change == "placement":
        occurrence.transform2 = types.SimpleNamespace(asArray=lambda: [2.0] * 16)
    else:
        design.allParameters[0].expression = "20 mm"
    assert module._submission_key("solve", _dialog_inputs()) != repeated
    assert module.wglink_send.mesh_geometry_key(design, [body]) != learned
    # An unmeasurable body may still be sent again, but never reuses a fraction.
    body.revisionId = ""
    assert module.wglink_send.mesh_geometry_key(design, [body]) is None
    assert isinstance(module._submission_key("solve", _dialog_inputs()), str)


@pytest.mark.parametrize("change", ["moved", "parameter"])
def test_a_press_after_the_model_changed_never_asks(monkeypatch, tmp_path: Path, change: str) -> None:
    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_send_changed_{change}")
    monkeypatch.setattr(fixture.module, "_active_document_id", lambda: "doc-1")
    _moved_model(fixture.app, [1.0] * 16)
    _press(fixture.module, "solve")
    first = fixture.module._requests.current()["requestId"]
    if change == "moved":
        _moved_model(fixture.app, [1.0] * 15 + [4.0])  # a joint moved the driver
    else:
        _moved_model(fixture.app, [1.0] * 16, parameter="12 mm")

    _press(fixture.module, "solve")

    assert fixture.ui.messages == []
    assert fixture.module._requests.current()["requestId"] != first


def test_the_same_model_pressed_again_is_asked_about(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_send_unchanged")
    monkeypatch.setattr(fixture.module, "_active_document_id", lambda: "doc-1")
    fixture.ui.dialog_result = "no"
    _moved_model(fixture.app, [1.0] * 16)
    _press(fixture.module, "solve")
    fixture.app.activeDocument.isModified = True  # saving state is not a change

    _press(fixture.module, "solve")

    assert len(fixture.ui.messages) == 1


# -- C6: the pickup check -------------------------------------------------------


def _deliver_followups(module, app) -> None:
    event = app.events.get(module.FOLLOWUP_EVENT_ID)
    handlers = list(event.handlers) if event is not None else [module.FollowupEventHandler()]
    for handler in handlers:
        handler.notify(None)


def _fire_timers(fixture) -> None:
    due = list(fixture.timers)
    fixture.timers.clear()
    for _delay, function in due:
        function()


def _palette(fixture):
    return fixture.ui.palettes.items["hornlab_wglink_requests"]


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_a_request_wg_leaves_untaken_is_shown_in_the_palette_under_its_command(
    monkeypatch, tmp_path: Path, operation: str
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_pickup_{operation}")
    module = fixture.module
    reads: list[int] = []
    monkeypatch.setattr(module, "_fusion_snapshot", lambda: reads.append(1) or {})
    monkeypatch.setattr(module.wglink_inbox, "SOLVE_PICKUP_NOTICE_SECONDS", 0.0)

    _run(module, operation)
    palette = _palette(fixture)
    palette.load()
    [(delay, _function)] = fixture.timers
    assert delay == module.wglink_requests.POLL_VISIBLE_SECONDS
    _fire_timers(fixture)
    _deliver_followups(module, fixture.app)

    assert fixture.ui.messages == []
    shown = palette.shown
    assert shown["headline"] == "Waveguide Generator has not picked up Horn A yet."
    assert [action["id"] for action in shown["actions"]] == ["openWg", "sendAgain"]
    assert module._recent_outcomes[-1]["outcome"] == "notTaken"
    # The request is not moved or dropped, nothing in Fusion was read, and
    # it is still followed: WG may take it yet.
    assert len(_inbox(fixture.ipc)) == 1
    assert reads == []
    assert set(module.wglink_activity.LOG.counts()["pickup_check"]) == {f"command:{operation}"}
    assert len(fixture.timers) == 1


def test_a_request_a_wg_without_receipts_took_ends_the_following(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_pickup_taken")
    module = fixture.module

    _run(module, "send")
    for path in (fixture.ipc / ".wg-solve-requests").iterdir():
        path.unlink()
    _fire_timers(fixture)
    _deliver_followups(module, fixture.app)

    assert fixture.ui.messages == []
    assert module._recent_outcomes[-1]["outcome"] == "taken"
    assert _palette(fixture).load()["headline"] == "Horn A · Waveguide Generator has it."
    assert module.wglink_activity.LOG.counts()["pickup_check"] == {"command:send": 1}
    assert fixture.timers == []  # nothing more to look at


def test_a_pickup_check_held_behind_a_command_runs_when_it_ends(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_pickup_busy")
    module = fixture.module
    fired: list[str] = []
    monkeypatch.setattr(fixture.app, "fireCustomEvent", lambda event_id, *_a: fired.append(event_id) or True)
    monkeypatch.setattr(module.wglink_inbox, "SOLVE_PICKUP_NOTICE_SECONDS", 0.0)
    monkeypatch.setattr(module, "_followup_event", object())
    monkeypatch.setattr(module, "_followup_app", fixture.app)

    _run(module, "send")
    palette = _palette(fixture)
    palette.load()
    module._command_busy = True
    _fire_timers(fixture)
    _deliver_followups(module, fixture.app)
    assert "Sent to" in palette.shown["headline"]  # held, not dropped
    assert len(module._followups) == 1

    # The command that held the thread finishes (the user cancels Detach).
    fixture.ui.dialog_result = "no"
    module._command_busy = False
    fired.clear()
    _run(module, "detach")
    assert module.FOLLOWUP_EVENT_ID in fired
    _deliver_followups(module, fixture.app)
    assert palette.shown["headline"] == "Waveguide Generator has not picked up Horn A yet."
    assert [title for title, _text in fixture.ui.messages] == ["Waveguide Generator — confirm Detach"]


@pytest.mark.parametrize("coordination", [False, None], ids=["off", "on"])
def test_the_pickup_check_rides_its_own_event_in_both_gate_states(
    monkeypatch, tmp_path: Path, coordination: bool | None
) -> None:
    """C8: the transfer path is behind neither gate."""

    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_pickup_gate_{coordination}")
    module = fixture.module
    monkeypatch.setattr(module.wglink_inbox, "SOLVE_PICKUP_NOTICE_SECONDS", 0.0)
    if coordination is not None:
        module.SETTINGS_PATH.write_text(json.dumps({"automatic_coordination": coordination}))
    module.run(None)
    try:
        assert module.FOLLOWUP_EVENT_ID in fixture.app.events
        _run(module, "send")
        palette = _palette(fixture)
        palette.load()
        _fire_timers(fixture)
        _deliver_followups(module, fixture.app)
        assert palette.shown["headline"] == "Waveguide Generator has not picked up Horn A yet."
        assert fixture.ui.messages == []
    finally:
        module.stop(None)


# -- C1/E1: converting what an earlier session queued ----------------------------


def _queued(ipc: Path, kind: str, operation_id: str, *, file_written: bool = False, answered: bool = False) -> None:
    # A fixture from the former queue format; production only reads it.
    item = {
        "schemaVersion": 1, "operationId": operation_id, "kind": kind,
        "bundlePath": "wgreturn/speaker.wgreturn", "manifestSha256": "sha256:" + "b" * 64,
        "requestedAt": "2026-09-20T12:00:00Z", "createdAt": "2026-09-20T12:00:00Z",
        "answer": {"outcome": "delivered"} if answered else None,
    }
    if kind == SOLVE:
        item.update(returnId="wgr_9", fileWritten=file_written)
    folder = ipc / ".wglink-outbox"
    folder.mkdir(exist_ok=True)
    (folder / f"{operation_id}.json").write_text(json.dumps(item))


def _queue_both(ipc: Path) -> None:
    _queued(ipc, wglink_legacy_queue.KIND_SNAPSHOT, "snap-1")
    _queued(ipc, wglink_legacy_queue.KIND_SOLVE, "solve-1")


def test_at_schema_4_every_unanswered_item_becomes_its_request_file(tmp_path: Path) -> None:
    _advertise(tmp_path, 4)
    _queue_both(tmp_path)
    _queued(tmp_path, wglink_legacy_queue.KIND_SNAPSHOT, "done-1", answered=True)

    result = wglink_legacy_queue.convert_outbox(tmp_path, solve_files=wglink_inbox)

    assert sorted(result["converted"]) == ["snap-1", "solve-1"]
    by_id = {payload["operationId"]: payload for payload in _inbox(tmp_path)}
    assert by_id["snap-1"]["kind"] == SNAPSHOT and by_id["snap-1"]["schemaVersion"] == 4
    assert by_id["solve-1"]["kind"] == SOLVE and by_id["solve-1"]["returnId"] == "wgr_9"
    assert by_id["snap-1"]["requestedAt"] == "2026-09-20T12:00:00Z"
    assert [item["operationId"] for item in _outbox(tmp_path)] == ["done-1"]


def test_at_schema_3_every_unanswered_item_stays_queued(tmp_path: Path) -> None:
    _advertise(tmp_path, 3)
    _queue_both(tmp_path)
    result = wglink_legacy_queue.convert_outbox(tmp_path, solve_files=wglink_inbox)
    assert _inbox(tmp_path) == []
    assert sorted(item["operationId"] for item in _outbox(tmp_path)) == ["snap-1", "solve-1"]
    assert sorted(result["left"]) == ["snap-1", "solve-1"]
    assert result["abandoned"] == [] and result["failed"] == []


@pytest.mark.parametrize("advertised", ["no-file", None, 2])
def test_when_wg_advertises_nothing_nothing_moves(tmp_path: Path, advertised: object) -> None:
    if advertised != "no-file":
        _advertise(tmp_path, advertised)
    _queue_both(tmp_path)

    result = wglink_legacy_queue.convert_outbox(tmp_path, solve_files=wglink_inbox)

    assert _inbox(tmp_path) == []
    assert sorted(item["operationId"] for item in _outbox(tmp_path)) == ["snap-1", "solve-1"]
    assert sorted(result["left"]) == ["snap-1", "solve-1"]


def test_a_solve_whose_file_was_written_when_queued_gets_no_second_file(tmp_path: Path) -> None:
    _advertise(tmp_path, 4)
    _queued(tmp_path, wglink_legacy_queue.KIND_SOLVE, "solve-2", file_written=True)

    result = wglink_legacy_queue.convert_outbox(tmp_path, solve_files=wglink_inbox)

    assert result["converted"] == ["solve-2"]
    assert _inbox(tmp_path) == [] and _outbox(tmp_path) == []


@pytest.mark.parametrize("coordination", [False, None], ids=["off", "on"])
def test_start_up_migrates_earlier_requests_in_both_gate_states(
    monkeypatch, tmp_path: Path, coordination: bool | None
) -> None:
    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    app = _Application(ui)
    module = _load_instance(monkeypatch, f"WGLink_convert_{coordination}", ui, app)
    if coordination is not None:
        module.SETTINGS_PATH.write_text(json.dumps({"automatic_coordination": coordination}))
    ipc = tmp_path / "ipc"
    ipc.mkdir()
    monkeypatch.setattr(module.wglink_workspace, "ipc_folder", lambda **_kwargs: ipc)
    _advertise(ipc, 4)
    _queue_both(ipc)
    order: list[str] = []
    real_convert = module.wglink_legacy_queue.convert_outbox
    monkeypatch.setattr(
        module.wglink_legacy_queue,
        "convert_outbox",
        lambda *a, **k: (order.append("convert"), real_convert(*a, **k))[1],
    )
    module.run(None)
    try:
        assert order == ["convert"]
        assert sorted(payload["operationId"] for payload in _inbox(ipc)) == ["snap-1", "solve-1"]
        assert _outbox(ipc) == []
    finally:
        module.stop(None)


# -- H8: the request is written only after the bundle is whole ------------------
#
# These run the real export (`wglink_send.send`, with its real staging folder
# and its real final `os.replace`) and the real request writer, and look at the
# workspace at the instant each one acts.


def _real_export(monkeypatch, tmp_path: Path, name: str):
    from test_wglink_send import ContractExportManager, Collection, body, component, face

    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    app = _Application(ui)
    app.activeProduct = types.SimpleNamespace(objectType="adsk::fusion::Design")
    module = _load_instance(monkeypatch, name, ui, app)
    ipc, _bundles = _per_request_folders(monkeypatch, module, tmp_path)
    _advertise(ipc, 4)
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(module.wglink_workspace, "capture_document", lambda: False)
    monkeypatch.setattr(module, "_required_manifest_features", lambda: {})
    root = component("Speaker", [body("cabinet", faces=[face("LF")])])
    design = types.SimpleNamespace(
        rootComponent=root,
        exportManager=ContractExportManager(),
        findAttributes=lambda _group, _name: Collection(),
    )
    monkeypatch.setattr(module.wglink_core, "_design", lambda _app: design)
    app.version = "2704.1.53"
    app.activeDocument = types.SimpleNamespace(name="Speaker")
    monkeypatch.setattr(module, "_start_timer", lambda delay, function: None)
    monkeypatch.setattr(module, "_owns_active_ipc_lease", lambda: True)
    return types.SimpleNamespace(module=module, ui=ui, ipc=ipc, workspace=workspace)


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _record_the_order(monkeypatch, fixture) -> list[dict]:
    """Wrap the final bundle rename and the request write; record what is on disk.

    Each request attempt appends the state of the bundle it names, read at the
    moment the request is about to become visible.
    """

    module = fixture.module
    events: list[dict] = []
    real_publish = module.wglink_send._publish
    real_request = module.wglink_inbox.publish_wg_request

    def publish(temp, target, overwrite):
        real_publish(temp, target, overwrite)
        events.append({"event": "bundle", "target": Path(target)})

    def request(path, payload):
        bundle = fixture.workspace / payload["bundlePath"]
        manifest = json.loads((bundle / "wgreturn.json").read_text(encoding="utf-8")) if bundle.is_dir() else {}
        members = {
            member: (bundle / member).is_file() and _sha(bundle / member) == record["sha256"]
            for member, record in manifest.get("files", {}).items()
        }
        events.append({
            "event": "request",
            "bundle_published_first": any(e["event"] == "bundle" for e in events),
            "bundle_is_dir": bundle.is_dir(),
            "manifest_hash_matches": bundle.is_dir() and _sha(bundle / "wgreturn.json") == payload["manifestSha256"],
            "members": members,
            "siblings": sorted(p.name for p in bundle.parent.iterdir() if p.name.startswith(".")),
            "request_visible": path.exists(),
        })
        return real_request(path, payload)

    monkeypatch.setattr(module.wglink_send, "_publish", publish)
    monkeypatch.setattr(module.wglink_inbox, "publish_wg_request", request)
    return events


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_the_request_becomes_visible_only_after_the_whole_bundle_is_in_place(
    monkeypatch, tmp_path: Path, operation: str
) -> None:
    """H8: at the moment WG could see the request, its bundle is final and hashes to it."""

    fixture = _real_export(monkeypatch, tmp_path, f"WGLink_order_{operation}")
    events = _record_the_order(monkeypatch, fixture)

    _run(fixture.module, operation)

    assert [e["event"] for e in events] == ["bundle", "request"]
    request = events[1]
    assert request["bundle_published_first"]
    assert request["bundle_is_dir"]
    assert request["manifest_hash_matches"]
    assert request["members"] and all(request["members"].values()), request["members"]
    assert "assembly.step" in request["members"]
    # No staging folder and no replaced-bundle backup is left beside it.
    assert request["siblings"] == []
    assert request["request_visible"] is False
    [payload] = _inbox(fixture.ipc)
    assert _sha(fixture.workspace / payload["bundlePath"] / "wgreturn.json") == payload["manifestSha256"]


def test_every_retry_of_the_request_also_finds_the_bundle_whole(monkeypatch, tmp_path: Path) -> None:
    fixture = _real_export(monkeypatch, tmp_path, "WGLink_order_retry")
    monkeypatch.setattr(fixture.module, "_retry_pause", lambda _seconds: None)
    events = _record_the_order(monkeypatch, fixture)
    recorded = fixture.module.wglink_inbox.publish_wg_request
    attempts: list[int] = []

    def flaky(path, payload):
        attempts.append(1)
        if len(attempts) == 1:
            recorded(path, payload)  # the file lands, the rename then reports an error
            raise OSError("reported an error after it landed")
        return recorded(path, payload)

    monkeypatch.setattr(fixture.module.wglink_inbox, "publish_wg_request", flaky)

    _run(fixture.module, "send")

    requests = [e for e in events if e["event"] == "request"]
    assert len(requests) == 2
    for request in requests:
        assert request["bundle_published_first"] and request["manifest_hash_matches"]
        assert all(request["members"].values()) and request["siblings"] == []


def _request_files(ipc: Path) -> list[str]:
    inbox = ipc / ".wg-solve-requests"
    return sorted(p.name for p in inbox.iterdir()) if inbox.exists() else []


def test_a_failure_between_the_bundle_and_the_request_leaves_no_request_file(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _real_export(monkeypatch, tmp_path, "WGLink_order_write_fails")
    monkeypatch.setattr(fixture.module, "_retry_pause", lambda _seconds: None)

    def refuse(_path, _payload):
        raise OSError("disk full")

    monkeypatch.setattr(fixture.module.wglink_inbox, "_write_json_atomically", refuse)

    _run(fixture.module, "solve")

    assert _request_files(fixture.ipc) == []  # no request, and no staging litter
    [bundle] = [p for p in (fixture.workspace / "wgreturn").iterdir() if not p.name.startswith(".")]
    assert (bundle / "wgreturn.json").is_file() and (bundle / "assembly.step").is_file()
    assert fixture.ui.messages[-1][0] == "Waveguide Generator"


def test_a_failure_taking_the_return_reference_leaves_no_request_file(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _real_export(monkeypatch, tmp_path, "WGLink_order_reference_fails")

    def unreadable(_bundle, _workspace):
        raise OSError("manifest unreadable")

    monkeypatch.setattr(fixture.module.wglink_inbox, "return_reference", unreadable)

    _run(fixture.module, "send")

    assert _request_files(fixture.ipc) == []
    assert fixture.ui.messages[-1][0] == "Waveguide Generator"


def test_a_bundle_that_could_not_be_published_is_never_requested(monkeypatch, tmp_path: Path) -> None:
    fixture = _real_export(monkeypatch, tmp_path, "WGLink_order_bundle_fails")
    requested: list[Path] = []
    real_request = fixture.module.wglink_inbox.publish_wg_request
    monkeypatch.setattr(
        fixture.module.wglink_inbox, "publish_wg_request",
        lambda path, payload: requested.append(path) or real_request(path, payload),
    )

    def refuse(_temp, _target, _overwrite):
        raise OSError("rename refused")

    monkeypatch.setattr(fixture.module.wglink_send, "_publish", refuse)

    _run(fixture.module, "send")

    assert requested == []
    assert _request_files(fixture.ipc) == []
    assert not [p for p in (fixture.workspace / "wgreturn").iterdir() if p.name.endswith(".wgreturn")]
    assert fixture.ui.messages[-1][0] == "Waveguide Generator"


@pytest.mark.parametrize("outcome", ["delivered", "rejected", "conflict", "expired"])
def test_startup_settles_earlier_answers_without_resending(monkeypatch, tmp_path: Path, outcome: str) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_earlier_answer_" + outcome)
    _queued(fixture.ipc, SNAPSHOT, "settled-1", answered=True)
    path = fixture.ipc / ".wglink-outbox" / "settled-1.json"
    item = json.loads(path.read_text())
    item["answer"] = {"outcome": outcome, "message": "earlier WG reason"}
    path.write_text(json.dumps(item))
    fixture.module._convert_outbox_at_startup()
    assert _inbox(fixture.ipc) == [] and not path.exists()
    assert len(fixture.ui.messages) == (0 if outcome == "delivered" else 1)
    if outcome == "rejected":
        assert "earlier WG reason" in "\n".join(fixture.ui.text_palette.written)
        assert "Send or Solve again" in fixture.ui.messages[0][1]


def test_a_held_earlier_answer_is_never_migrated_as_unanswered(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_held_earlier_answer")
    _queued(fixture.ipc, SNAPSHOT, "settled-1", answered=True)
    monkeypatch.setattr(fixture.module.wglink_legacy_queue.LegacyQueue, "delete", lambda *a: False)
    fixture.module._convert_outbox_at_startup()
    assert _inbox(fixture.ipc) == []
    assert len(_outbox(fixture.ipc)) == 1


@pytest.mark.parametrize("coordination", [False, True], ids=["off", "on"])
def test_file_only_startup_never_contacts_an_advertised_endpoint(monkeypatch, tmp_path: Path, coordination: bool) -> None:
    import socket
    import threading
    import urllib.request

    fixture = _transfer(monkeypatch, tmp_path, "WGLink_file_startup", live=True)
    module = fixture.module
    module.SETTINGS_PATH.write_text(json.dumps({"automatic_coordination": coordination}))
    (fixture.ipc / "wg-endpoint.json").write_text('{"baseUrl":"http://127.0.0.1:1"}')
    attempted = []
    def forbidden(*_args, **_kwargs):
        attempted.append("network")
        raise AssertionError("startup must use files")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(urllib.request, "build_opener", forbidden)
    before = set(threading.enumerate())
    module.run(None)
    try:
        if module._watch_handler is not None:
            module._watch_handler.notify(None)
        assert module._owned
        assert set(fixture.app.events) == ({module.FOLLOWUP_EVENT_ID, module.WATCH_EVENT_ID}
                                          if coordination else {module.FOLLOWUP_EVENT_ID})
        assert all(thread.name == "WGLinkFileRequests" for thread in set(threading.enumerate()) - before)
        assert not (fixture.ipc / ".wglink-installation.json").exists()
    finally:
        module.stop(None)
    assert attempted == []


def test_earlier_refusal_is_kept_until_its_notice_can_be_shown(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_earlier_refusal_restart")
    _queued(fixture.ipc, SNAPSHOT, "settled-1", answered=True)
    path = fixture.ipc / ".wglink-outbox" / "settled-1.json"
    item = json.loads(path.read_text())
    item["answer"] = {"outcome": "rejected", "reason": "snapshot_invalid", "message": "malformed"}
    path.write_text(json.dumps(item))
    original = fixture.module._ui
    monkeypatch.setattr(fixture.module, "_ui", lambda: None)
    fixture.module._convert_outbox_at_startup()
    assert path.exists() and _inbox(fixture.ipc) == []
    monkeypatch.setattr(fixture.module, "_ui", original)
    fixture.module._convert_outbox_at_startup()
    assert not path.exists() and _inbox(fixture.ipc) == []
    assert "malformed" in "\n".join(fixture.ui.text_palette.written)


@pytest.mark.parametrize("outcome,wording", [
    ("rejected", "did not accept"),
    ("conflict", "already names a different request"),
    ("expired", "within 7 days"),
])
@pytest.mark.parametrize("kind,command", [(SNAPSHOT, "Send to WG"), (SOLVE, "Solve in WG")])
def test_earlier_delivery_notice_names_its_actual_outcome(
    monkeypatch, tmp_path, outcome, wording, kind, command,
):
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_earlier_notice")
    _queued(fixture.ipc, kind, "settled-1", answered=True)
    path = fixture.ipc / ".wglink-outbox" / "settled-1.json"
    item = json.loads(path.read_text())
    item["answer"] = {"outcome": outcome, "reason": "snapshot_invalid", "message": "malformed"}
    path.write_text(json.dumps(item))
    fixture.module._convert_outbox_at_startup()
    text = fixture.ui.messages[-1][1]
    assert text == "Waveguide Generator could not receive an earlier model. Send or Solve again from Fusion."
    detail = "\n".join(fixture.ui.text_palette.written)
    assert outcome in detail and kind in detail and "settled-1" in detail
    assert "malformed" in detail and "snapshot_invalid" in detail
    assert fixture.module.wglink_protocol.user_text_violations(text) == []


def test_earlier_delivery_notice_is_shown_once_when_delete_fails(monkeypatch, tmp_path):
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_held_notice")
    _queued(fixture.ipc, SNAPSHOT, "settled-1", answered=True)
    path = fixture.ipc / ".wglink-outbox" / "settled-1.json"
    item = json.loads(path.read_text())
    item["answer"] = {"outcome": "rejected", "message": "malformed"}
    path.write_text(json.dumps(item))
    monkeypatch.setattr(fixture.module.wglink_legacy_queue.LegacyQueue, "delete", lambda *args: False)
    fixture.module._convert_outbox_at_startup()
    fixture.module._convert_outbox_at_startup()
    assert len(fixture.ui.messages) == 1
    # A new queue object reads the durable marker on the next startup.
    assert fixture.module.wglink_legacy_queue.LegacyQueue(fixture.ipc).notice_shown("settled-1")
