"""WG Send to CAD through the actual owner/timer/custom-event path."""
import json
import threading
import types
from pathlib import Path

import pytest

from test_wglink_activation_boundary import _boundary, _deliver_every_event
from test_wglink_addin_lifecycle import _linked_document, _per_request_folders, _per_request_handoff, _proxying_root


def _running(monkeypatch, tmp_path, name, *, real_geometry=False):
    fixture = _boundary(monkeypatch, tmp_path, name, coordination=None)
    module = fixture.module
    due = []
    monkeypatch.setattr(module, '_start_timer', lambda delay, fn: due.append((delay, fn)))
    fresh_geometry_state = module._fresh_geometry_state
    ipc, bundles = _per_request_folders(monkeypatch, module, tmp_path / 'files')
    if real_geometry:
        monkeypatch.setattr(module, '_fresh_geometry_state', fresh_geometry_state)
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


@pytest.mark.parametrize('signature', ['sha256:measured', ''])
def test_handled_request_publishes_measured_status(monkeypatch, tmp_path, signature):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_measured', real_geometry=True)
    m = f.module
    try:
        document_links = m._document_links
        _submit(monkeypatch, f, ipc, bundles)
        def insert(app, path, options):
            options['precondition']()
            app.activeProduct, _ = _linked_document(m, instance_id='wgi-new')
            monkeypatch.setattr(m, '_document_links', document_links)
        monkeypatch.setattr(m.wglink_core, 'insert', insert)
        # Stand in for Fusion's export fingerprint; the refresh, cache and
        # snapshot paths, including the managed body's state, all run for real.
        monkeypatch.setattr(m.wglink_send, 'return_state', lambda *_: {'hash': signature})
        before = m.wglink_activity.LOG.counts().get('status_publication', {}).get('command:wg-send', 0)
        _poll(m)
        _deliver_every_event(f.app)
        report = json.loads((ipc / '.fusion-status.json').read_text())
        assert report['diagnostics']['recentOutcomes'][-1]['outcome'] == 'applied'
        assert m._geometry_state_cache is not None, (m.wglink_activity.LOG.counts(), f.ui.text_palette.written)
        [link] = report['document']['links']
        assert (link['documentSignatureHash'] or '') == signature
        assert link['localBodyState'] != 'unknown'
        timings = report['diagnostics']['lastTickMs']
        assert timings['geometry_state'] in ('cached', 'stale')
        assert timings['geometry_refresh_pending'] is False
        assert m._geometry_refresh_pending is None
        activity = report['diagnostics']['activity']
        assert activity['counts']['geometry_measurement'] == {'command:wg-send': 1}
        # The existing claim receipt plus exactly one post-command status.
        assert activity['counts']['status_publication']['command:wg-send'] == before + 2
        assert activity['betweenCommands'] == {}
    finally:
        m.stop(None)


@pytest.mark.parametrize('result', ['idle', 'suppressed'])
def test_unhandled_request_does_not_request_or_measure_geometry(monkeypatch, tmp_path, result):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_unhandled', real_geometry=True)
    m = f.module
    try:
        _submit(monkeypatch, f, ipc, bundles)
        monkeypatch.setattr(m, '_apply_pending_handoff', lambda _: result)
        monkeypatch.setattr(m, '_request_geometry_refresh', lambda *_: pytest.fail('unhandled request refreshed'))
        monkeypatch.setattr(m, '_measure_geometry_state', lambda *_: pytest.fail('unhandled request measured'))
        m._geometry_refresh_pending = None
        assert m._service_wg_handoff() == result
    finally:
        m.stop(None)


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
        monkeypatch.setattr(m, '_measure_geometry_state', lambda *_: pytest.fail('empty inbox measured'))
        for _ in range(3): _poll(m)
        assert m._service_wg_handoff() == m.IDLE
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
        request = folder / 'no correlation.json'
        request.write_text('{}')
        _poll(m)
        _deliver_every_event(f.app)
        count = len(f.app.fired)
        assert request.exists()  # No correlatable ID: leave it and suppress wakeups.
        assert not m._recent_outcomes
        _poll(m)
        assert len(f.app.fired) == count
        request.write_text('{"changed":true}')
        _poll(m)
        assert len(f.app.fired) == count + 1
    finally:
        m.stop(None)


