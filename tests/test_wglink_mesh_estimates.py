"""Pinned sizing, ranges and request-bound mesh evidence without Fusion."""
import hashlib
import json
from pathlib import Path
import sys
import types

import pytest

from test_wglink_send import send_module as _send_module

send_module = _send_module

ADDIN = Path(__file__).resolve().parents[1] / 'fusion-addins/WGLink'
sys.path.insert(0, str(ADDIN))
import wglink_mesh as model  # noqa: E402
import wglink_mesh_history as history  # noqa: E402
import wglink_mesh_sizing as sizing  # noqa: E402
import wglink_inbox as inbox  # noqa: E402
import wglink_protocol as protocol  # noqa: E402


def test_sizing_manifest_and_uniform_count():
    source = json.loads((ADDIN / 'wglink_mesh_sizing.source.json').read_text())
    assert hashlib.sha256((ADDIN / 'wglink_mesh_sizing.py').read_bytes()).hexdigest() == source['sha256']
    assert source['repository'].endswith('/hornlab-waveguide-mesher.git')
    assert source['path'] == 'hornlab_mesher/mesh_sizing.py'
    mesh = protocol.default_mesh([])
    assert model.estimate_triangles(10000, [], mesh)['full'] == sizing.estimate_triangle_count([sizing.Region(10000, 25)])


@pytest.mark.parametrize('full,colour,ceiling', [(22000, 'green', False), (22004, 'amber', False),
                                               (88000, 'amber', False), (88004, 'amber', True)])
def test_range_colours_and_least_ceiling(full, colour, ceiling):
    estimate = {'full': full, 'half': round(full / 2), 'quarter': round(full / 4)}
    display = model.estimate_display(estimate)
    assert display['colour'] == colour
    assert (display['ceiling'] is not None) == ceiling
    assert f"{estimate['quarter']:,} – {full:,}" in display['text']
    assert not protocol.user_text_violations(display['text'])
    if full > 22000 and not ceiling:
        assert 'will ask before solving' in display['text']
    if ceiling:
        assert not protocol.user_text_violations(protocol.finding_text(display['ceiling']))


@pytest.mark.parametrize('symmetry,declared', [('off', False), ('auto', True)])
def test_off_and_declared_show_one_number_and_never_reduce_twice(symmetry, declared):
    display = model.estimate_display({'full': 24000, 'half': 12000, 'quarter': 6000},
                                      symmetry=symmetry, declared_reduced=declared)
    assert 'About 24,000 triangles' in display['text'] and '–' not in display['text']
    assert 'symmetry' not in display['text']
    assert display['ceiling'] is not None


@pytest.mark.parametrize('payload,expected', [(None, (22000, 18000)),
    ({'schemaVersion': 1, 'meshLimits': {'solvedTriangles': 30000, 'fullDomainWarning': 26000}}, (30000, 26000)),
    ({'schemaVersion': 9, 'meshLimits': {'solvedTriangles': 3}}, (22000, 18000)),
    ({'schemaVersion': 1, 'meshLimits': {'solvedTriangles': True, 'fullDomainWarning': -1}}, (22000, 18000))])
def test_capability_fallback_and_shared_preflight(tmp_path, payload, expected):
    if payload:
        (tmp_path / inbox.CAPABILITIES_FILENAME).write_text(json.dumps(payload))
    limits = inbox.wg_mesh_limits(tmp_path)
    assert tuple(limits.values()) == expected
    draft = {'kind': 'solve', 'sources': [{'role': 'HF'}], 'triangles_least': 25000}
    assert bool(protocol.preflight_findings(draft, limits=limits)) == (25000 > expected[0])


def status(folder, *, state='solved', count=19812, fraction=.25, manifest='sha256:one', request='solve1'):
    data = {'contract': protocol.REQUEST_STATUS_CONTRACT, 'schemaVersion': 1, 'seq': 1,
            'requestId': request, 'kind': 'prepare_and_solve', 'state': state, 'manifestSha256': manifest,
            'result': {'jobId': 'job1', 'runNumber': 1, 'mesh': {'trianglesSolved': count,
                       'solvedFraction': fraction, 'domainKind': 'quarter'}}}
    if state == 'needsYou':
        data['prompt'] = {'code': 'mesh_too_large', 'options': []}
    directory = folder / inbox.ACK_DIRECTORY
    directory.mkdir(exist_ok=True)
    (directory / ('solve1' + protocol.REQUEST_STATUS_SUFFIX)).write_text(json.dumps(data))


