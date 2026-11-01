"""WG Send to CAD through the actual owner/timer/custom-event path."""
import json
import threading
from pathlib import Path

import pytest

from test_wglink_activation_boundary import _boundary, _deliver_every_event
from test_wglink_addin_lifecycle import _per_request_folders, _per_request_handoff


def _running(monkeypatch, tmp_path, name):
    fixture = _boundary(monkeypatch, tmp_path, name, coordination=None)
    module = fixture.module
    due = []
    monkeypatch.setattr(module, '_start_timer', lambda delay, fn: due.append((delay, fn)))
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path / 'files')
    module.run(None)
    assert not module.SETTINGS_PATH.exists()
    assert (2.0, module._poll_wg_requests_due) in due
    return fixture, ipc, bundles, due


def _poll(module):
    # Fusion fakes reject all off-thread API access except fireCustomEvent.
    errors = []
    def worker():
        try:
            module._poll_wg_requests_due()
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive() and not errors


def _submit(monkeypatch, fixture, ipc, bundles):
    module = fixture.module
    _per_request_handoff(ipc, bundles, instance_id=None,
                         destination={'kind': 'new_document', 'value': 'req-1'})
    writes = []
    original = fixture.app.activeDocument
    def insert(app, path, options):
        options['precondition']()
        writes.append(app.activeDocument)
    monkeypatch.setattr(module.wglink_core, 'insert', insert)
    monkeypatch.setattr(module, '_document_links', lambda *_: [])
    return writes, original


def test_default_owner_picks_up_claims_and_reports_through_registered_path(monkeypatch, tmp_path):
    f, ipc, bundles, due = _running(monkeypatch, tmp_path, 'WGLink_wg_real_path')
    m = f.module
    try:
        writes, original = _submit(monkeypatch, f, ipc, bundles)
        _poll(m)
        _poll(m)
        assert f.app.fired.count(m.FOLLOWUP_EVENT_ID) == 1
        assert len(m._followups) == 1
        assert (ipc / '.fusion-handoffs/req-1.json').exists()  # worker never claims
        _deliver_every_event(f.app)
        assert len(writes) == 1 and writes[0] is not original, (f.ui.messages, f.ui.text_palette.written, m._request_trace, m._followups)
        assert original.name == 'Horn'
        assert not list((ipc / '.fusion-handoffs').iterdir())
        report = json.loads((ipc / '.fusion-status.json').read_text())
        assert report['diagnostics']['activation']['wgRequestPickup'] == 1
        [outcome] = report['diagnostics']['recentOutcomes']
        assert outcome['requestId'] == 'req-1' and outcome['outcome'] == 'applied'
        assert outcome['exportId'] == 'wge_5'
        assert report['document']['id'] == outcome['documentId']
        assert report['diagnostics']['activity']['betweenCommands'] == {}
    finally:
        m.stop(None)


@pytest.mark.parametrize('block', ['startup', 'command', 'busy', 'owner'])
def test_deferred_requests_remain_unclaimed_and_resume(monkeypatch, tmp_path, block):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_defer_' + block)
    m = f.module
    try:
        writes, _ = _submit(monkeypatch, f, ipc, bundles)
        owned = m._owns_active_ipc_lease
        if block == 'startup': f.app.isStartupComplete = False
        if block == 'command': f.ui.activeCommand = 'ExtrudeCommand'
        if block == 'busy': m._command_busy = True
        if block == 'owner': monkeypatch.setattr(m, '_owns_active_ipc_lease', lambda: False)
        _poll(m)
        _deliver_every_event(f.app)
        assert not writes and (ipc / '.fusion-handoffs/req-1.json').exists()
        assert m._handoff_attempted_id is None
        f.app.isStartupComplete = True
        f.ui.activeCommand = 'SelectCommand'
        m._command_busy = False
        monkeypatch.setattr(m, '_owns_active_ipc_lease', owned)
        _poll(m)
        _deliver_every_event(f.app)
        assert len(writes) == 1
    finally:
        m._command_busy = False
        m.stop(None)


def test_empty_poll_is_file_only_and_memo_stops_unusable_files(monkeypatch, tmp_path):
    f, ipc, bundles, due = _running(monkeypatch, tmp_path, 'WGLink_wg_idle')
    m = f.module
    try:
        report = ipc / '.fusion-status.json'
        before = report.stat().st_mtime_ns
        fired = list(f.app.fired)
        monkeypatch.setattr(m.wglink_handoff, '_read_json', lambda *_: pytest.fail('empty inbox read JSON'))
        for _ in range(3): _poll(m)
        assert m._wg_request_poll_count >= 3
        assert f.app.fired == fired and report.stat().st_mtime_ns == before
    finally:
        m.stop(None)


