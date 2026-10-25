"""History and explicit navigation; all Fusion calls stay on its main thread."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fusion-addins" / "WGLink"))
import wglink_protocol as protocol
import wglink_requests as requests
from fusion_fakes import FakeEvent
from test_wglink_request_palette import _following, _palette, _status
from test_wglink_transfer import _deliver_followups
from test_wglink_addin_lifecycle import _load_instance


def entry(rid="request-1", document="doc-1", *, now=None, state=None):
    now = time.time() if now is None else now
    request = requests.new_request(rid, requests.SOLVE, "Horn", document_id=document, now=now)
    request = requests.submitted(request, manifest_sha256="a" * 64, acknowledgement=True,
                                 request_status=True, confirmed=True, now=now)
    if state:
        request["status"] = protocol.validate_request_status({
            "contract": protocol.REQUEST_STATUS_CONTRACT, "schemaVersion": 1, "seq": 1,
            "requestId": rid, "state": state, "kind": requests.SOLVE})
    return request


def test_history_bound_expiry_and_stale_nonterminal(tmp_path):
    now = time.time()
    entries = [entry(f"req-{i}", now=now - i, state="solved") for i in range(60)]
    entries += [entry("old-live", now=now - 31 * 86400), entry("old-done", now=now - 31 * 86400, state="solved")]
    bounded = requests.bounded(entries, now=now)
    assert len(bounded) == 50 and bounded[0]["requestId"] == "req-0"
    old = requests.bounded(entries[-2:], now=now)
    assert [e["requestId"] for e in old] == ["old-live"]
    shown = requests.view(old[0], now=now)
    assert shown["headline"] == "Check in WG" and not shown["busy"]
    assert shown["detail"] == protocol.REQUEST_HISTORY_TEXT["missing"]


def test_tracker_filter_clear_and_history_update_keep_current_owner(tmp_path):
    tracker = requests.RequestTracker()
    save = lambda record: requests.save(tmp_path, record)
    tracker.begin(entry("one", "a"), save)
    tracker.begin(entry("two", "b", state="solved"), save)
    tracker.document_id = "a"
    assert [e["requestId"] for e in tracker.entries(filtered=True)] == ["one"]
    tracker.all_documents = True
    assert len(tracker.entries(filtered=True)) == 2
    previous = tracker.get("one")
    changed = {**previous, "quiet": True}
    assert tracker.update_entry(previous, changed, save)
    assert tracker.current()["requestId"] == "two"
    assert not tracker.update_entry(previous, {**previous, "local": requests.NOT_SENT}, save)
    tracker.clear_finished(save)
    assert [e["requestId"] for e in tracker.entries()] == ["one"]
    assert requests.load_history(tmp_path)[0]["quiet"] is True


def test_restart_imports_old_record_once_after_durable_history(tmp_path, monkeypatch):
    old = tmp_path / requests.LEGACY_RECORD_FILENAME
    old.write_text(json.dumps(entry()))
    real_write = requests.wglink_status.write_status_record
    def fail(*args):
        raise OSError("disk")
    monkeypatch.setattr(requests.wglink_status, "write_status_record", fail)
    import pytest
    with pytest.raises(OSError):
        requests.load_history(tmp_path)
    assert old.exists()
    monkeypatch.setattr(requests.wglink_status, "write_status_record", real_write)
    assert len(requests.load_history(tmp_path)) == 1
    assert not old.exists()
    assert len(requests.load_history(tmp_path)) == 1


def test_show_request_capability_and_one_pointer_per_press(tmp_path):
    request = entry()
    assert requests.show_request(tmp_path, request) is None
    assert not (tmp_path / protocol.SHOW_REQUESTS_DIRECTORY).exists()
    path = tmp_path / "wg-capabilities.json"
    for malformed in (None, ["showRequest"], "showRequest"):
        path.write_text(json.dumps(malformed))
        assert not requests.can_show(tmp_path)
    for value in (True, "1", 0, 2):
        path.write_text(json.dumps({protocol.SHOW_REQUEST_CAPABILITY: value}))
        assert requests.show_request(tmp_path, request) is None
    path.write_text(json.dumps({protocol.SHOW_REQUEST_CAPABILITY: 1}))
    first = requests.show_request(tmp_path, request)
    before = json.loads(first.read_text())
    assert requests.show_request(tmp_path, request) == first
    assert len(list(first.parent.glob("*.json"))) == 1
    assert json.loads(first.read_text())["at"] >= before["at"]
    assert requests.show_request(tmp_path, {**request, "requestId": "../escape"}) is None


def test_history_words_are_shared_and_user_facing():
    for text in protocol.REQUEST_HISTORY_TEXT.values():
        assert not protocol.user_text_violations(text)


def test_fake_document_switch_and_stop_start_preserve_history_without_resubmit(monkeypatch, tmp_path):
    fixture = _following(monkeypatch, tmp_path, "WGLink_history_start")
    module = fixture.module
    original = module._requests.current()
    second_entry = entry("second-request", "doc-other")
    module._follow(second_entry)
    module._requests.all_documents = True
    assert len(module._request_view()["entries"]) == 2
    # The real Fusion event is guarded by the same main-thread fake as palettes.
    module.stop(None)
    object.__setattr__(fixture.app, "documentActivated", FakeEvent())
    second = _load_instance(monkeypatch, "WGLink_history_restart", fixture.ui, fixture.app)
    monkeypatch.setattr(second.wglink_workspace, "ipc_folder", lambda **kwargs: fixture.ipc)
    monkeypatch.setattr(second, "_start_timer", lambda *args: None)
    second.run(None)
    try:
        assert len(second._requests.entries()) == 2
        second._requests.all_documents = True
        monkeypatch.setattr(second, "_active_document_id", lambda: "doc-other")
        for handler in fixture.app.documentActivated.handlers:
            handler.notify(None)
        assert not second._requests.all_documents
        assert [e["requestId"] for e in second._request_view()["entries"]] == ["second-request"]
        assert len(list((fixture.ipc / ".wg-solve-requests").glob("*.json"))) == 1
        assert second._requests.get(original["requestId"]) is not None
    finally:
        second.stop(None)


def test_poll_updates_previous_document_and_opens_when_it_needs_you(monkeypatch, tmp_path):
    fixture = _following(monkeypatch, tmp_path, "WGLink_history_poll")
    module = fixture.module
    module._follow(entry("second", "different"))
    _status(fixture, "needsYou", 1)
    module._poll_requests_once()
    _deliver_followups(module, fixture.app)
    assert module._requests.current()["requestId"] == "second"
    assert module._requests.get(fixture.request_id)["status"]["state"] == "needsYou"
    assert _palette(fixture).isVisible


def test_a_reappearing_status_clears_the_restart_notice_even_with_equal_seq(tmp_path):
    request = entry(state="preparing")
    request["missingStatus"] = True
    folder = tmp_path / ".wg-solve-acks"
    folder.mkdir()
    (folder / "request-1.status.json").write_text(json.dumps(request["status"]))
    updated = requests.poll(request, tmp_path)
    assert "missingStatus" not in updated
    assert requests.view(updated)["headline"] != "Check in WG"


def test_corrupt_history_entries_do_not_hide_valid_history(tmp_path):
    valid = entry()
    path = tmp_path / requests.RECORD_FILENAME
    path.write_text(json.dumps({"schema": 1, "entries": [None, {**valid, "pressedAt": "yesterday"},
                                                          {**valid, "requestId": "../outside"}, valid]}))
    assert [e["requestId"] for e in requests.load_history(tmp_path)] == ["request-1"]
