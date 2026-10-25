"""Sources & Mesh input mapping and document round trips, without Fusion."""
import html
import json
import math
import types

import pytest

from test_wglink_activation_boundary import _boundary
from test_wglink_fem_dialog import _building_inputs
from test_wglink_mesh_settings import AppearanceFace, FailingMeshAttributes
from test_wglink_send import Attributes, Collection, body, component


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
    state = module.wglink_mesh_dialog.MeshDialogState(module._mesh_dialog_services())
    module.wglink_mesh_dialog._build_mesh_dialog(inputs, state)
    if activate:
        inputs.activate()
        module.wglink_mesh_dialog.MeshDialogActivateHandler(state).notify(types.SimpleNamespace(
            command=types.SimpleNamespace(commandInputs=inputs)))
    assert scene.ui.messages == []
    return inputs, items, state


def change(scene, inputs, items, state, input_id):
    scene.module.wglink_mesh_dialog.MeshInputChangedHandler(state).notify(types.SimpleNamespace(inputs=items[input_id].commandInputs, input=items[input_id]))
    assert scene.ui.messages == []


# Fixed driver rows, created first in the protocol's role order.
HF, MF, LF, PORT = 1, 2, 3, 4


def target(scene, inputs, items, state, kind, row):
    """Click a row's Pick faces button."""
    change(scene, inputs, items, state, f"mesh_pick_{kind}_{row}")
    assert state.target == (kind, row)


def pick(scene, inputs, items, state, *entities):
    """Click entities in the model with the one picker, as Fusion reports it."""
    picker = items["mesh_faces"]
    for entity in entities:
        assert picker.addSelection(entity)
    change(scene, inputs, items, state, "mesh_faces")


def unpick(scene, inputs, items, state, *entities):
    picker = items["mesh_faces"]
    for entity in entities:
        picker._entities.remove(entity)
    change(scene, inputs, items, state, "mesh_faces")


def add_zone(scene, inputs, items, state, preset="Fine"):
    change(scene, inputs, items, state, f"mesh_add_group_{preset}")
    return next(reversed(state.groups))


def faces_of(state, kind, row):
    return (state.sources if kind == "source" else state.groups)[row]["faces"]




def test_live_picks_on_a_fixed_driver_row_save_and_reopen(scene):
    module = scene.module
    inputs, items, state = build(scene)
    inputs.command.onInputChanged = module.wglink_mesh_dialog.MeshInputChangedHandler(state).notify
    assert [state.sources[row]["role"] for row in (HF, MF, LF, PORT)] == ["HF", "MF", "LF", "PASSIVE_CARDIOID"]
    assert state.target == ("source", HF) and items["mesh_faces"]._entities == [scene.faces[0]]
    inputs.click(f"mesh_pick_source_{LF}")
    assert items["mesh_faces"]._entities == []  # the picker shows only the target row
    assert inputs.pick(scene.faces[1])  # Real Fusion fires inputChanged here.
    assert scene.ui.messages == []
    sources, _, _ = module.wglink_mesh_dialog._mesh_dialog_values(inputs, state)
    assert [source["selectors"] for source in sources] == [
        {"appearance_labels": ["HF"]}, {"appearance_labels": ["LF"]}]
    assert module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)
    assert module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state) == {"summary": ""}
    assert scene.faces[1].appearance.name == "LF"
    _, reopened, state = build(scene)
    assert faces_of(state, "source", LF) == [scene.faces[1]]
    assert faces_of(state, "source", HF) == [scene.faces[0]]
    assert reopened["mesh_faces"]._entities == [scene.faces[0]]
    assert reopened[f"mesh_source_count_{LF}"].formattedText == "1 face"
    assert reopened[f"mesh_source_count_{HF}"].formattedText == "<b>1 face ◀</b>"
    assert reopened[f"mesh_source_count_{MF}"].formattedText == "not used"


def test_row_buttons_and_row_clicks_retarget_and_edits_keep_keyboard_focus(scene):
    module = scene.module
    inputs, items, state = build(scene)
    inputs.command.onInputChanged = module.wglink_mesh_dialog.MeshInputChangedHandler(state).notify
    picker = items["mesh_faces"]
    inputs.click("mesh_add_group_Fine")
    back = next(reversed(state.groups))
    assert state.target == ("group", back) and picker.hasFocus and picker._entities == []
    assert items[f"mesh_group_name_{back}"].value == "Fine 1"
    items[f"mesh_group_name_{back}"].value = "Back"
    assert inputs.pick(scene.faces[1])
    inputs.click("mesh_add_group_Coarse")
    side = next(reversed(state.groups))
    assert items[f"mesh_group_name_{side}"].value == "Coarse 1"
    assert items[f"mesh_group_size_{side}"].value == 4
    assert state.target == ("group", side) and picker._entities == []
    items[f"mesh_group_name_{side}"].value = "Side"
    assert inputs.pick(scene.faces[2])
    inputs.click(f"mesh_pick_group_{back}")
    assert picker._entities == [scene.faces[1]] and picker.hasFocus
    assert items["mesh_groups_table"].selectedRow == 0 and items["mesh_sources_table"].selectedRow == -1
    # A genuine cell edit targets its row, while keeping keyboard focus there.
    items[f"mesh_group_size_{side}"].value = 3
    inputs.command.focused = items[f"mesh_group_size_{side}"]
    inputs.click(f"mesh_group_size_{side}")
    assert state.target == ("group", side)
    assert inputs.command.focused is items[f"mesh_group_size_{side}"]
    # A click on a row (Fusion moves the table's selectedRow) targets that row.
    items["mesh_groups_table"].selectedRow = 1
    inputs.click("mesh_groups_table")
    assert state.target == ("group", side) and picker._entities == [scene.faces[2]]
    items["mesh_sources_table"].selectedRow = 1
    inputs.click("mesh_sources_table")
    assert state.target == ("source", MF) and items["mesh_groups_table"].selectedRow == -1
    inputs.click(f"mesh_pick_group_{side}")
    module.wglink_mesh_dialog.MeshDialogActivateHandler(state).notify(types.SimpleNamespace(command=inputs.command))
    assert state.target == ("group", side)  # Orbit/Pan activation must not move the target.
    assert faces_of(state, "group", back) == [scene.faces[1]]
    assert scene.ui.messages == []
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    _, reopened, state = build(scene)
    assert faces_of(state, "group", back) == [scene.faces[1]]
    assert faces_of(state, "group", side) == [scene.faces[2]]
    assert reopened[f"mesh_group_size_{side}"].value == 3


