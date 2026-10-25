"""Exhaust every document API step, including restoration and nested edits."""
from __future__ import annotations

import types

import pytest

from test_wglink_fem_dialog import _inputs, _state, _building_inputs
from test_wglink_addin_lifecycle import _dialog_inputs, _chosen, _SelectionInput
from test_wglink_mesh_dialog import MF, add_zone, build, change, pick, scene, target  # noqa: F401
from test_wglink_mesh_settings import AppearanceFace
from test_wglink_send import Attributes, body, component


class Fresh:
    """A new Python wrapper per lookup, as Fusion mints one for every read.

    Two wrappers of one entity compare equal (Fusion's ``==``) but are distinct
    objects with distinct ``id`` and hash, so a journal keyed on wrapper identity
    misses the entity it already holds.
    """

    def __init__(self, target):
        object.__setattr__(self, "_target", target)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_target"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_target"), name, value)

    def __eq__(self, other):
        return unwrap(self) is unwrap(other)

    def __ne__(self, other):
        return not self == other

    __hash__ = object.__hash__


def unwrap(value):
    while isinstance(value, Fresh):
        value = object.__getattribute__(value, "_target")
    return value


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
        return self.faults.call("collection.item", False, lambda: Fresh(self.values[index]))

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
            value = fields["faults"].call("entity." + name, False,
                                          lambda: object.__getattribute__(self, name))
            return value if name == "faces" else Fresh(value)
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
        def lookup():
            found = self.values.get(name)
            return Fresh(found) if found is not None else None
        return self.faults.call("appearance.lookup:" + name, False, lookup)

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
    # An explicit override equal to the inherited appearance must restore as
    # an override, not collapse to "inherited".
    faces[2].override = inherited
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
                    attribute.parent = Fresh(entity)
                    found.append(attribute)
            return Items(faults, found)
        return faults.call("design.findAttributes:" + name, False, lookup)
    design = Entity(rootComponent=root, appearances=assets, findAttributes=find, faults=faults)
    scene.design, scene.faces, scene.cabinet = design, faces, air
    scene.fem_air, scene.fem_faces = fem_air, fem_faces
    scene.second_air = second_air
    scene.app.activeProduct = design
    # Every lookup mints a new design wrapper, as Design.cast(app.activeProduct) does.
    # Fusion wrappers are adsk.core.Base objects, compared by entity with ==.
    monkeypatch.setattr(module.adsk.core, "Base", Fresh, raising=False)
    monkeypatch.setattr(module.wglink_core, "_design", lambda _app: Fresh(design))
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
    zone = add_zone(scene, inputs, items, state)
    items[f"mesh_group_name_{zone}"].value = "Old panel"
    pick(scene, inputs, items, state, faces[1])
    module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    module._apply_fem_region(_inputs(module, fem_air, fem_faces), _state(module, 1, 2))
    second_state = _state(module, 1, 2)
    second_state.region_id = "side-air"
    module._apply_fem_region(_inputs(module, second_air, second_faces,
        fem_name=types.SimpleNamespace(value="Side air")), second_state)
    module.wglink_send.declare_body(air, "exterior-shell")
    module.wglink_send.declare_body(fem_air, "exterior-shell")
    entities = [root, air, *faces, fem_air, *fem_faces, second_air, *second_faces]
    initial_attributes = [dict(entity.attributes.values) for entity in entities]
    painted = [*faces, *fem_faces, *second_faces]
    initial_overrides = [member.override for member in painted]
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
        for member, original in zip(painted, initial_overrides):
            member.override = original
        assets.values.clear()
        assets.values.update(initial_assets)
    def snapshot():
        return ([dict(entity.attributes.values) for entity in entities],
                [member.override.id if member.override else None for member in painted],
                sorted((key, asset.id) for key, asset in assets.values.items()))
    return faults, reset, snapshot


