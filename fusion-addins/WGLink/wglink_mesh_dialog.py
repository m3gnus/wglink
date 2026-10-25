"""The Sources & Mesh… dialog: building it, reading it, validating it, saving it.

Layout: a Drivers table with one fixed row per role
(``wglink_protocol.DRIVER_LABELS`` order), a Size zones table, and one face
picker that fills whichever row is the target. The dialog's state holds each
row's faces; the picker only shows the target row's. Save runs the shared
writers (``wglink_send.apply_source_faces``, ``paint_surface_group``) under
one ``document_edit``; preview reuses only Save's face assignment phase so
colours show while picking, and Fusion rolls that back. Main thread only.

What only the entry module can provide (error reporting, the Text Commands
log, WG's advertised capabilities, the document identity, the command's
name) comes in as ``MeshDialogServices``.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import math
from typing import Any, Callable
import uuid

import adsk.core

if __package__:
    from . import wglink_activity, wglink_author, wglink_core, wglink_dialog_inputs, wglink_inbox
    from . import wglink_mesh, wglink_protocol, wglink_send, wglink_workspace
else:
    import wglink_activity
    import wglink_author
    import wglink_core
    import wglink_dialog_inputs
    import wglink_inbox
    import wglink_mesh
    import wglink_protocol
    import wglink_send
    import wglink_workspace


MESH_DIALOG_TITLE = "Sources & Mesh — Waveguide Generator"
MESH_DIALOG_RETRY = "Close and reopen Sources & Mesh… to retry before saving."
PICKER = "mesh_faces"
DRIVERS_TABLE = "mesh_sources_table"
ZONES_TABLE = "mesh_groups_table"
LINKED_SUFFIX = " (from the Waveguide Generator design)"
FIRST_PICK_HINT = "Choose a driver row's Pick faces, then click its faces in the model."


@dataclass(frozen=True)
class MeshDialogServices:
    """What the entry module lends the dialog."""

    report_error: Callable[[str, BaseException], None]
    log: Callable[[str], None]
    active_document_id: Callable[[], Any]
    source_identity_enabled: Callable[[], bool]
    automatic_domain_enabled: Callable[[], bool]
    command_name: str


class MeshDialogState:
    """The dialog's truth. Rows hold their faces; the one picker only shows the target's."""

    def __init__(self, services: MeshDialogServices):
        self.services = services
        self.command = None
        self.construction_failed = False
        self.sources = {}
        self.groups = {}
        self.next_id = 1
        self.restoring = False
        self.context = {}
        self.original_sources = []
        self.original_groups = []
        self.saved_settings = None
        self.preview_error = None
        # Why the last preview showed no colours (the settings could not be saved yet).
        self.preview_failure = None
        # A FEM region with a moving surface drives the model without a source.
        self.fem_driven = False
        self.activated = False
        # Whether the user has changed anything: before that, guidance, not errors.
        self.touched = False
        # ("source" | "group", row_id) whose faces the picker shows and edits.
        self.target = None
        # Zone row ids in table order.
        self.group_order = []
        self.notice = ""
        # Read-only texts as last set: Fusion reports every set as an input change.
        self.shown = {}


def _mesh_faces(entities):
    faces = {}
    for entity in entities:
        for face in wglink_core._items(entity.faces, strict=True) if hasattr(entity, "faces") else [entity]:
            faces.setdefault(wglink_send._face_key(face), face)
    return list(faces.values())


def _keys(faces):
    return [wglink_send._face_key(face) for face in faces]


def _mesh_dropdown(inputs, input_id, name, choices, selected):
    dropdown = inputs.addDropDownCommandInput(
        input_id, name, adsk.core.DropDownStyles.TextListDropDownStyle
    )
    for choice in choices:
        dropdown.listItems.add(choice, choice == selected)
    return dropdown


def _mesh_button(inputs, input_id, name, *, full_width=True):
    button = inputs.addBoolValueInput(input_id, name, False, "", False)
    button.text = name
    button.isFullWidth = full_width
    return button


def _rows(state, kind):
    return state.sources if kind == "source" else state.groups


def _row_label(kind, record):
    if kind == "group":
        return f"Size zone '{record.get('name') or 'unnamed'}'"
    label = wglink_protocol.driver_label(record["role"])
    return label + LINKED_SUFFIX if record.get("instance_id") else label


def _face_count_text(count):
    return f"{count} {wglink_protocol._plural(count, 'face', 'faces')}"


def _count_text(kind, record, target):
    faces = len(record["faces"])
    text = _face_count_text(faces) if faces else ("not used" if kind == "source" else "no faces")
    return f"<b>{text} ◀</b>" if target else html.escape(text)


def _add_mesh_row(inputs, state, kind, record=None, faces=()):
    """One table row: label or name, size, face count, Pick faces, and ✕ (clear or remove)."""

    record = dict(record or {})
    row_id = state.next_id
    state.next_id += 1
    is_source = kind == "source"
    linked = bool(record.get("instance_id")) if is_source else False
    rows = _rows(state, kind)
    rows[row_id] = {**record, "row_id": row_id, "original_faces": list(faces), "faces": list(faces)}
    table = inputs.itemById(DRIVERS_TABLE if is_source else ZONES_TABLE)
    cells = table.commandInputs
    index = table.rowCount
    if is_source:
        role = wglink_protocol.canonical_source_role(record.get("role", "HF"))
        rows[row_id]["role"] = role
        name = cells.addTextBoxCommandInput(f"mesh_source_label_{row_id}", "",
                                            html.escape(_row_label(kind, rows[row_id])), 1, True)
        size = record.get("size_mm", wglink_protocol.default_source_size_mm(role))
    else:
        name = cells.addStringValueInput(f"mesh_group_name_{row_id}", "", record.get("name", ""))
        size = record.get("size_mm", wglink_protocol.MESH_PRESETS["Fine"])
        rows[row_id].setdefault("group_id", uuid.uuid4().hex)
        state.group_order.append(row_id)
    value = cells.addValueInput(f"mesh_{kind}_size_{row_id}", "", "mm", wglink_dialog_inputs.mm_input(size))
    value.isEnabled = not linked
    value.tooltip = ("Mesh size on this driver's faces. Smaller is more accurate but slower."
                     if is_source else "Mesh size on this size zone's faces.")
    count = cells.addTextBoxCommandInput(f"mesh_{kind}_count_{row_id}", "", "", 1, True)
    row_inputs = [name, value, count]
    if not linked:
        # Linked membership comes from Waveguide Generator; there is nothing to pick here.
        row_inputs.append(_mesh_button(cells, f"mesh_pick_{kind}_{row_id}", "Pick faces", full_width=False))
        clear = _mesh_button(cells, f"mesh_{'clear_source' if is_source else 'remove_group'}_{row_id}", "✕",
                             full_width=False)
        clear.tooltip = ("Stop using this driver. Its faces go back to the wall size." if is_source
                         else "Remove this size zone. Its faces go back to the wall size.")
        row_inputs.append(clear)
    for column, item in enumerate(row_inputs):
        table.addCommandInput(item, index, column)
    return row_id


def _next_zone_name(state, preset):
    names = {str(record.get("name", "")).casefold() for record in state.groups.values()}
    serial = 1
    while f"{preset} {serial}".casefold() in names:
        serial += 1
    return f"{preset} {serial}"


def _table_rows(state, kind):
    return list(state.sources) if kind == "source" else list(state.group_order)


def _set_target(inputs, state, kind, row_id, *, notice="", focus_picker=True):
    """Point the one picker at a row, optionally giving it focus.

    The notice of an earlier action ends here, unless the retarget is itself
    what the notice reports.
    """

    state.target = (kind, row_id)
    state.notice = notice
    record = _rows(state, kind)[row_id]
    picker = inputs.itemById(PICKER)
    restoring, state.restoring = state.restoring, True
    try:
        picker.clearSelection()
        for face in record["faces"]:
            picker.addSelection(face)
        # One selected row across both tables: the target's.
        for table_id, table_kind in ((DRIVERS_TABLE, "source"), (ZONES_TABLE, "group")):
            table = inputs.itemById(table_id)
            table.selectedRow = (_table_rows(state, kind).index(row_id) if table_kind == kind else -1)
        if focus_picker:
            picker.hasFocus = True
    finally:
        state.restoring = restoring
    inputs.itemById("mesh_target").formattedText = (
        f"Picking faces for: <b>{html.escape(_row_label(kind, record))}</b>")


def _selected_row_changed(inputs, state, kind, row_id, *, focus_picker=True):
    """One row-selection path for table events and their child-cell events."""
    if _rows(state, kind)[row_id].get("instance_id"):
        # A linked row can be highlighted, but cannot take ownership of picks.
        _set_target(inputs, state, *state.target, notice=state.notice, focus_picker=False)
    elif state.target != (kind, row_id):
        _set_target(inputs, state, kind, row_id, focus_picker=focus_picker)


def _row_cell_changed(inputs, state, input_id):
    """Fusion reports the clicked table cell, not its TableCommandInput id.

    The event can arrive before ``selectedRow`` changes. The stable child id
    owns the target; our own display updates run under ``state.restoring``.
    """
    for kind, fields in (("source", ("label", "count", "size")),
                         ("group", ("name", "count", "size"))):
        for field in fields:
            prefix = f"mesh_{kind}_{field}_"
            if not input_id.startswith(prefix):
                continue
            row_id = int(input_id[len(prefix):])
            if row_id in _rows(state, kind):
                _selected_row_changed(inputs, state, kind, row_id,
                                      focus_picker=field not in ("name", "size"))
            return True
    return False


def _read_zone_names(inputs, state):
    """Size zone names as typed, so notices and labels use what the user sees."""

    for row_id, record in state.groups.items():
        item = inputs.itemById(f"mesh_group_name_{row_id}")
        if item is not None:
            record["name"] = str(item.value).strip()


def _picked(inputs, state):
    """Apply the picker's contents to the target row.

    A face picked here that another row holds moves here, and the notice says
    so. A whole body (size zones only) adds its faces that no other row
    holds; a linked driver's faces never move, they change in Waveguide
    Generator.
    """

    _read_zone_names(inputs, state)
    kind, row_id = state.target
    target = _rows(state, kind)[row_id]
    owners = {wglink_send._face_key(face): (other_kind, record)
              for other_kind in ("source", "group")
              for other_id, record in _rows(state, other_kind).items()
              if (other_kind, other_id) != state.target for face in record["faces"]}
    entities = wglink_dialog_inputs.selected_entities(inputs, PICKER)
    bodies = [entity for entity in entities if hasattr(entity, "faces")]
    notices, reload_picker = [], bool(bodies)
    body_faces = []
    if bodies and kind == "source":
        notices.append("A driver takes faces, not whole bodies.")
    elif bodies:
        body_faces = _mesh_faces(bodies)
        kept_elsewhere = [face for face in body_faces if wglink_send._face_key(face) in owners]
        if kept_elsewhere:
            notices.append(f"{_face_count_text(len(kept_elsewhere))} of that body stayed in other rows.")
    candidates = _mesh_faces([
        *(entity for entity in entities if not hasattr(entity, "faces")),
        *(face for face in body_faces if wglink_send._face_key(face) not in owners),
    ])
    kept, moved = [], {}
    for face in candidates:
        key = wglink_send._face_key(face)
        owner_kind, owner = owners.get(key, (None, None))
        if owner is not None and owner.get("instance_id"):
            notices.append(f"{_row_label(owner_kind, owner)} keeps its faces; change them in Waveguide Generator.")
            reload_picker = True
            continue
        if owner is not None:
            owner["faces"] = [member for member in owner["faces"] if wglink_send._face_key(member) != key]
            label = _row_label(owner_kind, owner)
            moved[label] = moved.get(label, 0) + 1
        kept.append(face)
    unchanged = _keys(kept) == _keys(target["faces"])
    target["faces"] = kept
    notices.extend(f"Moved {_face_count_text(count)} from {label} to {_row_label(kind, target)}."
                   for label, count in moved.items())
    if notices or not unchanged:
        # A repeated report of the same picks (Fusion can send one per
        # programmatic change) must not wipe the notice of the real change.
        state.notice = " ".join(notices)
        state.touched = True
    if reload_picker:
        # Show exactly what the row now holds: faces, never a body or a refused face.
        _set_target(inputs, state, kind, row_id, notice=state.notice)


def _clear_driver(inputs, state, row_id):
    record = state.sources[row_id]
    record["faces"] = []
    # Clearing also lets go of saved faces Fusion could not select (hidden bodies).
    record["unselected_saved_faces"] = set()
    state.touched = True
    notice = f"{_row_label('source', record)} is not used."
    if state.target == ("source", row_id):
        _set_target(inputs, state, "source", row_id, notice=notice)
    else:
        state.notice = notice


def _remove_zone(inputs, state, row_id):
    index = state.group_order.index(row_id)
    record = state.groups[row_id]
    _read_zone_names(inputs, state)
    inputs.itemById(ZONES_TABLE).deleteRow(index)
    state.group_order.remove(row_id)
    del state.groups[row_id]
    state.touched = True
    faces = len(record["faces"])
    notice = f"Removed {_row_label('group', record)}" + (
        f"; its {_face_count_text(faces)} go back to the wall size." if faces else ".")
    if state.target != ("group", row_id):
        state.notice = notice
        return
    if state.group_order:
        neighbour = state.group_order[min(index, len(state.group_order) - 1)]
        _set_target(inputs, state, "group", neighbour, notice=notice)
    else:
        _set_target(inputs, state, *_initial_target(state), notice=notice)


def _check_saved_faces(inputs, state):
    """On activate: which saved faces Fusion can still select, row by row, with the one picker."""

    picker = inputs.itemById(PICKER)
    for kind in ("source", "group"):
        for record in _rows(state, kind).values():
            if record.get("instance_id"):
                continue
            picker.clearSelection()  # also drops faces preselected before opening
            selectable, missing = [], []
            for face in record["original_faces"]:
                try:
                    selected = picker.addSelection(face)
                except Exception:
                    selected = False
                (selectable if selected else missing).append(face)
            record["faces"] = selectable
            record["unselected_saved_faces"] = {wglink_send._face_key(face) for face in missing}
    picker.clearSelection()


def _initial_target(state):
    for kind in ("source", "group"):
        for row_id, record in _rows(state, kind).items():
            if record["faces"] and not record.get("instance_id"):
                return kind, row_id
    return "source", next(row_id for row_id, record in state.sources.items() if not record.get("instance_id"))


def _build_mesh_dialog(inputs, state):
    state.restoring = True
    try:
        design = wglink_core._design(adsk.core.Application.get())
        # Resolve saved settings before the fail-soft source preview can read
        # them. A failed read leaves construction guarded until the next open.
        try:
            saved = wglink_send.read_mesh_settings(design)
        except wglink_core.WgLinkError as exc:
            raise wglink_core.WgLinkError(f"{exc} {MESH_DIALOG_RETRY}") from exc
        state.saved_settings = saved
        try:
            wglink_send._stamp_attributes(design)
            report = wglink_send.preflight_scope(
                adsk.core.Application.get(),
                {"source_identity": state.services.source_identity_enabled(), "display_automatic_domain": True,
                 "automatic_domain": state.services.automatic_domain_enabled()},
                authoring_context=state.context,
            )
        except wglink_send.SourceIdentityReadError as exc:
            raise wglink_core.WgLinkError(f"{exc} {MESH_DIALOG_RETRY}") from exc
        state.preview_error = report.get("source_error")
        state.fem_driven = report.get("fem_driven") is True
        sources = state.context.get("sources", [])
        mesh = wglink_mesh.snapshot(sources, saved)
        # What blocks Save, shown first so it stays in view above the tables.
        inputs.addTextBoxCommandInput("mesh_validation", "", "", 3, True).isFullWidth = True
        for input_id, title, expanded in (
            ("mesh_sources", "Drivers", True), ("mesh_walls", "Walls", True),
            ("mesh_groups", "Size zones", True), ("mesh_advanced", "Advanced", False),
            ("mesh_estimate_section", "Estimate", True),
        ):
            group = inputs.addGroupCommandInput(input_id, title)
            group.isExpanded = expanded
            if input_id == "mesh_groups":
                group.children.addTextBoxCommandInput(
                    "mesh_group_note", "",
                    "Optional: make parts of the model finer or coarser than the walls.", 1, True)
            if input_id in ("mesh_sources", "mesh_groups"):
                is_source = input_id == "mesh_sources"
                table = group.children.addTableCommandInput(
                    DRIVERS_TABLE if is_source else ZONES_TABLE, title, 0, "5:2:3:2:1")
                table.maximumVisibleRows = 6 if is_source else 4
                table.minimumVisibleRows = 4 if is_source else 1
                table.isFullWidth = True
                if not is_source:
                    for preset, size in wglink_protocol.MESH_PRESETS.items():
                        add = _mesh_button(table.commandInputs, f"mesh_add_group_{preset}",
                                           f"+ {preset} size zone ({size:g} mm)", full_width=False)
                        table.addToolbarCommandInput(add)
            if input_id == "mesh_groups":
                # The one face picker, right under the tables it fills.
                inputs.addTextBoxCommandInput("mesh_target", "", "", 1, True).isFullWidth = True
                picker = inputs.addSelectionInput(
                    PICKER, "Faces",
                    "Click faces in the model; click a selected face again to remove it. "
                    "A size zone also takes whole bodies.")
                wglink_dialog_inputs.add_selection_filters(picker, "Faces", "SolidBodies", "SurfaceBodies")
                picker.setSelectionLimits(0, 0)
                inputs.addTextBoxCommandInput("mesh_notice", "", "", 2, True).isFullWidth = True
        walls = inputs.itemById("mesh_walls").children
        wall = walls.addValueInput("mesh_wall", "Walls", "mm", wglink_dialog_inputs.mm_input(mesh["wall_size_mm"]))
        wall.tooltip = "Mesh size for every surface that is not a driver and not in a size zone."
        advanced = inputs.itemById("mesh_advanced").children
        grading = advanced.addValueInput("mesh_grading", "Grading distance", "mm",
                                         wglink_dialog_inputs.mm_input(mesh["grading_distance_mm"]))
        grading.tooltip = "How far from each driver the mesh grows from the driver's size to the wall size."
        symmetry = _mesh_dropdown(advanced, "mesh_symmetry", "Symmetry", ("Automatic", "Off"),
                                 "Off" if mesh["symmetry"] == "off" else "Automatic")
        symmetry.tooltip = "Off solves the whole model: slower, more memory."
        _mesh_button(advanced, "mesh_reset", "Reset to defaults")
        estimate = inputs.itemById("mesh_estimate_section").children
        estimate.addTextBoxCommandInput("mesh_estimate", "", "", 4, True)
        # One fixed row per role, in DRIVER_LABELS order; a saved source fills
        # its role's row. Linked sources keep their own read-only rows.
        for source in sources:
            record = {**source, "size_mm": mesh["source_size_mm"][source["id"]]}
            state.original_sources.append(
                {**record, "original_faces": list(state.context["members"].get(source["id"], []))})
        for role, label in wglink_protocol.DRIVER_LABELS:
            saved_rows = [source for source in state.original_sources if not source.get("instance_id")
                          and wglink_protocol.canonical_source_role(source["role"]) == role]
            record = {**saved_rows[0], "role": role} if saved_rows else {"role": role}
            if len(saved_rows) > 1:
                # Old documents can hold one role twice (PORT_EXIT beside its
                # renamed role). One row shows both; Save repaints them as one.
                record["merged"] = True
                state.notice = (f"{label}: {len(saved_rows)} saved drivers were combined into this row; "
                                "Save keeps them as one.")
            faces = [face for source in saved_rows for face in source["original_faces"]]
            _add_mesh_row(inputs, state, "source", record, faces)
        for source in state.original_sources:
            if source.get("instance_id"):
                _add_mesh_row(inputs, state, "source", source, source["original_faces"])
        all_faces = _mesh_faces(state.context.get("bodies", []))
        definitions = list(saved["surface_groups"] if saved is not None else [])
        painted = {}
        for face in all_faces:
            # An unpainted face that throws assetInst is "no override"; an
            # unreadable WGMESH override must not silently leave its zone.
            label = wglink_core._appearance_name(face, strict=True) or ""
            if label.startswith("WGMESH_"):
                painted.setdefault(label[len("WGMESH_"):], []).append(face)
        for group_id in sorted(painted) if saved is None else ():
            # Hand-painted labels may lack a usable id. Preserve their face
            # membership, but mint a valid id so Save can recover or clear them.
            recovered_id = group_id if group_id.strip() and group_id == group_id.strip() else uuid.uuid4().hex
            definitions.append({"group_id": recovered_id, "name": f"Recovered {group_id.strip()}".strip(),
                                "recovered_paint_id": group_id, "size_mm": wglink_protocol.MESH_PRESETS["Fine"]})
        for group in definitions:
            faces = painted.get(group.get("recovered_paint_id", group["group_id"]), [])
            _add_mesh_row(inputs, state, "group", group, faces)
            state.original_groups.extend(faces)
    finally:
        state.restoring = False
    _sync_mesh_dialog(inputs, state)


def _mesh_dialog_values(inputs, state):
    sources, groups = [], []
    for row_id, record in state.sources.items():
        # A driver row is a source when it has faces, is linked, or still owes
        # saved faces Fusion could not select (Save must not silently drop them).
        if not (record["faces"] or record.get("instance_id") or record.get("unselected_saved_faces")):
            continue
        sources.append(wglink_mesh.normalize_source({**record, "id": record.get("id", f"new-{row_id}"),
                        "faces": list(record["faces"]), "size_mm": (record["size_mm"] if record.get("instance_id")
                            else wglink_dialog_inputs.mm_value(inputs, f"mesh_source_size_{row_id}"))}))
    _read_zone_names(inputs, state)
    for row_id, record in state.groups.items():
        groups.append({**record, "size_mm": wglink_dialog_inputs.mm_value(inputs, f"mesh_group_size_{row_id}"),
                       "faces": list(record["faces"])})
    return sources, groups, {
        "wall_size_mm": wglink_dialog_inputs.mm_value(inputs, "mesh_wall"),
        "grading_distance_mm": wglink_dialog_inputs.mm_value(inputs, "mesh_grading"),
        "symmetry": ("off" if wglink_dialog_inputs.selected_name(inputs, "mesh_symmetry", "Automatic") == "Off"
                     else "auto"),
        "settings_origin": "user",
    }


def _mesh_dialog_findings(sources, groups, mesh, state):
    """Save's blockers: the shared authoring rules over this dialog's rows."""
    rows = []
    for kind, records in (("source", sources), ("group", groups)):
        for row in records:
            selected = {wglink_send._face_key(face) for face in row["faces"]}
            missing = row.get("unselected_saved_faces", set()) - selected
            rows.append({
                "kind": kind, "label": _row_label(kind, row), "name": row.get("name"),
                "size_mm": row["size_mm"], "face_keys": selected,
                "missing_saved_faces": min(len(missing), max(0, len(row["original_faces"]) - len(selected))),
            })
    return wglink_protocol.mesh_authoring_findings(
        rows, mesh, fem_driven=state.fem_driven, source_error=state.preview_error)


def _mesh_dialog_errors(sources, groups, mesh, state):
    return [finding["message"] for finding in _mesh_dialog_findings(sources, groups, mesh, state)]


def _mesh_snapshot_values(sources, groups, mesh):
    body_names = {wglink_send._face_key(face): str(getattr(getattr(face, "body", None), "name", "body"))
                  for row in [*sources, *groups] for face in row["faces"]}
    measured = [{**source, "suggested_resolution_mm": source["size_mm"],
                 "observed": wglink_send._observed(source["faces"], body_names)} for source in sources]
    result = {**mesh, "source_size_mm": {source["id"]: source["size_mm"] for source in sources},
              "surface_groups": [{"group_id": group["group_id"], "name": group["name"],
                  "size_mm": group["size_mm"], "selectors": {"appearance_labels": [wglink_author.group_appearance_name(group["group_id"])]},
                  "observed": wglink_send._observed(group["faces"], body_names)} for group in groups]}
    wglink_mesh.validate_settings({"schema": 1, **result})
    return measured, result


def _show(inputs, state, input_id, text):
    """Set a read-only text only when it changes: Fusion reports each set as an input change."""

    item = inputs.itemById(input_id)
    if item is not None and state.shown.get(input_id) != text:
        state.shown[input_id] = text
        item.formattedText = text


def _sync_mesh_dialog(inputs, state):
    """Render under the same guard as programmatic picker/table restoration."""
    restoring, state.restoring = state.restoring, True
    try:
        return _render_mesh_dialog(inputs, state)
    finally:
        state.restoring = restoring


def _render_mesh_dialog(inputs, state):
    for kind in ("source", "group"):
        for row_id, record in _rows(state, kind).items():
            _show(inputs, state, f"mesh_{kind}_count_{row_id}",
                  _count_text(kind, record, state.target == (kind, row_id)))
    _show(inputs, state, "mesh_notice", html.escape(state.notice))
    sources, groups, mesh = _mesh_dialog_values(inputs, state)
    findings = _mesh_dialog_findings(sources, groups, mesh, state)
    errors = [finding["message"] for finding in findings]
    warnings = []
    wall = mesh["wall_size_mm"]
    for source in sources:
        if (source["size_mm"] is not None and wall is not None and math.isfinite(wall)
                and 0 < wall < source["size_mm"] and math.isfinite(source["size_mm"])):
            warnings.append(f"{_row_label('source', source)}: {source['size_mm']:g} mm is coarser than the walls ({mesh['wall_size_mm']:g} mm); it will be meshed at {mesh['wall_size_mm']:g} mm.")
    limits = wglink_inbox.wg_mesh_limits(wglink_workspace.ipc_folder())
    limit = wglink_protocol.solved_triangle_limit(limits)
    estimate_text = f"An estimate is available when the settings are valid. Limit {limit:,}."
    if not errors:
        try:
            measured, snapshot = _mesh_snapshot_values(sources, groups, mesh)
            bodies = state.context.get("estimate_bodies", [])
            area = sum(wglink_send._face_area(face) for face in _mesh_faces(bodies))
            estimate = wglink_mesh.estimate_triangles(area, measured, snapshot)
            memberships = {**{"source:" + row["id"]: row["faces"] for row in sources},
                           **{"group:" + row["group_id"]: row["faces"] for row in groups}}
            learned = None
            try:
                learned = wglink_mesh.history.learn(wglink_workspace.ipc_folder(), state.services.active_document_id(),
                    wglink_send.mesh_geometry_key(wglink_core._design(adsk.core.Application.get()), bodies, memberships), snapshot, measured)
            except OSError:
                state.services.log("Could not update mesh calibration.")
            display = wglink_mesh.estimate_display(estimate, symmetry=mesh["symmetry"], limits=limits,
                drawn_fraction=state.context.get("drawn_fraction", 1.0), learned=learned)
            colour = "#b77900" if display["colour"] == "amber" else "#287a38"
            estimate_text = f'<font color="{colour}">{html.escape(display["text"])}</font>'
            if display["ceiling"] is not None:
                warnings.append(wglink_protocol.finding_text(display["ceiling"]))
        except (wglink_mesh.MeshSettingsError, wglink_core.WgLinkError, OSError):
            estimate_text = f"An estimate is unavailable for this geometry. Limit {limit:,}."
    inputs.itemById("mesh_estimate").formattedText = estimate_text
    if errors and not state.touched and [finding["code"] for finding in findings] == ["no_drive"]:
        # A fresh model has nothing wrong yet: say what to do, not what is missing.
        text = f'<font color="#808080">{html.escape(FIRST_PICK_HINT)}</font>'
    else:
        text = ("<b>Fix before saving:</b><br>" + "<br>".join(html.escape(error) for error in errors)) if errors else ""
    if warnings:
        text += ("<br>" if text else "") + "<b>Warnings:</b><br>" + "<br>".join(html.escape(warning) for warning in warnings)
    validation = inputs.itemById("mesh_validation")
    validation.formattedText = text
    validation.isVisible = bool(text)
    return not errors


def _apply_mesh_dialog(inputs, state, *, preview=False):
    if state is None:
        raise wglink_author.AuthorError("Open Sources & Mesh… and save again.")
    if state.construction_failed:
        raise wglink_author.AuthorError("Close and reopen Sources & Mesh… to retry before saving.")
    sources, groups, mesh = _mesh_dialog_values(inputs, state)
    errors = _mesh_dialog_errors(sources, groups, mesh, state)
    if errors:
        raise wglink_author.AuthorError("Fix before saving: " + " ".join(errors))
    # Validate the entire snapshot before the first document edit.
    _mesh_snapshot_values(sources, groups, mesh)
    app = adsk.core.Application.get()
    design = wglink_core._design(app)
    faces = _mesh_faces([
        *(face for source in state.original_sources if not source.get("instance_id")
          for face in source["original_faces"]),
        *state.original_groups,
        *(face for row in [*sources, *groups] if not row.get("instance_id") for face in row["faces"]),
    ])
    def prepare(edit):
        wglink_send._prepare_source_edit(edit, design, faces)
        for face in faces:
            native = wglink_send._native_face(face)
            edit.remember_appearance(native)
            edit.remember_attribute(native, wglink_send.GROUP_ORIGINAL_APPEARANCE_ATTRIBUTE)
        edit.remember_attribute(design.rootComponent, wglink_mesh.SETTINGS_ATTRIBUTE)
        for source in sources:
            if not source.get("instance_id"):
                edit.remember_asset(design, source["role"])
        for group in groups:
            edit.remember_asset(design, "WGMESH_" + group["group_id"])

    try:
        return wglink_send.document_edit("save sources and mesh", prepare,
            lambda edit: (_paint_mesh_dialog(design, app, state, sources, groups) if preview
                          else _save_mesh_dialog(design, app, state, sources, groups, mesh)))
    except wglink_core.WgLinkError as exc:
        raise wglink_author.AuthorError(str(exc)) from exc


def _user_settings(settings):
    """The values a person chooses; observed geometry and ids can change on their own."""
    return (settings.get("wall_size_mm"), settings.get("grading_distance_mm"), settings.get("symmetry"),
            sorted(settings.get("source_size_mm", {}).values()),
            sorted((group["group_id"], group["name"], group["size_mm"]) for group in settings.get("surface_groups", [])))


def _paint_mesh_dialog(design, app, state, sources, groups):
    """Save's face assignments, also used by Fusion's transient preview.

    No whole-model survey or mesh-settings write belongs in a colour preview.
    """
    if state.original_groups:
        wglink_send.paint_surface_group(app, design, state.original_groups, None)
    final_keys = {wglink_send._face_key(face) for source in sources if not source.get("instance_id") for face in source["faces"]}
    removed = [face for source in state.original_sources if not source.get("instance_id")
               for face in source["original_faces"] if wglink_send._face_key(face) not in final_keys]
    if removed:
        wglink_send.apply_source_faces(removed, wglink_author.CLEAR_SOURCE_LABEL, app=app, design=design)
    for source in sources:
        if source.get("instance_id"):
            continue
        unchanged = ({wglink_send._face_key(face) for face in source["faces"]} ==
                     {wglink_send._face_key(face) for face in source.get("original_faces", [])})
        original_role = source.get("selectors", {}).get("appearance_labels", [None])[0]
        if (unchanged and not source.get("merged") and original_role
                and wglink_protocol.canonical_source_role(original_role) == source["role"]):
            # Keep legacy PORT_EXIT paint; explicitly Save repairs its stamps.
            wglink_send.assign_source_identity(design, source["faces"], source["role"])
        else:
            wglink_send.apply_source_faces(source["faces"], source["role"], app=app, design=design)
    all_source_faces = [face for source in sources for face in source["faces"]]
    for group in groups:
        wglink_send.paint_surface_group(app, design, group["faces"], group["group_id"], source_faces=all_source_faces)


def _save_mesh_dialog(design, app, state, sources, groups, mesh):
    _paint_mesh_dialog(design, app, state, sources, groups)
    # Assignment can mint an identity. Store the ids Send will actually emit.
    context = {}
    report = wglink_send.preflight_scope(
        app, {"source_identity": state.services.source_identity_enabled()}, authoring_context=context)
    if report.get("source_error"):
        raise wglink_author.AuthorError(report["source_error"])
    for source in sources:
        matches = [current for current in context.get("sources", [])
                   if (current.get("instance_id") == source.get("instance_id") and
                       wglink_protocol.canonical_source_role(current["role"]) == source["role"])]
        if len(matches) != 1:
            raise wglink_author.AuthorError(
                f"Could not save {_row_label('source', source)}. Review its faces in Sources & Mesh….")
        source["id"] = matches[0]["id"]
    # A source this dialog never showed (its paint could not be read when the
    # dialog opened) must not be dropped from the saved settings by this Save.
    shown = {(source.get("instance_id"), source["role"]) for source in sources}
    unseen = [current for current in context.get("sources", [])
              if (current.get("instance_id"), wglink_protocol.canonical_source_role(current["role"])) not in shown]
    if unseen:
        raise wglink_author.AuthorError(
            f"{_row_label('source', unseen[0])} is in this document but was not shown here.")
    _, snapshot = _mesh_snapshot_values(sources, groups, mesh)
    if state.saved_settings and _user_settings(snapshot) == _user_settings(state.saved_settings):
        # Nothing the user chooses changed: keep who chose the saved values.
        snapshot["settings_origin"] = state.saved_settings.get("settings_origin", snapshot["settings_origin"])
    wglink_send.save_mesh_settings(design, snapshot)
    wglink_send.note_source_authoring()
    # No confirmation box: the dialog closing is the confirmation.
    return {"summary": ""}


class MeshInputChangedHandler(adsk.core.InputChangedEventHandler):
    def __init__(self, state):
        super().__init__()
        self.state = state

    def notify(self, args):
        if self.state.construction_failed or self.state.restoring:
            return
        self.state.restoring = True
        try:
            command = args.input.parentCommand
            inputs = command.commandInputs if command is not None else wglink_dialog_inputs.command_inputs(args.inputs)
            input_id = args.input.id
            state = self.state
            if not state.activated:
                pass
            elif input_id == PICKER:
                if state.target is not None:
                    _picked(inputs, state)
            elif input_id in (DRIVERS_TABLE, ZONES_TABLE):
                # A click on a row (outside its buttons) makes that row the target.
                kind = "source" if input_id == DRIVERS_TABLE else "group"
                rows = _table_rows(state, kind)
                selected = args.input.selectedRow
                if 0 <= selected < len(rows):
                    _selected_row_changed(inputs, state, kind, rows[selected])
            elif _row_cell_changed(inputs, state, input_id):
                pass
            elif input_id.startswith("mesh_pick_"):
                kind, row = input_id[len("mesh_pick_"):].rsplit("_", 1)
                if state.target != (kind, int(row)):
                    _set_target(inputs, state, kind, int(row))
            elif input_id.startswith("mesh_add_group_"):
                preset = input_id[len("mesh_add_group_"):]
                row = _add_mesh_row(inputs, state, "group", {
                    "name": _next_zone_name(state, preset), "size_mm": wglink_protocol.MESH_PRESETS[preset]})
                state.touched = True
                _set_target(inputs, state, "group", row)
            elif input_id.startswith("mesh_remove_group_"):
                _remove_zone(inputs, state, int(input_id.rsplit("_", 1)[1]))
            elif input_id.startswith("mesh_clear_source_"):
                _clear_driver(inputs, state, int(input_id.rsplit("_", 1)[1]))
            elif input_id == "mesh_reset":
                defaults = wglink_protocol.MESH_DEFAULTS
                to_internal = wglink_dialog_inputs.mm_to_internal
                inputs.itemById("mesh_wall").value = to_internal(defaults["wall_size_mm"])
                inputs.itemById("mesh_grading").value = to_internal(defaults["grading_distance_mm"])
                for row, source in state.sources.items():
                    if not source.get("instance_id"):
                        inputs.itemById(f"mesh_source_size_{row}").value = to_internal(
                            wglink_protocol.default_source_size_mm(source["role"]))
                for row in state.groups:
                    inputs.itemById(f"mesh_group_size_{row}").value = to_internal(wglink_protocol.MESH_PRESETS["Fine"])
            # Cell edits may retarget the picker, but retain their keyboard focus.
            _sync_mesh_dialog(inputs, state)
        except Exception as exc:
            self.state.services.report_error("Updating Sources & Mesh…", exc)
        finally:
            self.state.restoring = False


class MeshPreviewHandler(adsk.core.CommandEventHandler):
    """Show the colours Save would paint, while the dialog is open.

    It runs only Save's face-assignment phase with the same writers and
    journal inside Fusion's preview. Fusion rolls it back before every next
    preview, before Save and on Cancel. ``isValidResult`` stays False so Save
    always runs for real, and the authoring counters are left as they were.
    Settings that cannot be saved yet show no colours; the dialog's own
    validation explains why.
    """

    def __init__(self, state):
        super().__init__()
        self.state = state

    def notify(self, args):
        args.isValidResult = False
        state = self.state
        if state.construction_failed or not state.activated or state.restoring:
            return
        with wglink_activity.because(wglink_activity.command_cause("source")):
            try:
                with wglink_send.generations_unchanged():
                    _apply_mesh_dialog(args.command.commandInputs, state, preview=True)
                state.preview_failure = None
            except Exception as exc:  # noqa: BLE001 - a preview never interrupts the user
                state.preview_failure = repr(exc)


class MeshValidateInputsHandler(adsk.core.ValidateInputsEventHandler):
    def __init__(self, state):
        super().__init__()
        self.state = state

    def notify(self, args):
        try:
            args.areInputsValid = not self.state.construction_failed and _sync_mesh_dialog(
                wglink_dialog_inputs.command_inputs(args.inputs), self.state
            )
        except Exception:
            args.areInputsValid = False


class MeshDialogActivateHandler(adsk.core.CommandEventHandler):
    def __init__(self, state):
        super().__init__()
        self.state = state

    def notify(self, args):
        state = self.state
        if state.construction_failed or state.activated:
            return
        state.restoring = True
        try:
            inputs = args.command.commandInputs
            _check_saved_faces(inputs, state)
            state.activated = True
            # Once: later activations (Orbit/Pan) keep the user's target and focus.
            _set_target(inputs, state, *_initial_target(state), notice=state.notice)
            _sync_mesh_dialog(inputs, state)
        finally:
            state.restoring = False


class MeshDialogDestroyHandler(adsk.core.CommandEventHandler):
    def __init__(self, state):
        super().__init__()
        self.state = state

    def notify(self, args):
        args.command.parentCommandDefinition.name = self.state.services.command_name
