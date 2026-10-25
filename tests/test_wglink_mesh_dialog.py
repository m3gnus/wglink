"""Sources & Mesh input mapping and document round trips, without Fusion."""
import json
import math
import types

import pytest

from test_wglink_activation_boundary import _boundary
from test_wglink_fem_dialog import _building_inputs
from test_wglink_mesh_settings import AppearanceFace, FailingMeshAttributes
from test_wglink_send import Attributes, Collection, body, component, face


@pytest.fixture
def scene(monkeypatch, tmp_path):
    fixture = _boundary(monkeypatch, tmp_path, "WGLink_mesh_dialog", coordination=False)
    module = fixture.module
    inherited = types.SimpleNamespace(name="body paint", id="body-paint")
    faces = [AppearanceFace(inherited) for _ in range(3)]
    for member, area in zip(faces, (100, 500, 600)):
        member.area = area
    faces[0].appearance = types.SimpleNamespace(name="HF", id="hf")
    cabinet = body("Cabinet", faces=faces)
    for index, member in enumerate(faces):
        member.body = cabinet
        member.entityToken = f"face-{index}"
        member.attributes = Attributes()
    root = component("Assembly", [cabinet])
    root.attributes = Attributes()
    def find(group, name):
        found = []
        for entity in [root, *faces]:
            attribute = entity.attributes.itemByName(group, name)
            if attribute is not None:
                attribute.parent = entity
                attribute.name = name
                found.append(attribute)
        return Collection(found)
    appearances = {inherited.id: inherited, "hf": faces[0].appearance}
    design = types.SimpleNamespace(rootComponent=root, findAttributes=find,
        appearances=types.SimpleNamespace(itemById=appearances.get,
            itemByName=lambda name: next((a for a in appearances.values() if a.name == name), None)))
    monkeypatch.setattr(module.adsk.core, "AppearanceSourceTypes", types.SimpleNamespace(
        OverrideAppearanceSource=4, FaceAppearanceSource=3), raising=False)
    fixture.app.activeProduct = design
    fixture.design = design
    fixture.faces = faces
    fixture.cabinet = cabinet
    monkeypatch.setattr(module.wglink_core, "_design", lambda _app: design)
    def named(_app, _design, name):
        if name not in appearances:
            asset = types.SimpleNamespace(name=name, id=name)
            asset.deleteMe = lambda: appearances.pop(name, None) is not None
            appearances[name] = asset
        return appearances[name]
    monkeypatch.setattr(module.wglink_core, "_named_appearance", named)
    return fixture


def build(scene, *, activate=True):
    module = scene.module
    inputs, items = _building_inputs(module, phase="commandCreated")
    state = module.MeshDialogState()
    module._build_mesh_dialog(inputs, state)
    if activate:
        inputs.activate()
        module.MeshDialogActivateHandler(state).notify(types.SimpleNamespace(
            command=types.SimpleNamespace(commandInputs=inputs)))
    assert scene.ui.messages == []
    return inputs, items, state


def change(scene, inputs, items, state, input_id):
    scene.module.MeshInputChangedHandler(state).notify(types.SimpleNamespace(inputs=items[input_id].commandInputs, input=items[input_id]))
    assert scene.ui.messages == []


def test_live_face_and_dropdown_events_on_new_source_save_and_reopen(scene):
    module = scene.module
    inputs, items, state = build(scene)
    inputs.command.onInputChanged = module.MeshInputChangedHandler(state).notify
    inputs.click("mesh_add_source")
    inputs.click("mesh_source_faces_2")
    assert inputs.pick(scene.faces[1])  # Real Fusion fires inputChanged here.
    assert scene.ui.messages == []
    inputs.choose("mesh_source_role_2", "LF")
    sources, _, _ = module._mesh_dialog_values(inputs, state)
    assert sources[1]["selectors"] == {"appearance_labels": ["LF"]}
    assert scene.ui.messages == []
    assert module._sync_mesh_dialog(inputs, state)
    module._apply_mesh_dialog(inputs, state)
    _, reopened, state = build(scene)
    assert len(state.sources) == 2
    row = next(row for row, source in state.sources.items() if source['role'] == 'LF')
    assert reopened[f"mesh_source_role_{row}"].selectedItem.name == "LF"
    assert reopened[f"mesh_source_faces_{row}"]._entities == [scene.faces[1]]


def test_row_click_routes_picks_and_presets_without_rebuilding_inputs(scene):
    module = scene.module
    inputs, items, state = build(scene)
    inputs.command.onInputChanged = module.MeshInputChangedHandler(state).notify
    inputs.click("mesh_add_group")
    first = items["mesh_group_faces_2"]
    assert first.hasFocus
    items["mesh_group_name_2"].value = "Back"
    assert inputs.pick(scene.faces[1])
    inputs.click("mesh_add_group")
    second = items["mesh_group_faces_3"]
    assert second.hasFocus and not first.hasFocus
    items["mesh_group_name_3"].value = "Side"
    assert inputs.pick(scene.faces[2])
    inputs.choose("mesh_group_preset_2", "Coarse 40 mm")
    assert items["mesh_group_size_2"].value == 4
    inputs.choose("mesh_group_preset_3", "Fine 15 mm")
    inputs.click("mesh_group_faces_2")
    assert first.hasFocus and not second.hasFocus
    assert items["mesh_group_faces_2"] is first
    assert first._entities == [scene.faces[1]]
    assert second._entities == [scene.faces[2]]
    module.MeshDialogActivateHandler(state).notify(types.SimpleNamespace(command=inputs.command))
    assert first.hasFocus  # Orbit/Pan activation must not restore or steal focus.
    assert scene.ui.messages == []
    module._apply_mesh_dialog(inputs, state)
    _, reopened, _ = build(scene)
    assert reopened["mesh_group_faces_2"]._entities == [scene.faces[1]]
    assert reopened["mesh_group_faces_3"]._entities == [scene.faces[2]]


