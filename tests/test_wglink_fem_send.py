"""Declared FEM regions through the Fusion boundary: attributes and Send."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json
import sys
import types

import pytest

from test_wglink_addin_lifecycle import _proxying_root
from test_wglink_send import (  # noqa: F401 - send_module is a fixture
    Attributes,
    Collection,
    ContractExportManager,
    body,
    box,
    component,
    face,
    point,
    send_module,
    _linked_cut_design,
)


def fem_face(*, z=0.0, area=4.0, plane=True):
    fem_face.serial = getattr(fem_face, "serial", 0) + 1
    value = types.SimpleNamespace(
        entityToken=f"face-{fem_face.serial}",
        area=area,
        appearance=types.SimpleNamespace(name="Steel"),
        edges=Collection(),
        attributes=Attributes(),
        centroid=point(0.0, 0.0, z),
        boundingBox=box((-1.0, -1.0, z), (1.0, 1.0, z)),
        geometry=types.SimpleNamespace(
            objectType="adsk::core::Plane" if plane else "adsk::core::Cylinder",
            normal=point(0.0, 0.0, 1.0),
        ),
        isParamReversed=False,
    )
    return value


class Occurrences(Collection):
    """Root occurrences that can host WGLink's temporary export component."""

    def addNewComponent(self, _transform):
        occurrences = self

        class _Occurrence(types.SimpleNamespace):
            def deleteMe(self):
                occurrences.remove(self)
                return True

        occurrence = _Occurrence(
            component=component("WGLink FEM export"),
            isVisible=True,
            name="WGLink FEM export:1",
        )
        occurrence.component.objectType = "adsk::fusion::Component"
        self.append(occurrence)
        return occurrence


def air_body(*, visible=False):
    cone, port, wall = fem_face(z=0.0), fem_face(z=2.0), fem_face(z=5.0, plane=False)
    value = body("air", visible=visible, faces=[cone, port, wall])
    value.isLightBulbOn = visible

    class _Copy(types.SimpleNamespace):
        # Fusion's isVisible follows the light bulb (and the parents, here shown).
        @property
        def isVisible(self):
            return self.isLightBulbOn

    def copy_to(occurrence):
        copy = _Copy(
            name="air copy", isSolid=True, isLightBulbOn=False,
            faces=deepcopy(value.faces), attributes=deepcopy(value.attributes),
            boundingBox=value.boundingBox, volume=value.volume,
            entityToken="token-air-copy", objectType=value.objectType,
            parentComponent=occurrence.component,
        )
        occurrence.component.bRepBodies.append(copy)
        return copy

    value.copyToComponent = copy_to
    return value, cone, port


def _declare(send_module, air, cone, port, fem, *, name="Rear chamber", role="LF"):
    plan = fem.plan_region_declaration(
        name=name,
        mesh_size_mm=8,
        body_is_solid=True,
        groups=[
            {"name": "Cone", "kind": "moving", "drive_role": role, "mesh_size_mm": 3,
             "faces": [{"key": cone.entityToken, "body_matches": True, "area_cm2": cone.area}]},
            {"name": "Port", "kind": "interface", "mesh_size_mm": 4,
             "faces": [{"key": port.entityToken, "body_matches": True, "area_cm2": port.area}]},
        ],
    )
    send_module.declare_fem_region(air, plan, {"cone": [cone], "port": [port]})
    return plan


def _design(send_module, monkeypatch, root):
    root.occurrences = Occurrences(root.occurrences)
    manager = ContractExportManager()
    design = types.SimpleNamespace(
        rootComponent=root,
        exportManager=manager,
        findAttributes=lambda _group, _name: Collection(),
    )
    app = types.SimpleNamespace(version="2704.1.53", activeDocument=types.SimpleNamespace(name=root.name))
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)
    send_module.adsk.core.Matrix3D = types.SimpleNamespace(create=lambda: object())
    return design, app


def _fem(send_module):
    return sys.modules["wglink_fem"]


