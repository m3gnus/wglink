"""Shared mesh contract, producer serialization, and frozen-reader compatibility."""
from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'fusion-addins/WGLink'))
import wglink_protocol as protocol
from wglink_return import build_return_manifest, dumps_return_manifest, loads_return_manifest

FIXTURES = ROOT / 'tests/fixtures/mesh-sizing-v1'
BUNDLES = sorted(FIXTURES.glob('*.wgreturn'))
PROFILES = (protocol.ADDIN_WRITER, protocol.WG_INGRESS)


@pytest.fixture(params=PROFILES)
def profile(request):
    return request.param


@pytest.fixture
def manifest():
    return json.loads((BUNDLES[0] / 'wgreturn.json').read_text())


@pytest.mark.parametrize('bundle', BUNDLES, ids=lambda p: p.stem)
def test_shared_returns_validate_and_round_trip(bundle, profile):
    raw = (bundle / 'wgreturn.json').read_text()
    manifest = loads_return_manifest(raw)
    before = deepcopy(manifest)
    protocol.validate_structure(manifest, profile)
    assert manifest == before
    assert dumps_return_manifest(manifest) == raw
    assert manifest['wgreturn_version'] == '1.1'
    for name, record in manifest['files'].items():
        data = (bundle / name).read_bytes()
        assert record['size_bytes'] == len(data)
        assert record['sha256'] == 'sha256:' + hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize('field', ['wall_size_mm', 'grading_distance_mm', 'symmetry', 'source_size_mm', 'surface_groups', 'settings_origin'])
def test_all_mesh_fields_required(manifest, profile, field):
    del manifest['mesh'][field]
    with pytest.raises(protocol.ProtocolValidationError):
        protocol.validate_structure(manifest, profile)


@pytest.mark.parametrize('field', ['wall_size_mm', 'grading_distance_mm', 'source_size_mm', 'size_mm'])
@pytest.mark.parametrize('value', [0, -1, True, None, '25', float('nan'), float('inf'), -float('inf'), 10**400])
def test_mesh_sizes_positive_and_finite(manifest, profile, field, value):
    if field == 'source_size_mm':
        manifest['mesh'][field][manifest['sources'][0]['id']] = value
    elif field == 'size_mm':
        manifest['mesh']['surface_groups'][0][field] = value
    else:
        manifest['mesh'][field] = value
    with pytest.raises(protocol.ProtocolValidationError):
        protocol.validate_structure(manifest, profile)


@pytest.mark.parametrize('target,value', [
    ('mesh', None), ('mesh', []), ('source_size_mm', []), ('surface_groups', {}),
    ('symmetry', 'full'), ('symmetry', True), ('settings_origin', 'wg'),
])
def test_mesh_types_and_enums(manifest, profile, target, value):
    if target == 'mesh':
        manifest[target] = value
    else:
        manifest['mesh'][target] = value
    with pytest.raises(protocol.ProtocolValidationError):
        protocol.validate_structure(manifest, profile)


@pytest.mark.parametrize('symmetry', ['auto', 'off'])
@pytest.mark.parametrize('origin', ['defaults', 'user'])
def test_valid_enums_do_not_change_domain(manifest, profile, symmetry, origin):
    manifest['mesh'].update(symmetry=symmetry, settings_origin=origin, surface_groups=[])
    before = deepcopy(manifest['assembly']['domain'])
    protocol.validate_structure(manifest, profile)
    assert manifest['assembly']['domain'] == before


@pytest.mark.parametrize('missing_feature,missing_mesh,accepted', [
    (False, False, True), (True, True, True), (True, False, False), (False, True, False),
])
def test_mesh_and_feature_are_present_together(manifest, profile, missing_feature, missing_mesh, accepted):
    if missing_feature:
        manifest['required_features'].remove(protocol.MESH_SIZING_FEATURE)
    if missing_mesh:
        del manifest['mesh']
    if accepted:
        protocol.validate_structure(manifest, profile)
    else:
        with pytest.raises(protocol.ProtocolValidationError, match='required exactly when'):
            protocol.validate_structure(manifest, profile)


@pytest.mark.parametrize('change', ['missing', 'extra', 'different', 'no-suggestion'])
def test_source_map_is_complete_and_matches_suggestions(manifest, profile, change):
    sizes = manifest['mesh']['source_size_mm']
    source_id = manifest['sources'][0]['id']
    if change == 'missing':
        del sizes[source_id]
    elif change == 'extra':
        sizes['absent-source'] = 15
    elif change == 'different':
        sizes[source_id] += 1
    else:
        del manifest['sources'][0]['suggested_resolution_mm']
    with pytest.raises(protocol.ProtocolValidationError):
        protocol.validate_structure(manifest, profile)


