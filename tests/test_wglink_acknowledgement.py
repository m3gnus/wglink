"""WG's acknowledgement of a Send or Solve (S4-D2, handoff H9, add-in half).

WG writes ``<ipc>/.wg-solve-acks/<requestId>.json`` once it holds or refuses a
request. Until that file appears WGLink says only that the request was written
to WG's inbox; it never says "solving". The fixtures below have the exact shape
of WG's ``write_acknowledgement()`` output.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from test_wglink_transfer import (
    SNAPSHOT,
    SOLVE,
    _advertise,
    _deliver_followups,
    _inbox,
    _run,
    _transfer,
)


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
import wglink_inbox  # noqa: E402


def _ack_payload(request_id: str, outcome: str, reason: str | None = None, **extra) -> dict:
    payload = {
        "schemaVersion": 1,
        "commandId": request_id,
        "operationId": request_id,
        "outcome": outcome,
        "reason": reason,
        "jobId": None,
        "digest": None if reason else "sha256:" + "b" * 64,
        "at": "2026-09-29T10:00:00Z",
    }
    payload.update(extra)
    return payload


def _write_ack(ipc: Path, request_id: str, payload: object) -> Path:
    folder = ipc / wglink_inbox.ACK_DIRECTORY
    folder.mkdir(exist_ok=True)
    path = folder / f"{request_id}.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload, indent=2, sort_keys=True))
    return path


def _advertise_acks(ipc: Path, value: object = 1) -> None:
    path = ipc / wglink_inbox.CAPABILITIES_FILENAME
    payload = json.loads(path.read_text())
    payload["solveAcknowledgement"] = value
    path.write_text(json.dumps(payload))


def _acknowledging(monkeypatch, tmp_path: Path, name: str, operation: str = "solve"):
    fixture = _transfer(monkeypatch, tmp_path, name)
    _advertise_acks(fixture.ipc)
    _run(fixture.module, operation)
    [payload] = _inbox(fixture.ipc)
    return fixture, payload["operationId"]


def _tick(fixture) -> list[str]:
    """Run the timers now due and deliver the follow-ups; the new message titles."""

    before = len(fixture.ui.messages)
    due = list(fixture.timers)
    fixture.timers.clear()
    for _delay, function in due:
        function()
    _deliver_followups(fixture.module, fixture.app)
    return [title for title, _text in fixture.ui.messages[before:]]


def _shown(fixture) -> dict:
    """What the request palette shows now (its page loads on first look)."""

    palette = fixture.ui.palettes.items["hornlab_wglink_requests"]
    if not palette.loaded:
        palette.load()
    return palette.shown


def _claim(fixture, request_id: str) -> None:
    folder = fixture.ipc / wglink_inbox.SOLVE_REQUESTS_DIRECTORY
    (folder / f"{request_id}.json").rename(folder / f".wglink-claim-{request_id}.json")


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_command_time_wording_says_only_sent_and_waiting(
    monkeypatch, tmp_path: Path, operation: str
) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, f"WGLink_ack_words_{operation}", operation)

    assert fixture.ui.messages == []
    shown = _shown(fixture)
    assert shown["headline"] == "Horn A · Sent to Waveguide Generator. Waiting for it to pick up the model."
    assert "solving" not in json.dumps(shown).lower()
    assert shown["diagnostics"]["requestId"] == request_id


def test_an_accepted_send_ends_open_in_wg(monkeypatch, tmp_path: Path) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, "WGLink_ack_ok_send", "send")
    _claim(fixture, request_id)
    _write_ack(fixture.ipc, request_id, _ack_payload(request_id, "accepted"))

    assert _tick(fixture) == []  # accepted is never a message box

    shown = _shown(fixture)
    assert shown["headline"] == "Horn A is open in Waveguide Generator."
    assert shown["terminal"] is True
    assert fixture.module._recent_outcomes[-1]["outcome"] == "accepted"
    assert fixture.timers == []


def test_a_solve_accepted_by_a_wg_without_status_says_follow_it_in_wg(
    monkeypatch, tmp_path: Path
) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, "WGLink_ack_ok_solve", "solve")
    _claim(fixture, request_id)
    _write_ack(fixture.ipc, request_id, _ack_payload(request_id, "accepted"))

    assert _tick(fixture) == []

    shown = _shown(fixture)
    assert shown["headline"] == "Horn A · Accepted. Follow progress in Waveguide Generator."
    assert "solving" not in shown["headline"].lower()
    assert shown["terminal"] is True
    assert fixture.timers == []


@pytest.mark.parametrize(
    "reason",
    [
        "This add-in is older than Waveguide Generator; update WGLink.",
        "Another request already holds this model.",
    ],
    ids=["old-addin", "conflict"],
)
def test_a_refusal_shows_wgs_own_words_in_the_palette(monkeypatch, tmp_path: Path, reason: str) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, "WGLink_ack_refused")
    _claim(fixture, request_id)
    _write_ack(fixture.ipc, request_id, _ack_payload(request_id, "refused", reason))

    assert _tick(fixture) == []

    shown = _shown(fixture)
    assert shown["headline"] == "Waveguide Generator could not take Horn A."
    assert shown["detail"] == reason
    assert shown["terminal"] is True
    assert fixture.module._recent_outcomes[-1]["outcome"] == "refused"


def test_a_refusal_in_internal_words_keeps_them_in_diagnostics(monkeypatch, tmp_path: Path) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, "WGLink_ack_refused_jargon")
    _claim(fixture, request_id)
    reason = "The request is not a valid CAD Link request: manifest schema 7."
    _write_ack(fixture.ipc, request_id, _ack_payload(request_id, "refused", reason))

    _tick(fixture)

    shown = _shown(fixture)
    assert shown["headline"] == "Waveguide Generator could not take Horn A."
    assert shown["detail"] is None
    assert shown["diagnostics"]["receiptReason"] == reason


def test_an_ack_that_appears_late_moves_the_wording_on(monkeypatch, tmp_path: Path) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, "WGLink_ack_late", "send")
    _claim(fixture, request_id)

    for _ in range(3):
        assert _tick(fixture) == []
        assert len(fixture.timers) == 1  # one timer re-armed at a time
        assert fixture.timers[0][0] == fixture.module.wglink_requests.POLL_VISIBLE_SECONDS
    _write_ack(fixture.ipc, request_id, _ack_payload(request_id, "accepted"))

    assert _tick(fixture) == []
    assert fixture.module._recent_outcomes[-1]["outcome"] == "accepted"
    assert fixture.timers == []


@pytest.mark.parametrize("content", ["", "{not json", "[]", '{"schemaVersion": 1'], ids=["empty", "junk", "list", "cut"])
def test_an_unparsable_ack_is_not_yet(monkeypatch, tmp_path: Path, content: str) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, "WGLink_ack_junk")
    _claim(fixture, request_id)
    _write_ack(fixture.ipc, request_id, content)

    assert _tick(fixture) == []
    assert len(fixture.timers) == 1


def test_no_ack_after_the_wait_is_taken_not_confirmed(monkeypatch, tmp_path: Path) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, "WGLink_ack_timeout")
    _claim(fixture, request_id)
    monkeypatch.setattr(fixture.module.wglink_inbox, "ACK_WAIT_SECONDS", 0.0)

    assert _tick(fixture) == []

    shown = _shown(fixture)
    assert shown["headline"] == "Waveguide Generator picked up Horn A but has not confirmed it yet."
    assert "refused" not in json.dumps(shown).lower() and "solving" not in json.dumps(shown).lower()
    assert fixture.module._recent_outcomes[-1]["outcome"] == "takenUnconfirmed"
    # Still followed: a late receipt or status moves it on.
    assert len(fixture.timers) == 1


def test_a_request_still_in_the_inbox_after_a_minute_is_not_picked_up(
    monkeypatch, tmp_path: Path
) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, "WGLink_ack_untaken")
    monkeypatch.setattr(fixture.module.wglink_inbox, "SOLVE_PICKUP_NOTICE_SECONDS", 0.0)

    assert _tick(fixture) == []
    assert _shown(fixture)["headline"] == "Waveguide Generator has not picked up Horn A yet."
    assert fixture.module._recent_outcomes[-1]["outcome"] == "notTaken"
    assert len(_inbox(fixture.ipc)) == 1


@pytest.mark.parametrize(
    "extra",
    [{"manifestSha256": "sha256:" + "c" * 64}, {"kind": "receive_snapshot"}],
    ids=["other-manifest", "other-kind"],
)
def test_an_ack_naming_another_manifest_or_kind_is_ignored(
    monkeypatch, tmp_path: Path, extra: dict
) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, "WGLink_ack_mismatch")  # a Solve
    _claim(fixture, request_id)
    _write_ack(fixture.ipc, request_id, _ack_payload(request_id, "accepted", **extra))

    assert _tick(fixture) == []
    assert len(fixture.timers) == 1


def test_matching_manifest_and_kind_are_accepted(monkeypatch, tmp_path: Path) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, "WGLink_ack_match")
    [payload] = [p for p in [_inbox(fixture.ipc)[0]]]
    _claim(fixture, request_id)
    _write_ack(
        fixture.ipc,
        request_id,
        _ack_payload(request_id, "accepted", manifestSha256=payload["manifestSha256"], kind=SOLVE),
    )

    assert _tick(fixture) == []
    assert fixture.module._recent_outcomes[-1]["outcome"] == "accepted"


def test_unknown_fields_and_other_schema_versions(tmp_path: Path) -> None:
    rid = "0b1c2d3e-0000-4000-8000-000000000001"
    _write_ack(tmp_path, rid, _ack_payload(rid, "accepted", jobId="job-1", future="x"))
    assert wglink_inbox.read_acknowledgement(tmp_path, rid) == ("accepted", None)

    _write_ack(tmp_path, rid, _ack_payload(rid, "accepted", schemaVersion=2))
    assert wglink_inbox.read_acknowledgement(tmp_path, rid) is None

    _write_ack(tmp_path, rid, _ack_payload(rid, "maybe"))
    assert wglink_inbox.read_acknowledgement(tmp_path, rid) is None

    _write_ack(tmp_path, rid, _ack_payload(rid, "accepted", operationId="another"))
    assert wglink_inbox.read_acknowledgement(tmp_path, rid) is None

    assert wglink_inbox.read_acknowledgement(tmp_path, "../escape") is None
    assert wglink_inbox.read_acknowledgement(tmp_path / "missing", rid) is None


@pytest.mark.parametrize("value", [None, 0, True, "1"], ids=["absent", "zero", "bool", "string"])
def test_only_a_capability_of_at_least_one_enables_ack_reading(tmp_path: Path, value: object) -> None:
    _advertise(tmp_path, 4)
    if value is not None:
        _advertise_acks(tmp_path, value)
    assert wglink_inbox.wg_acknowledges(tmp_path) is False

    _advertise_acks(tmp_path, 1)
    assert wglink_inbox.wg_acknowledges(tmp_path) is True
    payload = json.loads((tmp_path / wglink_inbox.CAPABILITIES_FILENAME).read_text())
    payload["schemaVersion"] = 2
    (tmp_path / wglink_inbox.CAPABILITIES_FILENAME).write_text(json.dumps(payload))
    assert wglink_inbox.wg_acknowledges(tmp_path) is False


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_without_the_capability_no_receipt_is_read_and_taken_is_the_end(
    monkeypatch, tmp_path: Path, operation: str
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_ack_legacy_{operation}")
    _run(fixture.module, operation)
    [payload] = _inbox(fixture.ipc)
    # Even a receipt left in the folder is not read.
    _write_ack(fixture.ipc, payload["operationId"], _ack_payload(payload["operationId"], "refused", "no"))
    _claim(fixture, payload["operationId"])

    assert _tick(fixture) == []

    shown = _shown(fixture)
    assert shown["headline"] == (
        "Horn A · Waveguide Generator picked it up. Follow progress in Waveguide Generator."
        if operation == "solve" else "Horn A · Waveguide Generator has it.")
    assert "solving" not in shown["headline"].lower()
    assert fixture.module._recent_outcomes[-1]["outcome"] == "taken"
    assert fixture.timers == []


# -- packaged pair: files written by WG's real write_acknowledgement() ------------

FIXTURES = ROOT / "tests" / "fixtures" / "wg_acks"
PACKAGED = {
    "accepted": "accepted",
    "refused-old-addin": "refused",
    "refused-invalid": "refused",
    "refused-conflict": "refused",
}


def _packaged(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize("name", sorted(PACKAGED))
def test_the_reader_understands_what_wgs_writer_produced(tmp_path: Path, name: str) -> None:
    payload = _packaged(name)
    _write_ack(tmp_path, payload["commandId"], payload)

    outcome, reason = wglink_inbox.read_acknowledgement(
        tmp_path,
        payload["commandId"],
        kind=payload["kind"],
        manifest_sha256=payload["manifestSha256"],
    )

    assert outcome == PACKAGED[name]
    assert reason == payload["reason"]
    if outcome == "refused":
        assert reason  # WG's text, shown verbatim
    # A request of another kind or manifest under the same id is not this ack's.
    assert wglink_inbox.read_acknowledgement(tmp_path, payload["commandId"], kind="other") is None
    assert (
        wglink_inbox.read_acknowledgement(
            tmp_path, payload["commandId"], manifest_sha256="sha256:" + "f" * 64
        )
        is None
    )


@pytest.mark.parametrize("name", sorted(PACKAGED))
def test_a_packaged_ack_drives_the_palette(monkeypatch, tmp_path: Path, name: str) -> None:
    fixture, request_id = _acknowledging(monkeypatch, tmp_path, f"WGLink_ack_packaged_{name}")
    [request] = _inbox(fixture.ipc)
    _claim(fixture, request_id)
    # Only what names this request changes: its id, manifest and kind.
    payload = dict(
        _packaged(name),
        commandId=request_id,
        operationId=request_id,
        manifestSha256=request["manifestSha256"],
        kind=request["kind"],
    )
    _write_ack(fixture.ipc, request_id, payload)

    assert _tick(fixture) == []

    shown = _shown(fixture)
    assert shown["terminal"] is True
    if PACKAGED[name] == "accepted":
        assert shown["headline"] == "Horn A · Accepted. Follow progress in Waveguide Generator."
    else:
        assert shown["headline"] == "Waveguide Generator could not take Horn A."
        assert shown["detail"] == payload["reason"]