@pytest.mark.parametrize("kind,field", [
    ("source", "label"), ("source", "count"), ("source", "size"),
    ("group", "name"), ("group", "count"), ("group", "size"),
])
def test_native_child_cell_events_target_the_row_before_selected_row_changes(scene, kind, field):
    inputs, items, state = build(scene)
    zone = add_zone(scene, inputs, items, state)
    target(scene, inputs, items, state, "source", HF)
    row = MF if kind == "source" else zone
    table = items["mesh_sources_table" if kind == "source" else "mesh_groups_table"]
    assert table.selectedRow == (0 if kind == "source" else -1)
    cell = items[f"mesh_{kind}_{field}_{row}"]
    inputs.command.focused = cell
    # Real Fusion emits the child id (observed for label/count), not a table event.
    change(scene, inputs, items, state, cell.id)
    assert state.target == (kind, row)
    assert items["mesh_faces"]._entities == faces_of(state, kind, row)
    other = items["mesh_groups_table" if kind == "source" else "mesh_sources_table"]
    assert other.selectedRow == -1
    if field in ("name", "size"):
        assert inputs.command.focused is cell  # typing must keep keyboard focus
    else:
        assert items["mesh_faces"].hasFocus


def test_rendered_count_setter_events_cannot_take_the_picker_target(scene):
    module = scene.module
    dialog = module.wglink_mesh_dialog
    inputs, items, state = build(scene)
    target(scene, inputs, items, state, "source", MF)
    events = []
    handler = dialog.MeshInputChangedHandler(state)
    def on_changed(args):
        events.append((args.input.id, state.restoring))
        handler.notify(args)
    inputs.command.onInputChanged = on_changed
    inputs.command.text_change_events = True
    # Force a real formattedText setter on another row during a validation render.
    state.shown.pop(f"mesh_source_count_{HF}")
    dialog._sync_mesh_dialog(inputs, state)
    assert (f"mesh_source_count_{HF}", True) in events
    assert state.target == ("source", MF)
    assert items["mesh_sources_table"].selectedRow == 1
    assert state.restoring is False
    # A nested render must preserve an outer restoration guard too.
    state.restoring = True
    dialog._sync_mesh_dialog(inputs, state)
    assert state.restoring is True


def test_estimate_setter_can_run_a_live_preview_during_validation_render(scene):
    module = scene.module
    dialog = module.wglink_mesh_dialog
    inputs, items, state = build(scene)
    target(scene, inputs, items, state, "source", MF)
    pick(scene, inputs, items, state, scene.faces[1])
    preview = dialog.MeshPreviewHandler(state)
    handler = dialog.MeshInputChangedHandler(state)
    events = []
    generations = (module.wglink_send._source_authoring_generation,
                   module.wglink_send._mesh_authoring_generation)
    def on_changed(args):
        if args.input.id == "mesh_estimate":
            events.append(state.restoring)
            # Observed in Fusion: this setter synchronously raises executePreview.
            preview.notify(types.SimpleNamespace(
                command=types.SimpleNamespace(commandInputs=inputs), isValidResult=True))
        elif args.input.id.startswith("mesh_source_count_"):
            handler.notify(args)
    inputs.command.onInputChanged = on_changed
    inputs.command.text_change_events = True
    state.shown.pop(f"mesh_source_count_{HF}")
    dialog._sync_mesh_dialog(inputs, state)
    assert events == [False]
    assert scene.faces[1].appearance.name == "MF"
    assert state.preview_failure is None
    assert state.target == ("source", MF)
    assert (module.wglink_send._source_authoring_generation,
            module.wglink_send._mesh_authoring_generation) == generations


def _dialog_command(inputs):
    class Event:
        def __init__(self):
            self.handlers = []
        def add(self, handler):
            self.handlers.append(handler)
    initial, minimum = [], []
    command = types.SimpleNamespace(commandInputs=inputs, parentCommandDefinition=types.SimpleNamespace(name="Sources & Mesh…"),
        setDialogInitialSize=lambda *size: initial.append(size), setDialogMinimumSize=lambda *size: minimum.append(size),
        **{name: Event() for name in ("activate", "destroy", "inputChanged", "validateInputs", "execute",
                                      "executePreview")})
    return command, initial, minimum


