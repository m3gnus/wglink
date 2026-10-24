"""Exhaust every document API step, including restoration and nested edits."""
from __future__ import annotations

import types

import pytest

from test_wglink_fem_dialog import _inputs, _state, _building_inputs
from test_wglink_addin_lifecycle import _dialog_inputs, _chosen, _SelectionInput
from test_wglink_mesh_dialog import build, change, scene  # noqa: F401
from test_wglink_mesh_settings import AppearanceFace
from test_wglink_send import Attributes, body, component


class Faults:
    def __init__(self):
        self.trace = []
        self.target = None
        self.mode = "before"
        self.enabled = False
        self.triggered = False

    def call(self, label, write, call):
        if not self.enabled:
            return call()
        self.trace.append((label, write))
        fail = len(self.trace) == self.target
        if fail:
            self.triggered = True
            if write and self.mode == "refuse":
                return False
            if self.mode != "after" or not write:
                raise RuntimeError("Injected " + label)
        result = call()
        if fail:
            raise RuntimeError("Injected after " + label)
        return result


class Handles:
    def __init__(self, faults, attributes):
        self.faults, self.attributes = faults, attributes
        self.values = attributes.values

    def itemByName(self, group, name):
        def lookup():
            attribute = self.attributes.itemByName(group, name)
            if attribute is None:
                return None
            owner = self

            class Handle:
                parent = None

                @property
                def value(self):
                    return owner.faults.call("attribute.value:" + name, False, lambda: attribute.value)

                @value.setter
                def value(self, value):
                    owner.faults.call("attribute.set:" + name, True, lambda: setattr(attribute, "value", value))

                def deleteMe(self):
                    return owner.faults.call("attribute.delete:" + name, True, attribute.deleteMe)

            handle = Handle()
            handle.name = name
            return handle
        return self.faults.call("attribute.lookup:" + name, False, lookup)

    def add(self, group, name, value):
        return self.faults.call("attribute.add:" + name, True,
                                lambda: self.attributes.add(group, name, value))


class Items:
    def __init__(self, faults, values):
        self.faults, self.values = faults, values

    @property
    def count(self):
        return self.faults.call("collection.count", False, lambda: len(self.values))

    def item(self, index):
        return self.faults.call("collection.item", False, lambda: self.values[index])

    def __iter__(self):
        for index in range(self.count):
            yield self.item(index)


class Face(AppearanceFace):
    @property
    def appearance(self):
        return self.faults.call("face.appearance", False,
                               lambda: AppearanceFace.appearance.fget(self))

    @appearance.setter
    def appearance(self, value):
        self.faults.call("face.paint", True,
                         lambda: AppearanceFace.appearance.fset(self, value))

    @property
    def appearanceSourceType(self):
        return self.faults.call("face.appearanceSourceType", False,
                               lambda: AppearanceFace.appearanceSourceType.fget(self))


class Entity(types.SimpleNamespace):
    def __getattribute__(self, name):
        if name in ("faces", "attributes", "rootComponent", "appearances"):
            fields = object.__getattribute__(self, "__dict__")
            return fields["faults"].call("entity." + name, False,
                                        lambda: object.__getattribute__(self, name))
        return object.__getattribute__(self, name)


class Asset(types.SimpleNamespace):
    def __getattribute__(self, name):
        if name in ("name", "id"):
            fields = object.__getattribute__(self, "__dict__")
            return fields["faults"].call("appearance." + name, False,
                                        lambda: object.__getattribute__(self, name))
        return object.__getattribute__(self, name)


class Assets:
    def __init__(self, faults):
        self.faults = faults
        self.values = {}

    def itemByName(self, name):
        return self.faults.call("appearance.lookup:" + name, False, lambda: self.values.get(name))

    def itemById(self, key):
        return self.faults.call("appearance.id", False,
                               lambda: next((a for a in self.values.values() if a.id == key), None))

    def named(self, _app, _design, name):
        existing = self.itemByName(name)
        if existing is not None:
            return existing
        def add():
            asset = Asset(id=name, name=name, faults=self.faults)
            def delete():
                return self.faults.call("appearance.delete", True,
                                        lambda: self.values.pop(name, None) is not None)
            asset.deleteMe = delete
            self.values[name] = asset
            return asset
        return self.faults.call("appearance.add:" + name, True, add)