def _dialog_command(inputs):
    class Event:
        def __init__(self):
            self.handlers = []
        def add(self, handler):
            self.handlers.append(handler)
    initial, minimum = [], []
    command = types.SimpleNamespace(commandInputs=inputs, parentCommandDefinition=types.SimpleNamespace(name="Sources & Mesh…"),
        setDialogInitialSize=lambda *size: initial.append(size), setDialogMinimumSize=lambda *size: minimum.append(size),
        **{name: Event() for name in ("activate", "destroy", "inputChanged", "validateInputs", "execute")})
    return command, initial, minimum


def test_command_lifecycle_title_save_size_and_first_promoted_button(scene):
    module = scene.module
    inputs, items = _building_inputs(module)
    command, initial, minimum = _dialog_command(inputs)
    module.CommandCreatedHandler("source").notify(types.SimpleNamespace(command=command))
    assert scene.ui.messages == []
    assert module.COMMANDS["source"][:2] == ("hornlab_wglink_set_source", "Sources & Mesh…")
    assert module.PANEL_NAME == "Waveguide Generator"
    assert module.PROMOTED_COMMANDS == ("source", "solve", "send")
    assert command.parentCommandDefinition.name == "Sources & Mesh — Waveguide Generator"
    assert command.okButtonText == "Save" and not command.isExecutedWhenPreEmpted
    assert initial == [(660, 720)] and minimum == [(420, 420)]
    assert all(getattr(command, name).handlers for name in ("activate", "destroy", "inputChanged", "validateInputs", "execute"))
    assert [items[key].name for key in ("mesh_sources", "mesh_walls", "mesh_groups", "mesh_advanced", "mesh_estimate_section", "mesh_validation_section")] == [
        "Sources", "Walls", "Surface Groups", "Advanced", "Estimate", "Validation"]
    assert items["mesh_advanced"].isExpanded is False
    assert items["mesh_source_faces_1"].selectionCount == 0  # invalid in commandCreated
    command.activate.handlers[0].notify(types.SimpleNamespace(command=command))
    assert items["mesh_source_faces_1"].selection(0).entity is scene.faces[0]
    command.destroy.handlers[0].notify(types.SimpleNamespace(command=command))
    assert command.parentCommandDefinition.name == "Sources & Mesh…"


@pytest.mark.parametrize("operation, builder, apply, sync, state_attr", [
    ("source", "_build_mesh_dialog", "_apply_mesh_dialog", "_sync_mesh_dialog", "mesh_state"),
    ("fem", "_build_fem_dialog", "_apply_fem_region", "_sync_fem_validation", "fem_state"),
])
@pytest.mark.parametrize("finish_inputs", [False, True])
def test_failed_construction_keeps_handlers_and_refuses_save_once(
    scene, monkeypatch, operation, builder, apply, sync, state_attr, finish_inputs
):
    module = scene.module
    inputs, items = _building_inputs(module)
    command, _, _ = _dialog_command(inputs)
    monkeypatch.setattr(module, "_require_fem_regions", lambda: None)
    original_build = getattr(module, builder)
    events = ("activate", "inputChanged", "validateInputs", "execute")
    if operation == "source":
        events += ("destroy",)

    def refuse_partial_dialog():
        for _ in range(2):
            command.inputChanged.handlers[0].notify(types.SimpleNamespace())
            validate_args = types.SimpleNamespace(areInputsValid=True)
            command.validateInputs.handlers[0].notify(validate_args)
            assert validate_args.areInputsValid is False
            execute_args = types.SimpleNamespace(isValidResult=True)
            command.execute.handlers[0].notify(execute_args)
            assert execute_args.isValidResult is False
            command.activate.handlers[0].notify(types.SimpleNamespace())

    def broken_build(inputs, state):
        assert all(len(getattr(command, event).handlers) == 1 for event in events)
        assert all(getattr(command, event).handlers[0] in module._handlers for event in events)
        assert state.construction_failed
        # Events can also arrive while inputs are still being constructed.
        refuse_partial_dialog()
        if finish_inputs:
            original_build(inputs, state)
        else:
            inputs.addStringValueInput("partial", "Partial input", "")
        raise RuntimeError("dialog construction failed")

    monkeypatch.setattr(module, builder, broken_build)
    module.CommandCreatedHandler(operation).notify(types.SimpleNamespace(command=command))
    assert items  # Construction reached the strict input fake.
    assert len(scene.ui.messages) == 1
    assert "dialog construction failed" in scene.ui.messages[0][1]
    execute = command.execute.handlers[0]
    assert getattr(execute, state_attr).construction_failed

    def unexpected(*_args):
        pytest.fail("A failed dialog must not validate or save partial inputs")

    monkeypatch.setattr(module, apply, unexpected)
    monkeypatch.setattr(module, sync, unexpected)
    refuse_partial_dialog()
    assert len(scene.ui.messages) == 1
    if operation == "source":
        command.destroy.handlers[0].notify(types.SimpleNamespace(command=command))
        assert command.parentCommandDefinition.name == module.COMMANDS["source"][1]