def test_command_lifecycle_title_save_size_and_first_promoted_button(scene):
    module = scene.module
    inputs, items = _building_inputs(module)
    command, initial, minimum = _dialog_command(inputs)
    module.CommandCreatedHandler("source").notify(types.SimpleNamespace(command=command))
    assert scene.ui.messages == []
    assert module.COMMANDS["source"][:2] == ("hornlab_wglink_set_source", "Sources & Mesh…")
    assert module.PANEL_NAME == "Waveguide Generator"
    assert module.PROMOTED_COMMANDS == ("source", "solve", "send", "requests")
    assert command.parentCommandDefinition.name == "Sources & Mesh — Waveguide Generator"
    assert command.okButtonText == "Save" and not command.isExecutedWhenPreEmpted
    assert initial == [(660, 720)] and minimum == [(420, 420)]
    assert all(getattr(command, name).handlers for name in ("activate", "destroy", "inputChanged", "validateInputs", "execute"))
    assert [items[key].name for key in ("mesh_sources", "mesh_walls", "mesh_groups", "mesh_advanced", "mesh_estimate_section")] == [
        "Drivers", "Walls", "Size zones", "Advanced", "Estimate"]
    assert list(items).index("mesh_validation") < list(items).index("mesh_sources")  # in view, above the tables
    assert items["mesh_advanced"].isExpanded is False
    assert items["mesh_faces"].selectionCount == 0  # invalid in commandCreated
    command.activate.handlers[0].notify(types.SimpleNamespace(command=command))
    assert items["mesh_faces"].selection(0).entity is scene.faces[0]
    assert "HF – high-frequency driver" in items["mesh_target"].formattedText
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
    owner = module.wglink_mesh_dialog if operation == "source" else module
    original_build = getattr(owner, builder)
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

    monkeypatch.setattr(owner, builder, broken_build)
    module.CommandCreatedHandler(operation).notify(types.SimpleNamespace(command=command))
    assert items  # Construction reached the strict input fake.
    assert len(scene.ui.messages) == 1
    assert "dialog construction failed" in "\n".join(scene.ui.text_palette.written)
    execute = command.execute.handlers[0]
    assert getattr(execute, state_attr).construction_failed

    def unexpected(*_args):
        pytest.fail("A failed dialog must not validate or save partial inputs")

    monkeypatch.setattr(owner, apply, unexpected)
    monkeypatch.setattr(owner, sync, unexpected)
    refuse_partial_dialog()
    assert len(scene.ui.messages) == 1
    if operation == "source":
        command.destroy.handlers[0].notify(types.SimpleNamespace(command=command))
        assert command.parentCommandDefinition.name == module.COMMANDS["source"][1]


def test_defaults_filters_copy_and_restore_runs_once(scene):
    module = scene.module
    inputs, items, state = build(scene)
    assert [items[f"mesh_source_label_{row}"].formattedText for row in (HF, MF, LF, PORT)] == [
        "HF – high-frequency driver", "MF – mid-frequency driver",
        "LF – low-frequency driver", "Passive radiator"]
    assert [items[f"mesh_source_size_{row}"].value for row in (HF, MF, LF, PORT)] == [.6, 1.5, 2.5, 2.5]
    assert items["mesh_wall"].value == 2.5
    assert items["mesh_grading"].value == 10
    assert items["mesh_symmetry"].selectedItem.name == "Automatic"
    assert items["mesh_wall"].tooltip == "Mesh size for every surface that is not a driver and not in a size zone."
    assert items["mesh_grading"].tooltip == "How far from each driver the mesh grows from the driver's size to the wall size."
    assert items["mesh_symmetry"].tooltip == "Off solves the whole model: slower, more memory."
    picker = items["mesh_faces"]
    assert picker.filters == ["Faces", "SolidBodies", "SurfaceBodies"]
    assert picker.limits == (0, 0)
    assert not hasattr(picker, "isUseCurrentSelections")
    assert items["mesh_reset"].isFullWidth
    assert [item.text for item in items["mesh_groups_table"].toolbar] == [
        "+ Fine size zone (15 mm)", "+ Coarse size zone (40 mm)"]
    assert not [key for key in items if "preset" in key or key in ("mesh_add_source", "mesh_add_group")]
    assert "mesh_remove_source_1" not in items  # drivers are fixed rows
    unpick(scene, inputs, items, state, scene.faces[0])
    module.wglink_mesh_dialog.MeshDialogActivateHandler(state).notify(types.SimpleNamespace(command=types.SimpleNamespace(commandInputs=inputs)))
    assert picker.selectionCount == 0 and faces_of(state, "source", HF) == []  # Orbit/Pan must preserve user edits.
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
    inputs.choose("mesh_symmetry", "Off")
    zone = add_zone(scene, inputs, items, state, "Coarse")
    items[f"mesh_group_name_{zone}"].value = "Back"
    pick(scene, inputs, items, state, scene.faces[1], scene.faces[2])
    assert module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
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
    zone = next(iter(state.groups))
    assert items[f"mesh_group_name_{zone}"].value == "Back"
    assert items[f"mesh_group_size_{zone}"].value == 4
    assert faces_of(state, "group", zone) == scene.faces[1:]
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    assert module.wglink_send.read_mesh_settings(scene.design) == saved
    # Removing a zone clears its managed paint and removes the stored group.
    change(scene, inputs, items, state, f"mesh_remove_group_{zone}")
    assert f"mesh_group_name_{zone}" not in items and items["mesh_groups_table"].rowCount == 0
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    assert not module.wglink_send.read_mesh_settings(scene.design)["surface_groups"]
    assert all(member.override is None for member in scene.faces[1:])


@pytest.mark.parametrize("value", [0, -1, math.nan, math.inf])
def test_invalid_sizes_block_with_bottom_copy_before_document_edits(scene, value):
    module = scene.module
    inputs, items, state = build(scene)
    items["mesh_wall"].value = value
    args = types.SimpleNamespace(inputs=inputs)
    module.wglink_mesh_dialog.MeshValidateInputsHandler(state).notify(args)
    assert not args.areInputsValid
    assert items["mesh_validation"].formattedText == "<b>Fix before saving:</b><br>Walls: size must be greater than 0."
    with pytest.raises(module.wglink_author.AuthorError, match="Walls: size must be greater than 0"):
        module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    assert scene.design.rootComponent.attributes.values == {}
    assert scene.faces[0].appearance.name == "HF"