def document(scene, monkeypatch):
    module = scene.module
    monkeypatch.setattr(module, "_source_identity_enabled", lambda: True)
    faults = Faults()
    assets = Assets(faults)
    inherited = Asset(id="grey", name="Body paint", faults=faults)
    assets.values["grey"] = inherited
    faces = []
    for index in range(3):
        member = Face(inherited)
        member.faults = faults
        member.attributes = Handles(faults, Attributes())
        member.entityToken = f"face-{index}"
        faces.append(member)
    assets.values["HF"] = Asset(id="HF", name="HF", faults=faults)
    faces[0].override = assets.values["HF"]
    air = Entity(**vars(body("Air", faces=[])), faults=faults)
    air.entityToken = "air"
    air.faces = Items(faults, faces)
    air.attributes = Handles(faults, Attributes())
    for member in faces:
        member.body = air
    fem_faces = []
    for index in range(3):
        member = Face(inherited)
        member.faults = faults
        member.attributes = Handles(faults, Attributes())
        member.entityToken = f"fem-face-{index}"
        member.area = index + 1
        fem_faces.append(member)
    fem_air = Entity(**vars(body("FEM air", faces=[])), faults=faults)
    fem_air.entityToken = "fem-air"
    fem_air.faces = Items(faults, fem_faces)
    fem_air.attributes = Handles(faults, Attributes())
    for member in fem_faces:
        member.body = fem_air
    second_faces = []
    second_air = Entity(**vars(body("Side air", faces=[])), faults=faults)
    second_air.entityToken = "side-air"
    second_air.attributes = Handles(faults, Attributes())
    for index in range(3):
        member = Face(inherited)
        member.faults = faults
        member.attributes = Handles(faults, Attributes())
        member.entityToken = f"side-face-{index}"
        member.area, member.body = index + 1, second_air
        second_faces.append(member)
    second_air.faces = Items(faults, second_faces)
    root = Entity(**vars(component("Assembly", [air, fem_air, second_air])), faults=faults)
    root.attributes = Handles(faults, Attributes())
    def find(group, name):
        def lookup():
            found = []
            for entity in [root, air, *faces, fem_air, *fem_faces, second_air, *second_faces]:
                attribute = entity.attributes.itemByName(group, name)
                if attribute is not None:
                    attribute.parent = entity
                    found.append(attribute)
            return Items(faults, found)
        return faults.call("design.findAttributes:" + name, False, lookup)
    design = Entity(rootComponent=root, appearances=assets, findAttributes=find, faults=faults)
    scene.design, scene.faces, scene.cabinet = design, faces, air
    scene.fem_air, scene.fem_faces = fem_air, fem_faces
    scene.second_air = second_air
    scene.app.activeProduct = design
    monkeypatch.setattr(module.wglink_core, "_design", lambda _app: design)
    monkeypatch.setattr(module.wglink_core, "_named_appearance", assets.named)
    counter = [0]
    def uuid():
        counter[0] += 1
        return types.SimpleNamespace(hex=f"{counter[0]:032x}")
    monkeypatch.setattr(module.uuid, "uuid4", uuid)
    monkeypatch.setattr(module.wglink_send, "_mint_source_identity", lambda: "source-" + uuid().hex[-16:])
    # Seed non-default source sizing, group override history, and FEM metadata.
    inputs, items, state = build(scene)
    items["mesh_source_size_1"].value = 1.1
    change(scene, inputs, items, state, "mesh_add_group")
    items["mesh_group_name_2"].value = "Old panel"
    items["mesh_group_faces_2"].addSelection(faces[1])
    module._apply_mesh_dialog(inputs, state)
    module._apply_fem_region(_inputs(module, fem_air, fem_faces), _state(module, 1, 2))
    second_state = _state(module, 1, 2)
    second_state.region_id = "side-air"
    module._apply_fem_region(_inputs(module, second_air, second_faces,
        fem_name=types.SimpleNamespace(value="Side air")), second_state)
    module.wglink_send.declare_body(air, "exterior-shell")
    module.wglink_send.declare_body(fem_air, "exterior-shell")
    entities = [root, air, *faces, fem_air, *fem_faces, second_air, *second_faces]
    initial_attributes = [dict(entity.attributes.values) for entity in entities]
    initial_overrides = [member.override for member in faces]
    initial_assets = dict(assets.values)
    def reset():
        faults.enabled = False
        faults.trace = []
        faults.triggered = False
        faults.target = None
        scene.ui.messages.clear()
        counter[0] = 100
        for entity, original in zip(entities, initial_attributes):
            entity.attributes.values.clear()
            entity.attributes.values.update(original)
        for member, original in zip(faces, initial_overrides):
            member.override = original
        assets.values.clear()
        assets.values.update(initial_assets)
    def snapshot():
        return ([dict(entity.attributes.values) for entity in entities],
                [member.override.id if member.override else None for member in faces],
                sorted((key, asset.id) for key, asset in assets.values.items()))
    return faults, reset, snapshot


