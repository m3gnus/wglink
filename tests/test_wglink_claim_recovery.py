"""File claim recovery must prove owner termination and inspect its destination."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_wglink_activation_boundary import _boundary
from test_wglink_addin_lifecycle import (
    _per_request_folders,
    _per_request_handoff,
    _publish_like_wg,
)


def _snapshot(document_id="fusion:doc-a", **extra):
    return {"document_id": document_id, "links": [], "links_inspected": True,
            "applying_operation": None, **extra}


@pytest.mark.parametrize("channel", ["handoff", "returnRequest"])
def test_startup_preserves_another_process_live_claim(monkeypatch, tmp_path, channel):
    m = _boundary(monkeypatch, tmp_path, "WGLink_live_claim", coordination=False).module
    ipc, bundles = _per_request_folders(monkeypatch, m, tmp_path / "files")
    if channel == "handoff":
        _per_request_handoff(ipc, bundles)
    else:
        _publish_like_wg(ipc, "", ".fusion-return-requests", "req-1", 1, {
            "target": "fusion360", "sessionId": "other-session", "documentId": "fusion:doc-a",
        })
    script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import wglink_handoff as h
ipc, bundles = map(Path, sys.argv[2:4])
pending = (h.next_return_request(ipc, session_id='other-session') if sys.argv[4] == 'returnRequest'
           else h.next_pending_handoff(ipc, bundle_root=bundles))
claim = h.claim_request(pending)
print(claim.marker_path, flush=True)
sys.stdin.readline()
"""
    worker = subprocess.Popen(
        [sys.executable, "-c", script, str(Path(m.__file__).parent), str(ipc), str(bundles), channel],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        path = Path(worker.stdout.readline().strip())
        assert path.is_file()
        m._sweep_leftover_claims(_snapshot())
        assert path.exists()
        assert m._recent_outcomes == []
        assert len(m._deferred_file_claims) == 1
        worker.communicate("exit\n", timeout=5)
        assert worker.returncode == 0
        m._settle_file_claims(m._deferred_file_claims, _snapshot())
        assert not path.exists()
        assert m._recent_outcomes[-1]["outcome"] == "discarded"
    finally:
        if worker.poll() is None:
            worker.communicate("exit\n", timeout=5)


def test_startup_preserves_claim_with_unverifiable_owner(monkeypatch, tmp_path):
    m = _boundary(monkeypatch, tmp_path, "WGLink_unknown_claim", coordination=False).module
    ipc, bundles = _per_request_folders(monkeypatch, m, tmp_path / "files")
    _per_request_handoff(ipc, bundles)
    source = ipc / ".fusion-handoffs/req-1.json"
    claim = source.with_name(".wglink-claim-req-1-old.json")
    source.rename(claim)
    m._sweep_leftover_claims(_snapshot())
    assert claim.exists()
    assert m._recent_outcomes == []


def test_new_document_claim_is_bound_before_mutation_and_recovers_only_there(monkeypatch, tmp_path):
    m = _boundary(monkeypatch, tmp_path, "WGLink_bound_claim", coordination=False).module
    ipc, bundles = _per_request_folders(monkeypatch, m, tmp_path / "files")
    _per_request_handoff(ipc, bundles, instance_id=None,
                         destination={"kind": "new_document", "value": "req-1"})
    active = {"id": "fusion:doc-a"}
    monkeypatch.setattr(m, "_active_document_id", lambda: active["id"])
    def create(**_):
        active["id"] = "fusion:new-doc"
        return True
    monkeypatch.setattr(m, "_ensure_design_ready", create)
    monkeypatch.setattr(m, "_document_links", list)
    seen = []
    def insert(*_):
        [claim] = m.wglink_handoff.leftover_claims(ipc)
        seen.append(json.loads(claim.path.read_text()))
    monkeypatch.setattr(m.wglink_core, "insert", insert)
    # Keep the durable file as a process crash after mutation would do.
    monkeypatch.setattr(m.wglink_handoff, "acknowledge_handoff", lambda _: False)
    assert m._apply_pending_handoff() == m.HANDLED
    assert seen[0].get("recoveryDocumentId") == "fusion:new-doc"
    monkeypatch.setattr(m.wglink_protocol, "claim_owner_terminated", lambda _: True)
    claims = m.wglink_handoff.leftover_claims(ipc)
    m._recent_outcomes.clear()
    m._settle_file_claims(claims, _snapshot())
    assert claims[0].path.exists() and m._recent_outcomes == []
    m._settle_file_claims(m._deferred_file_claims, _snapshot("fusion:new-doc", applying_operation={
        "operation_id": "req-1",
    }))
    assert not claims[0].path.exists()
    assert m._recent_outcomes[-1]["outcome"] == "recoveryRequired"


def test_unbound_insert_is_unknown_until_its_own_evidence_is_found(monkeypatch, tmp_path):
    m = _boundary(monkeypatch, tmp_path, "WGLink_unbound_claim", coordination=False).module
    ipc, bundles = _per_request_folders(monkeypatch, m, tmp_path / "files")
    _per_request_handoff(ipc, bundles, instance_id=None,
                         destination={"kind": "new_document", "value": "req-1"})
    m.wglink_handoff.claim_request(m.wglink_handoff.next_pending_handoff(ipc, bundle_root=bundles))
    monkeypatch.setattr(m.wglink_protocol, "claim_owner_terminated", lambda _: True)
    claims = m.wglink_handoff.leftover_claims(ipc)
    m._settle_file_claims(claims, _snapshot())
    assert claims[0].path.exists() and m._recent_outcomes == []
    m._settle_file_claims(m._deferred_file_claims, _snapshot("fusion:new-doc", links=[{
        "operation_id": "req-1", "export_id": "wge_5",
    }]))
    assert not claims[0].path.exists()
    assert m._recent_outcomes[-1]["outcome"] == "reconciled"


def test_failed_destination_binding_keeps_claim_and_prevents_mutation(monkeypatch, tmp_path):
    m = _boundary(monkeypatch, tmp_path, "WGLink_binding_failed", coordination=False).module
    ipc, bundles = _per_request_folders(monkeypatch, m, tmp_path / "files")
    _per_request_handoff(ipc, bundles, instance_id=None,
                         destination={"kind": "new_document", "value": "req-1"})
    monkeypatch.setattr(m, "_ensure_design_ready", lambda **_: True)
    def failed(*_):
        raise OSError("destination binding failed")
    monkeypatch.setattr(m.wglink_handoff.wglink_status, "write_status_record", failed)
    monkeypatch.setattr(m.wglink_core, "insert", lambda *_: pytest.fail("mutated without binding"))
    assert m._apply_pending_handoff() == m.HANDLED
    [claim] = m.wglink_handoff.leftover_claims(ipc)
    assert claim.path.exists()
    assert m._recent_outcomes[-1]["outcome"] == "recoveryRequired"


def test_inaccessible_claim_owner_is_unverifiable(monkeypatch, tmp_path):
    m = _boundary(monkeypatch, tmp_path, "WGLink_owner_access", coordination=False).module
    import types
    monkeypatch.setattr(m.wglink_protocol, "sys", types.SimpleNamespace(platform="darwin"))
    def denied(*_):
        raise PermissionError("owner is not inspectable")
    monkeypatch.setattr(m.wglink_protocol.os, "getpgid", denied, raising=False)
    assert not m.wglink_protocol.claim_owner_terminated(".wglink-claim-req-1-0123456789ab-pid-123.json")


@pytest.mark.parametrize("result, terminated", [
    (42, False), (ProcessLookupError(), True), (PermissionError(), False),
    (OSError(), False), (OverflowError(), False),
])
def test_claim_owner_probe_is_read_only(monkeypatch, tmp_path, result, terminated):
    m = _boundary(monkeypatch, tmp_path, "WGLink_owner_read_only", coordination=False).module
    import types

    queried = []
    def getpgid(pid):
        queried.append(pid)
        if isinstance(result, Exception):
            raise result
        return result
    def signal_process(*_):
        pytest.fail("claim recovery must not signal another process")
    monkeypatch.setattr(m.wglink_protocol, "sys", types.SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(m.wglink_protocol, "os", types.SimpleNamespace(
        name="posix", getpgid=getpgid, kill=signal_process,
    ))
    assert m.wglink_protocol.claim_owner_terminated(
        ".wglink-claim-req-1-0123456789ab-pid-123.json"
    ) is terminated
    assert queried == [123]


def test_unreadable_claim_is_retained_and_refreshed_before_settlement(monkeypatch, tmp_path):
    m = _boundary(monkeypatch, tmp_path, "WGLink_claim_unreadable", coordination=False).module
    ipc, bundles = _per_request_folders(monkeypatch, m, tmp_path / "files")
    _per_request_handoff(ipc, bundles)
    h = m.wglink_handoff
    left = h.claim_request(h.next_pending_handoff(ipc, bundle_root=bundles))
    read = h._read_json
    monkeypatch.setattr(h, "_read_json", lambda path, **kw: None if path == left.marker_path else read(path, **kw))
    monkeypatch.setattr(m.wglink_protocol, "claim_owner_terminated", lambda _: True)
    claims = h.leftover_claims(ipc)
    snapshot = _snapshot(links=[{"operation_id": "req-1", "export_id": "wge_5"}])
    m._settle_file_claims(claims, snapshot)
    assert left.marker_path.exists() and m._recent_outcomes == []
    monkeypatch.setattr(h, "_read_json", read)
    m._settle_file_claims(m._deferred_file_claims, snapshot)
    assert not left.marker_path.exists()
    assert m._recent_outcomes[-1]["outcome"] == "reconciled"


def test_completed_live_claim_leaves_no_deferred_entry_or_false_outcome(monkeypatch, tmp_path):
    m = _boundary(monkeypatch, tmp_path, "WGLink_claim_completed", coordination=False).module
    ipc, bundles = _per_request_folders(monkeypatch, m, tmp_path / "files")
    _per_request_handoff(ipc, bundles)
    h = m.wglink_handoff
    left = h.claim_request(h.next_pending_handoff(ipc, bundle_root=bundles))
    m._settle_file_claims(h.leftover_claims(ipc), _snapshot())
    assert len(m._deferred_file_claims) == 1
    assert h.acknowledge_handoff(left)
    monkeypatch.setattr(m.wglink_protocol, "claim_owner_terminated", lambda _: True)
    m._settle_file_claims(m._deferred_file_claims, _snapshot())
    assert m._deferred_file_claims == []
    assert m._recent_outcomes == []


@pytest.mark.parametrize("applied", [False, True])
def test_saved_new_document_recovery_uses_its_positive_operation_evidence(monkeypatch, tmp_path, applied):
    m = _boundary(monkeypatch, tmp_path, "WGLink_claim_saved", coordination=False).module
    ipc, bundles = _per_request_folders(monkeypatch, m, tmp_path / "files")
    _per_request_handoff(ipc, bundles, instance_id=None,
                         destination={"kind": "new_document", "value": "req-1"})
    h = m.wglink_handoff
    left = h.claim_request(h.next_pending_handoff(ipc, bundle_root=bundles))
    h.bind_handoff_destination(left, "local:created-document")
    monkeypatch.setattr(m.wglink_protocol, "claim_owner_terminated", lambda _: True)
    snapshot = _snapshot("fusion:saved-document", links=[{
        "operation_id": "req-1", "export_id": "wge_5",
    }] if applied else [], applying_operation=None if applied else {"operation_id": "req-1"})
    m._settle_file_claims(h.leftover_claims(ipc), snapshot)
    assert not left.marker_path.exists()
    assert m._recent_outcomes[-1]["outcome"] == ("reconciled" if applied else "recoveryRequired")
