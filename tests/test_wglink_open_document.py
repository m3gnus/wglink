"""Cloud opening uses the real handoff dispatch, with main-thread Fusion fakes."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import types

import pytest

from fusion_fakes import MainThreadOnly
from test_wglink_wg_pickup import _running, _poll
from test_wglink_activation_boundary import _deliver_every_event
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fusion-addins" / "WGLink"))
import wglink_protocol  # noqa: E402
import wglink_handoff  # noqa: E402


def frame(**changes):
    return {"schemaVersion": 3, "target": "fusion360", "kind": "open_document",
            "requestId": "open-1", "operationId": "open-1", "deliverySequence": 1,
            "sessionId": "session-a", "documentId": "urn:adsk.wipprod:dm.lineage:cloud", **changes}


@pytest.mark.parametrize("changes", [
    {"schemaVersion": True}, {"deliverySequence": True}, {"deliverySequence": 0},
    {"requestId": "../path"}, {"operationId": "other"}, {"kind": "unknown"},
    {"target": "other"}, {"documentId": ""}, {"documentId": "cloud\n"},
    {"documentId": 7}, {"sessionId": None},
])
def test_invalid_open_frame(changes):
    assert wglink_protocol.validate_open_document_request(frame(**changes)) is None


def test_frame_claim_order_and_no_update_supersession(tmp_path):
    folder = tmp_path / wglink_handoff.HANDOFFS_DIRECTORY
    folder.mkdir()
    path = folder / "open-1.json"
    path.write_text(json.dumps(frame()))
    assert wglink_protocol.validate_open_document_request(frame()) == frame()
    assert wglink_handoff.has_requests(tmp_path)
    assert wglink_handoff.discard_superseded_handoffs(tmp_path, bundle_root=tmp_path) == []
    assert wglink_handoff.next_pending_handoff(tmp_path, bundle_root=tmp_path) is None
    pending = wglink_handoff.next_pending_handoff(tmp_path, bundle_root=tmp_path, session_id="session-a")
    assert isinstance(pending, wglink_handoff.PendingOpenDocument)
    assert wglink_handoff.unusable_handoffs(tmp_path, bundle_root=tmp_path) == []
    claim = wglink_handoff.claim_request(pending)
    assert claim and not path.exists()
    assert wglink_handoff.leftover_claims(tmp_path)[0].kind == "open_document"
    assert wglink_handoff.acknowledge_handoff(claim)


def test_open_claim_refresh_keeps_command_kind_and_owner_session(monkeypatch, tmp_path):
    from test_wglink_claim_recovery import _snapshot

    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, "WGLink_open_claim_refresh")
    m = f.module
    try:
        folder = ipc / wglink_handoff.HANDOFFS_DIRECTORY
        folder.mkdir(exist_ok=True)
        path = folder / "open-1.json"
        path.write_text(json.dumps(frame(sessionId=m._watch_session_id)))
        pending = m.wglink_handoff.read_pending_handoff(path, bundle_root=bundles)
        claimed = m.wglink_handoff.claim_request(pending)
        [claim] = m.wglink_handoff.leftover_claims(ipc)
        refreshed = m.wglink_handoff.refresh_leftover_claim(claim)
        assert refreshed.kind == "open_document"
        assert refreshed.session_id == m._watch_session_id
        # Opening is not a model mutation. Even matching model evidence cannot
        # reconcile it, and a live owner must retain its claim.
        snapshot = _snapshot(links=[{"operation_id": "open-1", "export_id": ""}])
        m._settle_file_claims([claim], snapshot)
        assert claimed.marker_path.exists() and m._recent_outcomes == []
        monkeypatch.setattr(m.wglink_protocol, "claim_owner_terminated", lambda _: True)
        m._settle_file_claims(m._deferred_file_claims, snapshot)
        assert not claimed.marker_path.exists()
        assert m._recent_outcomes[-1]["outcome"] == "discarded"
    finally:
        m.stop(None)


def test_document_filter_leaves_updates_and_still_delivers_cloud_open(monkeypatch, tmp_path):
    from test_wglink_addin_lifecycle import _per_request_handoff

    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, "WGLink_open_document_filter")
    m = f.module
    try:
        _per_request_handoff(ipc, bundles, request_id="update-1", sequence=1)
        _per_request_handoff(ipc, bundles, request_id="update-2", sequence=2)
        folder = ipc / wglink_handoff.HANDOFFS_DIRECTORY
        path = folder / "open-1.json"
        path.write_text(json.dumps(frame(deliverySequence=3, sessionId=m._watch_session_id)))
        updates = {path.name: path.read_bytes() for path in folder.glob("update-*.json")}
        monkeypatch.setattr(m, "_active_document_id", lambda: "fusion:other-document")
        pending = m._pending_handoff()
        assert isinstance(pending, m.wglink_handoff.PendingOpenDocument)
        assert pending.document_id == frame()["documentId"]
        assert {path.name: path.read_bytes() for path in folder.glob("update-*.json")} == updates
        assert m.wglink_handoff.acknowledge_handoff(m.wglink_handoff.claim_request(pending))
        assert m._pending_handoff() is None
        monkeypatch.setattr(m, "_active_document_id", lambda: "fusion:doc-a")
        assert m._pending_handoff().request_id == "update-2"
        assert not (folder / "update-1.json").exists()
        assert m._recent_outcomes[-1]["outcome"] == "superseded"
    finally:
        m.stop(None)


class Documents(MainThreadOnly):
    def __init__(self, app, opened, *, already_open=False, failure=None):
        super().__init__()
        self.app, self.opened, self.failure = app, opened, failure
        self.document = types.SimpleNamespace(dataFile=types.SimpleNamespace(id=frame()["documentId"]),
                                               activate=self.activate, name="Cloud")
        self.items = [self.document] if already_open else []

    def __iter__(self):
        return iter(self.items)

    def activate(self):
        self.opened.append("activate")
        self.app.activeDocument = self.document
        return True

    def open(self, data_file):
        self.opened.append(data_file)
        if self.failure:
            raise self.failure
        self.app.activeDocument = self.document
        return self.document


def test_other_consumer_leaves_open_request_for_its_intended_session(monkeypatch, tmp_path):
    intended, ipc, bundles, _ = _running(monkeypatch, tmp_path / "intended", "WGLink_open_intended_consumer")
    # Separate Fusion processes have separate registration leases.
    monkeypatch.delitem(sys.modules, intended.module._RUNTIME_MODULE_NAME)
    other, _, _, _ = _running(monkeypatch, tmp_path / "other", "WGLink_open_other_consumer")
    # Both processes poll the same WG inbox, with independent session identities.
    monkeypatch.setattr(other.module.wglink_workspace, "ipc_folder", lambda **_kwargs: ipc)
    monkeypatch.setattr(other.module.wglink_workspace, "bundle_folder", lambda: bundles)
    assert intended.module._watch_session_id != other.module._watch_session_id
    opened, other_opened = [], []
    try:
        intended.ui.activeCommand = other.ui.activeCommand = "SelectCommand"
        intended.app.documents = Documents(intended.app, opened, already_open=True)
        other.app.documents = Documents(other.app, other_opened, already_open=True)
        folder = ipc / wglink_handoff.HANDOFFS_DIRECTORY
        folder.mkdir(exist_ok=True)
        path = folder / "open-1.json"
        original = json.dumps(frame(sessionId=intended.module._watch_session_id))
        path.write_text(original)
        _poll(other.module)
        _deliver_every_event(other.app)
        assert path.exists(), "The other session consumed the open request"
        assert path.read_text() == original
        assert not opened and not other_opened
        assert not any(item["requestId"] == "open-1" for item in other.module._recent_outcomes)
        # The intended consumer shares the inbox and can still claim and open it.
        _poll(intended.module)
        _deliver_every_event(intended.app)
        assert opened == ["activate"]
        assert not list(folder.iterdir())
        report = json.loads((ipc / ".fusion-status.json").read_text())
        assert any(item["requestId"] == "open-1" and item["outcome"] == "applied"
                   for item in report["diagnostics"]["recentOutcomes"])
    finally:
        intended.module.stop(None)
        other.module.stop(None)


@pytest.mark.parametrize("mode", ["open", "activate", "missing", "offline", "fault", "session", "old_wg"])
def test_open_through_worker_and_main_dispatch(monkeypatch, tmp_path, mode):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, "WGLink_open_" + mode)
    m = f.module
    opened, resolved = [], []
    try:
        f.app.documents = Documents(f.app, opened, already_open=mode == "activate",
                                   failure=RuntimeError("private API detail") if mode == "offline" else
                                   ValueError("program fault") if mode == "fault" else None)
        class Data(MainThreadOnly):
            def findFileById(self, native_id):
                resolved.append(native_id)
                return None if mode == "missing" else types.SimpleNamespace(id=native_id)
        f.app.data = Data()
        if mode == "old_wg":
            def incompatible():
                raise m.wglink_text.UserFacing("wg_outdated", "Update Waveguide Generator.", "Restart it, then try again.", detail="private compatibility diagnostic")
            monkeypatch.setattr(m, "_require_compatible_wg", incompatible)
        if mode == "session":
            execute = m._execute_open_document

            def restarted_after_selection(request):
                execute(replace(request, session_id="previous-session"))

            monkeypatch.setattr(m, "_execute_open_document", restarted_after_selection)
        payload = frame(sessionId=m._watch_session_id)
        folder = ipc / wglink_handoff.HANDOFFS_DIRECTORY
        folder.mkdir(exist_ok=True)
        (folder / "open-1.json").write_text(json.dumps(payload))
        # Edit state blocks dispatch and leaves the exact request unclaimed.
        f.ui.activeCommand = "SketchLine"
        _poll(m)
        _deliver_every_event(f.app)
        assert not opened and not resolved
        assert (folder / "open-1.json").exists()
        f.ui.activeCommand = "SelectCommand"
        _poll(m)
        assert not opened and not resolved  # worker only queues the custom event
        _deliver_every_event(f.app)
        _poll(m)
        _deliver_every_event(f.app)
        assert not list(folder.iterdir())
        report = json.loads((ipc / ".fusion-status.json").read_text())
        assert report["openDocument"] == 1
        receipt = next(item for item in report["diagnostics"]["recentOutcomes"] if item["requestId"] == "open-1")
        assert receipt["outcome"] == ("applied" if mode in {"open", "activate"} else "failed" if mode == "fault" else "refused")
        assert len(opened) == (1 if mode in {"open", "activate", "offline", "fault"} else 0)
        assert len(resolved) == (0 if mode in {"activate", "session", "old_wg"} else 1)
        assert "private API detail" not in receipt.get("message", "")
        if mode in {"missing", "offline"}:
            assert "hub" in receipt["message"] and "captured" in receipt["message"]
        if mode == "old_wg":
            assert "Update Waveguide Generator" in receipt["message"]
            assert "private compatibility diagnostic" not in receipt["message"]
    finally:
        m.stop(None)
