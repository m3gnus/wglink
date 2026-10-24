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


def _building_inputs(module, *, phase="running"):
    """Mirror installed adsk/core.py getters and collection ownership.

    Command.commandInputs can resolve all command IDs. Group children are local:
    an inputChanged collection cannot resolve a sibling or nested group's IDs.
    """
    items = {}
    command = types.SimpleNamespace(focused=None, onInputChanged=None, phase=phase)

    def changed(item):
        if command.onInputChanged is not None:
            command.onInputChanged(types.SimpleNamespace(input=item, inputs=item.commandInputs))

    class Input:
        readonly = {"id", "name", "parentCommand", "parentCommandInput", "commandInputs",
                    "children", "listItems", "selectionCount", "selectedItem", "objectType", "isValid"}

        def __init__(self, input_id, name, collection, **readonly):
            self._readonly = dict(id=input_id, name=name, parentCommand=command,
                                  parentCommandInput=collection.parent, commandInputs=collection,
                                  isValid=True, objectType="adsk::core::CommandInput", **readonly)

        def __getattr__(self, key):
            try:
                return self._readonly[key]
            except KeyError:
                raise AttributeError(key) from None

        def __setattr__(self, key, value):
            if key == "isFocused":
                raise AttributeError("SelectionCommandInput uses hasFocus")
            if key in self.readonly:
                raise AttributeError(f"{key} is read-only in Fusion")
            super().__setattr__(key, value)

        def deleteMe(self):
            for member in list(items.values()):
                parent = member.parentCommandInput
                while parent is not None:
                    if parent is self:
                        if command.focused is member:
                            command.focused = None
                        member._readonly["isValid"] = False
                        items.pop(member.id, None)
                        break
                    parent = parent.parentCommandInput
            items.pop(self.id)
            self._readonly["isValid"] = False
            return True

    class Dropdown(Input):
        @property
        def selectedItem(self):
            return next((entry for entry in self._entries if entry.isSelected), None)

    class Selection(Input, _SelectionInput):
        # Fusion's isEnabled is a focus alias for selection inputs; hiding
        # one clears it. Ordinary value/dropdown inputs use normal enabling.
        @property
        def isEnabled(self):
            return self.hasFocus

        @isEnabled.setter
        def isEnabled(self, value):
            self.hasFocus = value

        @property
        def hasFocus(self):
            return command.focused is self

        @hasFocus.setter
        def hasFocus(self, value):
            if value and not self.isVisible:
                raise RuntimeError("A hidden selection input cannot receive focus")
            if value:
                command.focused = self
            elif self.hasFocus:
                command.focused = None
            changed(self)

        @property
        def isVisible(self):
            return getattr(self, "_visible", True)

        @isVisible.setter
        def isVisible(self, value):
            self._visible = value
            if not value:
                self.clearSelection()
                if self.hasFocus:
                    command.focused = None

        def addSelection(self, face):
            if command.phase == "commandCreated":
                raise RuntimeError("addSelection is invalid during commandCreated")
            if getattr(face, "selection_error", False):
                raise RuntimeError("Fusion could not select the saved face")
            if getattr(face, "selectable", True) is False:
                return False
            if self.limits and self.limits[1] and len(self._entities) >= self.limits[1]:
                return False
            kind = "SolidBodies" if hasattr(face, "faces") else "Faces"
            if self.filters and kind not in self.filters:
                return False
            self._entities.append(face)
            changed(self)
            return True

        def clearSelection(self):
            self._entities.clear()
            changed(self)
            return True

    class Inputs:
        def __init__(self, parent=None):
            self.parent = parent

        @property
        def command(self):
            return command

        def itemById(self, input_id):
            item = items.get(input_id)
            return item if self.parent is None or (item is not None and item.parentCommandInput is self.parent) else None

        def click(self, input_id):
            item = items[input_id]
            if isinstance(item, Selection):
                item.hasFocus = True
            else:
                assert getattr(item, "isEnabled", True)
                changed(item)

        def pick(self, entity):
            assert command.focused is not None, "No selection input has focus"
            return command.focused.addSelection(entity)

        def activate(self, preselected=()):
            # Native preselection does not call addSelection or inputChanged.
            # It is available when the command's activate handlers run.
            command.phase = "running"
            for entity in preselected:
                kind = "SolidBodies" if hasattr(entity, "faces") else "Faces"
                for item in list(items.values()):
                    if isinstance(item, Selection) and kind in item.filters:
                        if not item.limits or not item.limits[1] or len(item._entities) < item.limits[1]:
                            item._entities.append(entity)
                            break

        def choose(self, input_id, label):
            item = items[input_id]
            assert getattr(item, "isEnabled", True)
            next(entry for entry in item._entries if entry.name == label).isSelected = True
            changed(item)

        def addGroupCommandInput(self, input_id, name):
            item = Input(input_id, name, self)
            item._readonly["children"] = Inputs(item)
            items[input_id] = item
            return item

        def addStringValueInput(self, input_id, name, value):
            return self.addValueInput(input_id, name, "", value)

        def addValueInput(self, input_id, name, units, value):
            item = Input(input_id, name, self)
            item.value, item.unitType = value, units
            items[input_id] = item
            return item

        def addBoolValueInput(self, input_id, name, _checkbox, _resource, value):
            return self.addValueInput(input_id, name, "", value)

        def addTextBoxCommandInput(self, input_id, name, text, _rows, _readonly):
            item = self.addValueInput(input_id, name, "", text)
            item.formattedText = text
            return item

        def addDropDownCommandInput(self, input_id, name, _style):
            entries = []
            item = Dropdown(input_id, name, self)
            item._entries = entries
            class Entry:
                def __init__(self, label, selected):
                    self.name, self._selected = label, selected

                @property
                def isSelected(self):
                    return self._selected

                @isSelected.setter
                def isSelected(self, value):
                    if value:
                        for entry in entries:
                            entry._selected = False
                    self._selected = value
                    changed(item)

            def add(label, selected):
                entries.append(Entry(label, selected))
                item.listItems.count = len(entries)
            item._readonly["listItems"] = types.SimpleNamespace(add=add, item=lambda i: entries[i], count=0)
            items[input_id] = item
            return item

        def addSelectionInput(self, input_id, name, _prompt):
            item = Selection(input_id, name, self)
            _SelectionInput.__init__(item, [])
            items[input_id] = item
            if command.focused is None:
                command.focused = item
            return item

    module.adsk.core.ValueInput = types.SimpleNamespace(createByReal=lambda value: value)
    module.adsk.core.DropDownStyles = types.SimpleNamespace(TextListDropDownStyle=1)
    inputs = Inputs()
    command.commandInputs = inputs
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
        module._mesh_select(items["fem_kind_1"], label)
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


