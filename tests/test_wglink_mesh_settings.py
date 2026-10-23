"""Schema-1 document mesh storage, live paint, and the single Send emitter."""
import json
from pathlib import Path
import types
import sys

import pytest

from test_wglink_send import (
    Attributes, Collection, ContractExportManager, body, component, face, send_module,
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fusion-addins/WGLink"))
import wglink_mesh as mesh_model
import wglink_protocol as protocol

FIXTURES = Path(__file__).parent / 'fixtures/mesh-sizing-v1'


def fixture_mesh():
    return json.loads((FIXTURES / 'f1-single-source.wgreturn/wgreturn.json').read_text())['mesh']


def document(monkeypatch, send_module):
    source = face('HF')
    back = face('body paint', area=100)
    exterior = body('Cabinet', faces=[source, back])
    root = component('Mesh test', [exterior])
    root.attributes = Attributes()
    design = types.SimpleNamespace(rootComponent=root, exportManager=ContractExportManager(),
                                   findAttributes=lambda *_a: Collection())
    app = types.SimpleNamespace(version='2704.1.53', activeDocument=types.SimpleNamespace(name='Mesh test'))
    monkeypatch.setattr(send_module.wglink_core, '_design', lambda _app: design)
    return app, design, exterior, source, back


def test_attribute_round_trip_reopens_every_setting(send_module):
    design = types.SimpleNamespace(rootComponent=types.SimpleNamespace(attributes=Attributes()))
    value = fixture_mesh()
    value.update(wall_size_mm=32, grading_distance_mm=80, symmetry='off')
    value['source_size_mm']['source-hf'] = 4
    send_module.save_mesh_settings(design, value)
    # New Fusion/Python wrappers over the same document attribute collection.
    reopened = types.SimpleNamespace(rootComponent=types.SimpleNamespace(attributes=design.rootComponent.attributes))
    restored = send_module.read_mesh_settings(reopened)
    assert restored == {'schema': 1, **value, 'settings_origin': 'user'}
    assert set(design.rootComponent.attributes.values) == {('WGLink', 'mesh_settings')}
    restored['surface_groups'][0]['size_mm'] = 99
    assert send_module.read_mesh_settings(reopened)['surface_groups'][0]['size_mm'] == 40


@pytest.mark.parametrize('text', ['', '{', '[]', '{"schema":true}', '{"schema":2}'])
def test_bad_or_future_settings_refuse_instead_of_defaulting(send_module, text):
    design = types.SimpleNamespace(rootComponent=types.SimpleNamespace(attributes=Attributes()))
    design.rootComponent.attributes.add('WGLink', 'mesh_settings', text)
    with pytest.raises(send_module.wglink_core.WgLinkError, match='mesh settings|Mesh settings'):
        send_module.read_mesh_settings(design)


def test_defaults_and_source_id_reconciliation():
    sources = [{'id':'old', 'role':'HF', 'selectors':{}, 'suggested_resolution_mm':4},
               {'id':'new', 'role':'MF', 'selectors':{}}]
    defaults = mesh_model.snapshot(sources)
    assert defaults == {**protocol.default_mesh(sources), 'settings_origin':'defaults'}
    stored = mesh_model.loads_settings(mesh_model.dumps_settings({**defaults, 'source_size_mm':{'old':8, 'removed':30}}))
    result = mesh_model.snapshot(sources, stored)
    assert result['source_size_mm'] == {'old':8, 'new':15}
    assert stored['source_size_mm'] == {'old':8, 'removed':30}


@pytest.mark.parametrize('field,value', [('wall_size_mm', 0), ('grading_distance_mm', True),
                                        ('symmetry', 'full'), ('source_size_mm', {'s':float('inf')})])
def test_store_uses_protocol_validation(field, value):
    settings = fixture_mesh()
    settings[field] = value
    with pytest.raises(mesh_model.MeshSettingsError):
        mesh_model.dumps_settings(settings)


def test_send_defaults_without_creating_an_attribute(monkeypatch, tmp_path, send_module):
    app, design, *_ = document(monkeypatch, send_module)
    report = send_module.send(app, {'output_folder':str(tmp_path)})
    manifest = json.loads((Path(report['bundle_path'])/'wgreturn.json').read_text())
    assert manifest['mesh'] == protocol.default_mesh(manifest['sources'])
    assert protocol.MESH_SIZING_FEATURE in manifest['required_features']
    assert send_module.read_mesh_settings(design) is None
    protocol.validate_structure(manifest, protocol.WG_INGRESS)


def test_send_stored_snapshot_refreshes_paint_and_fingerprint(monkeypatch, tmp_path, send_module):
    app, design, exterior, _, back = document(monkeypatch, send_module)
    baseline = send_module.return_state(app)
    assert baseline['hash']
    settings = fixture_mesh()
    settings.update(wall_size_mm=30, grading_distance_mm=75, symmetry='off', source_size_mm={'source-hf':8})
    back.appearance = types.SimpleNamespace(name='WGMESH_back')
    send_module.save_mesh_settings(design, settings)
    first = send_module.return_state(app)
    assert first['hash'] != baseline['hash']
    assert first['state']['mesh']['surface_groups'][0]['observed']['total_area_mm2'] == 10000
    # The emitted evidence is live, not the old fixture/saved group's 143500.
    back.area = 120
    report = send_module.send(app, {'output_folder':str(tmp_path)})
    manifest = json.loads((Path(report['bundle_path'])/'wgreturn.json').read_text())
    snapshot = manifest['mesh']
    assert snapshot['settings_origin'] == 'user'
    assert snapshot['wall_size_mm'] == 30 and snapshot['grading_distance_mm'] == 75
    assert snapshot['symmetry'] == 'off' and snapshot['source_size_mm'] == {'source-hf':8}
    assert manifest['sources'][0]['suggested_resolution_mm'] == 8
    assert snapshot['surface_groups'][0]['observed']['total_area_mm2'] == 12000
    assert snapshot['surface_groups'][0]['observed']['bodies'] == ['Cabinet']
    assert manifest['assembly']['signature_hash'] != first['hash']
    protocol.validate_structure(manifest, protocol.ADDIN_WRITER)
    protocol.validate_structure(manifest, protocol.WG_INGRESS)
    # Size-only edits invalidate optimistic concurrency too.
    settings['wall_size_mm'] = 31
    send_module.save_mesh_settings(design, settings)
    assert send_module.return_state(app)['hash'] != manifest['assembly']['signature_hash']


def test_group_paint_reuses_library_writer_and_clear_restores_body(monkeypatch, send_module):
    app, design, exterior, source, back = document(monkeypatch, send_module)
    appearance = types.SimpleNamespace(name='WGMESH_back')
    lookups = []
    monkeypatch.setattr(send_module.wglink_core, '_named_appearance', lambda _a, _d, name: lookups.append(name) or appearance)
    selected_body = types.SimpleNamespace(faces=Collection([back]))
    send_module.paint_surface_group(app, design, [selected_body, back], 'back')
    assert back.appearance is appearance and lookups == ['WGMESH_back']
    with pytest.raises(send_module.wglink_core.WgLinkError, match='two surface groups'):
        send_module.paint_surface_group(app, design, [back], 'other')
    # Validation precedes every write, including mixed body/face selections.
    with pytest.raises(send_module.wglink_core.WgLinkError, match='both a source'):
        send_module.paint_surface_group(app, design, [exterior], 'back')
    assert source.appearance.name == 'HF'
    send_module.paint_surface_group(app, design, [back, source], None)
    assert back.appearance is None and source.appearance.name == 'HF'


def test_proxy_group_paint_and_geometric_source_refusal(monkeypatch, send_module):
    app, design, _, _, back = document(monkeypatch, send_module)
    proxy = types.SimpleNamespace(nativeObject=back)
    with pytest.raises(send_module.wglink_core.WgLinkError, match='both a source'):
        send_module.paint_surface_group(app, design, [proxy], 'back', source_faces=[back])
    monkeypatch.setattr(send_module.wglink_core, '_named_appearance', lambda *_a: types.SimpleNamespace(name='WGMESH_back'))
    send_module.paint_surface_group(app, design, [proxy], 'back')
    assert back.appearance.name == 'WGMESH_back'
    assert not hasattr(proxy, 'appearance')


@pytest.mark.parametrize('kind', ['empty', 'unknown', 'linked-source'])
def test_group_errors_refuse_before_step_export(monkeypatch, tmp_path, send_module, kind):
    app, design, exterior, _, back = document(monkeypatch, send_module)
    settings = fixture_mesh()
    if kind != 'empty':
        back.appearance = types.SimpleNamespace(name='WGMESH_back')
    if kind == 'unknown':
        settings['surface_groups'] = []
    send_module.save_mesh_settings(design, settings)
    if kind == 'linked-source':
        # Exercise membership check without manufacturing a linked payload.
        with pytest.raises(send_module.wglink_core.WgLinkError, match='both a source'):
            send_module._mesh_snapshot(design, send_module._sources([], [exterior]), [exterior], {send_module._face_key(back)})
        return
    calls = []
    monkeypatch.setattr(send_module, '_export_step', lambda *_a: calls.append(1))
    with pytest.raises(send_module.wglink_core.WgLinkError, match='no faces|no settings'):
        send_module.send(app, {'output_folder':str(tmp_path)})
    assert calls == [] and not list(tmp_path.glob('*.wgreturn'))


G2_CASES = json.loads((FIXTURES / 'estimate-g2.json').read_text())['cases']


@pytest.mark.parametrize('case', G2_CASES,
                         ids=lambda c: f"{c['fixture']}-{c['mode']}-groups-{c['groups']}")
def test_estimate_within_forty_percent_of_g2(case):
    # Geometry and counts from G2's own synthetic F1/F2 test helper, gmsh
    # 4.15.2. G2 uses face indices internally; the estimate consumes the same
    # measured areas with the producer's managed-paint selector spelling.
    sources = case['sources']
    mesh = protocol.default_mesh(sources)
    mesh['surface_groups'] = [{**group, 'selectors':{'appearance_labels':[f"WGMESH_{group['group_id']}"]}}
                              for group in case['surface_groups']]
    estimate = mesh_model.estimate_triangles(case['area_mm2'], sources, mesh,
                                           solved_fraction=case['solved_fraction'],
                                           curvature_regions=case['curvature_regions'])
    assert estimate['approximate'] is True
    assert estimate['triangles'] == pytest.approx(case['triangles'], rel=.4)


def test_uniform_estimate_matches_mesher_formula_and_grading_adds_triangles():
    from hornlab_mesher import mesh_sizing
    mesh = protocol.default_mesh([])
    assert mesh_model.estimate_triangles(10000, [], mesh)['triangles'] == mesh_sizing.estimate_triangle_count([(10000,25)])
    manifest = json.loads((FIXTURES / 'f1-single-source.wgreturn/wgreturn.json').read_text())
    mesh = manifest['mesh']
    uniform = mesh_sizing.estimate_triangle_count([
        (719850-143500-192,25), (143500,40), (192,6),
    ])
    estimate = mesh_model.estimate_triangles(719850, manifest['sources'], mesh, solved_fraction=.25)
    assert estimate['drawn_triangles'] > uniform
    mesh['symmetry'] = 'off'
    assert mesh_model.estimate_triangles(719850, manifest['sources'], mesh, solved_fraction=.25)['triangles'] == estimate['drawn_triangles']


def test_curvature_uses_chord_deviation_ceiling_and_rejects_bad_evidence():
    mesh = protocol.default_mesh([])
    curved = [{'area_mm2':10000, 'radius_mm':10}]
    estimate = mesh_model.estimate_triangles(10000, [], mesh, curvature_regions=curved)
    assert estimate['triangles'] == round(2.3 * 10000 / (8*10*.15))
    assert estimate['curvature_aware'] is True
    assert mesh_model.estimate_triangles(10000, [], mesh)['curvature_aware'] is False
    with pytest.raises(mesh_model.MeshSettingsError, match='within the wall area'):
        mesh_model.estimate_triangles(9999, [], mesh, curvature_regions=curved)


def test_group_appearance_fallback_has_visible_colour(monkeypatch, send_module):
    core = send_module.wglink_core
    colour = types.SimpleNamespace(value=None)
    minted = types.SimpleNamespace(name='WGMESH_back')
    appearances = Collection()
    appearances.addByCopy = lambda _source, name: appearances.append(minted) or minted
    library = types.SimpleNamespace(name='Materials', appearances=Collection([types.SimpleNamespace(name='Steel - Satin')]))
    app = types.SimpleNamespace(materialLibraries=Collection([library]))
    design = types.SimpleNamespace(appearances=appearances)
    monkeypatch.setattr(core, '_colour_property', lambda _appearance: colour)
    monkeypatch.setattr(core.adsk.core, 'Color', types.SimpleNamespace(create=lambda *rgba: rgba), raising=False)
    assert core._named_appearance(app, design, 'WGMESH_back') is minted
    assert colour.value == (40,190,210,255)
    colour.value = 'user colour'
    assert core._named_appearance(app, design, 'WGMESH_back') is minted
    assert colour.value == 'user colour'  # Never mutate existing appearance assets.


def test_mesh_writes_change_the_cheap_heartbeat_key(monkeypatch, tmp_path):
    from test_wglink_activation_boundary import _boundary
    fixture = _boundary(monkeypatch, tmp_path, 'WGLink_mesh_key', coordination=False)
    module = fixture.module
    monkeypatch.setattr(module, '_manifest_feature_enabled', lambda *_a: False)
    monkeypatch.setattr(module, '_source_identity_enabled', lambda: False)
    design = types.SimpleNamespace(rootComponent=types.SimpleNamespace(attributes=Attributes()))
    first = module._geometry_change_key(design, {})
    module.wglink_send.save_mesh_settings(design, fixture_mesh())
    second = module._geometry_change_key(design, {})
    assert first != second
    member = face('WGMESH_back')
    module.wglink_send.paint_surface_group(None, design, [member], None)
    assert second != module._geometry_change_key(design, {})