def test_declaration_round_trips_and_clears(send_module):
    fem = _fem(send_module)
    air, cone, port = air_body()
    plan = _declare(send_module, air, cone, port, fem)
    assert send_module.read_fem_region(air)["region_id"] == plan.region_id
    grouped = send_module.fem_region_faces(air, plan.region_id)
    assert grouped == {"cone": [cone], "port": [port]}

    # Reselecting the cone face alone drops the port face's old group.
    replan = fem.plan_region_declaration(
        name="Rear chamber", mesh_size_mm=8, body_is_solid=True,
        region_id=plan.region_id,
        groups=[{"name": "Cone", "kind": "moving", "drive_role": "LF", "mesh_size_mm": 3,
                 "faces": [{"key": cone.entityToken, "body_matches": True, "area_cm2": cone.area}]}],
    )
    send_module.declare_fem_region(air, replan, {"cone": [cone]})
    assert send_module.fem_region_faces(air, plan.region_id) == {"cone": [cone]}

    assert send_module.clear_fem_region(air) is True
    assert send_module.read_fem_region(air) is None
    assert send_module.fem_region_faces(air, plan.region_id) == {}
    assert send_module.clear_fem_region(air) is False


def test_send_exports_a_hidden_region_on_its_own(send_module, tmp_path, monkeypatch):
    fem = _fem(send_module)
    air, cone, port = air_body()
    _declare(send_module, air, cone, port, fem)
    root = component("Speaker", [body("cabinet", faces=[face("LF")]), air])
    _design(send_module, monkeypatch, root)

    report = send_module.send(
        types.SimpleNamespace(version="1"),
        {"output_folder": str(tmp_path), "capture_document": False, "fem_regions": True},
    )

    bundle = Path(report["bundle_path"])
    manifest = json.loads((bundle / "wgreturn.json").read_text("utf-8"))
    assert "fem-regions-v1" in manifest["required_features"]
    assert manifest["assembly"]["n_bodies_expected"] == 1
    assert [item["name"] for item in manifest["scope"]["included"]] == ["cabinet"]
    (region,) = manifest["scope"]["fem_regions"]
    assert region["file"] == "fem-regions/rear-chamber.step"
    assert manifest["files"][region["file"]]["purpose"] == "fem-region"
    assert send_module.count_step_bodies(bundle / region["file"]) == 1
    cone_group, port_group = region["surface_groups"]
    assert cone_group["drive_channel_id"] == "drive-lf"
    assert manifest["sources"][0]["default_drive_channel_id"] == cone_group["drive_channel_id"]
    assert cone_group["faces"] == [{
        "area_mm2": 400.0,
        "centroid_mm": [0.0, 0.0, 0.0],
        "normal": [0.0, 0.0, 1.0],
        "bbox_mm": [[-10.0, -10.0, 0.0], [10.0, 10.0, 0.0]],
    }]
    assert port_group["kind"] == "interface"
    # The temporary export component is gone again.
    assert list(root.occurrences) == []


def test_a_fem_diaphragm_alone_drives_the_return(send_module, tmp_path, monkeypatch):
    fem = _fem(send_module)
    air, cone, port = air_body()
    _declare(send_module, air, cone, port, fem)
    root = component("Driver", [body("phase plug", faces=[face("Steel")]), air])
    _design(send_module, monkeypatch, root)

    report = send_module.send(
        types.SimpleNamespace(version="1"),
        {"output_folder": str(tmp_path), "capture_document": False, "fem_regions": True},
    )
    assert report["sources"] == []


@pytest.mark.parametrize("separate_regions", [False, True],
                         ids=["same-region", "across-regions"])