@pytest.mark.parametrize('change', [
    'missing-id', 'empty-id', 'duplicate-id', 'missing-name', 'missing-size',
    'empty-selectors', 'wrong-label', 'duplicate-label', 'source-collision', 'extra-selector',
    'no-observed', 'zero-faces', 'boolean-faces', 'wrong-area-count', 'negative-area', 'infinite-total',
])
def test_invalid_surface_groups(manifest, profile, change):
    group = manifest['mesh']['surface_groups'][0]
    if change.startswith('missing-'):
        del group[{'missing-id': 'group_id', 'missing-name': 'name', 'missing-size': 'size_mm'}[change]]
    elif change == 'empty-id':
        group['group_id'] = ''
    elif change == 'duplicate-id':
        manifest['mesh']['surface_groups'].append(deepcopy(group))
    elif change == 'empty-selectors':
        group['selectors'] = {}
    elif change == 'wrong-label':
        group['selectors']['appearance_labels'] = ['WGMESH_wrong']
    elif change == 'duplicate-label':
        group['selectors']['appearance_labels'] *= 2
    elif change == 'source-collision':
        manifest['sources'][0]['selectors']['appearance_labels'] = group['selectors']['appearance_labels'][:]
    elif change == 'extra-selector':
        group['selectors']['shell_names'] = ['Back']
    elif change == 'no-observed':
        del group['observed']
    elif change == 'zero-faces':
        group['observed']['face_count'] = 0
    elif change == 'boolean-faces':
        group['observed']['face_count'] = True
    elif change == 'wrong-area-count':
        group['observed']['per_face_area_mm2'] = []
    elif change == 'negative-area':
        group['observed']['per_face_area_mm2'][0] = -1
    elif change == 'infinite-total':
        group['observed']['total_area_mm2'] = float('inf')
    with pytest.raises(protocol.ProtocolValidationError):
        protocol.validate_structure(manifest, profile)


def test_group_observations_allow_the_documented_shape_without_bodies(manifest, profile):
    del manifest['mesh']['surface_groups'][0]['observed']['bodies']
    protocol.validate_structure(manifest, profile)


@pytest.mark.parametrize('location', ['root', 'source', 'group'])
def test_symmetry_exception_only_applies_to_mesh(manifest, profile, location):
    target = manifest if location == 'root' else manifest['sources'][0] if location == 'source' else manifest['mesh']['surface_groups'][0]
    target['symmetry'] = 'auto'
    with pytest.raises(protocol.ProtocolValidationError, match='verdict'):
        protocol.validate_structure(manifest, profile)


def test_builder_attaches_feature_detaches_mesh_and_preserves_suggestions(manifest):
    mesh = manifest.pop('mesh')
    features = manifest.pop('required_features')
    features.remove(protocol.MESH_SIZING_FEATURE)
    manifest['return_record'] = manifest.pop('return')
    built = build_return_manifest(**manifest, required_features=features, mesh=mesh)
    mesh['wall_size_mm'] = 99
    assert built['mesh']['wall_size_mm'] == 25
    assert built['required_features'].count(protocol.MESH_SIZING_FEATURE) == 1
    assert loads_return_manifest(dumps_return_manifest(built)) == built


def test_defaults_and_presets_have_one_owner():
    assert protocol.MESH_DEFAULTS == {
        'wall_size_mm': 25, 'grading_distance_mm': 100, 'symmetry': 'auto',
        'source_size_mm': {'HF': 6, 'MF': 15, 'LF': 25, 'PASSIVE_CARDIOID': 25},
    }
    assert protocol.MESH_PRESETS == {'Fine': 15, 'Coarse': 40}
    sources = [{'id': 's', 'role': 'HF', 'suggested_resolution_mm': 4}, {'id': 'port', 'role': 'PORT_EXIT'}]
    mesh = protocol.default_mesh(sources)
    assert mesh['source_size_mm'] == {'s': 4, 'port': 25}
    assert mesh['surface_groups'] == [] and mesh['settings_origin'] == 'defaults'
    mesh['source_size_mm']['s'] = 40
    assert protocol.default_mesh(sources)['source_size_mm']['s'] == 4
    assert protocol.MESH_DEFAULTS['source_size_mm']['HF'] == 6


@pytest.fixture(scope='module')
def frozen():
    raw = gzip.decompress((FIXTURES / 'pre-w1-protocol.py.gz').read_bytes())
    provenance = json.loads((FIXTURES / 'PROVENANCE.json').read_text())
    assert hashlib.sha256(raw).hexdigest() == provenance['frozen_protocol_sha256']
    module = types.ModuleType('pre_w1_protocol')
    exec(compile(raw, 'pre-w1-protocol.py', 'exec'), module.__dict__)
    return module