def test_a_face_picked_for_another_row_moves_and_a_body_keeps_other_rows_faces(scene):
    module = scene.module
    inputs, items, state = build(scene)
    back = add_zone(scene, inputs, items, state)
    items[f"mesh_group_name_{back}"].value = "Back"
    assert not module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)
    assert "Size zone &#x27;Back&#x27;: pick at least one face." in items["mesh_validation"].formattedText
    pick(scene, inputs, items, state, scene.cabinet)  # a whole body: the HF face stays HF
    assert faces_of(state, "group", back) == scene.faces[1:]
    assert faces_of(state, "source", HF) == [scene.faces[0]]
    assert items["mesh_faces"]._entities == scene.faces[1:]  # faces, not the body
    assert state.notice == "1 face of that body stayed in other rows."
    assert items["mesh_notice"].formattedText == html.escape(state.notice)
    assert module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)
    side = add_zone(scene, inputs, items, state)
    items[f"mesh_group_name_{side}"].value = "Side"
    pick(scene, inputs, items, state, scene.faces[1])
    assert state.notice == "Moved 1 face from Size zone 'Back' to Size zone 'Side'."
    assert faces_of(state, "group", back) == [scene.faces[2]]
    assert items[f"mesh_group_count_{back}"].formattedText == "1 face"
    target(scene, inputs, items, state, "source", HF)
    pick(scene, inputs, items, state, scene.faces[2])
    assert state.notice == "Moved 1 face from Size zone 'Back' to HF – high-frequency driver."
    assert faces_of(state, "source", HF) == [scene.faces[0], scene.faces[2]]
    assert not module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)  # Back is now empty
    target(scene, inputs, items, state, "group", back)
    pick(scene, inputs, items, state, scene.cabinet)  # a driver can't take whole bodies; zones can
    target(scene, inputs, items, state, "source", MF)
    pick(scene, inputs, items, state, scene.cabinet)
    assert faces_of(state, "source", MF) == [] and items["mesh_faces"]._entities == []
    assert state.notice == "A driver takes faces, not whole bodies."
    target(scene, inputs, items, state, "source", HF)
    unpick(scene, inputs, items, state, scene.faces[0], scene.faces[2])
    for zone in list(state.groups):
        change(scene, inputs, items, state, f"mesh_remove_group_{zone}")
    assert not module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)
    assert "This model has no driver yet." in items["mesh_validation"].formattedText


def test_warnings_are_advisory_and_estimate_is_live_approximate_and_amber(scene, monkeypatch):
    module = scene.module
    inputs, items, state = build(scene)
    items["mesh_source_size_1"].value = 3
    counts, calls = [22000, 24000, 88004], []
    def estimate(area, sources, mesh, **kwargs):
        calls.append((area, sources, mesh, kwargs))
        count = counts.pop(0)
        return {"full": count, "half": round(count / 2), "quarter": round(count / 4)}
    monkeypatch.setattr(module.wglink_mesh, "estimate_triangles", estimate)
    for index in range(3):
        assert module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)
        assert ("#b77900" in items["mesh_estimate"].formattedText) == (index > 0)
        assert "About" in items["mesh_estimate"].formattedText
        assert "Limit 22,000." in items["mesh_estimate"].formattedText
        assert "HF – high-frequency driver: 30 mm is coarser than the walls (25 mm); it will be meshed at 25 mm." in items["mesh_validation"].formattedText
    assert "above the 22,000 limit" in items["mesh_validation"].formattedText
    assert calls[0][0] == 120000
    assert calls[0][3] == {}
    assert calls[0][2]["source_size_mm"] == {"source-hf": 30}


def test_reset_changes_sizes_only_and_preserves_groups_without_editing_document(scene):
    inputs, items, state = build(scene)
    items["mesh_wall"].value = 4
    items["mesh_grading"].value = 3
    items[f"mesh_source_size_{MF}"].value = 9
    inputs.choose("mesh_symmetry", "Off")
    zone = add_zone(scene, inputs, items, state, "Coarse")
    change(scene, inputs, items, state, "mesh_reset")
    assert items["mesh_wall"].value == 2.5
    assert items["mesh_grading"].value == 10
    assert items["mesh_source_size_1"].value == .6
    assert items[f"mesh_source_size_{MF}"].value == 1.5
    assert items["mesh_symmetry"].selectedItem.name == "Off"
    assert len(state.groups) == 1
    assert items[f"mesh_group_size_{zone}"].value == 1.5
    assert not scene.design.rootComponent.attributes.values


def test_linked_sources_are_read_only_and_keep_their_faces(scene, monkeypatch):
    module = scene.module
    original = module.wglink_send.preflight_scope
    def linked(app, options, *, authoring_context):
        result = original(app, options, authoring_context=authoring_context)
        authoring_context["sources"][0]["instance_id"] = "linked-design"
        return result
    monkeypatch.setattr(module.wglink_send, "preflight_scope", linked)
    inputs, items, state = build(scene)
    row = next(row for row, record in state.sources.items() if record.get("instance_id"))
    assert row == 5 and faces_of(state, "source", HF) == []
    assert items[f"mesh_source_label_{row}"].formattedText == "HF – high-frequency driver (from the Waveguide Generator design)"
    assert not items[f"mesh_source_size_{row}"].isEnabled
    assert f"mesh_pick_source_{row}" not in items
    assert items[f"mesh_source_count_{row}"].formattedText == "1 face"
    items[f"mesh_source_size_{row}"].value = 100
    sources, _, _ = module.wglink_mesh_dialog._mesh_dialog_values(inputs, state)
    assert [source["size_mm"] for source in sources] == [6]
    pick(scene, inputs, items, state, scene.faces[0])  # the target is HF, an editable row
    assert faces_of(state, "source", row) == [scene.faces[0]]
    assert faces_of(state, "source", HF) == [] and items["mesh_faces"]._entities == []
    assert state.notice == "HF – high-frequency driver (from the Waveguide Generator design) keeps its faces; change them in Waveguide Generator."


@pytest.mark.parametrize("field", ["label", "count", "size"])
def test_linked_child_cell_events_never_take_ownership_of_the_picker(scene, monkeypatch, field):
    module = scene.module
    original = module.wglink_send.preflight_scope
    def linked(app, options, *, authoring_context):
        result = original(app, options, authoring_context=authoring_context)
        authoring_context["sources"][0]["instance_id"] = "linked-design"
        return result
    monkeypatch.setattr(module.wglink_send, "preflight_scope", linked)
    inputs, items, state = build(scene)
    row = next(row for row, record in state.sources.items() if record.get("instance_id"))
    prior = state.target
    table = items["mesh_sources_table"]
    table.selectedRow = list(state.sources).index(row)
    change(scene, inputs, items, state, f"mesh_source_{field}_{row}")
    assert state.target == prior
    assert table.selectedRow == list(state.sources).index(prior[1])
    assert faces_of(state, "source", row) == [scene.faces[0]]
    assert items["mesh_faces"]._entities == []


