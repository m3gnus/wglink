"""FEM region declarations and the fem-regions-v1 manifest feature."""

from __future__ import annotations

import copy
import json
import math
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
        "declared_face_count": 2, "declared_face_ids": list(plan.groups[0].face_ids),
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


def _export(object_id, declaration, faces_by_group):
    ids = {group["group_id"]: group["declared_face_ids"][:len(faces_by_group.get(group["group_id"], ()))]
           for group in declaration["groups"]}
    return fem.RegionExport(object_id, declaration, faces_by_group, ids)


def _region_records():
    declaration = fem.read_region_attribute(_plan().body_attribute())
    return fem.plan_fem_regions([
        _export(
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
    assert cone["drive_channel_id"] == "drive-lf"
    assert "drive_channel_id" not in port
    assert [face["centroid_mm"][2] for face in cone["faces"]] == [0.0, 1.0]
    lines = fem.preflight_lines([record])
    assert lines == ["FEM region: Rear Chamber (8 mm; 1 interface, 1 moving)"]


def test_plan_fem_regions_refuses_lost_faces_and_copies():
    declaration = fem.read_region_attribute(_plan().body_attribute())
    with pytest.raises(fem.FemDeclarationError, match="has no faces left"):
        fem.plan_fem_regions([_export("b", declaration,
                                             {"cone": [_descriptor(), _descriptor()]})])
    faces = {"cone": [_descriptor(), _descriptor()], "port-exit": [_descriptor()]}
    with pytest.raises(fem.FemDeclarationError, match="Two bodies declare"):
        fem.plan_fem_regions([_export("a", declaration, faces),
                              _export("b", declaration, faces)])


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
    changed = {**_descriptor(), "area_mm2": current / 2}
    export = _export("b", declaration,
                              {"cone": [changed, changed], "port-exit": [_descriptor()]})
    if accepted:
        assert fem.plan_fem_regions([export])[0]["surface_groups"][0]["faces"] == [changed, changed]
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
    export = _export("b", declaration,
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
        m["scope"]["fem_regions"][0]["surface_groups"][1]["drive_channel_id"] = "drive-lf"

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


@pytest.mark.parametrize(("fixture", "profile"), PROFILES)
@pytest.mark.parametrize("count", [True, 1.0, "1", None])
def test_region_body_count_requires_an_integer(fixture, profile, count):
    manifest = _with_regions(_base(fixture))
    manifest["scope"]["fem_regions"][0]["n_bodies_expected"] = count
    with pytest.raises(protocol.ProtocolValidationError, match="n_bodies_expected.*exactly 1"):
        protocol.validate_structure(manifest, profile)


@pytest.mark.parametrize(("fixture", "profile"), PROFILES)
@pytest.mark.parametrize("record", ["region", "group", "face", "medium"])
def test_fem_records_refuse_unknown_members(fixture, profile, record):
    manifest = _with_regions(_base(fixture))
    region = manifest["scope"]["fem_regions"][0]
    group = region["surface_groups"][0]
    target = {"region": region, "group": group, "face": group["faces"][0], "medium": region["medium"]}[record]
    target["unexpected"] = 1
    with pytest.raises(protocol.ProtocolValidationError, match="unknown member.*unexpected"):
        protocol.validate_structure(manifest, profile)


@pytest.mark.parametrize(("fixture", "profile"), PROFILES)
@pytest.mark.parametrize("channel", ["drive-LF", "drive-lf ", "lf", "drive-", "drive-lf/2", "drive-lf--2"])
def test_fem_drive_id_uses_exterior_pattern(fixture, profile, channel):
    manifest = _with_regions(_base(fixture))
    manifest["scope"]["fem_regions"][0]["surface_groups"][0]["drive_channel_id"] = channel
    with pytest.raises(protocol.ProtocolValidationError, match="exterior source drive-id pattern"):
        protocol.validate_structure(manifest, profile)


def test_shared_source_allocator_keeps_exterior_collision_suffixes():
    used = set()
    assert fem.wglink_author.source_ids("LF", used) == ("source-lf", "drive-lf")
    assert fem.wglink_author.source_ids("LF", used) == ("source-lf-2", "drive-lf-2")
    assert fem.drive_channel_for_role("LF") == "drive-lf"


LINING = {"model": "miki", "thickness_mm": 30.0, "flow_resistivity_pa_s_per_m2": 5000.0}


@pytest.mark.parametrize(("fixture", "profile"), PROFILES)
@pytest.mark.parametrize("level", [0, -6.0, -120, math.nextafter(-120, math.inf)])
def test_optional_boundary_physics_validate_in_both_profiles(fixture, profile, level):
    manifest = _with_regions(_base(fixture))
    moving, wall = manifest["scope"]["fem_regions"][0]["surface_groups"]
    moving["relative_level_db"] = level
    wall["kind"] = "rigid"
    wall["lining"] = dict(LINING)
    protocol.validate_structure(manifest, profile)


BAD_OPTIONS = [
    ("rigid", "lining", value) for value in (None, True, 30, "miki", [])
] + [
    ("rigid", "lining", {**LINING, key: value})
    for key in ("thickness_mm", "flow_resistivity_pa_s_per_m2")
    for value in (None, True, "30", 0, -1, float("nan"), float("inf"), float("-inf"))
] + [
    ("rigid", "lining", {**LINING, "model": value})
    for value in (None, True, 1, "other")
] + [
    ("rigid", "lining", {key: value for key, value in LINING.items() if key != missing})
    for missing in LINING
] + [
    ("rigid", "lining", {**LINING, "extra": 1}),
    ("moving", "lining", LINING),
    ("interface", "lining", LINING),
    ("plane-wave-termination", "lining", LINING),
] + [
    ("moving", "relative_level_db", value)
    for value in (None, True, "-6", [], 0.001, math.nextafter(-120, -math.inf), -6500,
                  float("nan"), float("inf"), float("-inf"))
] + [
    (kind, "relative_level_db", -6)
    for kind in ("rigid", "interface", "plane-wave-termination")
]


@pytest.mark.parametrize(("fixture", "profile"), PROFILES)
@pytest.mark.parametrize(("kind", "key", "value"), BAD_OPTIONS)
def test_optional_boundary_physics_refuse_invalid_values(fixture, profile, kind, key, value):
    manifest = _with_regions(_base(fixture))
    group = manifest["scope"]["fem_regions"][0]["surface_groups"][0]
    group["kind"] = kind
    if kind != "moving":
        del group["drive_channel_id"]
    group[key] = value
    with pytest.raises(protocol.ProtocolValidationError, match=key):
        protocol.validate_structure(manifest, profile)


@pytest.mark.parametrize(("kind", "key", "value"), BAD_OPTIONS)
def test_planner_and_attribute_reader_refuse_invalid_boundary_options(kind, key, value):
    raw = {"name": "Boundary", "kind": kind, "mesh_size_mm": 3, "drive_role": "LF",
           "faces": [_face("f1")], key: value}
    with pytest.raises(fem.FemDeclarationError) as refusal:
        _plan(groups=[raw])
    message = str(refusal.value)
    assert (key in message
            or (kind == "moving" and key == "relative_level_db"
                and message == "Relative level for 'Boundary' must be between −120 and 0 dB.")
            or (key == "lining" and message.startswith(("Lining thickness", "Flow resistivity"))))
    raw.pop(key)
    stored = json.loads(_plan(groups=[raw]).body_attribute())
    stored["groups"][0][key] = value
    assert fem.read_region_attribute(json.dumps(stored)) is None
    with pytest.raises(fem.FemDeclarationError, match=key):
        fem.plan_fem_regions([_export("b", stored, {"boundary": [_descriptor()]})])


def test_boundary_options_persist_and_export_with_old_defaults_omitted():
    groups = [
        {"name": "Cone", "kind": "moving", "drive_role": "LF", "mesh_size_mm": 3,
         "faces": [_face("f1")], "relative_level_db": -6.0},
        {"name": "Wall", "kind": "rigid", "mesh_size_mm": 3,
         "faces": [_face("f2")], "lining": dict(LINING)},
    ]
    stored = fem.read_region_attribute(_plan(groups=groups).body_attribute())
    record = fem.plan_fem_regions([_export("b", stored, {
        "cone": [_descriptor()], "wall": [_descriptor(2)],
    })])[0]
    assert record["surface_groups"][0]["relative_level_db"] == -6.0
    assert record["surface_groups"][1]["lining"] == LINING
    assert stored["groups"][0]["relative_level_db"] == -6.0
    assert stored["groups"][1]["lining"] == LINING
    groups[0]["relative_level_db"] = 0
    del groups[1]["lining"]
    defaults = fem.read_region_attribute(_plan(groups=groups).body_attribute())
    assert "relative_level_db" not in defaults["groups"][0]
    assert "lining" not in defaults["groups"][1]
