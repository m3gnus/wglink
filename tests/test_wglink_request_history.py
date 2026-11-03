"""History and explicit navigation; all Fusion calls stay on its main thread."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import pytest

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
    def save(record):
        return requests.save(tmp_path, record)
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
    assert len(requests.load_history(tmp_path)) == 1
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


def test_failed_legacy_delete_never_truncates_history(tmp_path, monkeypatch, caplog):
    history = [entry(f"req-{i}", state="solved") for i in range(5)]
    requests.save(tmp_path, {"schema": 1, "entries": history})
    (tmp_path / requests.LEGACY_RECORD_FILENAME).write_text(json.dumps(entry("legacy")))
    real = Path.unlink
    def unlink(path, *args, **kwargs):
        if path.name == requests.LEGACY_RECORD_FILENAME:
            raise PermissionError("in use")
        return real(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    tracker = requests.RequestTracker()
    tracker.restore(requests.load_history(tmp_path))
    tracker.begin(entry("new"), save=lambda payload: requests.save(tmp_path, payload))
    assert {e["requestId"] for e in requests.load_history(tmp_path)} == {"legacy", "new", *[f"req-{i}" for i in range(5)]}
    assert "Could not finish legacy" in caplog.text


def test_save_after_incomplete_recovery_merges_disk(tmp_path):
    requests.save(tmp_path, {"schema": 1, "entries": [entry("old")]})
    tracker = requests.RequestTracker()  # recovery failed before restore
    tracker.begin(entry("new"), save=lambda payload: requests.save(tmp_path, payload))
    assert {e["requestId"] for e in requests.load_history(tmp_path)} == {"old", "new"}


def test_unknown_schema_is_read_only_and_explained(tmp_path, monkeypatch):
    path = tmp_path / requests.RECORD_FILENAME
    original = json.dumps({"schema": 2, "entries": [entry("future")]})
    path.write_text(original)
    with pytest.raises(requests.HistoryUnavailable, match="kept unchanged"):
        requests.load_history(tmp_path)
    with pytest.raises(requests.HistoryUnavailable):
        requests.save(tmp_path, {"schema": 1, "entries": [entry("new")]})
    assert path.read_text() == original


def test_recovery_retains_read_entries_before_poll_failure(monkeypatch, tmp_path):
    fixture = _following(monkeypatch, tmp_path, "WGLink_recovery_failure")
    module = fixture.module
    original = module._requests.entries()
    module._requests.begin(None)
    monkeypatch.setattr(module.wglink_inbox, "read_request_status", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk")))
    module._recover_request()
    assert len(module._requests.entries()) == len(original)
    module._follow(entry("new"))
    assert len(requests.load_history(fixture.ipc)) == len(original) + 1


def test_document_handler_and_removal_never_escape_fusion(monkeypatch, tmp_path):
    fixture = _following(monkeypatch, tmp_path, "WGLink_document_handler")
    module = fixture.module
    monkeypatch.setattr(module, "_active_document_id", lambda: (_ for _ in ()).throw(RuntimeError("Fusion")))
    module.RequestsDocumentHandler().notify(None)
    class BrokenEvent:
        def remove(self, handler):
            raise RuntimeError("Fusion teardown")
    module._request_document_event = BrokenEvent()
    module._request_document_handler = object()
    module._stop_followups(fixture.app)
    assert module._timers_closed and module._followup_event is None
    assert module._request_document_event is None


def test_unsaved_history_remains_in_this_document_after_save(monkeypatch, tmp_path):
    from types import SimpleNamespace
    fixture = _following(monkeypatch, tmp_path, "WGLink_saved_identity")
    module = fixture.module
    product = SimpleNamespace(rootComponent=SimpleNamespace(entityToken="stable-root"))
    document = SimpleNamespace(dataFile=None)
    monkeypatch.setattr(module, "_app", lambda: SimpleNamespace(activeDocument=document, activeProduct=product))
    local_id = module._active_document_id()
    module._follow(entry("before-save", local_id))
    document.dataFile = SimpleNamespace(id="saved-document")
    view = module._request_view()
    assert module._requests.get("before-save")["documentId"] == "fusion:saved-document"
    assert "before-save" in {row["requestId"] for row in view["entries"]}
    assert next(e for e in requests.load_history(fixture.ipc) if e["requestId"] == "before-save")["documentId"] == "fusion:saved-document"


def test_render_computes_identity_once_and_uses_shared_words(monkeypatch, tmp_path):
    from unittest.mock import Mock
    fixture = _following(monkeypatch, tmp_path, "WGLink_render_identity")
    module = fixture.module
    module._requests.all_documents = True
    module._follow({**entry("unnamed"), "documentName": None})
    identity = Mock(return_value="doc-1")
    monkeypatch.setattr(module, "_active_document_id", identity)
    shown = module._request_view()
    identity.assert_called_once()
    unnamed = next(e for e in shown["entries"] if e["requestId"] == "unnamed")
    assert unnamed["documentName"] == protocol.REQUEST_HISTORY_TEXT["unnamedDocument"]
    assert unnamed["kindLabel"] == protocol.REQUEST_HISTORY_TEXT["solve"]
    for row in shown["entries"]:
        for text in [row["headline"], row["detail"], row["documentName"], row["kindLabel"], *row["notes"], *row["options"], *[a["label"] for a in row["actions"]]]:
            if text:
                assert not protocol.user_text_violations(text)


def test_poll_batches_history_write_once_for_multiple_changes(monkeypatch, tmp_path):
    from unittest.mock import Mock
    fixture = _following(monkeypatch, tmp_path, "WGLink_poll_batch")
    module = fixture.module
    module._follow(entry("second"))
    monkeypatch.setattr(module.wglink_requests, "poll", lambda current, ipc: {**current, "heardAt": time.time() + 1})
    save = Mock()
    monkeypatch.setattr(module, "_save_followed", save)
    module._poll_requests_once()
    save.assert_called_once()
    assert len(save.call_args.args[0]["entries"]) == 2


def test_exporting_and_not_sent_never_advance_from_absent_inbox(tmp_path):
    for local in (requests.EXPORTING, requests.NOT_SENT):
        request = {**entry(), "local": local}
        assert requests.poll(request, tmp_path) == request


def test_palette_does_not_announce_whole_list_or_own_punctuation():
    page = (Path(__file__).resolve().parents[1] / "fusion-addins/WGLink/requests.html").read_text()
    assert 'id="history" role="status"' not in page
    assert 'aria-live' not in page
    assert 'view.labels || {}).rowTitle' in page


@pytest.mark.parametrize("content", ["", "bad-json", "[" * 2000, "\xff", '{"schema":1,"entries":null}', "null", "[]", "{}"])
def test_corrupt_history_is_set_aside_and_new_requests_can_be_saved(tmp_path, content):
    path = tmp_path / requests.RECORD_FILENAME
    original = content.encode("latin1")
    path.write_bytes(original)
    notice = []
    assert requests.load_history(tmp_path, on_corrupt=lambda: notice.append(True)) == []
    assert notice == [True] and not path.exists()
    backups = list(tmp_path.glob(requests.RECORD_FILENAME + ".corrupt-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    requests.save(tmp_path, entry("fresh"))
    assert [e["requestId"] for e in requests.load_history(tmp_path)] == ["fresh"]


def test_clear_finished_removes_unrecovered_disk_entries(tmp_path):
    requests.save(tmp_path, {"entries": [entry("done", state="solved"), entry("live")]})
    tracker = requests.RequestTracker()
    tracker.begin(entry("new"))
    tracker.clear_finished(lambda payload: requests.save(tmp_path, payload))
    assert {e["requestId"] for e in requests.load_history(tmp_path)} == {"live", "new"}


def test_recovery_poll_failure_keeps_read_history_and_arms_poll(monkeypatch, tmp_path):
    fixture = _following(monkeypatch, tmp_path, "WGLink_recover_poll_failure")
    module = fixture.module
    original = module._requests.entries()
    module._requests.begin(None)
    armed = []
    monkeypatch.setattr(module, "_arm_request_poll", lambda delay=None: armed.append(delay))
    monkeypatch.setattr(module.wglink_requests, "poll", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("poll")))
    module._recover_request()
    assert module._requests.entries() == original
    assert armed == [0.0]
    assert module._requests_hint == protocol.REQUEST_HISTORY_TEXT["historyCheck"]


def test_replaced_history_save_failure_never_fails_press_or_skips_poll(monkeypatch, tmp_path):
    fixture = _following(monkeypatch, tmp_path, "WGLink_replaced_save_failure")
    module = fixture.module
    previous = module._requests.current()
    assert previous
    monkeypatch.setattr(module.wglink_requests, "replaces", lambda *a: True)
    monkeypatch.setattr(module.wglink_requests, "withdraw", lambda *a: True)
    monkeypatch.setattr(module, "_save_followed", lambda *a: (_ for _ in ()).throw(RuntimeError("save")))
    armed = []
    monkeypatch.setattr(module, "_arm_request_poll", lambda delay=None: armed.append(delay))
    rid = module._submit_to_wg({"bundle_path": str(fixture.bundle), "return_id": "wgr_1"}, requests.SOLVE, superseded=previous)
    assert rid and module._requests.current()["requestId"] == rid
    assert armed == [None]


def test_history_row_template_keeps_literal_document_names(tmp_path):
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the palette renderer")
    page = (Path(requests.__file__).parent / "requests.html").read_text()
    script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    harness = r'''
const assert = require('node:assert/strict');
function element() { return { children: [], dataset: {}, appendChild(x) { this.children.push(x); },
  addEventListener() {}, setAttribute() {} }; }
const ids = { filters: element(), history: element(), diagnostics: element() };
global.document = { activeElement: null, getElementById(id) { return ids[id]; }, createElement: element };
global.window = { addEventListener() {} }; global.location = { hash: '' };
'''
    after = r'''
const name = "Horn $' $& {kind} {time} {document}";
const view = { labels: { rowTitle: '{document} / {kind} / {time}' }, entries: [
  { documentName: name, kindLabel: 'Solve', submittedAt: 0 }
] };
window.fusionJavaScriptHandler.handle('status', JSON.stringify(view));
assert.equal(ids.history.children[0].children[0].textContent, name + ' / Solve / ' + new Date(0).toLocaleString());
'''
    subprocess.run([node, "-e", harness + script + after], check=True, capture_output=True, text=True)


@pytest.mark.parametrize("mode", ["same", "equal", "resolved"])
def test_unsaved_identity_survives_token_drift_switch_and_first_save(monkeypatch, tmp_path, mode):
    from types import SimpleNamespace as NS
    fixture = _following(monkeypatch, tmp_path, "WGLink_local_drift")
    module = fixture.module

    class Base:
        pass

    class Root(Base):
        def __init__(self, target):
            self.target = target
            self.initial_token = target.token

        @property
        def entityToken(self):
            return self.initial_token if mode == "resolved" else self.target.token

        def __eq__(self, other):
            return mode != "resolved" and isinstance(other, Root) and self.target is other.target

    target = NS(token="root-token-a")
    root = Root(target)

    class Design:
        @property
        def rootComponent(self):
            return Root(target) if mode != "same" else root

        def findEntityByToken(self, _token):
            return [target]

    monkeypatch.setattr(module.wglink_core.adsk.core, "Base", Base, raising=False)
    document = NS(dataFile=None)
    app = NS(activeDocument=document, activeProduct=Design())
    monkeypatch.setattr(module, "_app", lambda: app)
    local_id = module._active_document_id()
    module._set_pending_settings(local_id, {"frequencyCount": 41})
    module._follow(entry("drift-request", local_id))
    target.token = "root-token-b"
    assert module._active_document_id() == local_id
    assert module._pending_values(local_id) == {"frequencyCount": 41}
    original_design = app.activeProduct
    app.activeDocument = NS(dataFile=None)
    app.activeProduct = NS(rootComponent=NS(entityToken="another-root"))
    assert module._active_document_id() != local_id
    app.activeDocument, app.activeProduct = document, original_design
    assert module._active_document_id() == local_id
    document.dataFile = NS(id="saved-after-drift")
    saved_id = module._active_document_id()
    assert saved_id == "fusion:saved-after-drift"
    assert module._pending_values(saved_id) == {"frequencyCount": 41}
    assert local_id not in module._pending_settings
    assert module._requests.get("drift-request")["documentId"] == saved_id
    changed_at = module._pending_settings[saved_id]["frequencyCount"]["after"]
    module._active_document_id()
    assert module._pending_settings[saved_id]["frequencyCount"]["after"] == changed_at
    assert next(e for e in requests.load_history(fixture.ipc) if e["requestId"] == "drift-request")["documentId"] == saved_id


def test_restored_sessions_merge_new_history_and_preserve_newer_updates(tmp_path):
    save = lambda payload: requests.save(tmp_path, payload)
    seed = entry("shared")
    requests.save(tmp_path, seed)
    a, b = requests.RequestTracker(), requests.RequestTracker()
    a.restore(requests.load_history(tmp_path))
    b.restore(requests.load_history(tmp_path))
    a.begin(entry("request-a"), save)
    b.begin(entry("request-b"), save)
    previous = a.get("shared")
    a.update_entry(previous, {**previous, "quiet": True}, save)
    b.save(save)
    history = {e["requestId"]: e for e in requests.load_history(tmp_path)}
    assert set(history) == {"shared", "request-a", "request-b"}
    assert history["shared"]["quiet"] is True


def test_clear_finished_tombstones_survive_stale_session_saves(tmp_path):
    save = lambda payload: requests.save(tmp_path, payload)
    requests.save(tmp_path, {"entries": [entry("done", state="solved"), entry("live")]})
    a, b = requests.RequestTracker(), requests.RequestTracker()
    a.restore(requests.load_history(tmp_path))
    b.restore(requests.load_history(tmp_path))
    a.clear_finished(save)
    b.begin(entry("other"), save)
    old = b.get("done")
    b.update_entry(old, {**old, "quiet": True}, save)
    assert {e["requestId"] for e in requests.load_history(tmp_path)} == {"live", "other"}


@pytest.mark.parametrize("resume_saved_first", [False, True])
def test_clear_stale_failure_keeps_another_sessions_resumed_request(tmp_path, resume_saved_first):
    save = lambda payload: requests.save(tmp_path, payload)
    requests.save(tmp_path, entry("shared", state="couldNotFinish"))
    a, b = requests.RequestTracker(), requests.RequestTracker()
    a.restore(requests.load_history(tmp_path))
    b.restore(requests.load_history(tmp_path))
    previous = b.get("shared")
    resumed = {**previous, "status": {**previous["status"], "seq": 2, "state": "preparing"}}
    assert b.update_entry(previous, resumed)  # B has heard from WG but has not saved yet.
    if resume_saved_first:
        b.save(save)
    a.clear_finished(save)
    b.save(save)
    [recovered] = requests.load_history(tmp_path)
    assert recovered["requestId"] == "shared"
    assert recovered["status"] == resumed["status"]
    # A's later stale clear must also leave the resumed request visible.
    a.restore([previous])
    a.clear_finished(save)
    assert requests.load_history(tmp_path) == [recovered]


@pytest.mark.parametrize(("state", "seq", "visible"), [
    ("preparing", 2, False), ("preparing", 3, False), ("preparing", 4, True),
    ("couldNotFinish", 4, False), ("solved", 4, False),
])
def test_cleared_history_only_accepts_a_newer_active_sequence(tmp_path, state, seq, visible):
    save = lambda payload: requests.save(tmp_path, payload)
    failed = entry("shared", state="couldNotFinish")
    failed["status"]["seq"] = 3
    requests.save(tmp_path, failed)
    tracker = requests.RequestTracker()
    tracker.restore(requests.load_history(tmp_path))
    tracker.clear_finished(save)
    snapshot = {**failed, "status": {**failed["status"], "seq": seq, "state": state}}
    requests.save(tmp_path, snapshot)
    recovered = requests.load_history(tmp_path)
    assert bool(recovered) is visible
    if visible:
        assert recovered[0]["status"] == snapshot["status"]
        requests.save(tmp_path, failed)  # A later local edit to an older status is still stale.
        assert requests.load_history(tmp_path) == recovered


@pytest.mark.parametrize("saved_before", [False, True])
def test_clear_finished_keeps_the_latest_unsaved_failure_sequence(tmp_path, saved_before):
    failed = entry("shared", state="couldNotFinish")
    if saved_before:
        requests.save(tmp_path, failed)
    failed["status"]["seq"] = 3
    tracker = requests.RequestTracker()
    tracker.restore([failed])  # A has heard a later failure but has not saved it.
    tracker.clear_finished(lambda payload: requests.save(tmp_path, payload))
    requests.save(tmp_path, {**failed, "status": {**failed["status"], "seq": 2, "state": "preparing"}})
    assert requests.load_history(tmp_path) == []
    requests.save(tmp_path, {**failed, "status": {**failed["status"], "seq": 4, "state": "preparing"}})
    [recovered] = requests.load_history(tmp_path)
    assert recovered["status"]["seq"] == 4
    assert recovered["status"]["state"] == "preparing"


def test_a_later_failure_advances_the_cleared_sequence_before_another_retry(tmp_path):
    failed = entry("shared", state="couldNotFinish")
    requests.save(tmp_path, failed)
    tracker = requests.RequestTracker()
    tracker.restore(requests.load_history(tmp_path))
    tracker.clear_finished(lambda payload: requests.save(tmp_path, payload))
    requests.save(tmp_path, {**failed, "status": {**failed["status"], "seq": 3}})
    requests.save(tmp_path, {**failed, "status": {**failed["status"], "seq": 2, "state": "preparing"}})
    assert requests.load_history(tmp_path) == []
    tracker.restore([{**failed, "status": {**failed["status"], "seq": 5}}])
    tracker.clear_finished(lambda payload: requests.save(tmp_path, payload))
    requests.save(tmp_path, {**failed, "status": {**failed["status"], "seq": 4, "state": "preparing"}})
    assert requests.load_history(tmp_path) == []
    requests.save(tmp_path, {**failed, "status": {**failed["status"], "seq": 6, "state": "preparing"}})
    [recovered] = requests.load_history(tmp_path)
    assert recovered["status"]["seq"] == 6


@pytest.mark.parametrize(("removed", "visible"), [({"shared": False}, False), (["shared"], False), ({"shared": True}, True)])
def test_legacy_history_tombstones_keep_their_meaning(tmp_path, removed, visible):
    path = tmp_path / requests.RECORD_FILENAME
    path.write_text(json.dumps({"schema": 1, "entries": [], "removedIds": removed}))
    requests.save(tmp_path, entry("shared", state="preparing"))
    assert bool(requests.load_history(tmp_path)) is visible


def test_clear_finished_does_not_remove_another_sessions_active_export(tmp_path):
    save = lambda payload: requests.save(tmp_path, payload)
    a = requests.RequestTracker()
    a.begin(requests.new_request("exporting", requests.SEND, "Horn"), save)
    b = requests.RequestTracker()
    b.restore(requests.load_history(tmp_path))  # recovery calls an unfinished export notSent
    b.clear_finished(save)
    submitted = requests.submitted(a.current(), manifest_sha256="a" * 64,
                                  acknowledgement=True, request_status=True, confirmed=True)
    a.replace(submitted, save)
    [recovered] = requests.load_history(tmp_path)
    assert recovered["requestId"] == "exporting"
    assert recovered["local"] == requests.SUBMITTED


def test_newer_wg_sequence_wins_over_a_later_stale_snapshot_edit(tmp_path):
    save = lambda payload: requests.save(tmp_path, payload)
    seed = entry("shared", state="solving")
    requests.save(tmp_path, seed)
    a, b = requests.RequestTracker(), requests.RequestTracker()
    a.restore(requests.load_history(tmp_path))
    b.restore(requests.load_history(tmp_path))
    previous = a.get("shared")
    a.update_entry(previous, {**previous, "status": {**previous["status"], "seq": 2, "state": "solved"}}, save)
    stale = b.get("shared")
    b.update_entry(stale, {**stale, "quiet": True}, save)
    [latest] = requests.load_history(tmp_path)
    assert latest["status"]["seq"] == 2
    assert latest["status"]["state"] == "solved"


def test_clear_finished_can_remove_a_recovered_abandoned_export(tmp_path):
    tracker = requests.RequestTracker()
    save = lambda payload: requests.save(tmp_path, payload)
    tracker.begin(requests.new_request("abandoned", requests.SEND, "Horn"), save)
    recovered = requests.RequestTracker()
    recovered.restore(requests.load_history(tmp_path))
    assert recovered.current()["local"] == requests.NOT_SENT
    recovered.clear_finished(save)
    tracker.save(save)  # an old exporting snapshot cannot resurrect it
    assert requests.load_history(tmp_path) == []