def test_custom_size_and_clearing_a_driver_and_restoring_guard(scene):
    module = scene.module
    inputs, items, state = build(scene)
    target(scene, inputs, items, state, "source", MF)
    pick(scene, inputs, items, state, scene.faces[1])
    items[f"mesh_source_size_{MF}"].value = 1.2
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    inputs, items, state = build(scene)
    assert faces_of(state, "source", MF) == [scene.faces[1]]
    assert items[f"mesh_source_size_{MF}"].value == 1.2
    target(scene, inputs, items, state, "source", MF)
    unpick(scene, inputs, items, state, scene.faces[1])
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    assert scene.faces[1].override is None
    add_zone(scene, inputs, items, state)
    state.restoring = True
    change(scene, inputs, items, state, "mesh_add_group_Fine")
    assert len(state.groups) == 1


@pytest.mark.parametrize("before,after", [(False, True), (True, False)])
def test_saved_source_size_survives_wg_capability_changes_and_send_mapping(scene, before, after):
    module = scene.module
    path = scene.ipc / module.wglink_inbox.CAPABILITIES_FILENAME
    payload = json.loads(path.read_text())
    path.write_text(json.dumps({**payload, "sourceIdentity": int(before)}))
    inputs, items, state = build(scene)
    items["mesh_source_size_1"].value = 1.1
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    path.write_text(json.dumps({**payload, "sourceIdentity": int(after)}))
    inputs, items, state = build(scene)
    assert items["mesh_source_size_1"].value == 1.1
    # The same source inventory and snapshot used by Send retain the size too.
    sources = module.wglink_send._sources([], [scene.cabinet], source_identity=after, design=scene.design)
    mesh = module.wglink_send._mesh_snapshot(scene.design, sources, [scene.cabinet], set())
    assert list(mesh["source_size_mm"].values()) == [11]
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
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
    assert faces_of(state, "source", HF) == [scene.faces[0]]
    assert faces_of(state, "source", MF) == [other.faces[0]]
    target(scene, inputs, items, state, "source", HF)
    unpick(scene, inputs, items, state, scene.faces[0])
    assert module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    assert scene.faces[0].override is None
    assert module.wglink_send.preflight_scope(scene.app)["scope_error"] is None


def test_failure_after_paint_restores_all_sources_groups_stamps_and_settings(scene, monkeypatch):
    module = scene.module
    inputs, items, state = build(scene)
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    original_settings = dict(scene.design.rootComponent.attributes.values)
    original_stamps = [dict(member.attributes.values) for member in scene.faces]
    inputs, items, state = build(scene)
    zone = add_zone(scene, inputs, items, state)
    items[f"mesh_group_name_{zone}"].value = "Back"
    pick(scene, inputs, items, state, scene.faces[1])
    def refuse(*_args):
        raise module.wglink_core.WgLinkError("Document refused the settings.")
    monkeypatch.setattr(module.wglink_send, "save_mesh_settings", refuse)
    with pytest.raises(module.wglink_author.AuthorError, match="The changes were restored"):
        module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    assert scene.design.rootComponent.attributes.values == original_settings
    assert [member.attributes.values for member in scene.faces] == original_stamps
    assert scene.faces[0].appearance.name == "HF"
    assert scene.faces[1].appearance.name == "body paint"


@pytest.mark.parametrize("field", ["name", "id", "parentCommand", "parentCommandInput", "commandInputs", "isValid", "objectType"])
def test_fusion_input_getters_reject_assignment(scene, field):
    inputs, items, _state = build(scene)
    for key in ("mesh_sources", "mesh_sources_table", "mesh_symmetry", "mesh_faces", "mesh_source_size_1"):
        with pytest.raises(AttributeError, match="read-only"):
            setattr(items[key], field, "invalid")
    with pytest.raises(AttributeError):
        items["mesh_sources"].children = inputs
    with pytest.raises(AttributeError):
        items["mesh_symmetry"].selectedItem = None
    with pytest.raises(AttributeError):
        items["mesh_symmetry"].listItems = []
    with pytest.raises(AttributeError):
        items["mesh_faces"].selectionCount = 1


def test_group_event_collection_cannot_resolve_other_inputs_and_validate_uses_command(scene):
    inputs, items, state = build(scene)
    children = items["mesh_sources"].children
    assert children.itemById("mesh_sources_table") is items["mesh_sources_table"]
    assert children.itemById("mesh_faces") is None
    assert children.itemById("mesh_wall") is None
    zone = add_zone(scene, inputs, items, state)
    items[f"mesh_group_name_{zone}"].value = "Back"
    pick(scene, inputs, items, state, scene.faces[1])
    args = types.SimpleNamespace(inputs=items[f"mesh_group_name_{zone}"].commandInputs)
    scene.module.wglink_mesh_dialog.MeshValidateInputsHandler(state).notify(args)
    assert args.areInputsValid
    # Execute receives the owning command and saves table inputs too.
    handler = scene.module.CommandExecuteHandler("source")
    handler.mesh_state = state
    handler.notify(types.SimpleNamespace(command=items[f"mesh_group_name_{zone}"].parentCommand))
    assert scene.module.wglink_send.read_mesh_settings(scene.design)["surface_groups"][0]["name"] == "Back"
    assert scene.ui.messages == []  # no "Saved" box


def test_first_activate_clears_preselection_and_reports_failed_saved_faces(scene):
    scene.faces[1].appearance = scene.faces[0].appearance
    for member in scene.faces[:2]:
        member.selectable = False
    inputs, items, state = build(scene, activate=False)
    picker = items["mesh_faces"]
    inputs.activate([scene.faces[2]])  # Fusion's automatic preselection.
    scene.module.wglink_mesh_dialog.MeshDialogActivateHandler(state).notify(types.SimpleNamespace(
        command=picker.parentCommand))
    assert picker.selectionCount == 0 and state.target == ("source", HF)
    text = items["mesh_validation"].formattedText
    assert "HF – high-frequency driver: 2 saved faces are hidden or changed. Show the body and reopen Sources &amp; Mesh…, or clear this row." in text
    assert not scene.module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)
    for member in scene.faces[:2]:
        member.selectable = True
    pick(scene, inputs, items, state, *scene.faces[:2])
    assert scene.module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)