@pytest.mark.parametrize('state', ['solved', 'needsYou'])
def test_learning_is_bound_to_request_document_geometry_and_settings(tmp_path, state):
    mesh = protocol.default_mesh([])
    geometry = [['body1', 'revision1', True]]
    estimate = {'full': 80000, 'half': 40000, 'quarter': 20000}
    history.remember(tmp_path, 'solve1', 'sha256:one', 'doc1', geometry, mesh, [], estimate)
    status(tmp_path, state=state)
    learned = history.learn(tmp_path, 'doc1', geometry, mesh, [])
    assert learned['matches'] and history.matching_fraction(learned) == .25
    assert learned['calibration']['estimated'] == 20000
    assert learned['calibration']['actual'] == 19812
    display = model.estimate_display(estimate, learned=learned)
    assert 'About 20,000 triangles' in display['text']
    assert 'Last WG mesh: 19,812 triangles (quarter model).' in display['text']
    assert not protocol.user_text_violations(display['text'])
    for current_mesh, current_geometry in (({**mesh, 'wall_size_mm': 30}, geometry),
                                           (mesh, [['body1', 'revision2', True]]), (mesh, None)):
        stale = history.learn(tmp_path, 'doc1', current_geometry, current_mesh, [])
        assert history.matching_fraction(stale) is None
        display = model.estimate_display(estimate, learned=stale)
        assert '20,000 – 80,000' in display['text'] and 'Last WG mesh:' not in display['text']
        assert 'Last time Waveguide Generator used a quarter model.' in display['text']
        assert not protocol.user_text_violations(display['text'])
    assert history.learn(tmp_path, 'doc2', geometry, mesh, []) is None


@pytest.mark.parametrize('change', ['manifest', 'request', 'state'])
def test_unrelated_status_never_teaches_a_fraction(tmp_path, change):
    mesh = protocol.default_mesh([])
    history.remember(tmp_path, 'solve1', 'sha256:one', 'doc', ['body'], mesh, [], {'full': 100})
    status(tmp_path, **{'manifest': 'sha256:other'} if change == 'manifest' else
           {'request': 'other'} if change == 'request' else {'state': 'preparing'})
    assert history.learn(tmp_path, 'doc', ['body'], mesh, []) is None


def test_cuts_cover_whole_drawn_scope():
    cuts = [{'body_object_id': 'b1', 'plane': 'x0'}, {'body_object_id': 'b1', 'plane': 'y0'}]
    assert model.drawn_fraction({'kind': 'automatic'}, cuts, [{'object_id': 'b1'}]) == .25
    assert model.drawn_fraction({}, cuts, [{'object_id': 'b1'}, {'object_id': 'b2'}]) == 1
    mesh = protocol.default_mesh([])
    assert model.least_solved_estimate(10000, [], mesh, declared_reduced=True)['triangles'] == model.estimate_triangles(10000, [], mesh)['full']


def test_body_revision_visibility_placement_and_parameters_invalidate_learning(monkeypatch, send_module):
    body = types.SimpleNamespace(entityToken='body', revisionId='1', isVisible=True)
    root = types.SimpleNamespace(bRepBodies=[body], allOccurrences=[])
    parameter = types.SimpleNamespace(name='width', expression='1 mm', value=.1)
    design = types.SimpleNamespace(rootComponent=root, allParameters=[parameter])
    first = send_module.mesh_geometry_key(design, [body])
    assert first is not None
    body.revisionId = '2'
    assert send_module.mesh_geometry_key(design, [body]) != first
    body.revisionId = '1'
    body.isVisible = False
    assert send_module.mesh_geometry_key(design, [body]) != first
    body.isVisible = True
    assert send_module.mesh_geometry_key(design, []) != first
    parameter.expression = '2 mm'
    assert send_module.mesh_geometry_key(design, [body]) != first
    body.revisionId = ''
    assert send_module.mesh_geometry_key(design, [body]) is None


def test_matching_evidence_can_reduce_a_drawn_half_further_without_double_reducing(tmp_path):
    mesh = protocol.default_mesh([])
    estimate = {'full': 40000, 'half': 20000, 'quarter': 10000}
    history.remember(tmp_path, 'solve1', 'sha256:one', 'doc', ['body'], mesh, [], estimate, drawn_fraction=.5)
    status(tmp_path, fraction=.25)
    learned = history.learn(tmp_path, 'doc', ['body'], mesh, [])
    assert history.matching_fraction(learned) == .5
    display = model.estimate_display(estimate, declared_reduced=True, learned=learned)
    assert 'About 20,000 triangles' in display['text'] and '–' not in display['text']
    assert learned['calibration']['estimated'] == 20000