def test_same_role_fem_moving_groups_refuse_before_export(
    send_module, tmp_path, monkeypatch, separate_regions
):
    fem = _fem(send_module)
    air, cone, port = air_body()
    _declare(send_module, air, cone, port, fem)
    regions = [air]
    if separate_regions:
        second, second_cone, second_port = air_body()
        second.name, second.entityToken = "front air", "token-front-air"
        _declare(send_module, second, second_cone, second_port, fem, name="Front chamber")
        regions.append(second)
    else:
        plan = fem.plan_region_declaration(
            name="Rear chamber", mesh_size_mm=8, body_is_solid=True,
            groups=[
                {"name": name, "kind": "moving", "drive_role": "LF", "mesh_size_mm": 3,
                 "faces": [{"key": item.entityToken, "body_matches": True, "area_cm2": item.area}]}
                for name, item in [("Cone", cone), ("Second cone", port)]
            ],
        )
        send_module.declare_fem_region(air, plan, {"cone": [cone], "second-cone": [port]})
    root = component("Speaker", [body("cabinet", faces=[face("LF")]), *regions])
    design, app = _design(send_module, monkeypatch, root)
    exports = []
    execute = design.exportManager.execute
    def track_export(options):
        exports.append(options)
        return execute(options)
    monkeypatch.setattr(design.exportManager, "execute", track_export)
    with pytest.raises(send_module.wglink_core.WgLinkError) as refusal:
        send_module.send(app, {"output_folder": str(tmp_path), "capture_document": False, "fem_regions": True})
    message = str(refusal.value)
    assert "Drive role 'LF' has more than one FEM moving group" in message
    assert "'Cone' in region 'Rear chamber'" in message
    assert ("'Cone' in region 'Front chamber'" if separate_regions
            else "'Second cone' in region 'Rear chamber'") in message
    assert exports == []
    assert list(tmp_path.iterdir()) == []


def test_fem_role_with_multiple_exterior_channels_refuses_before_export(
    send_module, tmp_path, monkeypatch
):
    # A linked throat and unclaimed exterior paint allocate two HF channels.
    design, app = _linked_cut_design(
        send_module, retained_fraction=1.0, low=(-1, -1, -1), high=(1, 1, 1)
    )
    root = design.rootComponent
    air, cone, port = air_body()
    _declare(send_module, air, cone, port, _fem(send_module), role="HF")
    root.bRepBodies.extend([body("second driver", faces=[face("HF")]), air])
    root.occurrences = Occurrences()
    send_module.adsk.core.Matrix3D = types.SimpleNamespace(create=lambda: object())
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)
    exports = []
    execute = design.exportManager.execute
    def track_export(options):
        exports.append(options)
        return execute(options)
    monkeypatch.setattr(design.exportManager, "execute", track_export)
    with pytest.raises(send_module.wglink_core.WgLinkError) as refusal:
        send_module.send(app, {"output_folder": str(tmp_path), "capture_document": False, "fem_regions": True})
    message = str(refusal.value)
    assert "Drive role 'HF'" in message
    assert "'Cone' in region 'Rear chamber'" in message
    assert "more than one exterior source channel" in message
    assert "'source-hf' (drive-hf)" in message
    assert "'source-hf-2' (drive-hf-2)" in message
    assert exports == []
    assert list(tmp_path.iterdir()) == []


def test_a_visible_region_refuses_before_any_export(send_module, tmp_path, monkeypatch):
    fem = _fem(send_module)
    air, cone, port = air_body(visible=True)
    _declare(send_module, air, cone, port, fem)
    root = component("Speaker", [body("cabinet", faces=[face("LF")]), air])
    _design(send_module, monkeypatch, root)

    with pytest.raises(send_module.wglink_core.WgLinkError, match="Hide the FEM region body 'air'"):
        send_module.send(
            types.SimpleNamespace(version="1"),
            {"output_folder": str(tmp_path), "capture_document": False, "fem_regions": True},
        )
    assert list(tmp_path.iterdir()) == []


def test_a_wg_without_the_capability_refuses_a_design_with_regions(send_module, tmp_path, monkeypatch):
    fem = _fem(send_module)
    air, cone, port = air_body()
    _declare(send_module, air, cone, port, fem)
    root = component("Speaker", [body("cabinet", faces=[face("LF")]), air])
    _design(send_module, monkeypatch, root)

    with pytest.raises(send_module.wglink_core.WgLinkError) as refusal:
        send_module.send(
            types.SimpleNamespace(version="1"),
            {"output_folder": str(tmp_path), "capture_document": False},
        )
    assert str(refusal.value) == fem.UNAVAILABLE_MESSAGE
    assert list(tmp_path.iterdir()) == []