@pytest.mark.parametrize('invalidity', ['malformed-json', 'missing-fields', 'bundle-root'])
def test_refused_unusable_file_is_retired_and_cannot_run_after_restart(monkeypatch, tmp_path, invalidity):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_retire_' + invalidity)
    m = f.module
    try:
        writes, original = _submit(monkeypatch, f, ipc, bundles)
        request = ipc / '.fusion-handoffs/req-1.json'
        payload = json.loads(request.read_text())
        if invalidity == 'bundle-root':
            monkeypatch.setattr(m.wglink_workspace, 'bundle_folder', lambda: tmp_path / 'other-bundles')
        else:
            request.write_text('{' if invalidity == 'malformed-json' else '{}')
        _poll(m)
        _deliver_every_event(f.app)
        assert not writes and f.app.activeDocument is original
        receipt = json.loads((ipc / '.fusion-status.json').read_text())['diagnostics']['recentOutcomes'][-1]
        assert receipt['requestId'] == 'req-1' and receipt['outcome'] == 'refused'
        assert 'send the model from WG again' in receipt['message']
        assert not list(request.parent.iterdir())  # Neither a runnable file nor a claim remains.

        m.stop(None)
        # Simulate switching back to the original workspace, or an updated
        # reader that can now parse the old file. Neither may resurrect it.
        monkeypatch.setattr(m.wglink_workspace, 'bundle_folder', lambda: bundles)
        read = m.wglink_handoff._read_json
        monkeypatch.setattr(m.wglink_handoff, '_read_json', lambda path, **kwargs:
                            payload if path == request else read(path, **kwargs))
        m.run(None)
        assert m._wg_request_memo is None
        for _ in range(2):
            _poll(m)
            _deliver_every_event(f.app)
        assert not writes and f.app.activeDocument is original
        assert not request.exists()
    finally:
        m.stop(None)


def test_unusable_claim_failure_retries_before_reporting_refusal(monkeypatch, tmp_path):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_unusable_claim_retry')
    m = f.module
    try:
        request = ipc / '.fusion-handoffs/broken.json'
        request.parent.mkdir()
        request.write_text('{}')
        claim = m.wglink_handoff.claim_request
        monkeypatch.setattr(m.wglink_handoff, 'claim_request', lambda _: None)
        _poll(m)
        _deliver_every_event(f.app)
        assert request.exists() and m._wg_request_memo is None
        assert not m._recent_outcomes
        monkeypatch.setattr(m.wglink_handoff, 'claim_request', claim)
        _poll(m)
        _deliver_every_event(f.app)
        assert not list(request.parent.iterdir())
        assert m._recent_outcomes[-1]['outcome'] == 'refused'
    finally:
        m.stop(None)


def test_unusable_file_changed_before_claim_is_retried_without_refusal(monkeypatch, tmp_path):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_unusable_claim_race')
    m = f.module
    try:
        writes, _ = _submit(monkeypatch, f, ipc, bundles)
        request = ipc / '.fusion-handoffs/req-1.json'
        payload = request.read_text()
        request.write_text('{}')
        rename = m.wglink_handoff.os.rename
        def changed(source, target):
            if source == request:
                request.write_text(payload)
            return rename(source, target)
        monkeypatch.setattr(m.wglink_handoff.os, 'rename', changed)
        _poll(m)
        _deliver_every_event(f.app)
        assert request.exists() and not writes
        assert m._wg_request_memo is None and not m._recent_outcomes
        _poll(m)
        _deliver_every_event(f.app)
        assert len(writes) == 1
        assert not list(request.parent.iterdir())
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
@pytest.mark.parametrize('edit_type', ['Sketch', 'BaseFeature', 'FormFeature', 'Unknown', None])
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