@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize("paint_id", ["lost", "", " untrimmed "])
def test_orphan_paint_recovers_as_editable_group_and_save_can_clear_or_keep_it(scene, keep, paint_id):
    module = scene.module
    assert module.wglink_send.read_mesh_settings(scene.design) is None
    scene.faces[1].appearance = module.wglink_core._named_appearance(scene.app, scene.design, f"WGMESH_{paint_id}")
    with pytest.raises(module.wglink_core.WgLinkError, match="no settings"):
        module.wglink_send._mesh_snapshot(scene.design, [], [scene.cabinet], set())
    inputs, items, state = build(scene)
    zone = next(iter(state.groups))
    assert items[f"mesh_group_name_{zone}"].value == f"Recovered {paint_id.strip()}".strip()
    assert items[f"mesh_group_size_{zone}"].value == 1.5
    assert faces_of(state, "group", zone) == [scene.faces[1]]
    if not keep:
        change(scene, inputs, items, state, f"mesh_remove_group_{zone}")
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
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
    zone = add_zone(scene, inputs, items, state)
    items[f"mesh_group_name_{zone}"].value = "Saved back panel"
    items[f"mesh_group_size_{zone}"].value = 4  # Fusion stores centimetres.
    pick(scene, inputs, items, state, *scene.faces[1:])
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
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
    assert "The saved mesh settings could not be used" in message
    assert "Open Sources & Mesh… and save them again" in message
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
        module.wglink_mesh_dialog._apply_mesh_dialog(inputs, failed)
    assert [entity.attributes.values for entity in entities] == before
    assert [member.appearance for member in scene.faces] == paint

    scene.ui.messages.clear()
    inputs, items, state = build(scene)
    assert items[f"mesh_group_name_{zone}"].value == "Saved back panel"
    assert items[f"mesh_group_size_{zone}"].value == 4
    assert faces_of(state, "group", zone) == scene.faces[1:]
    assert module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    assert module.wglink_send.read_mesh_settings(scene.design) == saved


@pytest.mark.parametrize("stage", ["lookup", "value"])
def test_failed_journal_read_aborts_before_mutation_or_settings_write_refusal(scene, monkeypatch, stage):
    module = scene.module
    inputs, items, state = build(scene)
    items["mesh_source_size_1"].value = 1.1
    zone = add_zone(scene, inputs, items, state)
    items[f"mesh_group_name_{zone}"].value = "Saved back panel"
    items[f"mesh_group_size_{zone}"].value = 4
    pick(scene, inputs, items, state, scene.faces[1])
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)

    inputs, items, state = build(scene)
    items["mesh_source_size_1"].value = 2.2
    items[f"mesh_group_name_{zone}"].value = "Unsaved panel"
    items[f"mesh_group_size_{zone}"].value = 5
    entities = [scene.design.rootComponent, *scene.faces]
    original_attributes = [dict(entity.attributes.values) for entity in entities]
    original_overrides = [member.override for member in scene.faces]
    saved = module.wglink_send.read_mesh_settings(scene.design)
    attributes = FailingMeshAttributes(entities[0].attributes, stage)
    scene.design.rootComponent.attributes = attributes
    mutations = []
    save = module.wglink_mesh_dialog._save_mesh_dialog
    def record_save(*args):
        mutations.append("save")
        return save(*args)
    def refuse(*_args):
        mutations.append("settings write")
        raise module.wglink_core.WgLinkError("settings write refused")
    monkeypatch.setattr(module.wglink_mesh_dialog, "_save_mesh_dialog", record_save)
    monkeypatch.setattr(module.wglink_send, "save_mesh_settings", refuse)

    with pytest.raises(module.wglink_author.AuthorError, match="Close and reopen") as caught:
        module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    assert "changes were restored" not in str(caught.value)
    assert attributes.failures == 0
    assert mutations == []
    assert [entity.attributes.values for entity in entities] == original_attributes
    assert all(member.override is old for member, old in zip(scene.faces, original_overrides))
    _, items, state = build(scene)
    assert items["mesh_source_size_1"].value == pytest.approx(1.1)
    assert items[f"mesh_group_name_{zone}"].value == "Saved back panel"
    assert items[f"mesh_group_size_{zone}"].value == 4
    assert faces_of(state, "group", zone) == [scene.faces[1]]
    assert module.wglink_send.read_mesh_settings(scene.design) == saved


def test_existing_settings_do_not_reconstruct_extra_groups_from_paint(scene):
    module = scene.module
    inputs, _, state = build(scene)
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    scene.faces[1].appearance = module.wglink_core._named_appearance(scene.app, scene.design, "WGMESH_orphan")
    _, _, reopened = build(scene)
    assert reopened.groups == {}


def test_any_zone_can_be_removed_and_reset_preserves_membership_names_and_symmetry(scene):
    module = scene.module
    inputs, items, state = build(scene)
    first, middle, last = (add_zone(scene, inputs, items, state) for _ in range(3))
    table = items["mesh_groups_table"]
    target(scene, inputs, items, state, "group", middle)
    change(scene, inputs, items, state, f"mesh_remove_group_{middle}")
    assert list(state.groups) == [first, last] and state.group_order == [first, last]
    assert f"mesh_group_name_{middle}" not in items and table.rowCount == 2
    assert table.cells[(1, 0)] is items[f"mesh_group_name_{last}"]  # rows below shift up
    # The removed zone was the target: its neighbour takes over, and the notice says why.
    assert state.target == ("group", last) and table.selectedRow == 1
    assert state.notice == "Removed Size zone 'Fine 2'."
    target(scene, inputs, items, state, "group", first)
    items[f"mesh_group_name_{first}"].value = "Back"
    pick(scene, inputs, items, state, scene.faces[1])
    items[f"mesh_group_size_{first}"].value = 4
    original_id = state.groups[first]["group_id"]
    inputs.choose("mesh_symmetry", "Off")
    change(scene, inputs, items, state, "mesh_reset")
    assert list(state.groups) == [first, last]
    assert state.groups[first]["group_id"] == original_id
    assert items[f"mesh_group_name_{first}"].value == "Back"
    assert faces_of(state, "group", first) == [scene.faces[1]]
    assert items[f"mesh_group_size_{first}"].value == module.wglink_protocol.MESH_PRESETS["Fine"] / 10
    assert items["mesh_symmetry"].selectedItem.name == "Off"


