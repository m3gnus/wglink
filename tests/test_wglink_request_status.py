"""The WG request-status contract: its validator and the words a user reads."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
import wglink_protocol as protocol  # noqa: E402


def _record(state: str, **fields):
    return {
        "contract": "wg-request-status", "schemaVersion": 1, "seq": 3,
        "requestId": "req-1", "operationId": "req-1", "kind": "prepare_and_solve",
        "manifestSha256": "sha256:" + "a" * 64, "documentName": "Horn A",
        "updatedAt": "2026-10-25T10:00:00Z", "state": state, **fields,
    }


EVERY_STATE = [
    _record("accepted", kind="receive_snapshot"),
    _record("preparing"),
    _record("needsYou", prompt={"code": "mesh_too_large", "nextStep": "Change the mesh resolution.",
                                "options": [{"id": "solve_anyway", "label": "Solve anyway"},
                                            {"id": "dismiss", "label": "Dismiss request"}],
                                "answerIn": "wg"}),
    _record("queued"),
    _record("solving", progress={"fraction": 0.5, "frequency": {"done": 12, "total": 36}}),
    _record("solving", progress={"fraction": 0.1}),
    _record("solved", result={"jobId": "job-1", "runNumber": 89,
                              "mesh": {"trianglesSolved": 19812, "solvedFraction": 0.25, "domainKind": "quarter"}},
            notice="fullModel"),
    _record("couldNotFinish", phase="preparing", failure={"recorded": True}),
    _record("couldNotFinish", phase="solving", failure={"recorded": True}),
    _record("couldNotFinish", phase="preparing",
            failure={"recorded": False, "message": "Horn A changed after Fusion asked WG to solve it.",
                     "nextStep": "Send it again from Fusion."}),
    _record("cancelled", cancelReason="stopped"),
    _record("cancelled", cancelReason="replaced"),
    _record("cancelled", cancelReason="dismissed"),
    _record("refused"),
    _record("somethingNewer"),
]


@pytest.mark.parametrize("record", EVERY_STATE, ids=lambda r: r["state"] + "-" + str(r.get("phase") or r.get("cancelReason") or ""))
def test_every_state_validates_and_reads_in_plain_words(record):
    valid = protocol.validate_request_status(record, request_id="req-1")
    assert valid is not None and valid["state"] == record["state"]
    text = protocol.request_status_text(valid)
    assert text["headline"] and text["actions"]
    words = " ".join([text["headline"], text["detail"] or "", *text["options"],
                      *(action["label"] for action in text["actions"])])
    assert protocol.user_text_violations(words) == [], words
    for internal in ("req-1", "job-1", "sha256", "mesh_too_large", "prepare_and_solve"):
        assert internal not in words


def test_the_texts_say_what_happened_to_the_model():
    by_state = {r["state"] + str(r.get("phase") or r.get("cancelReason") or ""): protocol.request_status_text(
        protocol.validate_request_status(r)) for r in EVERY_STATE}
    assert by_state["accepted"]["headline"] == "Horn A is open in Waveguide Generator."
    assert by_state["solving"]["headline"] in {"Horn A · Solving · 12 of 36 frequencies", "Horn A · Solving…"}
    needs = by_state["needsYou"]
    assert needs["detail"] == "Change the mesh resolution."
    assert needs["options"] == ["Solve anyway", "Dismiss request"]
    assert [a["id"] for a in needs["actions"]] == ["openWg", "openSourcesMesh"]
    solved = by_state["solved"]
    assert solved["headline"] == "Horn A · Solved (run 89)."
    assert solved["detail"].startswith("Solved the whole model")
    assert [a["id"] for a in solved["actions"]] == ["showResults"]
    assert by_state["couldNotFinishpreparing"]["headline"] in {
        "Waveguide Generator could not prepare Horn A. This has been recorded for the developers.",
        "Horn A changed after Fusion asked WG to solve it.",
    }
    assert "finish solving Horn A" in by_state["couldNotFinishsolving"]["headline"]
    assert by_state["cancelledreplaced"]["headline"] == "Horn A · Replaced by a newer solve."
    assert "Open Waveguide Generator" in by_state["somethingNewer"]["headline"]


def test_a_user_action_failure_reads_its_own_words_and_an_internal_one_never_does():
    user = protocol.request_status_text(protocol.validate_request_status(EVERY_STATE[9]))
    assert user["headline"] == "Horn A changed after Fusion asked WG to solve it."
    assert user["detail"] == "Send it again from Fusion."
    internal = _record("couldNotFinish", phase="preparing",
                       failure={"recorded": True, "message": "gmsh crashed"})
    text = protocol.request_status_text(protocol.validate_request_status(internal))
    assert "gmsh" not in text["headline"] and "recorded for the developers" in text["headline"]


def test_a_missing_document_name_reads_as_this_model():
    text = protocol.request_status_text(protocol.validate_request_status(
        _record("couldNotFinish", documentName=None, failure={"recorded": True})))
    assert text["headline"].startswith("Waveguide Generator could not prepare this model.")
    assert protocol.request_status_text(protocol.validate_request_status(_record("queued", documentName="")))[
        "headline"].startswith("This model ·")


@pytest.mark.parametrize("change", [
    {"contract": "something-else"}, {"schemaVersion": 2}, {"schemaVersion": True},
    {"requestId": "../x"}, {"requestId": "req-2"}, {"operationId": "req-2"},
    {"state": ""}, {"seq": -1}, {"seq": True},
])
def test_what_is_not_this_requests_status_reads_as_none(change):
    assert protocol.validate_request_status({**_record("queued"), **change}, request_id="req-1") is None
    assert protocol.validate_request_status("not a record") is None


def test_unknown_fields_are_dropped_and_malformed_optional_fields_read_absent():
    record = _record(
        "solving", future={"x": 1}, progress={"fraction": 1.5}, phase="later",
        prompt="x", result={"jobId": ""}, notice="unheardOf", cancelReason=3,
    )
    valid = protocol.validate_request_status(record)
    assert set(valid) == {"contract", "schemaVersion", "seq", "requestId", "operationId", "kind",
                          "manifestSha256", "documentName", "updatedAt", "state"}
    solved = protocol.validate_request_status(_record(
        "solved", result={"jobId": "job-1", "runNumber": 0, "mesh": {"trianglesSolved": 5, "solvedFraction": 0}}))
    assert solved["result"] == {"jobId": "job-1", "runNumber": None}


def test_terminal_states_and_unknown_states_are_under_way():
    assert {state for state in protocol.REQUEST_STATUS_STATES
            if protocol.request_status_terminal({"state": state})} == {
        "accepted", "solved", "couldNotFinish", "cancelled", "refused"}
    assert not protocol.request_status_terminal({"state": "somethingNewer"})


def test_the_forbidden_terms_match_whole_words_only():
    assert protocol.user_text_violations("The bundle was refused") == ["bundle", "refused"]
    assert protocol.user_text_violations("Gateway operational") == []
    assert protocol.user_text_violations("Source PASSIVE_CARDIOID") == ["PASSIVE_CARDIOID"]


@pytest.mark.parametrize("sentence", [
    "Return to Fusion and review the options.",
    "In the event of an error, return to Fusion.",
    "A byte of information is missing from this description.",
])
def test_ordinary_words_survive_status_validation_and_display(sentence):
    record = protocol.validate_request_status(_record("needsYou", prompt={
        "nextStep": sentence, "options": [{"id": "review", "label": "Review options"}],
    }))
    text = protocol.request_status_text(record)
    assert text["detail"] == sentence
    assert text["options"] == ["Review options"]


@pytest.mark.parametrize("sentence", [
    "The return folder is missing.", "The return is missing.",
    "The event handler stopped.", "The custom event was lost.",
    "Set options['overwrite'].", "KeyError: unknown value.",
    "The byte limit is too small.", "The file is 128 bytes.",
])
def test_technical_uses_of_ambiguous_words_still_stay_out_of_status(sentence):
    record = protocol.validate_request_status(_record("needsYou", prompt={"nextStep": sentence}))
    assert record["prompt"]["nextStep"] is None
    assert protocol.request_status_text(record)["detail"] is None


HOSTILE = [
    {"prompt": {"options": 5}}, {"prompt": {"options": True}}, {"prompt": "hi"},
    {"prompt": {"code": {"a": 1}, "nextStep": ["x"]}},
    {"state": "solving", "progress": {"fraction": 10 ** 400}},
    {"state": "solving", "progress": {"fraction": float("nan"), "frequency": {"done": 10 ** 400, "total": 1}}},
    {"state": "solved", "result": {"jobId": "j", "runNumber": 10 ** 400,
                                   "mesh": {"trianglesSolved": 5, "solvedFraction": 10 ** 400}}},
    {"state": ["x"]}, {"state": "x" * 5000}, {"state": None}, {"seq": "1"}, {"seq": 10 ** 400},
    {"documentName": "a\nb\x00c" + "d" * 5000}, {"documentName": 5},
    {"state": "couldNotFinish", "failure": {"recorded": False, "message": {"a": 1}}},
    {"state": "couldNotFinish", "failure": "x"}, {"requestId": ["req-1"]}, {"operationId": {"x": 1}},
    {"job": {"jobId": 5}}, {"notice": ["fullModel"]}, {"cancelReason": {}},
]


@pytest.mark.parametrize("change", HOSTILE, ids=lambda change: ",".join(change))
def test_hostile_records_never_raise_and_never_reach_a_user_raw(change):
    valid = protocol.validate_request_status({**_record("needsYou"), **change})
    text = protocol.request_status_text(valid or {})
    assert text["headline"] and "\x00" not in text["headline"] and len(text["headline"]) < 700
    assert protocol.request_status_terminal(valid or change) in {True, False}
    assert protocol.request_status_terminal({"state": ["x"]}) is False
    assert protocol.request_status_terminal("not a mapping") is False
    assert protocol.request_status_text(None)["headline"]  # type: ignore[arg-type]


def test_technical_words_from_wg_never_reach_the_user():
    record = protocol.validate_request_status(_record(
        "couldNotFinish", phase="preparing",
        failure={"recorded": False, "message": "Traceback (most recent call last): KeyError 'x'",
                 "nextStep": "Check the bundle manifest."}))
    assert record["failure"] == {"recorded": False, "message": None, "nextStep": None}
    text = protocol.request_status_text(record)
    assert text["headline"] == "Waveguide Generator could not finish Horn A."
    assert text["detail"] == "Open Waveguide Generator to see what to do."
    assert "recorded" not in text["headline"]
    held = protocol.validate_request_status(_record("needsYou", prompt={
        "nextStep": "The ingest gate refused it.", "options": [{"id": "x", "label": "Retry operation"}]}))
    assert held["prompt"]["nextStep"] is None and held["prompt"]["options"] == []


def test_a_recorded_failure_never_carries_wgs_words():
    record = protocol.validate_request_status(_record(
        "couldNotFinish", failure={"recorded": True, "message": "Horn A changed."}))
    assert record["failure"] == {"recorded": True}
    assert protocol.request_status_text(record)["headline"] == protocol.could_not_prepare_text("Horn A")
    assert protocol.could_not_prepare_text(None) == (
        "Waveguide Generator could not prepare this model. This has been recorded for the developers.")


def test_unknown_states_are_known_as_unknown():
    assert protocol.request_status_known({"state": "solving"})
    assert not protocol.request_status_known({"state": "somethingNewer"})
    assert not protocol.request_status_known({"state": ["solving"]})


@pytest.mark.parametrize("name", ["Gate horn", "Error test", "Operation 7", "Bundle B"])
def test_a_document_name_is_never_mistaken_for_internal_detail(name):
    record = protocol.validate_request_status(_record(
        "couldNotFinish", documentName=name,
        failure={"recorded": False, "message": f"{name} changed after Fusion asked WG to solve it.",
                 "nextStep": "Send it again from Fusion."}))
    assert record["failure"]["message"] == f"{name} changed after Fusion asked WG to solve it."
    assert protocol.request_status_text(record)["headline"].startswith(name)
    assert protocol.user_text_violations(f"{name} is ready", names=[name]) == []
    assert protocol.user_text_violations(f"{name}: the gate refused it", names=[name]) == ["gate", "refused"]


def test_a_return_the_preflight_rules_block_says_what_to_change():
    words = "2 sources share the role HF. Use one source per role."
    blocked = protocol.validate_request_status(
        _record("refused", failure={"recorded": False, "message": words}), request_id="req-1")
    text = protocol.request_status_text(blocked)
    assert text["headline"] == "Waveguide Generator could not take Horn A."
    assert text["detail"] == words
    plain = protocol.request_status_text(protocol.validate_request_status(_record("refused"), request_id="req-1"))
    assert plain["detail"] is None
    # Words that read as internal detail are dropped, never shown.
    leaky = protocol.validate_request_status(
        _record("refused", failure={"recorded": False, "message": "The manifest was refused."}), request_id="req-1")
    assert protocol.request_status_text(leaky)["detail"] is None


@pytest.mark.parametrize(("frequency", "headline"), [
    ({"done": 5, "total": 8, "channel": {"index": 2, "count": 3}},
     "Horn A · Solving · channel 2 of 3 · 5 of 8 frequencies"),
    ({"done": 5, "total": 8, "channel": {"index": 1, "count": 1}}, "Horn A · Solving · 5 of 8 frequencies"),
    ({"done": 5, "total": 8}, "Horn A · Solving · 5 of 8 frequencies"),
], ids=["multi-channel", "one-channel", "no-channel"])
def test_a_multi_channel_solve_names_its_channel(frequency, headline):
    record = protocol.validate_request_status(_record(
        "solving", progress={"fraction": 0.5, "frequency": frequency}))

    text = protocol.request_status_text(record)

    assert text["headline"] == headline
    assert protocol.user_text_violations(text["headline"], names=("Horn A",)) == []