def test_a_group_whose_faces_were_lost_refuses(send_module, tmp_path, monkeypatch):
    fem = _fem(send_module)
    air, cone, port = air_body()
    _declare(send_module, air, cone, port, fem)
    port.attributes = Attributes()  # the face was rebuilt by a later edit
    root = component("Speaker", [body("cabinet", faces=[face("LF")]), air])
    _design(send_module, monkeypatch, root)

    with pytest.raises(send_module.wglink_core.WgLinkError, match="'Port'.*no faces left"):
        send_module.send(
            types.SimpleNamespace(version="1"),
            {"output_folder": str(tmp_path), "capture_document": False, "fem_regions": True},
        )


@pytest.mark.parametrize("edit", ["partial-loss", "shrink", "grow", "legacy"])
def test_group_area_changes_refuse_before_export(send_module, tmp_path, monkeypatch, edit):
    fem = _fem(send_module)
    air, cone, port = air_body()
    extra = fem_face(z=1.0)
    air.faces.append(extra)
    plan = fem.plan_region_declaration(
        name="Rear chamber", mesh_size_mm=8, body_is_solid=True,
        groups=[{"name": "Cone", "kind": "moving", "drive_role": "LF", "mesh_size_mm": 3,
                 "faces": [{"key": item.entityToken, "body_matches": True, "area_cm2": item.area}
                           for item in (cone, extra)]}],
    )
    send_module.declare_fem_region(air, plan, {"cone": [cone, extra]})
    if edit == "partial-loss":
        extra.attributes = Attributes()
    elif edit == "shrink":
        cone.area *= 0.95
    elif edit == "grow":
        cone.area *= 1.05
    else:
        legacy = send_module.read_fem_region(air)
        del legacy["groups"][0]["declared_area_mm2"]
        air.attributes.add("WGLink", "fem_region", json.dumps(legacy))
        assert send_module.read_fem_region(air) is not None
    root = component("Speaker", [body("cabinet", faces=[face("LF")]), air])
    design, app = _design(send_module, monkeypatch, root)
    exports = []
    monkeypatch.setattr(design.exportManager, "execute", lambda options: exports.append(options))
    with pytest.raises(send_module.wglink_core.WgLinkError,
                       match="'Cone'.*'Rear chamber'.*re-declare this region"):
        send_module.send(app, {"output_folder": str(tmp_path), "capture_document": False,
                               "fem_regions": True})
    assert exports == []
    assert list(tmp_path.iterdir()) == []
    report = send_module.preflight_scope(app, {})
    assert "re-declare this region" in report["fem_region_error"]


def test_redeclaration_resets_the_area_baseline(send_module, tmp_path, monkeypatch):
    fem = _fem(send_module)
    air, cone, port = air_body()
    _declare(send_module, air, cone, port, fem)
    cone.area *= 0.5
    _declare(send_module, air, cone, port, fem)
    assert send_module.read_fem_region(air)["groups"][0]["declared_area_mm2"] == 200.0
    root = component("Speaker", [body("cabinet", faces=[face("LF")]), air])
    _, app = _design(send_module, monkeypatch, root)
    report = send_module.send(app, {"output_folder": str(tmp_path), "capture_document": False,
                                   "fem_regions": True})
    assert report["manifest"]["scope"]["fem_regions"][0]["surface_groups"][0]["faces"][0]["area_mm2"] == 200.0


def test_preflight_lists_regions_and_accepts_a_fem_driver(send_module, monkeypatch):
    fem = _fem(send_module)
    air, cone, port = air_body()
    _declare(send_module, air, cone, port, fem)
    root = component("Driver", [body("phase plug", faces=[face("Steel")]), air])
    _design(send_module, monkeypatch, root)

    report = send_module.preflight_scope(types.SimpleNamespace(version="1"), {})
    assert report["fem_regions"] == ["FEM region: Rear chamber (8 mm; 1 interface, 1 moving)"]
    assert report["source_error"] is None
    author = sys.modules["wglink_author"]
    summary = author.preflight_summary(report)
    assert "FEM region: Rear chamber (8 mm; 1 interface, 1 moving)" in summary.lines
    assert summary.warnings == ()


