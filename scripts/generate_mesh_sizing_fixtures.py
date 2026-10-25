#!/usr/bin/env python3
"""Build synthetic shared mesh-sizing-v1 returns; never read retained CAD data."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys

import gmsh

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'fusion-addins/WGLink'))
import wglink_protocol as protocol
from wglink_return import build_return_manifest, dumps_return_manifest

OUT = ROOT / 'tests/fixtures/mesh-sizing-v1'


def _styles(text: str, labels: dict[str, list[int]]) -> str:
    """Attach ordinary STEP presentation styles to the generated face ids."""
    next_id = max(int(n) for n in re.findall(r'#(\d+)\s*=', text)) + 1
    records = []
    for label, faces in labels.items():
        for face in faces:
            n = next_id
            records.extend([
                f"#{n} = COLOUR_RGB('{label}',0.3,0.5,0.7);",
                f"#{n+1} = FILL_AREA_STYLE_COLOUR('',#{n});",
                f"#{n+2} = FILL_AREA_STYLE('',(#{n+1}));",
                f"#{n+3} = SURFACE_STYLE_FILL_AREA(#{n+2});",
                f"#{n+4} = SURFACE_SIDE_STYLE('',(#{n+3}));",
                f"#{n+5} = SURFACE_STYLE_USAGE(.BOTH.,#{n+4});",
                f"#{n+6} = PRESENTATION_STYLE_ASSIGNMENT((#{n+5}));",
                f"#{n+7} = STYLED_ITEM('',(#{n+6}),#{face});",
            ])
            next_id += 8
    return text.replace('ENDSEC;\nEND-ISO-10303-21;', '\n'.join(records) + '\nENDSEC;\nEND-ISO-10303-21;')


def generate(name: str, *, quarter: bool) -> None:
    folder = OUT / f'{name}.wgreturn'
    folder.mkdir(parents=True, exist_ok=True)
    if quarter:
        origin, dimensions = (0, 0, 0), (240, 190, 780)
        patches = {
            'LF': [(20, 20, 60, 45), (20, 90, 60, 45)],
            'MF': [(110, 20, 35, 25), (110, 90, 35, 25)],
            'HF': [(170, 20, 16, 12)],
            'PASSIVE_CARDIOID': [(180, 90, 30, 20), (180, 140, 30, 20)],
        }
    else:
        origin, dimensions = (-250, -143.5, 0), (500, 287, 275)
        patches = {'HF': [(-8, -6, 16, 12)]}
    gmsh.clear()
    volume = gmsh.model.occ.addBox(*origin, *dimensions)
    tools = []
    for rectangles in patches.values():
        for x, y, width, height in rectangles:
            tools.append((2, gmsh.model.occ.addRectangle(x, y, dimensions[2], width, height)))
    gmsh.model.occ.fragment([(3, volume)], tools)
    gmsh.model.occ.synchronize()
    path = folder / 'assembly.step'
    gmsh.write(str(path))
    text = path.read_text()
    # Stable, anonymous header. All shape coordinates below are generated here.
    text = re.sub(r"'\d{4}-\d\d-\d\dT[^']*'", "'2026-01-01T00:00:00'", text)
    text = text.replace("('Author')", "('')")
    text = '\n'.join(line.rstrip() for line in text.splitlines()) + '\n'
    path.write_text(text)
    gmsh.clear()
    gmsh.model.occ.importShapes(str(path))
    gmsh.model.occ.synchronize()
    # The STEP importer enumerates ADVANCED_FACEs in file order, as WG does.
    face_ids = [int(n) for n in re.findall(r'#(\d+) = ADVANCED_FACE\(', text)]
    surfaces = gmsh.model.getEntities(2)
    assert len(surfaces) == len(face_ids)
    labelled: dict[str, list[int]] = {}
    areas: dict[str, list[float]] = {}
    for (_, tag), face_id in zip(surfaces, face_ids):
        x, y, z = gmsh.model.occ.getCenterOfMass(2, tag)
        label = 'WGMESH_back' if abs(z) < 1e-6 else None
        if abs(z - dimensions[2]) < 1e-6:
            for role, rectangles in patches.items():
                if any(abs(x-(px+w/2)) < 1e-6 and abs(y-(py+h/2)) < 1e-6
                       and abs(gmsh.model.occ.getMass(2, tag)-w*h) < 1e-6
                       for px,py,w,h in rectangles):
                    label = f'WGSRC_{role.lower()}'
        if label:
            labelled.setdefault(label, []).append(face_id)
            areas.setdefault(label, []).append(round(gmsh.model.occ.getMass(2, tag), 6))
    path.write_text(_styles(text, labelled))
    def evidence(label):
        return {'face_count': len(areas[label]), 'total_area_mm2': sum(areas[label]),
                'per_face_area_mm2': areas[label], 'bodies': ['Synthetic enclosure']}
    sources = []
    for role in patches:
        label = f'WGSRC_{role.lower()}'
        count = len(areas[label])
        sources.append({
            'id': f'f{2 if quarter else 1}-{role.lower()}', 'role': role, 'instance_id': None,
            'required': True, 'default_drive_channel_id': f'drive-{role.lower()}',
            'patch_policy': 'single-connected' if count == 1 else 'explicit-disconnected',
            'expected_connected_components': count,
            'selectors': {'appearance_labels': [label]}, 'observed': evidence(label),
            'suggested_resolution_mm': protocol.MESH_DEFAULTS['source_size_mm'][role],
        })
    mesh = protocol.default_mesh(sources)
    mesh['surface_groups'] = [{'group_id': 'back', 'name': 'Back', 'size_mm': protocol.MESH_PRESETS['Coarse'],
                               'selectors': {'appearance_labels': ['WGMESH_back']},
                               'observed': evidence('WGMESH_back')}]
    mesh['settings_origin'] = 'user'
    domain = {'kind': 'quarter', 'cut_planes': ['x0', 'y0'], 'declared_by': 'cad-author',
              'evidence': {plane: {'min_mm': 0, 'max_mm': extent, 'tolerance_mm': 0.01}
                           for plane, extent in zip(('x0', 'y0'), dimensions)}} if quarter else {'kind': 'automatic'}
    features = [*protocol.BASE_RETURN_FEATURES, protocol.SOURCE_IDENTITY_FEATURE, protocol.DOCUMENT_UP_FEATURE]
    if not quarter:
        features.append(protocol.DOMAIN_AUTOMATIC_FEATURE)
    data = path.read_bytes()
    manifest = build_return_manifest(
        return_record={'id': f'wgr_01J0000000000000000000000{2 if quarter else 1}', 'created_at': '2026-01-01T00:00:00Z'},
        generator={'adapter': 'synthetic-fixture', 'adapter_version': '0.3.3', 'cad_app': 'fusion360', 'cad_version': 'synthetic'},
        document={'name': 'Synthetic quarter enclosure' if quarter else 'Synthetic single-source enclosure', 'native_id': None},
        coordinate_system={'length_unit': 'mm', 'handedness': 'right', 'matrix_convention': 'row-major-local-to-parent',
                           'export_frame': 'root-component', 'document_up': '+z'},
        assembly={'file': 'assembly.step', 'n_bodies_expected': 1, 'bbox_mm': [list(origin), [a+b for a,b in zip(origin,dimensions)]], 'signature_hash': 'sha256:'+hashlib.sha256(data).hexdigest(), 'domain': domain},
        files={'assembly.step': {'sha256': 'sha256:'+hashlib.sha256(data).hexdigest(), 'size_bytes': len(data), 'media_type': 'model/step', 'purpose': 'exterior-assembly'}},
        scope={'selection': 'root', 'included': [{'object_id': f'f{2 if quarter else 1}-body', 'name': 'Synthetic enclosure', 'component': 'Fixture', 'body_kind': 'solid', 'visible': True, 'external_reference': 'none'}],
               'skipped': [], 'fem_air_volumes': [], 'status': 'clean'},
        instances=[], sources=sources, mesh=mesh, required_features=features,
    )
    protocol.validate_structure(manifest, protocol.WG_INGRESS)
    (folder / 'wgreturn.json').write_text(dumps_return_manifest(manifest))


def main():
    gmsh.initialize()
    gmsh.option.setNumber('General.Terminal', 0)
    try:
        generate('f1-single-source', quarter=False)
        generate('f2-quarter-multi-source', quarter=True)
    finally:
        gmsh.finalize()


if __name__ == '__main__':
    main()