def operation(scene, path):
    module = scene.module
    send = module.wglink_send
    air, faces = scene.cabinet, scene.faces
    if path == "mesh-restore-save":
        inputs, _items, state = build(scene)
        return module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
    if path == "mesh-save-and-restore":
        inputs, items, state = build(scene)
        items["mesh_source_size_1"].value = 2.2
        change(scene, inputs, items, state, f"mesh_remove_group_{next(iter(state.groups))}")
        target(scene, inputs, items, state, "source", MF)
        pick(scene, inputs, items, state, faces[1])
        zone = add_zone(scene, inputs, items, state)
        items[f"mesh_group_name_{zone}"].value = "New panel"
        pick(scene, inputs, items, state, faces[2])
        return module.wglink_mesh_dialog._apply_mesh_dialog(inputs, state)
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


SEND_PATHS = {"fem-bulk-clear", "source-adopt"}


def is_refusal(scene, error):
    """A refusal is shown verbatim. Any other exception reaches the command's
    unexpected-error boundary, which logs it (opening a dialog, restoring a
    preset); only refusals are held to plain text here."""
    module = scene.module
    return isinstance(error, (module.wglink_core.WgLinkError, module.wglink_author.AuthorError))


def assert_plain(message, path):
    """A refusal names no Fusion internals and the remedy fits where it ran."""
    for internal in ("Injected", "journaled", "assetInst", "wglink_", "mesh_group_", "_attribute",
                     "restoration checks", "RuntimeError", ".."):
        assert internal not in message, (path, message)
    if path in SEND_PATHS:
        assert "dialog" not in message and "Sources & Mesh" not in message, (path, message)
    assert message.count("Close and reopen") <= 1, (path, message)


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
        if is_refusal(scene, error):
            assert_plain(str(error), path)
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
            if is_refusal(scene, caught.value):
                assert_plain(message, path)
            if "changes were restored" in message:
                assert actual == before, (path, index, label, "false restoration")
            else:
                assert "Undo this command in Fusion" in message, (path, index, label, message)


def _journal_entities(send, count, token=lambda index: f"face-{index}"):
    entities = []
    for index in range(count):
        entity = types.SimpleNamespace(attributes=Attributes(), entityToken=token(index))
        entities.append(entity)
    return entities


def test_journal_lookups_do_not_compare_every_entry(scene, monkeypatch):
    """A large body journals and writes in linear time: no Fusion == per entry."""
    send = scene.module.wglink_send
    comparisons = []
    same = send._same_live_entity
    monkeypatch.setattr(send, "_same_live_entity",
                        lambda left, right: comparisons.append(1) or same(left, right))
    faces = _journal_entities(send, 400)
    send.document_edit(
        "save many faces",
        lambda edit: [edit.remember_attribute(face, "role") for face in faces + faces],
        lambda edit: [edit.set_attribute(Fresh(face), "role", "HF") for face in faces])
    assert all(face.attributes.values == {("WGLink", "role"): "HF"} for face in faces)
    assert comparisons == []


def test_one_entity_seen_with_two_tokens_is_still_one_verified_change(scene, monkeypatch):
    """Autodesk: two token reads of one entity may differ. Entity equality
    settles a write through the second token and the final readback."""
    send = scene.module.wglink_send
    monkeypatch.setattr(scene.module.adsk.core, "Base", Fresh, raising=False)
    (face,) = _journal_entities(send, 1)
    first, second = Fresh(face), Fresh(face)

    def prepare(edit):
        edit.remember_attribute(first, "role")
        face.entityToken = "face-0-reread"
        edit.remember_attribute(second, "role")  # a second entry: token differs

    def mutate(edit):
        edit.set_attribute(Fresh(face), "role", "LF")  # by token: the second entry
        face.entityToken = "face-0-third"
        edit.set_attribute(Fresh(face), "role", "MF")  # no token match: by equality

    send.document_edit("save one face", prepare, mutate)
    assert face.attributes.values == {("WGLink", "role"): "MF"}