@pytest.mark.parametrize("edit", ["small-loss", "compensating-growth", "same-count-replacement"])
def test_membership_loss_cannot_hide_in_total_area(send_module, tmp_path, monkeypatch, edit):
    fem = _fem(send_module)
    air, cone, port = air_body()
    cone.area, port.area = 9.95, 0.05  # 995 + 5 mm²
    plan = fem.plan_region_declaration(
        name="Rear chamber", mesh_size_mm=8, body_is_solid=True,
        groups=[{"name": "Cone", "kind": "moving", "drive_role": "LF", "mesh_size_mm": 3,
                 "faces": [{"key": item.entityToken, "body_matches": True, "area_cm2": item.area}
                           for item in (cone, port)]}],
    )
    send_module.declare_fem_region(air, plan, {"cone": [cone, port]})
    port.attributes = Attributes()
    if edit == "compensating-growth":
        cone.area = 10.0
    elif edit == "same-count-replacement":
        wall = air.faces[2]
        wall.area = 0.05
        wall.attributes.add("WGLink", fem.FACE_ATTRIBUTE,
                            fem.face_attribute(plan.region_id, "cone", "replacement"))
    root = component("Speaker", [body("cabinet", faces=[face("LF")]), air])
    design, app = _design(send_module, monkeypatch, root)
    exports = []
    monkeypatch.setattr(design.exportManager, "execute", lambda options: exports.append(options))
    with pytest.raises(send_module.wglink_core.WgLinkError, match="'Cone'.*membership changed"):
        send_module.send(app, {"output_folder": str(tmp_path), "capture_document": False, "fem_regions": True})
    assert exports == []
    assert list(tmp_path.iterdir()) == []


def test_region_ungrouped_wall_geometry_changes_return_hash(send_module, monkeypatch):
    air, cone, port = air_body()
    _declare(send_module, air, cone, port, _fem(send_module))
    root = component("Speaker", [body("cabinet", faces=[face("LF")]), air])
    _, app = _design(send_module, monkeypatch, root)
    before = send_module.return_state(app)
    assert before["hash"] is not None
    air.faces[2].boundingBox.maxPoint.z += 1.0
    after = send_module.return_state(app)
    assert before["state"]["fem_regions"] == after["state"]["fem_regions"]
    assert before["hash"] != after["hash"]


@pytest.mark.parametrize("failure", ["copy", "export"])
def test_temporary_component_is_removed_when_export_raises(send_module, tmp_path, monkeypatch, failure):
    air, _, _ = air_body()
    root = component("Speaker", [air])
    design, _ = _design(send_module, monkeypatch, root)
    def fail(*args):
        raise RuntimeError("export failed")
    if failure == "copy":
        air.copyToComponent = fail
    else:
        monkeypatch.setattr(send_module, "_export_step", fail)
    with pytest.raises(RuntimeError, match="export failed"):
        send_module._export_body_step(design, tmp_path / "region.step", air)
    assert list(root.occurrences) == []


@pytest.mark.parametrize("cleanup", ["false", "exception"])
@pytest.mark.parametrize("export_fails", [False, True])
def test_cleanup_failure_refuses_send_and_names_leftover(send_module, tmp_path, monkeypatch, cleanup, export_fails):
    air, cone, port = air_body()
    _declare(send_module, air, cone, port, _fem(send_module))
    root = component("Speaker", [body("cabinet", faces=[face("LF")]), air])
    design, app = _design(send_module, monkeypatch, root)
    original = root.occurrences.addNewComponent
    def add(transform):
        occurrence = original(transform)
        def delete():
            if cleanup == "exception":
                raise RuntimeError("locked")
            return False
        occurrence.deleteMe = delete
        return occurrence
    monkeypatch.setattr(root.occurrences, "addNewComponent", add)
    if export_fails:
        def fail(_occurrence):
            raise RuntimeError("copy failed")
        air.copyToComponent = fail
    with pytest.raises(send_module.wglink_core.WgLinkError, match="temporary FEM export component 'WGLink FEM export:1'"):
        send_module.send(app, {"output_folder": str(tmp_path), "capture_document": False, "fem_regions": True})
    assert len(root.occurrences) == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("visible", [False, True], ids=["hidden-copy", "visible-copy"])