def test_validation_names_rows_refuses_duplicate_names_and_separates_warnings(scene):
    module = scene.module
    inputs, items, state = build(scene)
    rows = []
    for name, member in (("Back", scene.faces[1]), (" back ", scene.faces[2])):
        rows.append(add_zone(scene, inputs, items, state))
        items[f"mesh_group_name_{rows[-1]}"].value = name
        pick(scene, inputs, items, state, member)
    items["mesh_source_size_1"].value = 3
    items[f"mesh_group_size_{rows[0]}"].value = 0
    assert not module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)
    text = html.unescape(items["mesh_validation"].formattedText)
    assert "Size zone 'Back': size must be greater than 0." in text
    assert "Size zone 'back': the same name as Size zone 'Back'." in text
    assert text.index("Fix before saving:") < text.index("Warnings:") < text.index("coarser than the walls")
    with pytest.raises(module.wglink_author.AuthorError, match="the same name as"):
        module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    assert scene.design.rootComponent.attributes.values == {}


@pytest.mark.parametrize("count,full,amber,warning", [
    (5500, 22000, False, False), (6000, 24000, True, False), (18000, 72000, True, False),
    (18001, 72004, True, False), (22001, 88004, True, True),
])
def test_estimate_range_uses_full_for_colour_and_quarter_for_ceiling(scene, monkeypatch, count, full, amber, warning):
    module = scene.module
    inputs, items, state = build(scene)
    monkeypatch.setattr(module.wglink_mesh, "estimate_triangles", lambda *_a, **_kw: {
        "full": full, "half": round(full / 2), "quarter": count})
    assert module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)
    text = items["mesh_estimate"].formattedText
    assert text.count(f"{full:,}") == (2 if full == 22000 else 1)
    assert f"About {count:,} – {full:,} triangles" in text
    assert "decides whether it can use symmetry" in text
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
    zone = add_zone(scene, inputs, items, state)
    items[f"mesh_group_name_{zone}"].value = "Original group"
    pick(scene, inputs, items, state, scene.faces[1])
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    original_attributes = [dict(member.attributes.values) for member in scene.faces]
    original_settings = dict(scene.design.rootComponent.attributes.values)
    original_overrides = [member.override for member in scene.faces]
    inputs, items, state = build(scene)
    zone = add_zone(scene, inputs, items, state)
    items[f"mesh_group_name_{zone}"].value = "New group"
    pick(scene, inputs, items, state, scene.faces[2])
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
        module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
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
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    inputs, _items, state = build(scene)
    monkeypatch.setattr(module.wglink_core, "_set_attribute", refuse_root)
    with pytest.raises(module.wglink_author.AuthorError, match="some changes could not be restored") as caught:
        module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
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
        module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
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
    monkeypatch.setattr(module.wglink_mesh_dialog, "_save_mesh_dialog", refuse)
    with pytest.raises(module.wglink_author.AuthorError, match="changes were restored"):
        module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    assert journaled == []


def test_source_stamp_failure_after_paint_uses_single_dialog_rollback_message(scene, monkeypatch):
    module = scene.module
    inputs, items, state = build(scene)
    target(scene, inputs, items, state, "source", MF)
    pick(scene, inputs, items, state, scene.faces[1])
    assign = module.wglink_send.assign_source_identity
    def refuse(design, faces, role):
        if role == "MF":
            raise module.wglink_core.WgLinkError("source stamp refused")
        return assign(design, faces, role)
    monkeypatch.setattr(module.wglink_send, "assign_source_identity", refuse)
    with pytest.raises(module.wglink_author.AuthorError, match="changes were restored") as caught:
        module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    assert "was kept" not in str(caught.value)
    assert scene.faces[1].override is None
    assert scene.faces[0].appearance.name == "HF"
    assert all(not member.attributes.values for member in scene.faces)
    assert not scene.design.rootComponent.attributes.values


def test_a_fem_driven_model_saves_sources_and_mesh_without_a_source(scene):
    """A moving FEM surface drives the model: Save and Send share the no_drive rule."""
    module = scene.module
    inputs, items, state = build(scene)
    unpick(scene, inputs, items, state, scene.faces[0])
    assert not module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)
    assert "This model has no driver yet." in items["mesh_validation"].formattedText
    state.fem_driven = True
    assert module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)
    assert "no source" not in items["mesh_validation"].formattedText


@pytest.mark.parametrize("symmetry", ["Automatic", "Off"])
def test_the_ceiling_warning_fits_this_dialog_and_its_symmetry(scene, monkeypatch, symmetry):
    module = scene.module
    inputs, items, state = build(scene)
    monkeypatch.setattr(module.wglink_mesh, "estimate_triangles", lambda *_a, **_kw: {
        "full": 120000, "half": 60000, "quarter": 30000})
    inputs.choose("mesh_symmetry", symmetry)
    assert module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)
    warning = items["mesh_validation"].formattedText
    assert "above the 22,000 limit" in warning
    assert "Sources &amp; Mesh" not in warning and "Sources & Mesh" not in warning
    assert ("symmetry" in warning) == (symmetry == "Automatic")


