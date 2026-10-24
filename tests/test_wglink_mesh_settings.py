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


class FailingMeshAttributes(Attributes):
    """Fail one mesh-settings read while retaining all document attributes."""

    def __init__(self, original, stage):
        self.values = original.values
        self.stage = stage
        self.failures = 1

    def itemByName(self, group, name):
        if name != mesh_model.SETTINGS_ATTRIBUTE:
            return super().itemByName(group, name)
        if self.stage == 'lookup' and self.failures:
            self.failures -= 1
            raise RuntimeError('Injected mesh-settings lookup failure')
        attribute = super().itemByName(group, name)
        if self.stage != 'value' or attribute is None or not self.failures:
            return attribute
        owner = self

        class Attribute:
            @property
            def value(self):
                owner.failures -= 1
                raise RuntimeError('Injected mesh-settings value failure')

        return Attribute()


@pytest.mark.parametrize('stage', ['lookup', 'value'])
def test_failed_mesh_settings_read_refuses_and_retries_without_writing(send_module, stage):
    attributes = Attributes()
    design = types.SimpleNamespace(rootComponent=types.SimpleNamespace(attributes=attributes))
    send_module.save_mesh_settings(design, fixture_mesh())
    before = dict(attributes.values)
    design.rootComponent.attributes = FailingMeshAttributes(attributes, stage)
    with pytest.raises(send_module.wglink_core.WgLinkError, match='Could not read the saved mesh settings'):
        send_module.read_mesh_settings(design)
    assert attributes.values == before
    assert send_module.read_mesh_settings(design)['surface_groups'][0]['size_mm'] == 40


def test_unavailable_attribute_collection_is_not_absent_settings(send_module):
    design = types.SimpleNamespace(rootComponent=types.SimpleNamespace(attributes=None))
    with pytest.raises(send_module.wglink_core.WgLinkError, match='Could not read the saved mesh settings'):
        send_module.read_mesh_settings(design)
    design.rootComponent.attributes = Attributes()
    assert send_module.read_mesh_settings(design) is None


class AppearanceFace:
    """Fusion returns the effective appearance, including inherited body paint."""
    def __init__(self, inherited):
        self.__dict__.update(vars(face('body paint', area=100)))
        self.attributes = Attributes()
        self.inherited = inherited
        self.override = None

    @property
    def appearance(self):
        return self.override if self.override is not None else self.inherited

    @appearance.setter
    def appearance(self, value):
        self.override = value

    @property
    def appearanceSourceType(self):
        return 4 if self.override is not None else 1


class OccurrenceFace:
    def __init__(self, native, occurrence):
        self.nativeObject = native
        self.assemblyContext = occurrence
        self.entityToken = f'{occurrence.entityToken}/face'

    @property
    def appearance(self):
        return self.nativeObject.appearance

    @property
    def area(self):
        return self.nativeObject.area


def _asset(design, asset, name=None):
    # A mocked Fusion resolver must also put its copy into the document library.
    if name is not None:
        asset.name = name
    if asset not in design.appearances:
        design.appearances.append(asset)
    asset.deleteMe = lambda: design.appearances.remove(asset) is None
    return asset


def document(monkeypatch, send_module):
    source = face('HF')
    back = AppearanceFace(types.SimpleNamespace(name='body paint', id='grey'))
    exterior = body('Cabinet', faces=[source, back])
    back.body = exterior
    root = component('Mesh test', [exterior])
    root.attributes = Attributes()
    design = types.SimpleNamespace(rootComponent=root, exportManager=ContractExportManager(),
                                   findAttributes=lambda *_a: Collection())
    design.appearances = Collection([back.inherited])
    design.appearances.itemById = lambda key: next((a for a in design.appearances if a.id == key), None)
    monkeypatch.setattr(send_module.adsk.core, 'AppearanceSourceTypes', types.SimpleNamespace(
        OverrideAppearanceSource=4, FaceAppearanceSource=3), raising=False)
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


