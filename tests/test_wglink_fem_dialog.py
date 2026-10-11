"""The Declare FEM Region command, executed against fake dialog inputs."""

from __future__ import annotations

import json
from pathlib import Path
import types

import pytest

from test_wglink_activation_boundary import _boundary
from test_wglink_addin_lifecycle import (
    _Attributes,
    _SelectionInput,
    _chosen,
    _dialog_inputs,
)


def _value(value: object) -> object:
    return types.SimpleNamespace(value=value)


def _air(module):
    faces = []
    air = types.SimpleNamespace(
        name="Rear air",
        isSolid=True,
        attributes=_Attributes(),
        objectType="adsk::fusion::BRepBody",
        entityToken="air",
    )
    for index in range(3):
        faces.append(types.SimpleNamespace(
            entityToken=f"face-{index}", attributes=_Attributes(), body=air,
        ))
    air.faces = faces
    return air, faces


def _inputs(module, air, faces, **overrides):
    items = {
        "fem_body": _SelectionInput([air]),
        "fem_action": _chosen(module.FEM_DECLARE_LABEL),
        "fem_name": _value("Rear chamber"),
        "fem_size": _value(0.8),  # Fusion holds centimetres: 8 mm
        "fem_loss": _chosen("0.02"),
        "fem_name_1": _value("Cone"),
        "fem_kind_1": _chosen("Driver diaphragm (moving)"),
        "fem_role_1": _chosen("MF"),
        "fem_size_1": _value(0.3),
        "fem_faces_1": _SelectionInput([faces[0]]),
        "fem_name_2": _value("Port"),
        "fem_kind_2": _chosen("Interface to exterior air"),
        "fem_role_2": _chosen("LF"),
        "fem_size_2": _value(0.4),
        "fem_faces_2": _SelectionInput([faces[1]]),
    }
    items.update(overrides)
    return _dialog_inputs(**items)


@pytest.fixture
def fixture(monkeypatch, tmp_path: Path):
    fixture = _boundary(monkeypatch, tmp_path, "WGLink_fem", coordination=False)
    design = types.SimpleNamespace(findAttributes=lambda _group, _name: [])
    monkeypatch.setattr(fixture.module.wglink_core, "_design", lambda _app: design)
    return fixture


def _state(module, *group_ids):
    state = module.FemDialogState()
    state.group_ids = list(group_ids)
    state.next_group_id = max(group_ids, default=0) + 1
    return state


def test_declaring_writes_the_body_and_its_faces(fixture):
    module = fixture.module
    air, faces = _air(module)
    report = module._apply_fem_region(_inputs(module, air, faces), _state(module, 1, 2))

    assert "Declared FEM region 'Rear chamber' (8 mm)" in report["summary"]
    region = json.loads(air.attributes.values[("WGLink", "fem_region")])
    assert region["region_id"] == "rear-chamber"
    assert region["medium"]["bulk_loss_factor"] == 0.02
    assert [(group["name"], group["kind"], group.get("drive_role"), group["mesh_size_mm"])
            for group in region["groups"]] == [
        ("Cone", "moving", "MF", 3.0), ("Port", "interface", None, 4.0),
    ]
    stamps = [face.attributes.values.get(("WGLink", "fem_surface_group")) for face in faces]
    assert [json.loads(stamp)["group_id"] if stamp else None for stamp in stamps] == [
        "cone", "port", None,
    ]


def test_validation_keeps_ok_disabled_until_the_declaration_is_complete(fixture):
    module = fixture.module
    air, faces = _air(module)
    state = _state(module, 1, 2)
    inputs = _inputs(module, air, faces, fem_faces_2=_SelectionInput([faces[0]]))
    assert module._fem_validation_error(inputs, state) == (
        "A face can belong to only one surface group."
    )
    other = types.SimpleNamespace(entityToken="other", body=object(), attributes=_Attributes())
    inputs = _inputs(module, air, faces, fem_faces_2=_SelectionInput([other]))
    assert "face of another body" in module._fem_validation_error(inputs, state)
    assert module._fem_validation_error(_inputs(module, air, faces), state) is None


def test_the_command_executes_and_clearing_takes_it_all_back_off(fixture):
    module = fixture.module
    air, faces = _air(module)
    handler = module.CommandExecuteHandler("fem")
    handler.fem_state = _state(module, 1, 2)
    handler.notify(types.SimpleNamespace(command=types.SimpleNamespace(
        commandInputs=_inputs(module, air, faces)
    )))
    assert ("WGLink", "fem_region") in air.attributes.values

    report = module._apply_fem_region(
        _inputs(module, air, faces, fem_action=_chosen(module.FEM_CLEAR_LABEL)),
        _state(module),
    )
    assert report["summary"] == "Cleared the FEM region and its surface groups."
    assert air.attributes.values == {}
    assert all(face.attributes.values == {} for face in faces)