def test_defaults_filters_copy_and_restore_runs_once(scene):
    module = scene.module
    inputs, items, state = build(scene)
    assert items["mesh_source_role_1"].selectedItem.name == "HF"
    assert items["mesh_source_size_1"].value == .6
    assert items["mesh_wall"].value == 2.5
    assert items["mesh_grading"].value == 10
    assert items["mesh_symmetry"].selectedItem.name == "Automatic"
    assert items["mesh_wall"].tooltip == "Mesh size for every surface that is not a source or in a surface group."
    assert items["mesh_grading"].tooltip == "Distance over which the mesh grows from each source's size to the wall size."
    assert items["mesh_symmetry"].tooltip == "Off solves the whole model: slower, more memory."
    assert items["mesh_source_faces_1"].filters == ["Faces"]
    assert items["mesh_source_faces_1"].limits == (0, 0)
    assert not hasattr(items["mesh_source_faces_1"], "isUseCurrentSelections")
    for key in ("mesh_add_source", "mesh_remove_source_1", "mesh_add_group"):
        assert items[key].isFullWidth
    change(scene, inputs, items, state, "mesh_add_group")
    assert items["mesh_group_faces_2"].filters == ["Faces", "SolidBodies", "SurfaceBodies"]
    assert items["mesh_group_faces_2"].limits == (0, 0)
    items["mesh_source_faces_1"].clearSelection()
    module.MeshDialogActivateHandler(state).notify(types.SimpleNamespace(command=types.SimpleNamespace(commandInputs=inputs)))
    assert items["mesh_source_faces_1"].selectionCount == 0  # Orbit/Pan must preserve user edits.
    assert not state.restoring


@pytest.mark.parametrize("identities", [False, True])
def test_save_reopen_restores_every_value_and_face_and_repaints(scene, identities):
    module = scene.module
    if identities:
        path = scene.ipc / module.wglink_inbox.CAPABILITIES_FILENAME
        payload = json.loads(path.read_text())
        path.write_text(json.dumps({**payload, "sourceIdentity": 1}))
    inputs, items, state = build(scene)
    items["mesh_wall"].value = 3.2
    items["mesh_grading"].value = 8
    items["mesh_source_size_1"].value = .4  # Stored old suggestion is kept.
    module._mesh_select(items["mesh_symmetry"], "Off")
    change(scene, inputs, items, state, "mesh_add_group")
    items["mesh_group_name_2"].value = "Back"
    module._mesh_select(items["mesh_group_preset_2"], "Coarse 40 mm")
    change(scene, inputs, items, state, "mesh_group_preset_2")
    items["mesh_group_faces_2"].addSelection(scene.faces[1])
    items["mesh_group_faces_2"].addSelection(scene.faces[2])
    assert module._sync_mesh_dialog(inputs, state)
    module._apply_mesh_dialog(inputs, state)
    saved = module.wglink_send.read_mesh_settings(scene.design)
    assert saved["wall_size_mm"] == 32 and saved["grading_distance_mm"] == 80
    assert saved["symmetry"] == "off" and list(saved["source_size_mm"].values()) == [4]
    group = saved["surface_groups"][0]
    assert (group["name"], group["size_mm"], group["observed"]["face_count"]) == ("Back", 40, 2)
    assert all(member.appearance.name == f"WGMESH_{group['group_id']}" for member in scene.faces[1:])
    inputs, items, state = build(scene)
    assert items["mesh_wall"].value == 3.2 and items["mesh_grading"].value == 8
    assert items["mesh_source_size_1"].value == .4
    assert items["mesh_symmetry"].selectedItem.name == "Off"
    assert items["mesh_group_name_2"].value == "Back"
    assert items["mesh_group_preset_2"].selectedItem.name == "Coarse 40 mm"
    assert items["mesh_group_faces_2"]._entities == scene.faces[1:]
    module._apply_mesh_dialog(inputs, state)
    assert module.wglink_send.read_mesh_settings(scene.design) == saved
    # Removing a group clears its managed paint and removes the stored group.
    change(scene, inputs, items, state, f"mesh_remove_group_{next(reversed(state.groups))}")
    module._apply_mesh_dialog(inputs, state)
    assert not module.wglink_send.read_mesh_settings(scene.design)["surface_groups"]
    assert all(member.override is None for member in scene.faces[1:])


@pytest.mark.parametrize("value", [0, -1, math.nan, math.inf])
def test_invalid_sizes_block_with_bottom_copy_before_document_edits(scene, value):
    module = scene.module
    inputs, items, state = build(scene)
    items["mesh_wall"].value = value
    args = types.SimpleNamespace(inputs=inputs)
    module.MeshValidateInputsHandler(state).notify(args)
    assert not args.areInputsValid
    assert items["mesh_validation"].formattedText == "<b>Fix before saving:</b><br>Walls: size must be greater than 0."
    with pytest.raises(module.wglink_author.AuthorError, match="Walls: size must be greater than 0"):
        module._apply_mesh_dialog(inputs, state)
    assert scene.design.rootComponent.attributes.values == {}
    assert scene.faces[0].appearance.name == "HF"


def test_source_group_and_group_group_overlap_and_empty_validation(scene):
    module = scene.module
    inputs, items, state = build(scene)
    change(scene, inputs, items, state, "mesh_add_group")
    items["mesh_group_name_2"].value = "Back"
    assert not module._sync_mesh_dialog(inputs, state)
    assert "Group 2 (Back): select at least one face." in items["mesh_validation"].formattedText
    items["mesh_group_faces_2"].addSelection(scene.cabinet)  # Expands bodies, including the source.
    assert not module._sync_mesh_dialog(inputs, state)
    assert "A face cannot be in both a source and a surface group." in items["mesh_validation"].formattedText
    items["mesh_group_faces_2"].clearSelection()
    items["mesh_group_faces_2"].addSelection(scene.faces[1])
    change(scene, inputs, items, state, "mesh_add_group")
    items["mesh_group_name_3"].value = "Side"
    items["mesh_group_faces_3"].addSelection(scene.faces[1])
    assert not module._sync_mesh_dialog(inputs, state)
    assert "A face cannot be in two surface groups." in items["mesh_validation"].formattedText
    change(scene, inputs, items, state, f"mesh_remove_group_{next(reversed(state.groups))}")
    change(scene, inputs, items, state, f"mesh_remove_source_{next(reversed(state.sources))}")
    assert not module._sync_mesh_dialog(inputs, state)
    assert "This model has no source to drive it." in items["mesh_validation"].formattedText