@pytest.mark.parametrize('role,instance_id', [('HF', None), ('PORT_EXIT', None), ('MF', 'linked')])
def test_partial_cad_sources_get_selectors_at_the_shared_mesh_boundary(role, instance_id):
    source = {'id': 'kept-source', 'role': role, 'instance_id': instance_id,
              'suggested_resolution_mm': protocol.default_source_size_mm(role),
              'observed': {'total_area_mm2': 100}}
    snapshot = mesh_model.snapshot([source])
    completed = mesh_model.normalize_source(source)
    assert completed['selectors']['appearance_labels'] == [role]
    if instance_id:
        assert completed['selectors']['linked_throat'] == {'instance_id': instance_id}
    assert 'selectors' not in source
    assert set(snapshot['source_size_mm']) == {'kept-source'}
    assert mesh_model.estimate_triangles(1000, [source], snapshot)['triangles'] > 0


def test_source_normalization_preserves_existing_selectors_and_live_entities():
    class LiveFace:
        def __deepcopy__(self, _memo):
            pytest.fail('Fusion entities must not be deep-copied')
    member = LiveFace()
    source = {'id': 'legacy', 'role': 'PASSIVE_CARDIOID', 'faces': [member],
              'selectors': {'appearance_labels': ['PORT_EXIT'], 'advanced_face_indices': [2]}}
    result = mesh_model.normalize_source(source)
    assert result == source and result['faces'][0] is member
    result['selectors']['appearance_labels'].append('HF')
    assert source['selectors']['appearance_labels'] == ['PORT_EXIT']
    with pytest.raises(mesh_model.MeshSettingsError, match='selectors'):
        mesh_model.normalize_source({**source, 'selectors': None})
    with pytest.raises(mesh_model.MeshSettingsError, match='role'):
        mesh_model.snapshot([{'id': 'unresolved', 'role': 'unknown'}])


def test_partial_saved_group_is_completed_once_for_all_document_readers(send_module):
    design = types.SimpleNamespace(rootComponent=types.SimpleNamespace(attributes=Attributes()))
    value = {'schema': 1, **fixture_mesh()}
    del value['surface_groups'][0]['selectors']
    design.rootComponent.attributes.add('WGLink', 'mesh_settings', json.dumps(value))
    saved = send_module.read_mesh_settings(design)
    group = saved['surface_groups'][0]
    assert group['selectors'] == {'appearance_labels': [f"WGMESH_{group['group_id']}"]}
    assert len(saved['surface_groups']) == len(value['surface_groups'])
    assert saved['source_size_mm'] == value['source_size_mm']
    # Reading is non-mutating; Save writes the complete shape.
    assert json.loads(design.rootComponent.attributes.values[('WGLink', 'mesh_settings')]) == value
    send_module.save_mesh_settings(design, saved)
    assert send_module.read_mesh_settings(design) == saved


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
    back.appearance = types.SimpleNamespace(name='WGMESH_back', id='WGMESH_back')
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
    appearance = types.SimpleNamespace(name='WGMESH_back', id='WGMESH_back')
    lookups = []
    monkeypatch.setattr(send_module.wglink_core, '_named_appearance', lambda _a, _d, name: lookups.append(name) or _asset(design, appearance, name))
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
    assert back.override is None and back.appearance is back.inherited
    assert source.appearance.name == 'HF'


def test_proxy_group_paint_and_geometric_source_refusal(monkeypatch, send_module):
    app, design, _, _, back = document(monkeypatch, send_module)
    proxy = types.SimpleNamespace(nativeObject=back)
    with pytest.raises(send_module.wglink_core.WgLinkError, match='both a source'):
        send_module.paint_surface_group(app, design, [proxy], 'back', source_faces=[back])
    monkeypatch.setattr(send_module.wglink_core, '_named_appearance', lambda *_a: _asset(design, types.SimpleNamespace(name='WGMESH_back', id='WGMESH_back')))
    send_module.paint_surface_group(app, design, [proxy], 'back')
    assert back.appearance.name == 'WGMESH_back'
    assert not hasattr(proxy, 'appearance')