def test_group_name_edit_keeps_creation_title_and_validation_runs_on_group_event(fixture, monkeypatch):
    module = fixture.module
    inputs, items = _building_inputs(module)
    state = module.FemDialogState()
    module._add_fem_group(inputs, state, name="Original")
    errors = []
    monkeypatch.setattr(module, "_log", errors.append)
    monkeypatch.setattr(module, "_sync_fem_validation", lambda actual, _state: actual is inputs)
    items["fem_name_1"].value = "Renamed"
    module.FemInputChangedHandler(state)._changed(types.SimpleNamespace(
        inputs=items["fem_name_1"].commandInputs, input=items["fem_name_1"]))
    assert not errors
    assert items["fem_group_1"].name == "Original"
    args = types.SimpleNamespace(inputs=items["fem_name_1"].commandInputs)
    module.FemValidateInputsHandler(state).notify(args)
    assert args.areInputsValid


def test_body_focus_events_preserve_unsaved_group_controls_and_face_selections(fixture):
    module = fixture.module
    inputs, items = _building_inputs(module)
    state = module.FemDialogState()
    module._build_fem_dialog(inputs, state)
    air, faces = _air(module)
    inputs.command.onInputChanged = module.FemInputChangedHandler(state)._changed
    inputs.click('fem_body')
    assert inputs.pick(air)
    inputs.click('fem_add_group')
    selection = items['fem_faces_1']
    inputs.click('fem_faces_1')
    assert selection.hasFocus and not items['fem_body'].hasFocus
    items['fem_name_1'].value = 'Edited cone'
    assert inputs.pick(faces[0])
    inputs.choose('fem_role_1', 'HF')
    inputs.click('fem_body')  # Focus event, same membership: do not rebuild.
    assert items['fem_faces_1'] is selection and selection.isValid
    assert items['fem_name_1'].value == 'Edited cone'
    assert items['fem_role_1'].selectedItem.name == 'HF'
    assert selection._entities == [faces[0]]
    inputs.click('fem_faces_1')
    assert inputs.pick(faces[1])
    assert selection._entities == faces[:2]
    # A different body must still load its own declaration.
    other, _ = _air(module)
    other.entityToken = 'other-air'
    items['fem_body'].clearSelection()
    inputs.click('fem_body')
    assert inputs.pick(other)
    assert state.group_ids == [] and not selection.isValid
    assert 'fem_faces_1' not in items


