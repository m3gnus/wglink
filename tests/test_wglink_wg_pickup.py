"""WG Send to CAD through the actual owner/timer/custom-event path."""
import json
import threading
import types
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


def test_created_document_without_active_design_is_terminal_and_created_once(monkeypatch, tmp_path):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_not_ready_after_add')
    m = f.module
    try:
        writes, original = _submit(monkeypatch, f, ipc, bundles)
        created = []
        def add(_kind):
            created.append(types.SimpleNamespace(name='Untitled'))
            f.app.activeDocument = created[-1]
            f.app.activeProduct = types.SimpleNamespace(objectType='adsk::drawing::Drawing')
            return created[-1]
        monkeypatch.setattr(f.app.documents, 'add', add)
        for _ in range(3):
            _poll(m)
            _deliver_every_event(f.app)
        assert len(created) == 1 and not writes
        assert original.name == 'Horn' and f.app.activeDocument is created[0]
        assert not list((ipc / '.fusion-handoffs').iterdir())
        receipt = json.loads((ipc / '.fusion-status.json').read_text())['diagnostics']['recentOutcomes'][-1]
        assert receipt['outcome'] == 'refused'
        assert 'remains open' in receipt['message'] and 'close it without saving' in receipt['message']
        assert not m._handoff_retry_pending
    finally:
        m.stop(None)


@pytest.mark.parametrize('carrier', ['followup', 'tick'])
@pytest.mark.parametrize('edit_type', ['Sketch', 'BaseFeature', 'Component', None])
def test_design_edit_sessions_defer_both_handoff_carriers(monkeypatch, tmp_path, carrier, edit_type):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_edit_' + carrier + str(edit_type))
    m = f.module
    try:
        writes, _ = _submit(monkeypatch, f, ipc, bundles)
        f.design.activeEditObject = (types.SimpleNamespace(objectType='adsk::fusion::' + edit_type)
                                     if edit_type else None)
        def deliver():
            if carrier == 'tick':
                m._on_watch_tick()
            else:
                _poll(m)
                _deliver_every_event(f.app)
        deliver()
        assert not writes and (ipc / '.fusion-handoffs/req-1.json').exists()
        assert not m._handoff_attempted_id
        assert not m._wg_request_memo
        f.design.activeEditObject = f.design.rootComponent
        deliver()
        assert len(writes) == 1
    finally:
        m.stop(None)


def test_non_design_product_does_not_defer_new_document_handoff(monkeypatch, tmp_path):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_non_design')
    m = f.module
    try:
        writes, _ = _submit(monkeypatch, f, ipc, bundles)
        f.app.activeProduct = types.SimpleNamespace(objectType='adsk::drawing::Drawing')
        _poll(m)
        _deliver_every_event(f.app)
        assert len(writes) == 1
    finally:
        m.stop(None)


@pytest.mark.parametrize('release_fails', [False, True])
def test_not_ready_release_retains_failed_claim_and_publishes_no_false_pickup(monkeypatch, tmp_path, release_fails):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_release_' + str(release_fails))
    m = f.module
    try:
        writes, _ = _submit(monkeypatch, f, ipc, bundles)
        monkeypatch.setattr(m, '_ensure_design_ready', lambda **_: False)
        rename = m.wglink_handoff.os.rename
        def release(source, target):
            if release_fails and Path(source).name.startswith(m.wglink_handoff.CLAIM_PREFIX):
                raise PermissionError('release is held open')
            return rename(source, target)
        monkeypatch.setattr(m.wglink_handoff.os, 'rename', release)
        _poll(m)
        _deliver_every_event(f.app)
        assert not writes
        [retained] = list((ipc / '.fusion-handoffs').iterdir())
        assert retained.name.startswith(m.wglink_handoff.CLAIM_PREFIX) == release_fails
        report = json.loads((ipc / '.fusion-status.json').read_text())['diagnostics']
        assert not any(item['outcome'] == 'claimed' for item in report.get('recentOutcomes', []))
        assert report.get('lastRequest', {}).get('outcome') != 'running'
    finally:
        m.stop(None)


@pytest.mark.parametrize('failure', ['_service_geometry_refresh', '_fusion_snapshot', '_settle_claims'])
def test_post_job_receipt_survives_observation_failure(monkeypatch, tmp_path, failure):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_receipt_' + failure)
    m = f.module
    try:
        _submit(monkeypatch, f, ipc, bundles)
        def broken(*args, **kwargs):
            raise RuntimeError('temporary observation failure')
        monkeypatch.setattr(m, failure, broken)
        if failure == '_settle_claims':
            monkeypatch.setattr(m, '_claims_outstanding', lambda: True)
        _poll(m)
        _deliver_every_event(f.app)
        report = json.loads((ipc / '.fusion-status.json').read_text())
        assert report['diagnostics']['recentOutcomes'][-1]['outcome'] == 'applied'
        assert not m._command_busy
        if failure == '_fusion_snapshot':
            assert report['document'] is None
    finally:
        m.stop(None)


@pytest.mark.parametrize('failure', ['false', 'raise'])
def test_failed_followup_event_keeps_job_and_retries_delivery(monkeypatch, tmp_path, failure):
    f, ipc, bundles, due = _running(monkeypatch, tmp_path, 'WGLink_wg_event_' + failure)
    m = f.module
    try:
        writes, _ = _submit(monkeypatch, f, ipc, bundles)
        fire = f.app.fireCustomEvent
        def failed(_event):
            if failure == 'raise':
                raise RuntimeError('event temporarily unavailable')
            return False
        monkeypatch.setattr(f.app, 'fireCustomEvent', failed)
        _poll(m)
        assert len(m._followups) == 1 and m._service_wg_handoff in m._followups_once
        assert (m.FOLLOWUP_RETRY_SECONDS, m._raise_followup_event) in due
        _poll(m)
        assert len(m._followups) == 1
        monkeypatch.setattr(f.app, 'fireCustomEvent', fire)
        m._raise_followup_event()
        _deliver_every_event(f.app)
        assert len(writes) == 1 and not m._followups_once
    finally:
        m.stop(None)


def test_watch_tick_reuses_snapshot_until_a_request_runs(monkeypatch, tmp_path):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_tick_snapshot')
    m = f.module
    try:
        read = m._fusion_snapshot
        snapshots = []
        def snapshot():
            result = read()
            snapshots.append(result)
            return result
        monkeypatch.setattr(m, '_fusion_snapshot', snapshot)
        m._on_watch_tick()
        assert len(snapshots) == 1
        writes, _ = _submit(monkeypatch, f, ipc, bundles)
        snapshots.clear()
        m._on_watch_tick()
        assert len(writes) == 1
        assert snapshots[0]['document_id'] != snapshots[-1]['document_id']
        report = json.loads((ipc / '.fusion-status.json').read_text())
        assert report['document']['id'] == snapshots[-1]['document_id']
    finally:
        m.stop(None)