def test_warnings_are_advisory_and_estimate_is_live_approximate_and_amber(scene, monkeypatch):
    module = scene.module
    inputs, items, state = build(scene)
    module._mesh_select(items["mesh_source_role_1"], "LF")
    items["mesh_source_size_1"].value = 3
    counts, calls = [18000, 18001, 22001], []
    def estimate(area, sources, mesh, **kwargs):
        calls.append((area, sources, mesh, kwargs))
        count = counts.pop(0)
        return {"triangles": count, "drawn_triangles": count, "approximate": True}
    monkeypatch.setattr(module.wglink_mesh, "estimate_triangles", estimate)
    for index in range(3):
        assert module._sync_mesh_dialog(inputs, state)
        assert ("#b77900" in items["mesh_estimate"].formattedText) == (index > 0)
        assert "approximate" in items["mesh_estimate"].formattedText
        assert "Limit 22,000." in items["mesh_estimate"].formattedText
        assert "Source 1 (LF): 30 mm is coarser than the walls (25 mm); it will be meshed at 25 mm." in items["mesh_validation"].formattedText
    assert "above the 22,000 limit" in items["mesh_validation"].formattedText
    assert calls[0][0] == 120000
    assert calls[0][3] == {"solved_fraction": 0.25}
    assert calls[0][2]["source_size_mm"] == {"source-hf": 30}


def test_reset_changes_sizes_only_and_preserves_groups_without_editing_document(scene):
    module = scene.module
    inputs, items, state = build(scene)
    items["mesh_wall"].value = 4
    items["mesh_grading"].value = 3
    module._mesh_select(items["mesh_symmetry"], "Off")
    change(scene, inputs, items, state, "mesh_add_group")
    change(scene, inputs, items, state, "mesh_reset")
    assert items["mesh_wall"].value == 2.5
    assert items["mesh_grading"].value == 10
    assert items["mesh_source_size_1"].value == .6
    assert items["mesh_symmetry"].selectedItem.name == "Off"
    assert len(state.groups) == 1
    assert items["mesh_group_size_2"].value == 1.5
    assert not scene.design.rootComponent.attributes.values


def test_linked_sources_are_read_only_and_cannot_be_removed(scene, monkeypatch):
    module = scene.module
    original = module.wglink_send.preflight_scope
    def linked(app, options, *, authoring_context):
        result = original(app, options, authoring_context=authoring_context)
        authoring_context["sources"][0]["instance_id"] = "linked-design"
        return result
    monkeypatch.setattr(module.wglink_send, "preflight_scope", linked)
    inputs, items, state = build(scene)
    assert items["mesh_linked_1"].formattedText == "from the linked WG design"
    assert not items["mesh_source_role_1"].isEnabled
    assert not items["mesh_source_size_1"].isEnabled
    assert not items["mesh_source_faces_1"].isVisible
    assert "mesh_remove_source_1" not in items
    assert len(state.sources) == 1
    items["mesh_source_size_1"].value = 100
    sources, _, _ = module._mesh_dialog_values(inputs, state)
    assert sources[0]["size_mm"] == 6


def test_custom_size_and_add_remove_sources_and_restoring_guard(scene):
    module = scene.module
    inputs, items, state = build(scene)
    change(scene, inputs, items, state, "mesh_add_source")
    assert items["mesh_source_role_2"].selectedItem.name == "MF"
    items["mesh_source_faces_2"].addSelection(scene.faces[1])
    items["mesh_source_size_2"].value = 1.2
    module._apply_mesh_dialog(inputs, state)
    inputs, items, state = build(scene)
    assert len(state.sources) == 2
    mf_row = next(row for row, record in state.sources.items() if record["role"] == "MF")
    assert items[f"mesh_source_size_{mf_row}"].value == 1.2
    last_row = next(reversed(state.sources))
    removed_faces = list(state.sources[last_row]["original_faces"])
    change(scene, inputs, items, state, f"mesh_remove_source_{next(reversed(state.sources))}")
    module._apply_mesh_dialog(inputs, state)
    assert all(member.override is None for member in removed_faces)
    change(scene, inputs, items, state, "mesh_add_group")
    row_id = next(iter(state.groups))
    items[f"mesh_group_size_{row_id}"].value = 2.1
    change(scene, inputs, items, state, f"mesh_group_size_{row_id}")
    assert items[f"mesh_group_preset_{row_id}"].selectedItem.name == "Custom"
    state.restoring = True
    change(scene, inputs, items, state, "mesh_add_group")
    assert len(state.groups) == 1


@pytest.mark.parametrize("before,after", [(False, True), (True, False)])
def test_saved_source_size_survives_wg_capability_changes_and_send_mapping(scene, before, after):
    module = scene.module
    path = scene.ipc / module.wglink_inbox.CAPABILITIES_FILENAME
    payload = json.loads(path.read_text())
    path.write_text(json.dumps({**payload, "sourceIdentity": int(before)}))
    inputs, items, state = build(scene)
    items["mesh_source_size_1"].value = 1.1
    module._apply_mesh_dialog(inputs, state)
    path.write_text(json.dumps({**payload, "sourceIdentity": int(after)}))
    inputs, items, state = build(scene)
    assert items["mesh_source_size_1"].value == 1.1
    # The same source inventory and snapshot used by Send retain the size too.
    sources = module.wglink_send._sources([], [scene.cabinet], source_identity=after, design=scene.design)
    mesh = module.wglink_send._mesh_snapshot(scene.design, sources, [scene.cabinet], set())
    assert list(mesh["source_size_mm"].values()) == [11]
    module._apply_mesh_dialog(inputs, state)
    assert list(module.wglink_send.read_mesh_settings(scene.design)["source_size_mm"].values()) == [11]