class _FailingAttributes(_Attributes):
    """Fail a real attribute API operation once, without erasing its data."""

    def __init__(self, original, stage):
        self.values = original.values
        self.stage = stage
        self.failures = 1

    def itemByName(self, group, name):
        if self.stage == "lookup" and self.failures:
            self.failures -= 1
            raise RuntimeError("Injected attribute lookup failure")
        attribute = super().itemByName(group, name)
        if self.stage != "value" or attribute is None:
            return attribute
        owner = self

        class Attribute:
            @property
            def value(self):
                if owner.failures:
                    owner.failures -= 1
                    raise RuntimeError("Injected attribute value failure")
                return attribute.value

        return Attribute()


def _live_fem_dialog(module):
    inputs, items = _building_inputs(module, phase="commandCreated")
    state = module.FemDialogState()
    module._build_fem_dialog(inputs, state)
    inputs.command.onInputChanged = module.FemInputChangedHandler(state)._changed
    inputs.activate()
    return inputs, items, state


@pytest.mark.parametrize("stage", ["lookup", "value", "face-value", "selection-false", "selection-error"])
def test_failed_restoration_blocks_save_and_reselection_retries(fixture, stage):
    module = fixture.module
    air, faces = _air(module)
    module._apply_fem_region(_inputs(module, air, faces), _state(module, 1, 2))
    saved = [dict(entity.attributes.values) for entity in [air, *faces]]
    if stage in ("lookup", "value"):
        air.attributes = _FailingAttributes(air.attributes, stage)
    elif stage == "face-value":
        faces[1].attributes = _FailingAttributes(faces[1].attributes, "value")
    elif stage == "selection-false":
        faces[1].selectable = False
    else:
        faces[1].selection_error = True
    inputs, items, state = _live_fem_dialog(module)
    assert inputs.pick(air)
    assert state.loaded_body is None and not state.restoring
    assert "Could not load the saved FEM region" in items["fem_validation"].formattedText
    assert "Reselect the air body to retry before saving" in state.restoration_error
    # A subsequent edit/validation must not accept the partially restored rows.
    inputs.click("fem_name")
    validation = types.SimpleNamespace(inputs=inputs, areInputsValid=True)
    module.FemValidateInputsHandler(state).notify(validation)
    assert not validation.areInputsValid
    with pytest.raises(module.wglink_author.AuthorError, match="Could not load"):
        module._apply_fem_region(inputs, state)
    # Execute repeats the refusal, even if the UI allowed an OK click.
    execute = module.CommandExecuteHandler("fem")
    execute.fem_state = state
    args = types.SimpleNamespace(command=inputs.command, isValidResult=True)
    execute.notify(args)
    assert not args.isValidResult
    assert [entity.attributes.values for entity in [air, *faces]] == saved
    faces[1].selectable = True
    faces[1].selection_error = False
    items["fem_body"].clearSelection()
    inputs.click("fem_body")
    assert inputs.pick(air)
    assert state.loaded_body is air and state.restoration_error is None
    assert module._sync_fem_validation(inputs, state)
    assert [items[module._fem_input_id("faces", row)]._entities for row in state.group_ids] == [[faces[0]], [faces[1]]]
    module._apply_fem_region(inputs, state)
    restored = module.wglink_send.read_fem_region(air)
    original = json.loads(saved[0][("WGLink", "fem_region")])
    # Saving regenerates face IDs; group declarations and membership survive.
    for region in (restored, original):
        for group in region["groups"]:
            group.pop("declared_face_ids")
    assert restored == original
    assert module.wglink_send.fem_region_faces(air, restored["region_id"]) == {
        "cone": [faces[0]], "port": [faces[1]],
    }


