"""Upgrade recovery uses WG's file outcome channel without replaying work."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_wglink_addin_lifecycle import (
    _design_module, _per_request_folders, _shared_snapshot,
)


def _fixture(monkeypatch, tmp_path):
    module, _ui = _design_module(monkeypatch, "WGLink_legacy_recovery")
    ipc, _bundles = _per_request_folders(monkeypatch, module, tmp_path)
    snapshot = _shared_snapshot()
    snapshot["applying_operation"] = None
    monkeypatch.setattr(module, "_fusion_snapshot", lambda: snapshot)
    monkeypatch.setattr(module, "_owns_active_ipc_lease", lambda: True)
    def no_mutation(*args, **kwargs):
        pytest.fail("legacy claims must never replay a mutation or export")
    monkeypatch.setattr(module.wglink_core, "insert", no_mutation)
    monkeypatch.setattr(module.wglink_core, "update", no_mutation)
    monkeypatch.setattr(module.wglink_send, "send", no_mutation)
    return module, ipc, snapshot


def _claim(ipc, *, kind="update_link", state="claimed", outcome=None, operation="req-1"):
    folder = ipc / ".wglink-live-claims"
    folder.mkdir(exist_ok=True)
    path = folder / (operation + ".json")
    payload = {
        "schemaVersion": 1, "operationId": operation, "claimId": "claim-1",
        "attemptGeneration": 1, "kind": kind, "state": state,
        "request": {"expectedDocumentId": "fusion:doc-a", "exportId": "wge_5"},
    }
    if outcome is not None:
        payload["outcome"] = outcome
    path.write_text(json.dumps(payload))
    return path


@pytest.mark.parametrize("kind", ["insert_link", "update_link", "request_return"])
def test_legacy_claim_before_work_reports_interruption(monkeypatch, tmp_path, kind):
    module, ipc, _snapshot = _fixture(monkeypatch, tmp_path)
    claim = _claim(ipc, kind=kind)
    module._start_without_coordination()
    status = json.loads((ipc / ".fusion-status.json").read_text())
    assert status["diagnostics"]["recentOutcomes"] == [{
        "channel": "returnRequest" if kind == "request_return" else "handoff",
        "requestId": "req-1", "outcome": "failed" if kind == "request_return" else "recoveryRequired",
    }]
    assert not claim.exists()


@pytest.mark.parametrize("kind", ["insert_link", "update_link"])
def test_legacy_mutation_evidence_reports_done(monkeypatch, tmp_path, kind):
    module, ipc, snapshot = _fixture(monkeypatch, tmp_path)
    claim = _claim(ipc, kind=kind)
    snapshot["links"][0].update(operation_id="req-1", export_id="wge_5")
    module._start_without_coordination()
    status = json.loads((ipc / ".fusion-status.json").read_text())
    assert status["diagnostics"]["recentOutcomes"][0]["outcome"] == "reconciled"
    assert not claim.exists()


def test_legacy_applying_mark_reports_recovery(monkeypatch, tmp_path):
    module, ipc, snapshot = _fixture(monkeypatch, tmp_path)
    _claim(ipc)
    snapshot["applying_operation"] = {"operation_id": "req-1"}
    module._start_without_coordination()
    assert module._recent_outcomes[0]["outcome"] == "recoveryRequired"


@pytest.mark.parametrize("kind,outcome,expected", [
    ("request_return", {"outcome": "applied"}, "applied"),
    ("update_link", {"outcome": "applied", "evidence": {
        "operationId": "req-1", "exportId": "wge_5"}}, "reconciled"),
    ("update_link", {"outcome": "refused"}, "refused"),
    ("update_link", {"outcome": "applied"}, "recoveryRequired"),
])
def test_legacy_published_outcome_is_used(monkeypatch, tmp_path, kind, outcome, expected):
    module, ipc, _snapshot = _fixture(monkeypatch, tmp_path)
    _claim(ipc, kind=kind, state="outcome", outcome=outcome)
    module._start_without_coordination()
    status = json.loads((ipc / ".fusion-status.json").read_text())
    assert status["diagnostics"]["recentOutcomes"][0]["outcome"] == expected
    # WG's mutation reader uses lastRequest for refusals.
    assert status["diagnostics"]["lastRequest"]["outcome"] == expected


def test_legacy_claim_is_kept_when_publication_fails(monkeypatch, tmp_path):
    module, ipc, _snapshot = _fixture(monkeypatch, tmp_path)
    claim = _claim(ipc)
    original = module.wglink_watch.write_fusion_status_payload
    def failed(*args):
        raise OSError("held status")
    monkeypatch.setattr(module.wglink_watch, "write_fusion_status_payload", failed)
    module._start_without_coordination()
    assert claim.exists()
    monkeypatch.setattr(module.wglink_watch, "write_fusion_status_payload", original)
    module._start_without_coordination()
    assert not claim.exists()


def test_repeated_start_does_not_report_legacy_claim_twice(monkeypatch, tmp_path):
    module, ipc, _snapshot = _fixture(monkeypatch, tmp_path)
    claim = _claim(ipc)
    original = Path.unlink
    def held(path, *args, **kwargs):
        if path == claim:
            raise PermissionError("held claim")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", held)
    module._start_without_coordination()
    first = json.loads((ipc / ".fusion-status.json").read_text())["diagnostics"]["recentOutcomes"]
    module._recent_outcomes.clear()  # a fresh registration has no memory
    module._start_without_coordination()
    assert json.loads((ipc / ".fusion-status.json").read_text())["diagnostics"]["recentOutcomes"] == first
    assert len(first) == 1 and claim.exists()
    monkeypatch.setattr(Path, "unlink", original)
    module._start_without_coordination()
    assert not claim.exists()


def test_no_legacy_journal_does_nothing(monkeypatch, tmp_path):
    module, ipc, _snapshot = _fixture(monkeypatch, tmp_path)
    module._start_without_coordination()
    assert module._recent_outcomes == []
    assert not (ipc / ".wglink-live-claims").exists()


def test_legacy_unavailable_document_reports_recovery(monkeypatch, tmp_path):
    module, ipc, snapshot = _fixture(monkeypatch, tmp_path)
    claim = _claim(ipc)
    snapshot.update(document_id=None, document_name=None, links=[])
    module._start_without_coordination()
    assert module._recent_outcomes[0]["outcome"] == "recoveryRequired"
    assert not claim.exists()


@pytest.mark.parametrize("coordination", [False, True])
def test_actual_startup_reconciles_legacy_claim_in_both_gate_states(monkeypatch, tmp_path, coordination):
    module, ipc, _snapshot = _fixture(monkeypatch, tmp_path)
    module.SETTINGS_PATH.write_text(json.dumps({"automatic_coordination": coordination}))
    claim = _claim(ipc)
    module.run(None)
    try:
        assert module._owned
        assert not claim.exists()
        status = json.loads((ipc / ".fusion-status.json").read_text())
        assert status["diagnostics"]["recentOutcomes"][0]["outcome"] == "recoveryRequired"
    finally:
        module.stop(None)


def test_malformed_legacy_claim_does_not_hide_valid_entries(monkeypatch, tmp_path):
    module, ipc, _snapshot = _fixture(monkeypatch, tmp_path)
    claim = _claim(ipc)
    invalid = claim.parent / "bad.json"
    invalid.write_text("{")
    module._start_without_coordination()
    assert not claim.exists() and invalid.exists()
    assert module._recent_outcomes[0]["outcome"] == "recoveryRequired"


def test_legacy_recovery_batch_fits_the_status_window(monkeypatch, tmp_path):
    module, ipc, _snapshot = _fixture(monkeypatch, tmp_path)
    paths = [_claim(ipc, operation="req-{:02d}".format(index)) for index in range(20)]
    module._start_without_coordination()
    assert sum(path.exists() for path in paths) == 4
    status = json.loads((ipc / ".fusion-status.json").read_text())
    assert len(status["diagnostics"]["recentOutcomes"]) == 16


def test_held_completed_claim_keeps_its_published_outcome_after_document_switch(monkeypatch, tmp_path):
    module, ipc, snapshot = _fixture(monkeypatch, tmp_path)
    claim = _claim(ipc)
    snapshot["links"][0].update(operation_id="req-1", export_id="wge_5")
    original = Path.unlink
    def held(path, *args, **kwargs):
        if path == claim:
            raise PermissionError("held claim")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", held)
    module._start_without_coordination()
    snapshot.update(document_id="fusion:doc-b", links=[])
    module._recent_outcomes.clear()
    module._start_without_coordination()
    assert module._recent_outcomes == [{
        "channel": "handoff", "requestId": "req-1", "outcome": "reconciled",
    }]
    monkeypatch.setattr(Path, "unlink", original)
    module._start_without_coordination()
    assert claim.exists()  # WG cannot accept a completion for another document.
    snapshot["document_id"] = "fusion:doc-a"
    module._start_without_coordination()
    assert not claim.exists()


def test_legacy_refusals_do_not_overwrite_the_single_readable_result(monkeypatch, tmp_path):
    module, ipc, _snapshot = _fixture(monkeypatch, tmp_path)
    first = _claim(ipc, state="outcome", outcome={"outcome": "refused"}, operation="req-1")
    second = _claim(ipc, state="outcome", outcome={"outcome": "refused"}, operation="req-2")
    _claim(ipc, operation="req-3")
    module._start_without_coordination()
    assert not first.exists() and second.exists()
    status = json.loads((ipc / ".fusion-status.json").read_text())
    assert status["diagnostics"]["lastRequest"]["correlationId"] == "req-1"
    assert status["diagnostics"]["lastRequest"]["outcome"] == "refused"