def test_mesh_dialog_learns_actual_count_and_invalidates_an_unsaved_face_swap(scene, monkeypatch, tmp_path):
    from test_wglink_mesh_estimates import status
    module = scene.module
    scene.cabinet.revisionId = 'revision-1'
    scene.cabinet.entityToken = 'cabinet'
    monkeypatch.setattr(module, '_active_document_id', lambda: 'doc')
    monkeypatch.setattr(module.wglink_workspace, 'ipc_folder', lambda **_kw: tmp_path)
    inputs, items, state = build(scene)
    sources, groups, mesh = module.wglink_mesh_dialog._mesh_dialog_values(inputs, state)
    measured, snapshot = module.wglink_mesh_dialog._mesh_snapshot_values(sources, groups, mesh)
    memberships = {'source:' + row['id']: row['faces'] for row in sources}
    bodies = state.context['estimate_bodies']
    evidence = module.wglink_send.mesh_evidence(scene.design, bodies, measured, snapshot, memberships)
    module.wglink_mesh.history.remember(tmp_path, 'solve1', 'sha256:one', 'doc',
        evidence['geometry'], snapshot, measured, evidence['estimate'])
    status(tmp_path)
    module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)
    text = items['mesh_estimate'].formattedText
    assert 'Last WG mesh: 19,812 triangles (quarter model)' in text and '–' not in text
    # A new face selection is not painted until Save. Its BRep revision and
    # source selector are unchanged; equal areas must still invalidate evidence.
    scene.faces[1].area = scene.faces[0].area
    items['mesh_faces']._entities = [scene.faces[1]]
    change(scene, inputs, items, state, 'mesh_faces')
    module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)
    text = items['mesh_estimate'].formattedText
    assert '–' in text and 'Last WG mesh:' not in text
    assert 'Last time Waveguide Generator used a quarter model.' in text
    assert not module.wglink_protocol.user_text_violations(text)


def test_history_io_is_advisory_and_advertised_limits_reach_the_dialog(scene, monkeypatch):
    module = scene.module
    inputs, items, state = build(scene)
    monkeypatch.setattr(module.wglink_mesh.history, 'learn', lambda *_a, **_kw: (_ for _ in ()).throw(OSError('read only')))
    monkeypatch.setattr(module.wglink_inbox, 'wg_mesh_limits', lambda *_a: {'solvedTriangles': 1, 'fullDomainWarning': 1})
    assert module.wglink_mesh_dialog._sync_mesh_dialog(inputs, state)
    text = items['mesh_estimate'].formattedText
    assert 'About' in text and 'Limit 1.' in text
    assert '#b77900' in text
    assert 'above the 1 limit' in items['mesh_validation'].formattedText
    assert not module.wglink_protocol.user_text_violations(text)


@pytest.mark.parametrize("summary, shown", [("", []), ("Done.", [("Waveguide Generator", "Done.")])])
def test_a_save_with_nothing_to_say_shows_no_message_box(scene, monkeypatch, summary, shown):
    module = scene.module
    monkeypatch.setattr(module.wglink_mesh_dialog, "_apply_mesh_dialog",
                        lambda _inputs, _state: {"summary": summary})
    module.CommandExecuteHandler("source").notify(types.SimpleNamespace(
        command=types.SimpleNamespace(commandInputs=types.SimpleNamespace()), isValidResult=True))
    assert scene.ui.messages == shown


def test_a_repeated_pick_report_keeps_the_move_notice(scene):
    inputs, items, state = build(scene)
    add_zone(scene, inputs, items, state)
    pick(scene, inputs, items, state, scene.faces[0])
    assert state.notice == "Moved 1 face from HF – high-frequency driver to Size zone 'Fine 1'."
    change(scene, inputs, items, state, "mesh_faces")  # Fusion reports the same selection again
    assert state.notice.startswith("Moved 1 face")
    pick(scene, inputs, items, state, scene.faces[1])  # a new, plain pick clears it
    assert state.notice == ""


def test_preview_repaints_after_fusion_rollback_without_survey_or_settings_writes(scene, monkeypatch):
    module = scene.module
    dialog = module.wglink_mesh_dialog
    inputs, items = _building_inputs(module)
    command, _initial, _minimum = _dialog_command(inputs)
    module.CommandCreatedHandler("source").notify(types.SimpleNamespace(command=command))
    [preview] = command.executePreview.handlers
    assert isinstance(preview, dialog.MeshPreviewHandler) and preview in module._handlers
    state = preview.state
    args = types.SimpleNamespace(command=command, isValidResult=True)
    preview.notify(args)  # before activation: nothing to show yet
    assert args.isValidResult is False and scene.faces[1].override is None
    inputs.activate()
    command.activate.handlers[0].notify(types.SimpleNamespace(command=command))
    target(scene, inputs, items, state, "source", MF)
    pick(scene, inputs, items, state, scene.faces[1])
    generations = (module.wglink_send._source_authoring_generation, module.wglink_send._mesh_authoring_generation)
    causes = []
    monkeypatch.setattr(module.wglink_activity, "because", lambda cause: causes.append(cause) or __import__("contextlib").nullcontext())
    monkeypatch.setattr(module.wglink_send, "preflight_scope",
                        lambda *a, **k: pytest.fail("a colour preview must not survey the model"))
    monkeypatch.setattr(module.wglink_send, "save_mesh_settings",
                        lambda *a, **k: pytest.fail("a colour preview must not publish mesh settings"))
    preview.notify(args)
    # Fusion rolls this back before Save or Cancel; here it shows Save's colour.
    assert scene.faces[1].appearance.name == "MF" and args.isValidResult is False
    assert causes == [module.wglink_activity.command_cause("source")]
    assert (module.wglink_send._source_authoring_generation, module.wglink_send._mesh_authoring_generation) == generations
    # Fusion rolls back the previous preview before every callback, even when
    # only a size/name changed. The next callback must restore the same colour.
    scene.faces[1].appearance = None
    items[f"mesh_source_size_{MF}"].value = 2
    preview.notify(args)
    assert scene.faces[1].appearance.name == "MF"
    assert state.preview_failure is None
    assert (module.wglink_send._source_authoring_generation, module.wglink_send._mesh_authoring_generation) == generations
