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
            area=float(index + 1),
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
    _capabilities(fixture, {"schemaVersion": 1, "femRegions": 1})
    design = types.SimpleNamespace(findAttributes=lambda _group, _name: [])
    monkeypatch.setattr(fixture.module.wglink_core, "_design", lambda _app: design)
    return fixture


def _capabilities(fixture, payload):
    path = fixture.ipc / fixture.module.wglink_inbox.CAPABILITIES_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("payload, enabled", [
    (None, False),
    ({"schemaVersion": 1}, False),
    ({"schemaVersion": 1, "femRegions": False}, False),
    ({"schemaVersion": 1, "femRegions": True}, False),
    ({"schemaVersion": 1, "femRegions": "1"}, False),
    ({"schemaVersion": 1, "femRegions": 0}, False),
    ({"schemaVersion": 2, "femRegions": 1}, False),
    ("unreadable", False),
    ({"schemaVersion": 1, "femRegions": 1}, True),
])
def test_startup_registers_the_command_and_icon_only_under_the_capability(
    monkeypatch, tmp_path, payload, enabled
):
    fixture = _boundary(monkeypatch, tmp_path, "WGLink_fem_gate", coordination=False)
    module = fixture.module
    path = fixture.ipc / module.wglink_inbox.CAPABILITIES_FILENAME
    if payload is None:
        path.unlink()
    elif payload == "unreadable":
        path.write_text("not json", encoding="utf-8")
    else:
        _capabilities(fixture, payload)
    registered = []
    original = fixture.ui.commandDefinitions.addButtonDefinition

    def add(command_id, name, description, resource_folder):
        registered.append((command_id, name, description, resource_folder))
        return original(command_id, name, description, resource_folder)

    monkeypatch.setattr(fixture.ui.commandDefinitions, "addButtonDefinition", add)
    module.run(None)
    try:
        assert module._owned
        assert fixture.ui.messages == []
        fem_id = module.COMMANDS["fem"][0]
        assert (fixture.ui.commandDefinitions.itemById(fem_id) is not None) is enabled
        manage = module._panel.controls.itemById(module.MANAGE_DROPDOWN_ID)
        assert (fem_id in [item.definition.id for item in manage.controls._items]) is enabled
        assert len(registered) == (8 if enabled else 7)
        if not enabled:
            assert all("fem" not in " ".join(record).casefold() for record in registered)
            # Enabling WG later does not add a command to the current panel.
            _capabilities(fixture, {"schemaVersion": 1, "femRegions": 1})
            module.run(None)
            assert fixture.ui.commandDefinitions.itemById(fem_id) is None
            assert len(registered) == 7
        else:
            record = next(record for record in registered if record[0] == fem_id)
            assert record[1] == "Declare FEM Region (Experimental)…"
            assert "Experimental: geometry-checked only and not yet acoustically qualified." in record[2]
    finally:
        module.stop(None)
    assert fixture.ui.commandDefinitions.itemById(fem_id) is None


@pytest.mark.parametrize("enabled", [False, True])
def test_manifest_options_add_fem_only_when_advertised(fixture, enabled):
    payload = {"schemaVersion": 1, "automaticDomain": 1, "documentUp": 1}
    expected = {"automatic_domain": True, "document_up": True}
    if enabled:
        payload["femRegions"] = 1
        expected["fem_regions"] = True
    _capabilities(fixture, payload)
    assert fixture.module._required_manifest_features() == expected


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_backend_resume_drops_a_withdrawn_fem_capability(monkeypatch, tmp_path, operation):
    from test_wglink_launcher import (
        _advertise_launcher, _checked_fixture, _finish, _inbox, _installed_entry, _run,
    )

    fixture = _checked_fixture(monkeypatch, tmp_path, "WGLink_fem_resume")
    path = fixture.ipc / fixture.module.wglink_inbox.CAPABILITIES_FILENAME
    payload = json.loads(path.read_text())
    _capabilities(fixture, {**payload, "femRegions": 1})
    _advertise_launcher(fixture, [str(_installed_entry(tmp_path))])
    monkeypatch.setattr(fixture.module.wglink_launcher, "collecting", lambda *args: True)
    try:
        _run(fixture.module, operation)
        assert fixture.exports == []
        _capabilities(fixture, payload)
        _finish(fixture)
        assert len(fixture.exports) == 1
        assert "fem_regions" not in fixture.exports[0]
        assert [title for title, _text in fixture.ui.messages] == ["WGLink"]
        assert "FEM" not in fixture.ui.messages[0][1]
        assert [request["kind"] for request in _inbox(fixture.ipc)] == [
            "receive_snapshot" if operation == "send" else "prepare_and_solve"
        ]
    finally:
        fixture.module._backend_admission.close()
        fixture.module._stop_followups(fixture.app)


def test_withdrawn_capability_refuses_open_and_execute_then_hides_on_restart(fixture):
    module = fixture.module
    module.run(None)
    try:
        assert fixture.ui.commandDefinitions.itemById(module.COMMANDS["fem"][0])
        _capabilities(fixture, {"schemaVersion": 1})
        air, faces = _air(module)
        inputs = _inputs(module, air, faces)
        args = types.SimpleNamespace(command=types.SimpleNamespace(commandInputs=inputs))
        module.CommandCreatedHandler("fem").notify(args)
        execute = module.CommandExecuteHandler("fem")
        execute.fem_state = _state(module, 1, 2)
        execute.notify(args)
        assert len(fixture.ui.messages) == 2
        assert all(module.wglink_fem.UNAVAILABLE_MESSAGE in text
                   for _title, text in fixture.ui.messages)
        assert air.attributes.values == {}
        assert all(face.attributes.values == {} for face in faces)
        module.stop(None)
        fixture.ui.messages.clear()
        module.run(None)
        assert module._owned
        assert fixture.ui.messages == []
        assert fixture.ui.commandDefinitions.itemById(module.COMMANDS["fem"][0]) is None
    finally:
        module.stop(None)


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
    assert [group["declared_area_mm2"] for group in region["groups"]] == [100.0, 200.0]
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
