"""Sources & Mesh input mapping and document round trips, without Fusion."""
import json
import math
import types

import pytest

from test_wglink_activation_boundary import _boundary
from test_wglink_fem_dialog import _building_inputs
from test_wglink_send import Attributes, Collection, body, component, face


@pytest.fixture
def scene(monkeypatch, tmp_path):
    fixture = _boundary(monkeypatch, tmp_path, "WGLink_mesh_dialog", coordination=False)
    module = fixture.module
    faces = [face("HF", area=100), face("body paint", area=500), face("body paint", area=600)]
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
    design = types.SimpleNamespace(rootComponent=root, findAttributes=find)
    fixture.app.activeProduct = design
    fixture.design = design
    fixture.faces = faces
    fixture.cabinet = cabinet
    monkeypatch.setattr(module.wglink_core, "_design", lambda _app: design)
    monkeypatch.setattr(module.wglink_core, "_named_appearance", lambda _app, _design, name: types.SimpleNamespace(name=name))
    return fixture


def build(scene, *, activate=True):
    module = scene.module
    inputs, items = _building_inputs(module)
    state = module.MeshDialogState()
    module._build_mesh_dialog(inputs, state)
    if activate:
        module.MeshDialogActivateHandler(state).notify(types.SimpleNamespace(
            command=types.SimpleNamespace(commandInputs=inputs)))
    assert scene.ui.messages == []
    return inputs, items, state


def change(scene, inputs, items, state, input_id):
    scene.module.MeshInputChangedHandler(state).notify(types.SimpleNamespace(inputs=inputs, input=items[input_id]))
    assert scene.ui.messages == []


def test_command_lifecycle_title_save_size_and_first_promoted_button(scene):
    module = scene.module
    inputs, items = _building_inputs(module)
    class Event:
        def __init__(self):
            self.handlers = []
        def add(self, handler):
            self.handlers.append(handler)
    initial, minimum = [], []
    command = types.SimpleNamespace(commandInputs=inputs, parentCommandDefinition=types.SimpleNamespace(name="Sources & Mesh…"),
        setDialogInitialSize=lambda *size: initial.append(size), setDialogMinimumSize=lambda *size: minimum.append(size),
        **{name: Event() for name in ("activate", "destroy", "inputChanged", "validateInputs", "execute")})
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
    assert not items["mesh_source_faces_1"].isUseCurrentSelections
    for key in ("mesh_add_source", "mesh_remove_source", "mesh_add_group", "mesh_remove_group"):
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
    change(scene, inputs, items, state, "mesh_remove_group")
    module._apply_mesh_dialog(inputs, state)
    assert not module.wglink_send.read_mesh_settings(scene.design)["surface_groups"]
    assert all(member.appearance is None for member in scene.faces[1:])


@pytest.mark.parametrize("value", [0, -1, math.nan, math.inf])
def test_invalid_sizes_block_with_bottom_copy_before_document_edits(scene, value):
    module = scene.module
    inputs, items, state = build(scene)
    items["mesh_wall"].value = value
    args = types.SimpleNamespace(inputs=inputs)
    module.MeshValidateInputsHandler(state).notify(args)
    assert not args.areInputsValid
    assert items["mesh_validation"].formattedText == "<b>Fix before saving:</b><br>Sizes must be greater than 0."
    with pytest.raises(module.wglink_author.AuthorError, match="Sizes must be greater than 0"):
        module._apply_mesh_dialog(inputs, state)
    assert scene.design.rootComponent.attributes.values == {}
    assert scene.faces[0].appearance.name == "HF"


def test_source_group_and_group_group_overlap_and_empty_validation(scene):
    module = scene.module
    inputs, items, state = build(scene)
    change(scene, inputs, items, state, "mesh_add_group")
    items["mesh_group_name_2"].value = "Back"
    assert not module._sync_mesh_dialog(inputs, state)
    assert "A surface group must not be empty." in items["mesh_validation"].formattedText
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
    change(scene, inputs, items, state, "mesh_remove_group")
    change(scene, inputs, items, state, "mesh_remove_source")
    assert not module._sync_mesh_dialog(inputs, state)
    assert "There must be at least one source." in items["mesh_validation"].formattedText


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
        assert "LF source 30 mm is coarser than the walls (25 mm); it will be meshed at 25 mm." in items["mesh_validation"].formattedText
    assert "above the 22,000 triangle limit" in items["mesh_validation"].formattedText
    assert calls[0][0] == 120000
    assert calls[0][3] == {"solved_fraction": 0.25}
    assert calls[0][2]["source_size_mm"] == {"source-hf": 30}


def test_reset_uses_protocol_defaults_and_removes_groups_without_editing_document(scene):
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
    assert items["mesh_symmetry"].selectedItem.name == "Automatic"
    assert state.groups == {}
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
    assert not items["mesh_remove_source"].isEnabled
    change(scene, inputs, items, state, "mesh_remove_source")
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
    change(scene, inputs, items, state, "mesh_remove_source")
    module._apply_mesh_dialog(inputs, state)
    assert all(member.appearance is None for member in removed_faces)
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
    other = body("Other", faces=[face("MF")])
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
    change(scene, inputs, items, state, "mesh_remove_source")
    assert module._sync_mesh_dialog(inputs, state)
    module._apply_mesh_dialog(inputs, state)
    assert scene.faces[0].appearance is None
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
    with pytest.raises(module.wglink_author.AuthorError, match="This dialog's changes were restored"):
        module._apply_mesh_dialog(inputs, state)
    assert scene.design.rootComponent.attributes.values == original_settings
    assert [member.attributes.values for member in scene.faces] == original_stamps
    assert scene.faces[0].appearance.name == "HF"
    assert scene.faces[1].appearance.name == "body paint"