@pytest.mark.parametrize('component', ['root', 'sub-component'])
def test_idle_gate_accepts_any_component(monkeypatch, tmp_path, component):
    f, _, _, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_component_' + component)
    try:
        f.design.activeEditObject = (f.design.rootComponent if component == 'root' else
                                    types.SimpleNamespace(objectType='adsk::fusion::Component',
                                                          entityToken='activated-sub-component'))
        assert f.module._handoff_idle()
    finally:
        f.module.stop(None)


@pytest.mark.parametrize(('workspace_id', 'tab_id', 'active', 'idle'), [
    ('FusionSolidEnvironment', 'SolidTab', True, True),
    ('TSplineEnvironment', 'SolidTab', True, False),
    ('FusionSolidEnvironment', 'BasefeatureSolidTab', True, False),
    ('FusionSolidEnvironment', 'BASEFEATURESolidTab', True, False),
    ('FusionSolidEnvironment', 'SurfaceTab', True, True),
    ('FusionSolidEnvironment', 'BasefeatureSolidTab', False, True),
])
def test_idle_gate_checks_workspace_and_active_tabs(monkeypatch, tmp_path, workspace_id, tab_id, active, idle):
    f, _, _, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_workspace')
    try:
        f.ui.activeWorkspace = types.SimpleNamespace(
            id=workspace_id,
            toolbarTabs=[types.SimpleNamespace(id='SolidTab', isActive=True),
                         types.SimpleNamespace(id=tab_id, isActive=active)],
        )
        assert f.module._handoff_idle() is idle
    finally:
        f.module.stop(None)


@pytest.mark.parametrize('unreadable', ['workspace', 'tabs', 'tab-active', 'tab-id'])
def test_unreadable_workspace_or_tabs_defer(monkeypatch, tmp_path, unreadable):
    f, _, _, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_unreadable_workspace')
    class Unreadable:
        def __getattr__(self, name):
            raise RuntimeError('workspace state unavailable')
    try:
        if unreadable == 'workspace':
            f.ui.activeWorkspace = Unreadable()
        elif unreadable == 'tabs':
            f.ui.activeWorkspace = types.SimpleNamespace(id='FusionSolidEnvironment')
        elif unreadable == 'tab-active':
            f.ui.activeWorkspace.toolbarTabs = [Unreadable()]
        else:
            f.ui.activeWorkspace.toolbarTabs = [types.SimpleNamespace(isActive=True)]
        assert not f.module._handoff_idle()
    finally:
        f.module.stop(None)


def test_unreadable_edit_state_defers(monkeypatch, tmp_path):
    f, _, _, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_unreadable_edit')
    class UnreadableDesign:
        objectType = 'adsk::fusion::Design'

        @property
        def activeEditObject(self):
            raise RuntimeError('edit state unavailable')
    try:
        f.app.activeProduct = UnreadableDesign()
        assert not f.module._handoff_idle()
    finally:
        f.module.stop(None)


def test_request_delivered_with_non_root_component_active(monkeypatch, tmp_path):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_activated_component')
    m = f.module
    try:
        writes, _ = _submit(monkeypatch, f, ipc, bundles)
        f.design.activeEditObject = types.SimpleNamespace(
            objectType='adsk::fusion::Component', entityToken='activated-sub-component')
        assert f.design.activeEditObject.entityToken != f.design.rootComponent.entityToken
        _poll(m)
        _deliver_every_event(f.app)
        assert len(writes) == 1
        assert not list((ipc / '.fusion-handoffs').iterdir())
        receipt = json.loads((ipc / '.fusion-status.json').read_text())['diagnostics']['recentOutcomes'][-1]
        assert receipt['requestId'] == 'req-1' and receipt['outcome'] == 'applied'
    finally:
        m.stop(None)