def test_hidden_painted_source_can_be_repaired_even_when_send_scope_refuses(scene):
    module = scene.module
    scene.cabinet.isVisible = False
    other = body("Other", faces=[AppearanceFace(scene.faces[1].inherited)])
    other.faces[0].appearance = types.SimpleNamespace(name="MF", id="mf")
    other.faces[0].body = other
    other.faces[0].attributes = Attributes()
    other.faces[0].entityToken = "other-source"
    other.parentComponent = scene.design.rootComponent
    scene.design.rootComponent.bRepBodies.append(other)
    normal_preview = module.wglink_send.preflight_scope(scene.app)
    assert normal_preview["scope_error"]
    inputs, items, state = build(scene)
    assert len(state.sources) == 2
    # The hidden HF source is last in the preview's role order.
    assert items["mesh_source_role_2"].selectedItem.name == "HF"
    change(scene, inputs, items, state, f"mesh_remove_source_{next(reversed(state.sources))}")
    assert module._sync_mesh_dialog(inputs, state)
    module._apply_mesh_dialog(inputs, state)
    assert scene.faces[0].override is None
    assert module.wglink_send.preflight_scope(scene.app)["scope_error"] is None


def test_failure_after_paint_restores_all_sources_groups_stamps_and_settings(scene, monkeypatch):
    module = scene.module
    inputs, items, state = build(scene)
    module._apply_mesh_dialog(inputs, state)
    original_settings = dict(scene.design.rootComponent.attributes.values)
    original_stamps = [dict(member.attributes.values) for member in scene.faces]
    inputs, items, state = build(scene)
    change(scene, inputs, items, state, "mesh_add_group")
    items["mesh_group_name_2"].value = "Back"
    items["mesh_group_faces_2"].addSelection(scene.faces[1])
    def refuse(*_args):
        raise module.wglink_core.WgLinkError("Document refused the settings.")
    monkeypatch.setattr(module.wglink_send, "save_mesh_settings", refuse)
    with pytest.raises(module.wglink_author.AuthorError, match="The changes were restored"):
        module._apply_mesh_dialog(inputs, state)
    assert scene.design.rootComponent.attributes.values == original_settings
    assert [member.attributes.values for member in scene.faces] == original_stamps
    assert scene.faces[0].appearance.name == "HF"
    assert scene.faces[1].appearance.name == "body paint"


@pytest.mark.parametrize("field", ["name", "id", "parentCommand", "parentCommandInput", "commandInputs", "isValid", "objectType"])
def test_fusion_input_getters_reject_assignment(scene, field):
    inputs, items, _state = build(scene)
    for key in ("mesh_sources", "mesh_source_role_1", "mesh_source_faces_1", "mesh_source_size_1"):
        with pytest.raises(AttributeError, match="read-only"):
            setattr(items[key], field, "invalid")
    with pytest.raises(AttributeError):
        items["mesh_sources"].children = inputs
    with pytest.raises(AttributeError):
        items["mesh_source_role_1"].selectedItem = None
    with pytest.raises(AttributeError):
        items["mesh_source_role_1"].listItems = []
    with pytest.raises(AttributeError):
        items["mesh_source_faces_1"].selectionCount = 1


def test_group_event_collection_cannot_resolve_other_inputs_and_validate_uses_command(scene):
    inputs, items, state = build(scene)
    children = items["mesh_sources"].children
    assert children.itemById("mesh_add_source") is items["mesh_add_source"]
    assert children.itemById("mesh_source_faces_1") is None
    assert children.itemById("mesh_wall") is None
    change(scene, inputs, items, state, "mesh_add_group")
    items["mesh_group_name_2"].value = "Back"
    items["mesh_group_faces_2"].addSelection(scene.faces[1])
    args = types.SimpleNamespace(inputs=items["mesh_group_faces_2"].commandInputs)
    scene.module.MeshValidateInputsHandler(state).notify(args)
    assert args.areInputsValid
    # Execute receives the owning command and saves grouped inputs too.
    handler = scene.module.CommandExecuteHandler("source")
    handler.mesh_state = state
    handler.notify(types.SimpleNamespace(command=items["mesh_group_faces_2"].parentCommand))
    assert scene.module.wglink_send.read_mesh_settings(scene.design)["surface_groups"][0]["name"] == "Back"


def test_first_activate_clears_preselection_and_reports_failed_saved_faces(scene):
    scene.faces[1].appearance = scene.faces[0].appearance
    for member in scene.faces[:2]:
        member.selectable = False
    inputs, items, state = build(scene, activate=False)
    selection = items["mesh_source_faces_1"]
    inputs.activate([scene.faces[2]])  # Fusion's automatic preselection.
    scene.module.MeshDialogActivateHandler(state).notify(types.SimpleNamespace(
        command=selection.parentCommand))
    assert selection.selectionCount == 0
    text = items["mesh_validation"].formattedText
    assert "Source 1 (HF): 2 saved faces could not be selected (hidden or changed). Reselect them." in text
    assert not scene.module._sync_mesh_dialog(inputs, state)
    for member in scene.faces[:2]:
        member.selectable = True
        assert selection.addSelection(member)
    assert scene.module._sync_mesh_dialog(inputs, state)


