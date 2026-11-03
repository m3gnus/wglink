"""Cloud opening uses the real handoff dispatch, with main-thread Fusion fakes."""
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
    pending = wglink_handoff.next_pending_handoff(tmp_path, bundle_root=tmp_path)
    assert isinstance(pending, wglink_handoff.PendingOpenDocument)
    assert wglink_handoff.unusable_handoffs(tmp_path, bundle_root=tmp_path) == []
    claim = wglink_handoff.claim_request(pending)
    assert claim and not path.exists()
    assert wglink_handoff.leftover_claims(tmp_path)[0].kind == "open_document"
    assert wglink_handoff.acknowledge_handoff(claim)


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
        payload = frame(sessionId="old-session" if mode == "session" else m._watch_session_id)
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
