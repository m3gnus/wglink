"""The Declare FEM Region command, executed against fake dialog inputs."""

from __future__ import annotations

import json
import math
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
    ({"schemaVersion": 1, "femRegions": 2}, True),
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
            assert record[1] == "Declare FEM Region…"
            assert record[2] == (
                "Mark a solid body of air as a FEM region and name its driver, interface "
                "and termination faces for a coupled FEM/BEM solve."
            )
    finally:
        module.stop(None)
    assert fixture.ui.commandDefinitions.itemById(fem_id) is None


@pytest.mark.parametrize("enabled", [False, True])
def test_manifest_options_add_fem_only_when_advertised(fixture, enabled):
    payload = {"schemaVersion": 1, "automaticDomain": 1, "meshSizing": 1, "documentUp": 1}
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
        assert [title for title, _text in fixture.ui.messages] == ["Waveguide Generator"]
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


@pytest.mark.parametrize("accept", [True, False])
def test_clear_recovery_prompt_works_without_registering_fem(fixture, monkeypatch, accept):
    module = fixture.module
    _capabilities(fixture, {"schemaVersion": 1})
    calls = []
    def prompt(text, title, buttons, icon):
        calls.append((text, buttons, icon))
        return module.adsk.core.DialogResults.DialogYes if accept else module.adsk.core.DialogResults.DialogNo
    monkeypatch.setattr(fixture.ui, "messageBox", prompt)
    module.run(None)
    try:
        assert fixture.ui.commandDefinitions.itemById(module.COMMANDS["fem"][0]) is None
        assert module._confirm_clear_fem() is accept
        assert calls == [(
            "This design declares FEM regions, which this Waveguide Generator does not offer. "
            "Clear the FEM declarations and continue without them?",
            module.adsk.core.MessageBoxButtonTypes.YesNoButtonType,
            module.adsk.core.MessageBoxIconTypes.QuestionIconType,
        )]
    finally:
        module.stop(None)


@pytest.mark.parametrize("operation", ["send", "solve"])
def test_send_and_solve_wire_the_clear_prompt_on_the_command_thread(fixture, monkeypatch, operation):
    import threading

    module = fixture.module
    _capabilities(fixture, {"schemaVersion": 1})
    monkeypatch.setattr(module, "_send_options", lambda _inputs: {})
    monkeypatch.setattr(module, "_require_compatible_wg", lambda **_kwargs: None)
    monkeypatch.setattr(module.CommandExecuteHandler, "_admit_backend", lambda *_args: False)
    monkeypatch.setattr(module, "_submit_to_wg", lambda *_args: "request")
    command_thread = threading.get_ident()
    calls = []
    def send(_app, _options, **kwargs):
        calls.append((kwargs["confirm_clear_fem"], threading.get_ident()))
        return {"bundle_path": "return.wgreturn", "return_id": "wgr_test", "scope": {"status": "clean"}, "sources": []}
    monkeypatch.setattr(module.wglink_send, "send", send)
    module.CommandExecuteHandler(operation).notify(types.SimpleNamespace(
        command=types.SimpleNamespace(commandInputs=_dialog_inputs())
    ))
    assert calls == [(module._confirm_clear_fem, command_thread)]


LINING = {"model": "miki", "thickness_mm": 30.0, "flow_resistivity_pa_s_per_m2": 5000.0}


def test_dialog_boundary_options_persist_and_clear(fixture):
    module = fixture.module
    air, faces = _air(module)
    inputs = _inputs(module, air, faces,
        fem_level_1=_value(-6.0),
        fem_kind_2=_chosen("Rigid wall (local size)"),
        fem_lining_2=_value(True), fem_thickness_2=_value(3.0),
        fem_resistivity_2=_value(5000.0),
    )
    module._apply_fem_region(inputs, _state(module, 1, 2))
    groups = module.wglink_send.read_fem_region(air)["groups"]
    assert groups[0]["relative_level_db"] == -6.0
    assert groups[1]["lining"] == LINING
    inputs = _inputs(module, air, faces,
        fem_level_1=_value(0.0), fem_kind_2=_chosen("Rigid wall (local size)"),
        fem_lining_2=_value(False), fem_thickness_2=_value(0.0),
        fem_resistivity_2=_value(-1.0),
    )
    module._apply_fem_region(inputs, _state(module, 1, 2))
    groups = module.wglink_send.read_fem_region(air)["groups"]
    assert "relative_level_db" not in groups[0]
    assert "lining" not in groups[1]