def operation(scene, path):
    module = scene.module
    send = module.wglink_send
    air, faces = scene.cabinet, scene.faces
    if path == "mesh-restore-save":
        inputs, _items, state = build(scene)
        return module._apply_mesh_dialog(inputs, state)
    if path == "mesh-save-and-restore":
        inputs, items, state = build(scene)
        items["mesh_source_size_1"].value = 2.2
        change(scene, inputs, items, state, "mesh_remove_group_2")
        change(scene, inputs, items, state, "mesh_add_source")
        items["mesh_source_faces_3"].addSelection(faces[1])
        change(scene, inputs, items, state, "mesh_add_group")
        items["mesh_group_name_4"].value = "New panel"
        items["mesh_group_faces_4"].addSelection(faces[2])
        return module._apply_mesh_dialog(inputs, state)
    if path.startswith("fem-"):
        air, faces = scene.fem_air, scene.fem_faces
    if path == "fem-save":
        inputs = _inputs(module, air, faces)
        inputs.itemById("fem_name").value = "Edited air"
        return module._apply_fem_region(inputs, _state(module, 1, 2))
    if path == "fem-restore-save":
        inputs, _ = _building_inputs(module)
        state = module.FemDialogState()
        module._build_fem_dialog(inputs, state)
        module._restore_fem_region(inputs, state, air)
        inputs.itemById("fem_body").addSelection(air)
        return module._apply_fem_region(inputs, state)
    if path == "fem-clear":
        return send.clear_fem_region(air)
    if path == "fem-bulk-clear":
        return send.clear_fem_regions([air, scene.second_air])
    if path == "source-assign":
        return send.assign_source_identity(scene.design, [faces[0], faces[2]], "HF")
    if path == "source-adopt":
        return send._adopt_painted_source(scene.design, [faces[2]], "MF", None)
    if path == "source-save":
        return module._apply_source_faces([faces[2]], "MF")
    if path == "source-clear":
        return module._apply_source_faces([faces[0]], module.wglink_author.CLEAR_SOURCE_LABEL)
    if path == "group-clear-restore":
        return send.paint_surface_group(scene.app, scene.design, [faces[1]], None)
    if path == "group-save":
        return send.paint_surface_group(scene.app, scene.design, [faces[2]], "new-panel")
    if path in ("body-dialog-save", "body-dialog-clear"):
        choice = module.wglink_author.declaration_choices()[1 if path.endswith("save") else 2]
        return module._apply_body_declaration(_dialog_inputs(
            declare_bodies=_SelectionInput([scene.cabinet, scene.fem_air]),
            declaration=_chosen(choice)))
    if path == "body-save":
        return send.declare_body(air, "exclude")
    if path == "body-clear":
        return send.clear_declaration(air)
    if path == "mesh-settings-save":
        mesh = send.read_mesh_settings(scene.design)
        mesh["wall_size_mm"] = 41
        return send.save_mesh_settings(scene.design, mesh)
    raise AssertionError(path)


PATHS = ["mesh-restore-save", "mesh-save-and-restore", "fem-save", "fem-restore-save", "fem-clear", "fem-bulk-clear",
         "source-assign", "source-adopt", "source-save", "source-clear",
         "group-clear-restore", "group-save", "body-save", "body-clear",
         "body-dialog-save", "body-dialog-clear", "mesh-settings-save"]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("mode", ["before", "after", "refuse"])
def test_every_document_read_and_write_is_atomic_and_honestly_reported(scene, monkeypatch, path, mode):
    faults, reset, snapshot = document(scene, monkeypatch)
    reset()
    before = snapshot()
    faults.enabled = True
    operation(scene, path)
    faults.enabled = False
    expected = snapshot()
    trace = list(faults.trace)
    assert trace
    for index, (label, write) in enumerate(trace, 1):
        if mode != "before" and not write:
            continue
        reset()
        faults.target, faults.mode, faults.enabled = index, mode, True
        error = None
        try:
            operation(scene, path)
        except Exception as exc:
            error = exc
        finally:
            faults.enabled = False
        assert faults.triggered, (path, index, label)
        actual = snapshot()
        assert actual == before or actual == expected, (path, mode, index, label, str(error))
        if error is None:
            assert actual == expected, (path, mode, index, label, "false success")
        elif "changes were restored" in str(error) or "Nothing was changed" in str(error):
            assert actual == before, (path, mode, index, label, "false restoration")
        assert scene.module.wglink_send._active_document_edit is None


@pytest.mark.parametrize("path", PATHS)
def test_second_fault_during_rollback_never_claims_success(scene, monkeypatch, path):
    """A refused recovery can leave changes; the message must direct Fusion Undo."""
    faults, reset, snapshot = document(scene, monkeypatch)
    edit_type = scene.module.wglink_send._DocumentEdit
    verify, rollback = edit_type.verify, edit_type.rollback
    rollback_start = []
    def fail_commit(edit):
        verify(edit)
        raise RuntimeError("Injected commit failure")
    def record_rollback(edit):
        rollback_start.append(len(faults.trace))
        return rollback(edit)
    monkeypatch.setattr(edit_type, "verify", fail_commit)
    monkeypatch.setattr(edit_type, "rollback", record_rollback)
    reset()
    before = snapshot()
    faults.enabled = True
    with pytest.raises(Exception):
        operation(scene, path)
    faults.enabled = False
    trace = list(faults.trace)
    assert snapshot() == before
    assert rollback_start
    start = rollback_start[-1]
    for index, (label, write) in enumerate(trace[start:], start + 1):
        for mode in (["before", "after", "refuse"] if write else ["before"]):
            reset()
            faults.target, faults.mode, faults.enabled = index, mode, True
            with pytest.raises(Exception) as caught:
                operation(scene, path)
            faults.enabled = False
            assert faults.triggered, (path, index, label)
            actual = snapshot()
            message = str(caught.value)
            if "changes were restored" in message:
                assert actual == before, (path, index, label, "false restoration")
            else:
                assert "Undo this command in Fusion" in message, (path, index, label, message)