def test_frozen_reader_refuses_unknown_required_feature(manifest, frozen):
    # The old reader also forbids mesh.symmetry as verdict data, before its
    # feature check. Isolate the feature negotiation to pin its exact message.
    del manifest['mesh']
    with pytest.raises(frozen.ProtocolValidationError, match='unknown required feature\\(s\\): mesh-sizing-v1'):
        frozen.validate_structure(manifest, frozen.WG_INGRESS)


def test_frozen_reader_refuses_complete_new_return(manifest, frozen):
    with pytest.raises(frozen.ProtocolValidationError) as caught:
        frozen.validate_structure(manifest, frozen.WG_INGRESS)
    assert str(caught.value) == '$.mesh.symmetry: CAD-authored verdict keys are forbidden'


@pytest.mark.parametrize('path', sorted((ROOT / 'tests/fixtures/no-fem-e9f0dfb').glob('**/wgreturn.json')))
def test_legacy_returns_keep_the_same_verdict_and_serialization(path, frozen, profile):
    raw = path.read_text()
    manifest = json.loads(raw)
    before = deepcopy(manifest)
    frozen.validate_structure(manifest, profile)
    protocol.validate_structure(manifest, profile)
    assert manifest == before and 'mesh' not in manifest
    assert dumps_return_manifest(manifest) == raw
    assert manifest['sources'][0]['suggested_resolution_mm'] == 4


@pytest.mark.parametrize('bundle', BUNDLES, ids=lambda p: p.stem)
def test_synthetic_step_selectors_resolve_to_the_recorded_faces(bundle):
    # Isolate gmsh's process-global model from the rest of the add-in tests.
    import subprocess
    code = r'''
import json, sys
from pathlib import Path
import gmsh
from hornlab_mesher.step_text import advanced_face_order, parse_styled_face_groups
folder = Path(sys.argv[1])
m = json.loads((folder / 'wgreturn.json').read_text())
step = folder / 'assembly.step'
labels = parse_styled_face_groups(step)
order = advanced_face_order(step)
gmsh.initialize()
gmsh.option.setNumber('General.Terminal', 0)
try:
    gmsh.model.occ.importShapes(str(step))
    gmsh.model.occ.synchronize()
    assert len(gmsh.model.getEntities(3)) == m['assembly']['n_bodies_expected']
    surfaces = gmsh.model.getEntities(2)
    assert len(order) == len(surfaces)
    face_tags = dict(zip(order, (tag for _, tag in surfaces)))
    for record in [*m['sources'], *m['mesh']['surface_groups']]:
        label, = record['selectors']['appearance_labels']
        tags = [face_tags[face] for face in labels[label]]
        observed = record['observed']
        assert len(tags) == observed['face_count']
        areas = sorted(gmsh.model.occ.getMass(2, tag) for tag in tags)
        assert all(abs(a-b) < 1e-5 for a,b in zip(areas, sorted(observed['per_face_area_mm2'])))
        assert abs(sum(areas)-observed['total_area_mm2']) < 1e-5
finally:
    gmsh.finalize()
'''
    result = subprocess.run([sys.executable, '-c', code, str(bundle)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('source_id', ['symmetry', 'tag', 'freshness', 'config'])
def test_source_size_map_keys_are_source_ids_not_verdict_fields(manifest, profile, source_id):
    old = manifest['sources'][0]['id']
    manifest['sources'][0]['id'] = source_id
    manifest['mesh']['source_size_mm'][source_id] = manifest['mesh']['source_size_mm'].pop(old)
    protocol.validate_structure(manifest, profile)


def test_source_shell_labels_cannot_collide_with_group_paint(manifest, profile):
    manifest['sources'][0]['selectors'] = {'shell_names': ['WGMESH_back']}
    with pytest.raises(protocol.ProtocolValidationError, match='collide with source labels'):
        protocol.validate_structure(manifest, profile)


def test_shared_fixture_shapes():
    f1, f2 = [json.loads((bundle / 'wgreturn.json').read_text()) for bundle in BUNDLES]
    assert len(f1['scope']['included']) == f1['assembly']['n_bodies_expected'] == 1
    assert f1['assembly']['bbox_mm'] == [[-250, -143.5, 0], [250, 143.5, 275]]
    assert f1['assembly']['domain'] == {'kind': 'automatic'}
    assert [(s['role'], s['observed']['face_count']) for s in f1['sources']] == [('HF', 1)]
    assert f1['sources'][0]['patch_policy'] == 'single-connected'
    assert f1['sources'][0]['expected_connected_components'] == 1
    assert f1['sources'][0]['observed']['total_area_mm2'] == 192
    assert f2['assembly']['domain']['kind'] == 'quarter'
    assert f2['assembly']['domain']['cut_planes'] == ['x0', 'y0']
    assert [(s['role'], s['observed']['face_count']) for s in f2['sources']] == [
        ('LF', 2), ('MF', 2), ('HF', 1), ('PASSIVE_CARDIOID', 2)]