@pytest.mark.parametrize(("overrides", "message"), [
    *[({"fem_level_1": _value(value)},
       "Relative level for 'Cone' must be between −120 and 0 dB.")
      for value in (0.1, math.nextafter(-120, -math.inf), -6500,
                    float("nan"), float("inf"), float("-inf"))],
    *[({"fem_thickness_2": _value(value)},
       "Lining thickness for 'Port' must be a positive finite number in mm.")
      for value in (0, -1, float("nan"), float("inf"))],
    *[({"fem_resistivity_2": _value(value)},
       "Flow resistivity for 'Port' must be a positive finite number in Pa·s/m².")
      for value in (0, -1, float("nan"), float("inf"))],
])
def test_dialog_refuses_invalid_boundary_fields(fixture, overrides, message):
    module = fixture.module
    air, faces = _air(module)
    defaults = dict(fem_level_1=_value(0), fem_kind_2=_chosen("Rigid wall (local size)"),
                    fem_lining_2=_value(True), fem_thickness_2=_value(3),
                    fem_resistivity_2=_value(5000))
    defaults.update(overrides)
    inputs = _inputs(module, air, faces, **defaults)
    state = _state(module, 1, 2)
    assert module._fem_validation_error(inputs, state) == message
    with pytest.raises(module.wglink_author.AuthorError, match=message):
        module._apply_fem_region(inputs, state)
    assert air.attributes.values == {}


@pytest.mark.parametrize("level", [-120, math.nextafter(-120, math.inf), 0])
def test_dialog_accepts_relative_level_boundaries(fixture, level):
    module = fixture.module
    air, faces = _air(module)
    inputs = _inputs(module, air, faces, fem_level_1=_value(level))
    state = _state(module, 1, 2)
    assert module._fem_validation_error(inputs, state) is None
    module._apply_fem_region(inputs, state)
    group = module.wglink_send.read_fem_region(air)["groups"][0]
    assert group.get("relative_level_db", 0) == level


def _building_inputs(module):
    """Build real dialog code against a minimal Fusion command-input surface."""
    items = {}

    class Inputs:
        def itemById(self, input_id):
            return items.get(input_id)

        def addGroupCommandInput(self, input_id, name):
            item = types.SimpleNamespace(children=self, name=name, deleteMe=lambda: items.pop(input_id))
            items[input_id] = item
            return item

        def addStringValueInput(self, input_id, name, value):
            return self.addValueInput(input_id, name, "", value)

        def addValueInput(self, input_id, name, units, value):
            item = types.SimpleNamespace(id=input_id, name=name, value=value, unitType=units)
            items[input_id] = item
            return item

        def addBoolValueInput(self, input_id, name, _checkbox, _resource, value):
            return self.addValueInput(input_id, name, "", value)

        def addTextBoxCommandInput(self, input_id, name, text, _rows, _readonly):
            item = self.addValueInput(input_id, name, "", text)
            item.formattedText = text
            return item

        def addTableCommandInput(self, input_id, name, _columns, _ratio):
            item = self.addGroupCommandInput(input_id, name)
            item.cells = {}
            item.addCommandInput = lambda cell, row, col: item.cells.update({(row, col): cell})
            return item

        def addDropDownCommandInput(self, input_id, name, _style):
            entries = []
            class Dropdown:
                id = input_id
                @property
                def selectedItem(self):
                    return next((entry for entry in entries if entry.isSelected), None)
                @selectedItem.setter
                def selectedItem(self, value):
                    for entry in entries:
                        entry.isSelected = entry.name == value.name
            item = Dropdown()
            item.name = name
            def add(label, selected):
                entries.append(types.SimpleNamespace(name=label, isSelected=selected))
                item.listItems.count = len(entries)
            item.listItems = types.SimpleNamespace(add=add, item=lambda i: entries[i], count=0)
            items[input_id] = item
            return item

        def addSelectionInput(self, input_id, _name, _prompt):
            item = _SelectionInput([])
            item.addSelection = lambda face: item._entities.append(face)
            item.id = input_id
            item.clearSelection = lambda: item._entities.clear()
            items[input_id] = item
            return item

    module.adsk.core.ValueInput = types.SimpleNamespace(createByReal=lambda value: value)
    module.adsk.core.DropDownStyles = types.SimpleNamespace(TextListDropDownStyle=1)
    inputs = Inputs()
    inputs.addGroupCommandInput("fem_groups", "Surface groups")
    return inputs, items