@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize("paint_id", ["lost", "", " untrimmed "])
def test_orphan_paint_recovers_as_editable_group_and_save_can_clear_or_keep_it(scene, keep, paint_id):
    module = scene.module
    assert module.wglink_send.read_mesh_settings(scene.design) is None
    scene.faces[1].appearance = module.wglink_core._named_appearance(scene.app, scene.design, f"WGMESH_{paint_id}")
    with pytest.raises(module.wglink_core.WgLinkError, match="no settings"):
        module.wglink_send._mesh_snapshot(scene.design, [], [scene.cabinet], set())
    inputs, items, state = build(scene)
    assert items["mesh_group_name_2"].value == f"Recovered {paint_id.strip() or 'surface group'}"
    assert items["mesh_group_size_2"].value == 1.5
    assert items["mesh_group_faces_2"]._entities == [scene.faces[1]]
    if not keep:
        change(scene, inputs, items, state, "mesh_remove_group_2")
    module._apply_mesh_dialog(inputs, state)
    saved = module.wglink_send.read_mesh_settings(scene.design)
    assert len(saved["surface_groups"]) == int(keep)
    if keep:
        group_id = saved["surface_groups"][0]["group_id"]
        assert group_id and group_id == group_id.strip()
        if paint_id == "lost":
            assert group_id == paint_id
        assert scene.faces[1].appearance.name == f"WGMESH_{group_id}"
    else:
        assert scene.faces[1].override is None
    module.wglink_send._mesh_snapshot(scene.design, [], [scene.cabinet], set())


@pytest.mark.parametrize("stage", ["lookup", "value"])
def test_failed_settings_read_blocks_save_preserves_metadata_and_reopen_retries(scene, stage):
    module = scene.module
    inputs, items, state = build(scene)
    change(scene, inputs, items, state, "mesh_add_group")
    items["mesh_group_name_2"].value = "Saved back panel"
    items["mesh_group_size_2"].value = 4  # Fusion stores centimetres.
    for member in scene.faces[1:]:
        items["mesh_group_faces_2"].addSelection(member)
    module._apply_mesh_dialog(inputs, state)
    entities = [scene.design.rootComponent, *scene.faces]
    before = [dict(entity.attributes.values) for entity in entities]
    paint = [member.appearance for member in scene.faces]
    saved = module.wglink_send.read_mesh_settings(scene.design)
    scene.design.rootComponent.attributes = FailingMeshAttributes(entities[0].attributes, stage)

    inputs, items = _building_inputs(module, phase="commandCreated")
    command, _, _ = _dialog_command(inputs)
    module.CommandCreatedHandler("source").notify(types.SimpleNamespace(command=command))
    assert len(scene.ui.messages) == 1
    message = scene.ui.messages[0][1]
    assert "Could not read the saved mesh settings from this document" in message
    assert "Close and reopen Sources & Mesh… to retry before saving" in message
    failed = command.execute.handlers[0].mesh_state
    assert failed.construction_failed and not failed.restoring
    assert not failed.groups  # No reconstructed names or sizes after a failed read.
    for _ in range(2):
        command.inputChanged.handlers[0].notify(types.SimpleNamespace())
        validation = types.SimpleNamespace(inputs=inputs, areInputsValid=True)
        command.validateInputs.handlers[0].notify(validation)
        assert not validation.areInputsValid
        execute = types.SimpleNamespace(command=command, isValidResult=True)
        command.execute.handlers[0].notify(execute)
        assert not execute.isValidResult
    with pytest.raises(module.wglink_author.AuthorError, match="reopen Sources & Mesh"):
        module._apply_mesh_dialog(inputs, failed)
    assert [entity.attributes.values for entity in entities] == before
    assert [member.appearance for member in scene.faces] == paint

    scene.ui.messages.clear()
    inputs, items, state = build(scene)
    assert items["mesh_group_name_2"].value == "Saved back panel"
    assert items["mesh_group_size_2"].value == 4
    assert items["mesh_group_faces_2"]._entities == scene.faces[1:]
    assert module._sync_mesh_dialog(inputs, state)
    module._apply_mesh_dialog(inputs, state)
    assert module.wglink_send.read_mesh_settings(scene.design) == saved


@pytest.mark.parametrize("stage", ["lookup", "value"])
def test_failed_journal_read_aborts_before_mutation_or_settings_write_refusal(scene, monkeypatch, stage):
    module = scene.module
    inputs, items, state = build(scene)
    items["mesh_source_size_1"].value = 1.1
    change(scene, inputs, items, state, "mesh_add_group")
    items["mesh_group_name_2"].value = "Saved back panel"
    items["mesh_group_size_2"].value = 4
    items["mesh_group_faces_2"].addSelection(scene.faces[1])
    module._apply_mesh_dialog(inputs, state)

    inputs, items, state = build(scene)
    items["mesh_source_size_1"].value = 2.2
    items["mesh_group_name_2"].value = "Unsaved panel"
    items["mesh_group_size_2"].value = 5
    entities = [scene.design.rootComponent, *scene.faces]
    original_attributes = [dict(entity.attributes.values) for entity in entities]
    original_overrides = [member.override for member in scene.faces]
    saved = module.wglink_send.read_mesh_settings(scene.design)
    attributes = FailingMeshAttributes(entities[0].attributes, stage)
    scene.design.rootComponent.attributes = attributes
    mutations = []
    save = module._save_mesh_dialog
    def record_save(*args):
        mutations.append("save")
        return save(*args)
    def refuse(*_args):
        mutations.append("settings write")
        raise module.wglink_core.WgLinkError("settings write refused")
    monkeypatch.setattr(module, "_save_mesh_dialog", record_save)
    monkeypatch.setattr(module.wglink_send, "save_mesh_settings", refuse)

    with pytest.raises(module.wglink_author.AuthorError, match="Close and reopen") as caught:
        module._apply_mesh_dialog(inputs, state)
    assert "changes were restored" not in str(caught.value)
    assert attributes.failures == 0
    assert mutations == []
    assert [entity.attributes.values for entity in entities] == original_attributes
    assert all(member.override is old for member, old in zip(scene.faces, original_overrides))
    _, items, _ = build(scene)
    assert items["mesh_source_size_1"].value == pytest.approx(1.1)
    assert items["mesh_group_name_2"].value == "Saved back panel"
    assert items["mesh_group_size_2"].value == 4
    assert items["mesh_group_faces_2"]._entities == [scene.faces[1]]
    assert module.wglink_send.read_mesh_settings(scene.design) == saved