def test_invalid_file_is_memoized_and_changed_file_is_retried(monkeypatch, tmp_path):
    f, ipc, bundles, due = _running(monkeypatch, tmp_path, 'WGLink_wg_memo')
    m = f.module
    try:
        folder = ipc / '.fusion-handoffs'
        folder.mkdir(exist_ok=True)
        request = folder / 'broken.json'
        request.write_text('{}')
        _poll(m)
        _deliver_every_event(f.app)
        count = len(f.app.fired)
        _poll(m)
        assert len(f.app.fired) == count
        request.write_text('{"changed":true}')
        _poll(m)
        assert len(f.app.fired) == count + 1
    finally:
        m.stop(None)


def test_failure_keeps_new_document_for_inspection_and_protects_refusal_modal(monkeypatch, tmp_path):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_failed_insert')
    m = f.module
    try:
        _, original = _submit(monkeypatch, f, ipc, bundles)
        def refuse(*_):
            raise m.wglink_core.WgLinkError('Cannot insert. Send the model again.')
        monkeypatch.setattr(m.wglink_core, 'insert', refuse)
        message = m._message
        def modal(*args):
            assert m._command_busy
            _poll(m)  # nested custom-event delivery cannot run another job
            m._run_followups()
            message(*args)
        monkeypatch.setattr(m, '_message', modal)
        _poll(m)
        _deliver_every_event(f.app)
        assert f.app.activeDocument is not original and original.name == 'Horn'
        assert not (ipc / '.fusion-handoffs/req-1.json').exists()
        record = json.loads((ipc / '.fusion-status.json').read_text())['diagnostics']['recentOutcomes'][-1]
        assert record['outcome'] == 'refused'
        assert 'remains open' in record['message'] and 'close it without saving' in record['message']
    finally:
        m.stop(None)


def test_preflight_refusal_is_busy_and_return_requests_stay_unconsumed(monkeypatch, tmp_path):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_preflight')
    m = f.module
    try:
        _submit(monkeypatch, f, ipc, bundles)
        returned = ipc / '.fusion-return-requests/return.json'
        returned.parent.mkdir()
        returned.write_text('{}')
        def refuse():
            raise m.wglink_core.WgLinkError('WG is too old. Update WG and send again.')
        monkeypatch.setattr(m, '_require_compatible_wg', refuse)
        monkeypatch.setattr(m, '_message', lambda *_: m._command_busy or pytest.fail('unprotected modal'))
        _poll(m)
        _deliver_every_event(f.app)
        assert returned.exists()
        assert json.loads((ipc / '.fusion-status.json').read_text())['diagnostics']['recentOutcomes'][-1]['outcome'] == 'refused'
    finally:
        m.stop(None)


def test_transient_claim_failure_is_retried_without_changing_the_file(monkeypatch, tmp_path):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_claim_retry')
    m = f.module
    try:
        writes, _ = _submit(monkeypatch, f, ipc, bundles)
        claim = m.wglink_handoff.claim_request
        monkeypatch.setattr(m.wglink_handoff, 'claim_request', lambda _: None)
        _poll(m)
        _deliver_every_event(f.app)
        assert not writes and m._wg_request_memo is None
        monkeypatch.setattr(m.wglink_handoff, 'claim_request', claim)
        _poll(m)
        _deliver_every_event(f.app)
        assert len(writes) == 1
    finally:
        m.stop(None)


def test_a_request_arriving_during_invalid_file_read_is_not_memoized(monkeypatch, tmp_path):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_memo_race')
    m = f.module
    try:
        folder = ipc / '.fusion-handoffs'
        folder.mkdir()
        (folder / 'invalid.json').write_text('{}')
        read = m.wglink_handoff.next_pending_handoff
        once = []
        writes = []
        def racing_read(*args, **kwargs):
            result = read(*args, **kwargs)
            if not once:
                once.append(True)
                new_writes, _ = _submit(monkeypatch, f, ipc, bundles)
                writes.append(new_writes)
            return result
        monkeypatch.setattr(m.wglink_handoff, 'next_pending_handoff', racing_read)
        _poll(m)
        _deliver_every_event(f.app)
        assert not writes[0]
        _poll(m)
        _deliver_every_event(f.app)
        assert len(writes[0]) == 1
    finally:
        m.stop(None)


def test_unreadable_document_still_publishes_the_refusal_receipt(monkeypatch, tmp_path):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_unreadable_receipt')
    m = f.module
    try:
        _submit(monkeypatch, f, ipc, bundles)
        monkeypatch.setattr(m, '_fusion_snapshot', lambda: {
            'document_name': 'Horn', 'document_id': 'fusion:doc-a',
            'links': [], 'links_inspected': False, 'diagnostics': {},
        })
        def unreadable(*_):
            raise m.LinksNotInspected(m._NOT_INSPECTED_TEXT)
        monkeypatch.setattr(m, '_document_links', unreadable)
        _poll(m)
        _deliver_every_event(f.app)
        report = json.loads((ipc / '.fusion-status.json').read_text())
        assert report['document'] is None  # no invented empty observation of Horn
        assert report['diagnostics']['recentOutcomes'][-1]['outcome'] == 'refused'
    finally:
        m.stop(None)