def test_dialog_builds_defaults_and_updates_kind_and_lining_visibility(fixture):
    module = fixture.module
    inputs, items = _building_inputs(module)
    state = module.FemDialogState()
    module._add_fem_group(inputs, state, kind="rigid")
    assert items["fem_lining_1"].name == "Absorbing lining"
    assert items["fem_lining_1"].value is False
    assert items["fem_thickness_1"].value == 3.0  # cm internally
    assert items["fem_resistivity_1"].value == 5000.0
    assert items["fem_level_1"].name == "Relative level (dB)"
    assert items["fem_level_1"].value == 0.0
    assert items["fem_lining_1"].isVisible
    assert not items["fem_thickness_1"].isEnabled
    assert not items["fem_level_1"].isVisible
    items["fem_lining_1"].value = True
    module.FemInputChangedHandler(state)._changed(types.SimpleNamespace(
        input=items["fem_lining_1"], inputs=inputs,
    ))
    assert items["fem_thickness_1"].isEnabled
    assert items["fem_resistivity_1"].isEnabled
    for kind in ("moving", "interface", "plane-wave-termination"):
        label = next(label for label, value in module.wglink_fem.KIND_CHOICES if value == kind)
        items["fem_kind_1"].selectedItem = types.SimpleNamespace(name=label)
        module.FemInputChangedHandler(state)._changed(types.SimpleNamespace(
            input=items["fem_kind_1"], inputs=inputs,
        ))
        assert items["fem_level_1"].isVisible == (kind == "moving")
        assert items["fem_role_1"].isVisible == (kind == "moving")
        assert not items["fem_lining_1"].isVisible
        assert not items["fem_thickness_1"].isVisible
        assert not items["fem_resistivity_1"].isVisible


@pytest.mark.parametrize("new_options", [False, True])
def test_restoring_dialog_reads_old_defaults_and_saved_boundary_options(fixture, new_options):
    module = fixture.module
    air, faces = _air(module)
    overrides = dict(fem_kind_2=_chosen("Rigid wall (local size)"))
    if new_options:
        overrides.update(fem_level_1=_value(-9), fem_lining_2=_value(True),
                         fem_thickness_2=_value(4), fem_resistivity_2=_value(6000))
    module._apply_fem_region(_inputs(module, air, faces, **overrides), _state(module, 1, 2))
    inputs, items = _building_inputs(module)
    state = module.FemDialogState()
    module._restore_fem_region(inputs, state, air)
    assert items["fem_level_1"].value == (-9 if new_options else 0)
    assert items["fem_lining_2"].value is new_options
    assert items["fem_thickness_2"].value == (4 if new_options else 3)
    assert items["fem_resistivity_2"].value == (6000 if new_options else 5000)
    assert items["fem_faces_1"].selection(0).entity is faces[0]
    assert items["fem_faces_2"].selection(0).entity is faces[1]
    assert not state.restoring
