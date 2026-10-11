"""FEM region declarations and the fem-regions-v1 manifest feature."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
ORACLE = ROOT / "tests/fixtures/wgreturn-endpoint-oracle"
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
import wglink_fem as fem  # noqa: E402
import wglink_protocol as protocol  # noqa: E402


def _face(key, *, body_matches=True, area_cm2=1.0):
    return {"key": key, "body_matches": body_matches, "area_cm2": area_cm2}


def _plan(**overrides):
    arguments = {
        "name": "Rear Chamber",
        "mesh_size_mm": 8.0,
        "body_is_solid": True,
        "groups": [
            {"name": "Cone", "kind": "Driver diaphragm (moving)", "mesh_size_mm": 3,
             "drive_role": "lf", "faces": [_face("f1"), _face("f2")]},
            {"name": "Port exit", "kind": "interface", "mesh_size_mm": 4,
             "faces": [_face("f3")]},
        ],
    }
    arguments.update(overrides)
    return fem.plan_region_declaration(**arguments)


def test_plan_stores_region_and_groups():
    plan = _plan()
    assert plan.region_id == "rear-chamber"
    assert [group.group_id for group in plan.groups] == ["cone", "port-exit"]
    assert plan.groups[0].drive_role == "LF"
    assert plan.groups[1].drive_role is None
    stored = fem.read_region_attribute(plan.body_attribute())
    assert stored["medium"] == {
        "sound_speed_m_per_s": 343.0, "density_kg_per_m3": 1.21, "bulk_loss_factor": 0.0,
    }
    assert stored["groups"][0] == {
        "group_id": "cone", "name": "Cone", "kind": "moving", "mesh_size_mm": 3.0,
        "drive_role": "LF", "declared_area_mm2": 200.0,
    }
    assert fem.read_face_attribute(plan.face_attribute(plan.groups[1])) == (
        "rear-chamber", "port-exit",
    )
    assert "2 surface groups on 3 faces" in plan.summary


def test_plan_keeps_region_id_unique_against_other_bodies():
    assert _plan(other_region_ids=["rear-chamber"]).region_id == "rear-chamber-2"
    assert _plan(region_id="kept").region_id == "kept"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"body_is_solid": False}, "closed solid body"),
        ({"name": "  "}, "Give the FEM region a name"),
        ({"mesh_size_mm": 0}, "region mesh size must be greater than zero"),
        ({"groups": [{"name": "", "kind": "rigid", "mesh_size_mm": 1, "faces": [_face(1)]}]},
         "Surface Group 1 needs a name"),
        ({"groups": [{"name": "A", "kind": "rigid", "mesh_size_mm": 1, "faces": [_face(1)]},
                     {"name": "a", "kind": "rigid", "mesh_size_mm": 1, "faces": [_face(2)]}]},
         "used twice"),
        ({"groups": [{"name": "A", "kind": "wobbly", "mesh_size_mm": 1, "faces": [_face(1)]}]},
         "not a surface group kind"),
        ({"groups": [{"name": "A", "kind": "moving", "mesh_size_mm": 1, "faces": [_face(1)]}]},
         "needs a drive role"),
        ({"groups": [{"name": "A", "kind": "rigid", "mesh_size_mm": 1, "faces": []}]},
         "Select at least one face"),
        ({"groups": [{"name": "A", "kind": "rigid", "mesh_size_mm": 1,
                      "faces": [_face(1, body_matches=False)]}]},
         "face of another body"),
        ({"groups": [{"name": "A", "kind": "rigid", "mesh_size_mm": 1, "faces": [_face(1)]},
                     {"name": "B", "kind": "rigid", "mesh_size_mm": 1, "faces": [_face(1)]}]},
         "only one surface group"),
        ({"medium": {"bulk_loss_factor": 2}}, "between 0 and 1"),
    ],
)
def test_plan_refusals(overrides, message):
    with pytest.raises(fem.FemDeclarationError, match=message):
        _plan(**overrides)


def test_attributes_reject_foreign_values():
    assert fem.read_region_attribute("not json") is None
    assert fem.read_region_attribute(json.dumps({"schema": 2, "region_id": "x", "groups": []})) is None
    assert fem.read_face_attribute(json.dumps({"schema": 1, "region_id": "x"})) is None


def test_face_descriptor_converts_to_millimetres_in_export_frame():
    def shift(value, vector=False):
        return list(value) if vector else [value[0] + 1.0, value[1], value[2]]

    descriptor = fem.face_descriptor(
        area_cm2=2.0, centroid_cm=(0.5, 0.0, 1.0), normal=(0.0, 0.0, 2.0),
        bbox_min_cm=(0.0, -1.0, 1.0), bbox_max_cm=(1.0, 1.0, 1.0), to_export_frame=shift,
    )
    assert descriptor == {
        "area_mm2": 200.0,
        "centroid_mm": [15.0, 0.0, 10.0],
        "normal": [0.0, 0.0, 1.0],
        "bbox_mm": [[10.0, -10.0, 10.0], [20.0, 10.0, 10.0]],
    }


def _descriptor(z=0.0):
    return {"area_mm2": 100.0, "centroid_mm": [0.0, 0.0, z], "normal": [0.0, 0.0, 1.0],
            "bbox_mm": [[-5.0, -5.0, z], [5.0, 5.0, z]]}


def _region_records():
    declaration = fem.read_region_attribute(_plan().body_attribute())
    return fem.plan_fem_regions([
        fem.RegionExport(
            object_id="body-1",
            declaration=declaration,
            faces_by_group={"cone": [_descriptor(1.0), _descriptor()], "port-exit": [_descriptor(9.0)]},
        )
    ])


def test_plan_fem_regions_builds_manifest_records():
    (record,) = _region_records()
    assert record["file"] == "fem-regions/rear-chamber.step"
    assert record["n_bodies_expected"] == 1
    cone, port = record["surface_groups"]
    assert cone["drive_channel_id"] == "drive-LF"
    assert "drive_channel_id" not in port
    assert [face["centroid_mm"][2] for face in cone["faces"]] == [0.0, 1.0]
    lines = fem.preflight_lines([record])
    assert lines == ["FEM region: Rear Chamber (8 mm; 1 interface, 1 moving)"]


def test_plan_fem_regions_refuses_lost_faces_and_copies():
    declaration = fem.read_region_attribute(_plan().body_attribute())
    with pytest.raises(fem.FemDeclarationError, match="has no faces left"):
        fem.plan_fem_regions([fem.RegionExport("b", declaration,
                                             {"cone": [_descriptor(), _descriptor()]})])
    faces = {"cone": [_descriptor(), _descriptor()], "port-exit": [_descriptor()]}
    with pytest.raises(fem.FemDeclarationError, match="Two bodies declare"):
        fem.plan_fem_regions([fem.RegionExport("a", declaration, faces),
                              fem.RegionExport("b", declaration, faces)])


@pytest.mark.parametrize("area_cm2", [None, 0, -1, float("nan"), float("inf"), "bad"])
def test_declaration_requires_measurable_face_areas(area_cm2):
    with pytest.raises(fem.FemDeclarationError, match="face area"):
        _plan(groups=[{"name": "Wall", "kind": "rigid", "mesh_size_mm": 1,
                       "faces": [_face("wall", area_cm2=area_cm2)]}])


@pytest.mark.parametrize("current, accepted", [
    (198.0, True), (202.0, True), (199.0, True), (201.0, True),
    (197.999, False), (202.001, False), (100.0, False),
])
def test_group_area_drift_threshold(current, accepted):
    declaration = fem.read_region_attribute(_plan().body_attribute())
    changed = {**_descriptor(), "area_mm2": current}
    export = fem.RegionExport("b", declaration,
                              {"cone": [changed], "port-exit": [_descriptor()]})
    if accepted:
        assert fem.plan_fem_regions([export])[0]["surface_groups"][0]["faces"] == [changed]
    else:
        with pytest.raises(fem.FemDeclarationError, match="'Cone'.*'Rear Chamber'.*more than 1 %"):
            fem.plan_fem_regions([export])


@pytest.mark.parametrize("baseline", [None, 0, -1, float("nan"), float("inf"), "bad"])
def test_old_or_invalid_area_baseline_requires_redeclaration(baseline):
    declaration = fem.read_region_attribute(_plan().body_attribute())
    if baseline is None:
        del declaration["groups"][0]["declared_area_mm2"]
    else:
        declaration["groups"][0]["declared_area_mm2"] = baseline
    export = fem.RegionExport("b", declaration,
                              {"cone": [_descriptor(), _descriptor()], "port-exit": [_descriptor()]})
    with pytest.raises(fem.FemDeclarationError, match="'Cone'.*'Rear Chamber'.*re-declare this region"):
        fem.plan_fem_regions([export])


def test_preflight_warns_without_interface():
    record = _region_records()[0]
    record["surface_groups"] = record["surface_groups"][:1]
    assert "No interface group" in fem.preflight_lines([record])[-1]


# ----------------------------------------------------------- protocol profiles


def _base(name):
    cases = json.loads((ORACLE / name).read_text("utf-8"))
    return copy.deepcopy(next(case["manifest"] for case in cases if case["name"] == "base"))


def _with_regions(manifest, records=None):
    records = _region_records() if records is None else records
    manifest["required_features"].append(protocol.FEM_REGIONS_FEATURE)
    manifest["scope"]["fem_regions"] = records
    for record in records:
        manifest["files"][record["file"]] = {
            "sha256": "sha256:" + "0" * 64, "size_bytes": 10,
            "media_type": "model/step", "purpose": protocol.FEM_REGION_PURPOSE,
        }
    return manifest


PROFILES = [
    ("cases.json", protocol.ADDIN_WRITER),
    ("wg_ingress_cases.json", protocol.WG_INGRESS),
]


@pytest.mark.parametrize(("fixture", "profile"), PROFILES)
def test_both_profiles_accept_fem_regions(fixture, profile):
    protocol.validate_structure(_with_regions(_base(fixture)), profile)


@pytest.mark.parametrize(("fixture", "profile"), PROFILES)
def test_fem_diaphragm_alone_is_a_drivable_return(fixture, profile):
    manifest = _with_regions(_base(fixture))
    manifest["sources"] = []
    protocol.validate_structure(manifest, profile)
    manifest["scope"]["fem_regions"][0]["surface_groups"][0]["kind"] = "rigid"
    del manifest["scope"]["fem_regions"][0]["surface_groups"][0]["drive_channel_id"]
    with pytest.raises(protocol.ProtocolValidationError, match="source"):
        protocol.validate_structure(manifest, profile)


def _mutations():
    def no_feature(m):
        m["required_features"].remove(protocol.FEM_REGIONS_FEATURE)

    def no_key(m):
        del m["scope"]["fem_regions"]

    def empty(m):
        m["scope"]["fem_regions"] = []

    def bad_id(m):
        m["scope"]["fem_regions"][0]["region_id"] = "Rear Chamber"

    def missing_file(m):
        del m["files"]["fem-regions/rear-chamber.step"]

    def extra_file(m):
        m["files"]["fem-regions/other.step"] = dict(m["files"]["fem-regions/rear-chamber.step"])

    def two_bodies(m):
        m["scope"]["fem_regions"][0]["n_bodies_expected"] = 2

    def bad_kind(m):
        m["scope"]["fem_regions"][0]["surface_groups"][1]["kind"] = "impedance"

    def moving_without_channel(m):
        del m["scope"]["fem_regions"][0]["surface_groups"][0]["drive_channel_id"]

    def channel_on_wall(m):
        m["scope"]["fem_regions"][0]["surface_groups"][1]["drive_channel_id"] = "drive-LF"

    def no_faces(m):
        m["scope"]["fem_regions"][0]["surface_groups"][1]["faces"] = []

    def bad_normal(m):
        m["scope"]["fem_regions"][0]["surface_groups"][1]["faces"][0]["normal"] = [0, 0, 2]

    def inverted_bbox(m):
        m["scope"]["fem_regions"][0]["surface_groups"][1]["faces"][0]["bbox_mm"] = [[1, 0, 0], [0, 0, 0]]

    def lossy(m):
        m["scope"]["fem_regions"][0]["medium"]["bulk_loss_factor"] = 1.5

    def forbidden_tag(m):
        m["scope"]["fem_regions"][0]["surface_groups"][1]["tag"] = 12

    return [
        (no_feature, "fem-regions-v1 is required"),
        (no_key, "fem_regions: is required"),
        (empty, "non-empty list"),
        (bad_id, "lower-case letters"),
        (missing_file, "must name a files entry"),
        (extra_file, "agree exactly"),
        (two_bodies, "must be exactly 1"),
        (bad_kind, "must be one of"),
        (moving_without_channel, "required for a moving group"),
        (channel_on_wall, "only on a moving group"),
        (no_faces, "at least one face"),
        (bad_normal, "unit vector"),
        (inverted_bbox, "must not exceed"),
        (lossy, "at most 1.0"),
        (forbidden_tag, "tag"),
    ]


@pytest.mark.parametrize(("fixture", "profile"), PROFILES)
@pytest.mark.parametrize(("mutate", "message"), _mutations(), ids=lambda item: getattr(item, "__name__", None))
def test_both_profiles_refuse_malformed_regions(fixture, profile, mutate, message):
    manifest = _with_regions(_base(fixture))
    mutate(manifest)
    with pytest.raises(protocol.ProtocolValidationError, match=message):
        protocol.validate_structure(manifest, profile)
