"""Declared FEM regions through the Fusion boundary: attributes and Send."""

from __future__ import annotations

from pathlib import Path
import json
import sys
import types

import pytest

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
        copy = _Copy(name="air copy", isSolid=True, isLightBulbOn=False, faces=Collection())
        occurrence.component.bRepBodies.append(copy)
        return copy

    value.copyToComponent = copy_to
    return value, cone, port


def _declare(send_module, air, cone, port, fem):
    plan = fem.plan_region_declaration(
        name="Rear chamber",
        mesh_size_mm=8,
        body_is_solid=True,
        groups=[
            {"name": "Cone", "kind": "moving", "drive_role": "LF", "mesh_size_mm": 3,
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
    assert cone_group["drive_channel_id"] == "drive-LF"
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
    assert str(refusal.value) == (
        "This Waveguide Generator does not offer FEM regions "
        "(update it, or turn FEM regions on). Clear the declarations to send without them."
    )
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