def test_leftover_region_copy_refuses_next_send_before_export(
    send_module, tmp_path, monkeypatch, visible
):
    air, cone, port = air_body()
    _declare(send_module, air, cone, port, _fem(send_module))
    root = component("Speaker", [body("cabinet", faces=[face("LF")]), air])
    design, app = _design(send_module, monkeypatch, root)
    original = root.occurrences.addNewComponent

    def add(transform):
        occurrence = original(transform)
        occurrence.deleteMe = lambda: False
        return occurrence

    monkeypatch.setattr(root.occurrences, "addNewComponent", add)
    options = {"output_folder": str(tmp_path), "capture_document": False, "fem_regions": True}
    with pytest.raises(send_module.wglink_core.WgLinkError, match="Delete the leftover component"):
        send_module.send(app, options)

    (leftover,) = root.occurrences
    (copy,) = leftover.component.bRepBodies
    assert send_module.read_fem_region(copy) == send_module.read_fem_region(air)
    copy.isLightBulbOn = visible
    exports = []
    monkeypatch.setattr(design.exportManager, "execute", lambda options: exports.append(options))

    with pytest.raises(send_module.wglink_core.WgLinkError) as refusal:
        send_module.send(app, options)
    message = str(refusal.value)
    if visible:
        assert "Hide the FEM region body 'air copy'" in message
        assert "would also be exported as exterior geometry" in message
    else:
        assert "Two bodies declare FEM region 'rear-chamber'" in message
        assert "Speaker/air" in message
        assert "WGLink FEM export:1/air copy" in message
        assert "delete the leftover copy" in message.lower()
    assert exports == []
    assert list(tmp_path.iterdir()) == []
    assert list(root.occurrences) == [leftover]


@pytest.mark.parametrize("accept", [True, False])
def test_missing_capability_clear_prompt_continues_or_cancels(send_module, tmp_path, monkeypatch, accept):
    air, cone, port = air_body()
    _declare(send_module, air, cone, port, _fem(send_module))
    root = component("Speaker", [body("cabinet", faces=[face("LF")]), air])
    _, app = _design(send_module, monkeypatch, root)
    prompts = []
    def confirm():
        prompts.append(True)
        return accept
    options = {"output_folder": str(tmp_path), "capture_document": False}
    if accept:
        report = send_module.send(app, options, confirm_clear_fem=confirm)
        assert "fem_regions" not in report["manifest"]["scope"]
        assert air.attributes.values == {}
        assert all(item.attributes.values == {} for item in air.faces)
    else:
        with pytest.raises(send_module.wglink_core.WgLinkError, match="Send cancelled"):
            send_module.send(app, options, confirm_clear_fem=confirm)
        assert send_module.read_fem_region(air) is not None
        assert cone.attributes.values and port.attributes.values
        assert list(tmp_path.iterdir()) == []
    assert prompts == [True]


@pytest.mark.parametrize("attribute", ["not json", "{}", '{"schema":2,"region_id":"future","groups":[]}',
                                       '{"schema":1,"region_id":"bad","groups":[{}]}', "unreadable"])