def test_idle_gate_recognizes_fresh_wrappers_of_the_root_component(monkeypatch, tmp_path):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_idle_proxy')
    m = f.module
    try:
        writes, _ = _submit(monkeypatch, f, ipc, bundles)
        f.app.activeProduct = _proxying_root(f.design)
        assert f.app.activeProduct.activeEditObject is not f.app.activeProduct.rootComponent
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


@pytest.mark.parametrize('block', ['read', 'bundle_folder', 'selection'])
def test_transient_idle_result_retries_the_unchanged_valid_file(monkeypatch, tmp_path, block):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_transient_' + block)
    m = f.module
    try:
        writes, _ = _submit(monkeypatch, f, ipc, bundles)
        request = ipc / '.fusion-handoffs/req-1.json'
        before = request.stat()
        if block == 'bundle_folder':
            read_bundles = m.wglink_workspace.bundle_folder
            monkeypatch.setattr(m.wglink_workspace, 'bundle_folder', lambda: None)
        elif block == 'selection':
            select = m.wglink_handoff.next_pending_handoff
            monkeypatch.setattr(m.wglink_handoff, 'next_pending_handoff', lambda *_, **__: None)
        else:
            read = Path.read_text
            def unreadable(path, *args, **kwargs):
                if path == request:
                    raise PermissionError('request temporarily held open')
                return read(path, *args, **kwargs)
            monkeypatch.setattr(Path, 'read_text', unreadable)
        _poll(m)
        _deliver_every_event(f.app)
        assert not writes and m._wg_request_memo is None
        assert not m._recent_outcomes
        assert (request.stat().st_mtime_ns, request.stat().st_size) == (before.st_mtime_ns, before.st_size)
        if block == 'bundle_folder':
            monkeypatch.setattr(m.wglink_workspace, 'bundle_folder', read_bundles)
        elif block == 'selection':
            monkeypatch.setattr(m.wglink_handoff, 'next_pending_handoff', select)
        else:
            monkeypatch.setattr(Path, 'read_text', read)
        _poll(m)
        _deliver_every_event(f.app)
        assert len(writes) == 1
    finally:
        m.stop(None)


@pytest.mark.parametrize('carrier', ['followup', 'tick'])
@pytest.mark.parametrize('invalid', ['json', 'schema', 'sequence', 'bundle'])
def test_proven_unusable_request_reports_a_correlated_terminal_refusal(monkeypatch, tmp_path, carrier, invalid):
    f, ipc, bundles, _ = _running(monkeypatch, tmp_path, 'WGLink_wg_invalid_' + carrier + invalid)
    m = f.module
    try:
        writes, _ = _submit(monkeypatch, f, ipc, bundles)
        request = ipc / '.fusion-handoffs/req-1.json'
        payload = json.loads(request.read_text())
        if invalid == 'json':
            request.write_text('{broken')
        else:
            payload[{'schema': 'schemaVersion', 'sequence': 'deliverySequence', 'bundle': 'bundlePath'}[invalid]] = (
                'wrong' if invalid != 'bundle' else str(tmp_path / 'outside/horn.wglink'))
            request.write_text(json.dumps(payload))
        if carrier == 'tick':
            m._on_watch_tick()
        else:
            _poll(m)
            _deliver_every_event(f.app)
        assert not writes and not list(request.parent.iterdir())
        report = json.loads((ipc / '.fusion-status.json').read_text())
        receipt = report['diagnostics']['recentOutcomes'][-1]
        assert receipt['requestId'] == 'req-1' and receipt['outcome'] == 'refused'
        assert 'send the model from WG again' in receipt['message']
        count = len(f.app.fired)
        _poll(m)
        assert len(f.app.fired) == count
    finally:
        m.stop(None)