def test_existing_settings_do_not_reconstruct_extra_groups_from_paint(scene):
    module = scene.module
    inputs, _, state = build(scene)
    module._apply_mesh_dialog(inputs, state)
    scene.faces[1].appearance = module.wglink_core._named_appearance(scene.app, scene.design, "WGMESH_orphan")
    _, _, reopened = build(scene)
    assert reopened.groups == {}


def test_any_row_can_be_removed_and_reset_preserves_membership_names_and_symmetry(scene):
    module = scene.module
    inputs, items, state = build(scene)
    change(scene, inputs, items, state, "mesh_add_source")
    change(scene, inputs, items, state, "mesh_add_source")
    change(scene, inputs, items, state, "mesh_remove_source_1")
    assert list(state.sources) == [2, 3]
    assert "mesh_source_faces_1" not in items
    for _ in range(3):
        change(scene, inputs, items, state, "mesh_add_group")
    change(scene, inputs, items, state, "mesh_remove_group_5")
    assert list(state.groups) == [4, 6]
    items["mesh_group_name_4"].value = "Back"
    items["mesh_group_faces_4"].addSelection(scene.faces[1])
    items["mesh_group_size_4"].value = 4
    original_id = state.groups[4]["group_id"]
    module._mesh_select(items["mesh_symmetry"], "Off")
    change(scene, inputs, items, state, "mesh_reset")
    assert list(state.groups) == [4, 6]
    assert state.groups[4]["group_id"] == original_id
    assert items["mesh_group_name_4"].value == "Back"
    assert items["mesh_group_faces_4"]._entities == [scene.faces[1]]
    assert items["mesh_group_size_4"].value == module.wglink_protocol.MESH_PRESETS["Fine"] / 10
    assert items["mesh_symmetry"].selectedItem.name == "Off"


def test_validation_names_rows_refuses_duplicate_names_and_separates_warnings(scene):
    module = scene.module
    inputs, items, state = build(scene)
    for name, member in (("Back", scene.faces[1]), (" back ", scene.faces[2])):
        change(scene, inputs, items, state, "mesh_add_group")
        row = next(reversed(state.groups))
        items[f"mesh_group_name_{row}"].value = name
        items[f"mesh_group_faces_{row}"].addSelection(member)
    items["mesh_source_size_1"].value = 3
    items["mesh_group_size_2"].value = 0
    assert not module._sync_mesh_dialog(inputs, state)
    text = items["mesh_validation"].formattedText
    assert "Group 2 (Back): size must be greater than 0." in text
    assert "Group 3 (back): duplicate group name with Group 2 (Back)." in text
    assert text.index("Fix before saving:") < text.index("Warnings:") < text.index("coarser than the walls")
    with pytest.raises(module.wglink_author.AuthorError, match="duplicate group name"):
        module._apply_mesh_dialog(inputs, state)
    assert scene.design.rootComponent.attributes.values == {}


@pytest.mark.parametrize("count,full,amber,warning", [
    (6000, 24000, False, False), (18000, 72000, False, False),
    (18001, 72004, True, False), (22001, 88004, True, True),
])
def test_estimate_thresholds_use_solved_part_and_do_not_repeat_full_count(scene, monkeypatch, count, full, amber, warning):
    module = scene.module
    inputs, items, state = build(scene)
    monkeypatch.setattr(module.wglink_mesh, "estimate_triangles", lambda *_a, **_kw: {
        "triangles": count, "drawn_triangles": full})
    assert module._sync_mesh_dialog(inputs, state)
    text = items["mesh_estimate"].formattedText
    assert text.count(f"{full:,}") == 1
    assert f"≈ {count:,} triangles for the part WG solves" in text
    assert "assuming quarter symmetry" in text
    assert ("#b77900" in text) == amber
    assert ("above the 22,000" in items["mesh_validation"].formattedText) == warning
    assert "mesh_summary" not in items


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("failure", ["settings", "instances"])
def test_save_failure_restores_original_group_journal_and_exact_inheritance(scene, monkeypatch, explicit, failure):
    module = scene.module
    original_paint = types.SimpleNamespace(id="original-blue", name="Original blue")
    previous_lookup = scene.design.appearances.itemById
    scene.design.appearances.itemById = lambda key: original_paint if key == original_paint.id else previous_lookup(key)
    if explicit:
        scene.faces[1].appearance = original_paint
    inputs, items, state = build(scene)
    change(scene, inputs, items, state, "mesh_add_group")
    items["mesh_group_name_2"].value = "Original group"
    items["mesh_group_faces_2"].addSelection(scene.faces[1])
    module._apply_mesh_dialog(inputs, state)
    original_attributes = [dict(member.attributes.values) for member in scene.faces]
    original_settings = dict(scene.design.rootComponent.attributes.values)
    original_overrides = [member.override for member in scene.faces]
    inputs, items, state = build(scene)
    change(scene, inputs, items, state, "mesh_add_group")
    items["mesh_group_name_3"].value = "New group"
    items["mesh_group_faces_3"].addSelection(scene.faces[2])
    if failure == "settings":
        def refuse(*_args):
            raise module.wglink_core.WgLinkError("settings refused")
        monkeypatch.setattr(module.wglink_send, "save_mesh_settings", refuse)
    else:
        # The original group clears and repaints before this real W2a guard fires.
        reused = types.SimpleNamespace()
        scene.faces[2].body = types.SimpleNamespace(parentComponent=reused)
        scene.design.rootComponent.allOccurrences = [types.SimpleNamespace(component=reused) for _ in range(2)]
    with pytest.raises(module.wglink_author.AuthorError, match="The changes were restored") as caught:
        module._apply_mesh_dialog(inputs, state)
    if failure == "instances":
        assert "Make Independent" in str(caught.value)
        scene.faces[2].body = scene.cabinet
        scene.design.rootComponent.allOccurrences = []
    assert [member.attributes.values for member in scene.faces] == original_attributes
    assert scene.design.rootComponent.attributes.values == original_settings
    assert all(member.override is old for member, old in zip(scene.faces, original_overrides))
    # The original W2a journal still clears to the exact pre-group appearance.
    module.wglink_send.paint_surface_group(scene.app, scene.design, [scene.faces[1]], None)
    assert scene.faces[1].override is (original_paint if explicit else None)