def test_invalid_region_attributes_refuse_by_body_name(send_module, tmp_path, monkeypatch, attribute):
    air, _, _ = air_body()
    air.attributes.add("WGLink", "fem_region", attribute)
    if attribute == "unreadable":
        class Unreadable:
            @property
            def value(self):
                raise RuntimeError("unreadable")
        air.attributes.itemByName = lambda *args: Unreadable()
    root = component("Speaker", [body("cabinet", faces=[face("LF")]), air])
    _, app = _design(send_module, monkeypatch, root)
    with pytest.raises(send_module.wglink_core.WgLinkError, match="body 'air'.*unreadable, malformed"):
        send_module.send(app, {"output_folder": str(tmp_path), "capture_document": False, "fem_regions": True})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("in_occurrence", [False, True])
def test_fresh_root_wrappers_accept_only_direct_root_regions(
    send_module, tmp_path, monkeypatch, in_occurrence
):
    air, cone, port = air_body()
    _declare(send_module, air, cone, port, _fem(send_module))
    root = component("Speaker", [body("cabinet", faces=[face("LF")])])
    if in_occurrence:
        child = component("Child", [air])
        occurrence = types.SimpleNamespace(component=child, name="Child:1", fullPathName="Child:1", isVisible=True)
        root.occurrences.append(occurrence)
    else:
        root.bRepBodies.append(air)
        air.parentComponent = root
    design, app = _design(send_module, monkeypatch, root)
    design = _proxying_root(design)
    assert design.rootComponent is not design.rootComponent
    monkeypatch.setattr(send_module.wglink_core, "_design", lambda _app: design)
    options = {"output_folder": str(tmp_path), "capture_document": False, "fem_regions": True}
    if in_occurrence:
        with pytest.raises(send_module.wglink_core.WgLinkError, match="body 'air'.*directly to the root component"):
            send_module.send(app, options)
        assert list(tmp_path.iterdir()) == []
    else:
        report = send_module.send(app, options)
        assert report["manifest"]["scope"]["fem_regions"][0]["name"] == "Rear chamber"
        assert list(root.occurrences) == []


@pytest.mark.parametrize("malformed", [False, True])
def test_failed_direct_lookup_still_reads_a_region_via_tolerant_fallback(
    send_module, tmp_path, monkeypatch, malformed
):
    air, cone, port = air_body()
    fem = _fem(send_module)
    _declare(send_module, air, cone, port, fem)
    if malformed:
        air.attributes.add("WGLink", fem.REGION_ATTRIBUTE, "malformed")
    lookup = air.attributes.itemByName
    failed = False
    def unavailable_once(group, name):
        nonlocal failed
        if name == fem.REGION_ATTRIBUTE and not failed:
            failed = True
            raise RuntimeError("attributes unavailable")
        return lookup(group, name)
    air.attributes.itemByName = unavailable_once
    root = component("Speaker", [body("cabinet", faces=[face("LF")]), air])
    _, app = _design(send_module, monkeypatch, root)
    options = {"output_folder": str(tmp_path), "capture_document": False, "fem_regions": True}
    if malformed:
        with pytest.raises(send_module.wglink_core.WgLinkError, match="body 'air'.*unreadable, malformed"):
            send_module.send(app, options)
        assert list(tmp_path.iterdir()) == []
    else:
        report = send_module.send(app, options)
        assert report["manifest"]["scope"]["fem_regions"][0]["name"] == "Rear chamber"
    assert failed


def test_clear_recovery_removes_malformed_body_and_face_attributes(send_module, tmp_path, monkeypatch):
    fem = _fem(send_module)
    air, cone, port = air_body()
    air.attributes.add("WGLink", fem.REGION_ATTRIBUTE, "malformed")
    cone.attributes.add("WGLink", fem.FACE_ATTRIBUTE, "malformed")
    port.attributes.add("WGLink", fem.FACE_ATTRIBUTE, '{"schema":2}')
    root = component("Speaker", [body("cabinet", faces=[face("LF")]), air])
    _, app = _design(send_module, monkeypatch, root)
    report = send_module.send(app, {"output_folder": str(tmp_path), "capture_document": False},
                              confirm_clear_fem=lambda: True)
    assert "fem_regions" not in report["manifest"]["scope"]
    assert air.attributes.values == {}
    assert all(item.attributes.values == {} for item in air.faces)