@pytest.mark.parametrize('override', [False, True])
def test_group_clear_restores_exact_override_after_reopen(monkeypatch, send_module, override):
    app, design, _, _, back = document(monkeypatch, send_module)
    red = types.SimpleNamespace(name='red', id='custom-red')
    design.appearances.append(red)
    if override:
        back.appearance = red
    cyan = types.SimpleNamespace(name='WGMESH_back', id='WGMESH_back')
    monkeypatch.setattr(send_module.wglink_core, '_named_appearance', lambda _a, _d, name: _asset(design, cyan, name))
    send_module.paint_surface_group(app, design, [back], 'back')
    saved = dict(back.attributes.values)
    assert back.appearance is cyan
    assert json.loads(saved[('WGLink', send_module.GROUP_ORIGINAL_APPEARANCE_ATTRIBUTE)]) == {
        'schema': 1, 'id': 'custom-red' if override else None,
    }
    # Repeated assignment must not replace the original with managed cyan.
    send_module.paint_surface_group(app, design, [back], 'back')
    assert back.attributes.values == saved
    reopened = AppearanceFace(types.SimpleNamespace(name='new body paint', id='new-grey'))
    reopened.appearance = cyan
    reopened.attributes.values = saved
    # A fresh appearance wrapper, with a renamed display label and the same id.
    restored_red = types.SimpleNamespace(name='renamed custom red', id='custom-red')
    design.appearances[design.appearances.index(red)] = restored_red
    send_module.paint_surface_group(app, design, [reopened], None)
    assert reopened.override is (restored_red if override else None)
    assert reopened.appearance is (restored_red if override else reopened.inherited)
    assert not reopened.attributes.values
    # Leaving one group and joining another captures the restored original.
    send_module.paint_surface_group(app, design, [reopened], 'other')
    send_module.paint_surface_group(app, design, [reopened], None)
    assert reopened.override is (restored_red if override else None)


def test_group_rejoin_after_manual_repaint_captures_the_new_override(monkeypatch, send_module):
    app, design, _, _, back = document(monkeypatch, send_module)
    cyan = types.SimpleNamespace(name='WGMESH_back', id='WGMESH_back')
    monkeypatch.setattr(send_module.wglink_core, '_named_appearance', lambda _a, _d, name: _asset(design, cyan, name))
    send_module.paint_surface_group(app, design, [back], 'back')
    red = types.SimpleNamespace(name='red', id='red')
    design.appearances.append(red)
    back.appearance = red  # Manual paint has left the group; the old attribute remains.
    send_module.paint_surface_group(app, design, [back], 'other')
    send_module.paint_surface_group(app, design, [back], None)
    assert back.appearance is red
    assert not back.attributes.values


@pytest.mark.parametrize('failure', ['unreadable-original', 'attribute-write', 'missing-asset', 'bad-record'])
def test_group_original_failures_keep_current_paint(monkeypatch, send_module, failure):
    app, design, _, _, back = document(monkeypatch, send_module)
    red = types.SimpleNamespace(name='red', id='red')
    design.appearances.append(red)
    back.appearance = red
    cyan = types.SimpleNamespace(name='WGMESH_back', id='WGMESH_back')
    monkeypatch.setattr(send_module.wglink_core, '_named_appearance', lambda _a, _d, name: _asset(design, cyan, name))
    if failure == 'unreadable-original':
        red.id = ''
    elif failure == 'attribute-write':
        back.attributes.add = lambda *_a: None  # Fusion may refuse without raising.
    if failure in ('unreadable-original', 'attribute-write'):
        with pytest.raises(send_module.wglink_core.WgLinkError,
                           match='original appearance of a selected face|Could not paint the managed appearance') as caught:
            send_module.paint_surface_group(app, design, [back], 'back')
        assert 'mesh_group_original_appearance' not in str(caught.value)  # no internals
        assert back.appearance is red
        return
    send_module.paint_surface_group(app, design, [back], 'back')
    if failure == 'missing-asset':
        design.appearances.remove(red)
    else:
        back.attributes.add('WGLink', send_module.GROUP_ORIGINAL_APPEARANCE_ATTRIBUTE, '{')
    saved = dict(back.attributes.values)
    with pytest.raises(send_module.wglink_core.WgLinkError, match='original appearance'):
        send_module.paint_surface_group(app, design, [back], None)
    assert back.appearance is cyan and back.attributes.values == saved


def repeated_component(design, back):
    comp = component('Repeated cabinet', [back.body])
    occurrences = [types.SimpleNamespace(component=comp, entityToken=f'parent:{i}/cabinet:1')
                   for i in range(2)]
    design.rootComponent.allOccurrences = Collection(occurrences)
    return [OccurrenceFace(back, occ) for occ in occurrences]