def test_incomplete_rollback_does_not_claim_restoration(scene, monkeypatch):
    module = scene.module
    inputs, _items, state = build(scene)
    original_set = module.wglink_core._set_attribute
    def refuse_root(entity, name, value, **options):
        if entity is scene.design.rootComponent:
            original_set(entity, name, "partial settings write", **options)
            raise RuntimeError("settings write refused")
        return original_set(entity, name, value, **options)
    # Seed saved settings so rollback must restore an attribute too.
    module._apply_mesh_dialog(inputs, state)
    inputs, _items, state = build(scene)
    monkeypatch.setattr(module.wglink_core, "_set_attribute", refuse_root)
    with pytest.raises(module.wglink_author.AuthorError, match="some changes could not be restored") as caught:
        module._apply_mesh_dialog(inputs, state)
    assert "The changes were restored" not in str(caught.value)


@pytest.mark.parametrize("stage", ["lookup", "value"])
@pytest.mark.parametrize("check", ["before-restore", "after-restore"])
def test_rollback_retries_unreadable_state_and_only_claims_verified_restoration(scene, monkeypatch, stage, check):
    module = scene.module
    inputs, _items, state = build(scene)
    assert not scene.design.rootComponent.attributes.values
    save = module.wglink_send.save_mesh_settings
    failed_attributes = []
    def refuse(design, mesh):
        # A refused write can have persisted a value before raising.
        save(design, mesh)
        attributes = FailingMeshAttributes(design.rootComponent.attributes, stage)
        failed_attributes.append(attributes)
        if check == "after-restore":
            attributes.failures = 0
            lookup = attributes.itemByName
            def item_by_name(group, name):
                attribute = lookup(group, name)
                if name == module.wglink_mesh.SETTINGS_ATTRIBUTE and attribute is not None:
                    def pretend_delete():
                        # Fusion acknowledges deletion but leaves an unreadable value.
                        attributes.failures = 1
                        return True
                    attribute.deleteMe = pretend_delete
                return attribute
            attributes.itemByName = item_by_name
        design.rootComponent.attributes = attributes
        raise module.wglink_core.WgLinkError("settings write refused")
    monkeypatch.setattr(module.wglink_send, "save_mesh_settings", refuse)

    outcome = "some changes could not be restored" if check == "after-restore" else "changes were restored"
    with pytest.raises(module.wglink_author.AuthorError, match=outcome) as caught:
        module._apply_mesh_dialog(inputs, state)
    assert "settings write refused" in str(caught.value)
    assert failed_attributes[0].failures == 0
    if check == "after-restore":
        assert "Undo this command in Fusion" in str(caught.value)
        assert "The changes were restored" not in str(caught.value)
        assert scene.design.rootComponent.attributes.values  # Restoration really failed.
    else:
        assert not scene.design.rootComponent.attributes.values


def test_failure_never_repaints_read_only_linked_sources(scene, monkeypatch):
    module = scene.module
    original = module.wglink_send.preflight_scope
    def linked(app, options, *, authoring_context):
        result = original(app, options, authoring_context=authoring_context)
        authoring_context["sources"][0]["instance_id"] = "linked-design"
        return result
    monkeypatch.setattr(module.wglink_send, "preflight_scope", linked)
    inputs, _items, state = build(scene)
    journaled = []
    remember = module.wglink_send._DocumentEdit.remember_appearance
    def record(edit, member):
        journaled.append(member)
        remember(edit, member)
    monkeypatch.setattr(module.wglink_send._DocumentEdit, "remember_appearance", record)
    def refuse(*_args):
        raise RuntimeError("save refused")
    monkeypatch.setattr(module, "_save_mesh_dialog", refuse)
    with pytest.raises(module.wglink_author.AuthorError, match="changes were restored"):
        module._apply_mesh_dialog(inputs, state)
    assert journaled == []


def test_source_stamp_failure_after_paint_uses_single_dialog_rollback_message(scene, monkeypatch):
    module = scene.module
    inputs, items, state = build(scene)
    change(scene, inputs, items, state, "mesh_add_source")
    items["mesh_source_faces_2"].addSelection(scene.faces[1])
    assign = module.wglink_send.assign_source_identity
    def refuse(design, faces, role):
        if role == "MF":
            raise module.wglink_core.WgLinkError("source stamp refused")
        return assign(design, faces, role)
    monkeypatch.setattr(module.wglink_send, "assign_source_identity", refuse)
    with pytest.raises(module.wglink_author.AuthorError, match="changes were restored") as caught:
        module._apply_mesh_dialog(inputs, state)
    assert "was kept" not in str(caught.value)
    assert scene.faces[1].override is None
    assert scene.faces[0].appearance.name == "HF"
    assert all(not member.attributes.values for member in scene.faces)
    assert not scene.design.rootComponent.attributes.values


def test_a_fem_driven_model_saves_sources_and_mesh_without_a_source(scene):
    """A moving FEM surface drives the model: Save and Send share the no_drive rule."""
    module = scene.module
    inputs, items, state = build(scene)
    change(scene, inputs, items, state, f"mesh_remove_source_{next(reversed(state.sources))}")
    assert not module._sync_mesh_dialog(inputs, state)
    assert "This model has no source to drive it." in items["mesh_validation"].formattedText
    state.fem_driven = True
    assert module._sync_mesh_dialog(inputs, state)
    assert "no source" not in items["mesh_validation"].formattedText