class _BodyHandle:
    """Fresh wrappers can have different tokens and resolve to one body."""

    def __init__(self, body, token):
        self.body = body
        self.entityToken = token

    def __getattr__(self, name):
        return getattr(self.body, name)

    def __eq__(self, other):
        return self.body is (other.body if isinstance(other, _BodyHandle) else other)


def test_changed_body_token_preserves_rows_but_different_body_reloads(fixture):
    module = fixture.module
    air, faces = _air(module)
    other, other_faces = _air(module)
    module._apply_fem_region(_inputs(module, other, other_faces), _state(module, 1, 2))
    first = _BodyHandle(air, "air-before")
    again = _BodyHandle(air, "air-after")
    different = _BodyHandle(other, "other")
    resolved = {first.entityToken: [air], again.entityToken: [air], different.entityToken: [other]}
    design = module.wglink_core._design(None)
    lookups = []
    def find(token):
        lookups.append(token)
        return resolved[token]
    design.findEntityByToken = find
    inputs, items, state = _live_fem_dialog(module)
    assert inputs.pick(first)
    inputs.click("fem_add_group")
    row = state.group_ids[0]
    selection = items[module._fem_input_id("faces", row)]
    items[module._fem_input_id("name", row)].value = "Unsaved cone"
    assert inputs.pick(faces[0])
    items["fem_body"].clearSelection()
    inputs.click("fem_body")
    assert inputs.pick(again)
    assert "air-before" in lookups and "air-after" in lookups
    assert items[module._fem_input_id("faces", row)] is selection and selection.isValid
    assert selection._entities == [faces[0]]
    assert items[module._fem_input_id("name", row)].value == "Unsaved cone"
    items["fem_body"].clearSelection()
    inputs.click("fem_body")
    assert inputs.pick(different)
    assert not selection.isValid
    assert len(state.group_ids) == 2
    assert [items[module._fem_input_id("name", row)].value for row in state.group_ids] == ["Cone", "Port"]
    assert module._sync_fem_validation(inputs, state)


def test_fem_native_preselection_restores_on_activate_and_keeps_edits_on_reactivation(fixture):
    from test_wglink_mesh_dialog import _dialog_command

    module = fixture.module
    air, faces = _air(module)
    module._apply_fem_region(_inputs(module, air, faces), _state(module, 1, 2))
    inputs, items = _building_inputs(module, phase="commandCreated")
    command, _, _ = _dialog_command(inputs)
    module.CommandCreatedHandler("fem").notify(types.SimpleNamespace(command=command))
    execute = command.execute.handlers[0]
    state = execute.fem_state
    assert state.loaded_body is None and state.group_ids == []
    with pytest.raises(RuntimeError, match="invalid during commandCreated"):
        items["fem_body"].addSelection(air)
    inputs.command.onInputChanged = command.inputChanged.handlers[0]._changed
    inputs.activate([air])  # No inputChanged event accompanies native preselection.
    assert state.loaded_body is None
    args = types.SimpleNamespace(command=command)
    command.activate.handlers[0].notify(args)
    assert state.loaded_body is air
    selection = items["fem_faces_1"]
    assert selection._entities == [faces[0]]
    items["fem_name_1"].value = "Edited after activation"
    command.activate.handlers[0].notify(args)  # e.g. Orbit/Pan has ended.
    assert items["fem_faces_1"] is selection
    assert items["fem_name_1"].value == "Edited after activation"
    assert fixture.ui.messages == []