@pytest.mark.parametrize('selection', ['face', 'body', 'native', 'duplicate', 'different-faces'])
def test_group_subset_of_repeated_component_refuses_before_writes(monkeypatch, send_module, selection):
    app, design, _, _, back = document(monkeypatch, send_module)
    first, second = repeated_component(design, back)
    selected = [first]
    if selection == 'body':
        selected = [types.SimpleNamespace(faces=Collection([first]), assemblyContext=first.assemblyContext)]
    elif selection == 'native':
        selected = [back]
    elif selection == 'duplicate':
        selected = [first, OccurrenceFace(back, first.assemblyContext)]
    elif selection == 'different-faces':
        other = AppearanceFace(back.inherited)
        other.body = back.body
        selected.append(OccurrenceFace(other, second.assemblyContext))
    calls = []
    monkeypatch.setattr(send_module.wglink_core, '_named_appearance', lambda *_a: calls.append(1))
    with pytest.raises(send_module.wglink_core.WgLinkError) as exc:
        send_module.paint_surface_group(app, design, selected, 'back')
    assert str(exc.value) == ('This face belongs to a component used 2 times; '
                              'make it unique (Make Independent) or select all instances')
    assert first.appearance is second.appearance is back.inherited
    assert not back.attributes.values and not calls


@pytest.mark.parametrize('all_instances', [False, True])
def test_group_unique_or_explicit_all_instances_paints_measures_and_clears(monkeypatch, send_module, all_instances):
    app, design, _, _, back = document(monkeypatch, send_module)
    proxies = repeated_component(design, back)
    if not all_instances:
        # Make Independent leaves only one occurrence referencing this component.
        design.rootComponent.allOccurrences.pop()
        proxies = proxies[:1]
    cyan = types.SimpleNamespace(name='WGMESH_back', id='WGMESH_back')
    monkeypatch.setattr(send_module.wglink_core, '_named_appearance', lambda _a, _d, name: _asset(design, cyan, name))
    selections = [types.SimpleNamespace(faces=Collection([p]), assemblyContext=p.assemblyContext)
                  for p in proxies]
    send_module.paint_surface_group(app, design, selections, 'back')
    assert all(p.appearance is cyan for p in proxies)
    send_module.save_mesh_settings(design, fixture_mesh())
    exported = [types.SimpleNamespace(name=f'cabinet:{i}', faces=Collection([p]))
                for i, p in enumerate(proxies)]
    snapshot = send_module._mesh_snapshot(design, [], exported, set())
    observed = snapshot['surface_groups'][0]['observed']
    assert observed['face_count'] == len(proxies)
    assert observed['total_area_mm2'] == len(proxies) * 10000
    if all_instances:
        with pytest.raises(send_module.wglink_core.WgLinkError, match='used 2 times'):
            send_module.paint_surface_group(app, design, proxies[:1], None)
        assert all(p.appearance is cyan for p in proxies)
    send_module.paint_surface_group(app, design, selections, None)
    assert back.override is None and not back.attributes.values


@pytest.mark.parametrize('kind', ['empty', 'unknown', 'linked-source'])
def test_group_errors_refuse_before_step_export(monkeypatch, tmp_path, send_module, kind):
    app, design, exterior, _, back = document(monkeypatch, send_module)
    settings = fixture_mesh()
    if kind != 'empty':
        back.appearance = types.SimpleNamespace(name='WGMESH_back', id='WGMESH_back')
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
    minted = types.SimpleNamespace(name='WGMESH_back', id='WGMESH_back')
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
    design = types.SimpleNamespace(rootComponent=types.SimpleNamespace(attributes=Attributes(), allOccurrences=[]))
    first = module._geometry_change_key(design, {})
    module.wglink_send.save_mesh_settings(design, fixture_mesh())
    second = module._geometry_change_key(design, {})
    assert first != second
    member = AppearanceFace(types.SimpleNamespace(name='body paint', id='grey'))
    member.appearance = types.SimpleNamespace(name='WGMESH_back', id='back')
    monkeypatch.setattr(module.adsk.core, 'AppearanceSourceTypes', types.SimpleNamespace(
        OverrideAppearanceSource=4, FaceAppearanceSource=3), raising=False)
    module.wglink_send.paint_surface_group(None, design, [member], None)
    assert second != module._geometry_change_key(design, {})
