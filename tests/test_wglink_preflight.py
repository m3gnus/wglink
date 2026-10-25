"""One preflight rule set: the Send/Solve dialog, Sources & Mesh and WG's ingress."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import types

import pytest

from test_wglink_addin_lifecycle import (
    _Definitions,
    _Panels,
    _UI,
    _compatible_wg,
    _design_module,
    _dialog_inputs,
    _load_instance,
    _per_request_folders,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
CASES = json.loads((FIXTURES / "preflight-v1/cases.json").read_text("utf-8"))
sys.path.insert(0, str(ROOT / "fusion-addins/WGLink"))
import wglink_protocol as protocol  # noqa: E402


def _edited(base: dict, edits: dict) -> dict:
    manifest = deepcopy(base)
    for dotted, value in edits.items():
        *parents, last = dotted.split(".")
        node = manifest
        for part in parents:
            node = node[int(part)] if isinstance(node, list) else node.setdefault(part, {})
        if isinstance(node, list):
            index = int(last)
            if index == len(node):
                node.append(deepcopy(value))
            else:
                node[index] = deepcopy(value)
        else:
            node[last] = deepcopy(value)
    return manifest


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda case: case["name"])
def test_manifest_cases_shared_with_wg_ingress(case):
    base = json.loads((FIXTURES / CASES["base"]).read_text("utf-8"))
    findings = protocol.preflight_findings(protocol.manifest_draft(_edited(base, case["edits"])))
    assert [[finding["code"], finding["severity"]] for finding in findings] == case["expected"]
    for finding in findings:
        assert set(finding) == {"code", "severity", "message", "remedy"}
        assert protocol.PREFLIGHT_RULES[finding["code"]][0] == finding["severity"]
        text = protocol.finding_text(finding)
        for word in ("PASSIVE_CARDIOID", "manifest", "bundle", "schema", "Traceback"):
            assert word not in text


@pytest.mark.parametrize("case", CASES["draft_cases"], ids=lambda case: case["name"])
def test_draft_cases_shared_with_wg(case):
    findings = protocol.preflight_findings(case["draft"])
    assert [[finding["code"], finding["severity"]] for finding in findings] == case["expected"]
    text = " ".join(protocol.finding_text(finding) for finding in findings)
    assert all(repr(name) in text for name in case.get("message_names", ()))
    assert not any(repr(name) in text for name in case.get("message_omits", ()))


def test_every_rule_has_a_known_severity_and_the_ceiling_is_answered_by_its_hold():
    assert {severity for severity, _remedy in protocol.PREFLIGHT_RULES.values()} <= {
        protocol.PREFLIGHT_BLOCK, protocol.PREFLIGHT_ASK, protocol.PREFLIGHT_NOTE,
    }
    for code in protocol.ACKNOWLEDGES:
        assert protocol.PREFLIGHT_RULES[code][0] == protocol.PREFLIGHT_ASK
    assert protocol.ACKNOWLEDGES == {"mesh_ceiling": "mesh_too_large"}


def test_the_ceiling_is_asked_only_for_a_solve_whose_most_reduced_count_is_over():
    over = {"sources": [{"role": "HF", "face_count": 1}], "triangles_least": 22001}
    assert protocol.preflight_findings({**over, "kind": "send"}) == []
    [finding] = protocol.preflight_findings({**over, "kind": "solve"})
    assert (finding["code"], finding["severity"]) == ("mesh_ceiling", "ask")
    assert "22,001" in finding["message"] and "22,000" in finding["message"]
    assert "Sources & Mesh" in finding["remedy"]
    assert protocol.preflight_findings({**over, "kind": "solve", "triangles_least": 22000}) == []
    # WG's own advertised limit wins over the fallback.
    assert protocol.preflight_findings({**over, "kind": "solve"}, limits={"solvedTriangles": 30000}) == []


def test_the_ceiling_words_follow_symmetry_and_the_dialog_they_are_shown_in():
    auto = protocol.ceiling_finding(30000)
    assert auto["message"].startswith("Even if WG can use symmetry")
    off = protocol.ceiling_finding(30000, symmetry="off")
    assert off["message"].startswith("This model meshes to about 30,000")
    assert "symmetry" not in protocol.finding_text(off)
    inside = protocol.ceiling_finding(30000, in_sources_and_mesh=True)
    assert "Sources & Mesh" not in protocol.finding_text(inside)
    assert "Larger mesh sizes" in inside["remedy"]
    [solve] = protocol.preflight_findings({"kind": "solve", "sources": [{"role": "HF", "face_count": 1}],
                                           "triangles_least": 30000, "symmetry": "off"})
    assert solve["message"] == off["message"] and "Sources & Mesh" in solve["remedy"]


def test_a_scope_that_cannot_be_exported_is_the_only_finding():
    [finding] = protocol.preflight_findings({
        "scope_error": "Export refused for body 'helper'.",
        "has_unclassified_visible_surface_refusal": True,
        "manage_dropdown_name": "Manage Link…",
        "sources": [],
    })
    assert finding["code"] == "scope_unclassified_surface"
    assert finding["message"].endswith("Declare Body… is under Manage Link….")


def test_a_group_left_out_of_this_selection_is_a_note_not_an_error():
    [finding] = protocol.preflight_findings({
        "sources": [{"role": "HF", "face_count": 1}], "groups_out_of_scope": ["Back"],
    })
    assert (finding["code"], finding["severity"]) == ("group_out_of_scope", "note")
    assert "'Back'" in finding["message"] and "left out" in finding["message"]


def test_authoring_rules_let_a_fem_driven_model_save_without_a_source():
    mesh = {"wall_size_mm": 25.0, "grading_distance_mm": 100.0}
    assert [f["code"] for f in protocol.mesh_authoring_findings([], mesh)] == ["no_drive"]
    assert protocol.mesh_authoring_findings([], mesh, fem_driven=True) == []
    rows = [
        {"kind": "source", "label": "Source 1 (HF)", "role": "HF", "size_mm": 6.0, "face_keys": {"a"}},
        {"kind": "source", "label": "Source 2 (HF)", "role": "HF", "size_mm": 6.0, "face_keys": {"a"}},
        {"kind": "group", "label": "Group 3 (unnamed)", "name": "", "size_mm": 15.0, "face_keys": ()},
    ]
    assert [(f["code"], f["message"]) for f in protocol.mesh_authoring_findings(rows, mesh)] == [
        ("source_role_duplicate", "Source 2 (HF): same role as Source 1 (HF). Use one source per role."),
        ("face_in_two_rows", "Source 2 (HF): a face is also in Source 1 (HF). A face cannot be in two sources."),
        ("group_without_faces", "Group 3 (unnamed): select at least one face."),
        ("group_unnamed", "Group 3 (unnamed): give this surface group a name."),
    ]


def test_acknowledged_holds_are_canonical_and_known():
    assert protocol.request_acknowledged(None) == ()
    assert protocol.request_acknowledged(["mesh_too_large", "future_hold", "mesh_too_large"]) == ("mesh_too_large",)
    for bad in ("mesh_too_large", [1], {"mesh_too_large": True}):
        with pytest.raises(protocol.ProtocolValidationError):
            protocol.request_acknowledged(bad)


def test_only_a_solve_request_carries_its_answers():
    sys.path.insert(0, str(ROOT / "fusion-addins/WGLink"))
    import wglink_inbox

    fields = dict(command_id="req-1", bundle_relative="wgreturn/a.wgreturn",
                  manifest_sha256="sha256:" + "0" * 64, requested_at="2026-10-25T00:00:00Z")
    solve = wglink_inbox.wg_request_payload(
        kind=wglink_inbox.KIND_PREPARE_AND_SOLVE, return_id="wgr_1", acknowledged=("mesh_too_large",), **fields)
    assert solve["acknowledged"] == ["mesh_too_large"]
    plain = wglink_inbox.wg_request_payload(kind=wglink_inbox.KIND_PREPARE_AND_SOLVE, return_id="wgr_1", **fields)
    assert "acknowledged" not in plain
    with pytest.raises(ValueError):
        wglink_inbox.wg_request_payload(
            kind=wglink_inbox.KIND_RECEIVE_SNAPSHOT, return_id=None, acknowledged=("mesh_too_large",), **fields)


# -- the Send and Solve dialog -------------------------------------------------

def _module(monkeypatch, name, *, answer=None):
    ui = _UI(_Panels(), _Definitions(reserve_ids=False), dialog_result=answer)
    module = _load_instance(monkeypatch, name, ui)
    module.adsk.core.MessageBoxButtonTypes = types.SimpleNamespace(
        YesNoButtonType="yes-no", YesNoCancelButtonType="yes-no-cancel")
    return module, ui


def _state(module, operation, *findings):
    state = module.SendDialogState(operation)
    state.findings = [protocol.preflight_finding(code, message) for code, message in findings]
    return state


def test_ok_is_disabled_while_a_blocking_finding_stands(monkeypatch):
    module, _ui = _module(monkeypatch, "WGLink_preflight_validate")
    state = _state(module, "send", ("no_drive", protocol.NO_DRIVE_MESSAGE))
    args = types.SimpleNamespace(areInputsValid=True)
    module.SendValidateInputsHandler(state).notify(args)
    assert args.areInputsValid is False
    state.findings = [protocol.preflight_finding("open_body", "'Shell' is not a closed solid.")]
    module.SendValidateInputsHandler(state).notify(args)
    assert args.areInputsValid is True


def test_the_preview_caches_the_shared_verdict_and_shows_the_remedy(monkeypatch):
    module, _ui = _module(monkeypatch, "WGLink_preflight_cache")
    monkeypatch.setattr(module, "_required_manifest_features", lambda: {"automatic_domain": True})
    monkeypatch.setattr(module, "_send_selection", lambda _inputs: "root")
    monkeypatch.setattr(module.wglink_send, "preflight_scope", lambda _app, _options: {
        "selection": "root", "included": [{"name": "Cabinet", "body_kind": "solid"}], "instance_ids": [],
        "sources": [], "triangles_least": 40000,
    })
    box = types.SimpleNamespace(formattedText="")
    state = module.SendDialogState("solve")
    module._sync_preflight(_dialog_inputs(preflight=box), state)
    assert [finding["code"] for finding in state.findings] == ["no_drive", "mesh_ceiling"]
    assert protocol.PREFLIGHT_RULES["no_drive"][1] in box.formattedText.replace("&amp;", "&")


def _ceiling(module, operation="solve"):
    return _state(module, operation, ("mesh_ceiling", "This model meshes to about 40,000 triangles."))


@pytest.mark.parametrize("answer, expected", [("yes", ("mesh_too_large",)), ("no", "change_mesh"),
                                              ("cancel", "cancelled")])
def test_the_ceiling_question_offers_solve_anyway_or_a_mesh_change(monkeypatch, answer, expected):
    module, ui = _module(monkeypatch, "WGLink_preflight_ask", answer=answer)
    assert module._ask_before_sending(_ceiling(module)) == expected
    [(title, text)] = ui.messages
    assert "before solving" in title
    assert "Solve anyway" in text and "Change mesh resolution" in text and "40,000" in text


def test_an_open_body_alone_asks_send_anyway(monkeypatch):
    module, ui = _module(monkeypatch, "WGLink_preflight_open", answer="yes")
    state = _state(module, "send", ("open_body", "'Shell' is not a closed solid."))
    assert module._ask_before_sending(state) == ()
    assert "Send anyway?" in ui.messages[0][1]
    assert module._ask_before_sending(module.SendDialogState("send")) == ()  # Nothing to ask.
    assert len(ui.messages) == 1


def _solve(monkeypatch, module, state):
    _compatible_wg(monkeypatch, module)
    monkeypatch.setattr(module, "_send_options", lambda _inputs: {"selection": "root"})
    sent, submitted = [], []
    monkeypatch.setattr(module.wglink_send, "send", lambda _app, options, **_kw: (sent.append(options), {
        "bundle_path": "/workspace/wgreturn/test.wgreturn", "return_id": "wgr_test",
        "scope": {"status": "clean"}, "sources": [],
    })[1])
    monkeypatch.setattr(module, "_submit_to_wg", lambda _report, kind, **kw: (submitted.append((kind, kw)), "req")[1])
    handler = module.CommandExecuteHandler("solve")
    handler.send_state = state
    args = types.SimpleNamespace(command=types.SimpleNamespace(commandInputs=_dialog_inputs()), isValidResult=True)
    handler.notify(args)
    return args, sent, submitted


def test_solve_anyway_is_sent_with_the_request_so_wg_does_not_ask_again(monkeypatch):
    module, _ui = _module(monkeypatch, "WGLink_preflight_solve_anyway", answer="yes")
    _args, sent, submitted = _solve(monkeypatch, module, _ceiling(module))
    assert len(sent) == 1 and "acknowledged" not in sent[0]
    assert submitted == [(module.wglink_inbox.KIND_PREPARE_AND_SOLVE, {"acknowledged": ("mesh_too_large",)})]


def test_cancel_returns_to_the_dialog_and_exports_nothing(monkeypatch):
    module, _ui = _module(monkeypatch, "WGLink_preflight_cancel", answer="cancel")
    args, sent, submitted = _solve(monkeypatch, module, _ceiling(module))
    assert args.isValidResult is False
    assert sent == [] and submitted == []


def test_change_mesh_resolution_opens_sources_and_mesh_after_the_command(monkeypatch):
    module, _ui = _module(monkeypatch, "WGLink_preflight_change_mesh", answer="no")
    scheduled = []
    monkeypatch.setattr(module, "_schedule_followup", scheduled.append)
    args, sent, submitted = _solve(monkeypatch, module, _ceiling(module))
    assert sent == [] and submitted == []
    assert scheduled == [module._open_sources_and_mesh]
    executed = []
    monkeypatch.setattr(module, "_ui", lambda: types.SimpleNamespace(commandDefinitions=types.SimpleNamespace(
        itemById=lambda command_id: types.SimpleNamespace(execute=lambda: executed.append(command_id)))))
    module._open_sources_and_mesh()
    assert executed == [module.COMMANDS["source"][0]]


def test_the_acknowledgement_reaches_the_request_file(monkeypatch, tmp_path: Path):
    module, _ui = _design_module(monkeypatch, "WGLink_preflight_request")
    ipc, _bundles = _per_request_folders(monkeypatch, module, tmp_path)
    (ipc / "wg-capabilities.json").write_text(json.dumps(
        {"schemaVersion": 1, "solveCommandDelivery": 4, "fusionRequestDelivery": 3}))
    bundle = tmp_path / "workspace" / "wgreturn" / "speaker.wgreturn"
    bundle.mkdir(parents=True)
    (bundle / "wgreturn.json").write_bytes(b"{}")
    monkeypatch.setattr(module, "_owns_active_ipc_lease", lambda: True)
    module._submit_to_wg({"return_id": "wgr_1", "bundle_path": str(bundle)},
                         module.wglink_inbox.KIND_PREPARE_AND_SOLVE, acknowledged=("mesh_too_large",))
    [written] = [path for path in (ipc / ".wg-solve-requests").iterdir() if not path.name.startswith(".")]
    assert json.loads(written.read_text())["acknowledged"] == ["mesh_too_large"]
