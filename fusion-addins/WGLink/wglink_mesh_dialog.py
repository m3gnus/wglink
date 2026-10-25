"""The Sources & Mesh… dialog: building it, reading it, validating it, saving it.

Moved out of WGLink.py unchanged so a dev sync can reload it in a running
Fusion. It reaches the entry module's helpers (selection readers, unit
conversion, the source writer, error reporting) through ``host``, which
WGLink.py binds to an attribute view of its own globals when it loads; that
lookup is live, so the entry module stays the single owner of each helper and
of ``_source_authoring_generation``. Main thread only, like every dialog.
"""

from __future__ import annotations

import html
import math
from typing import Any
import uuid

import adsk.core

if __package__:
    from . import wglink_author, wglink_core, wglink_inbox, wglink_mesh, wglink_protocol, wglink_send
    from . import wglink_workspace
else:
    import wglink_author
    import wglink_core
    import wglink_inbox
    import wglink_mesh
    import wglink_protocol
    import wglink_send
    import wglink_workspace

# WGLink.py's globals, bound by WGLink.py; kept across importlib.reload.
host: Any = globals().get("host")


MESH_DIALOG_TITLE = "Sources & Mesh — Waveguide Generator"


class MeshDialogState:
    """The dialog's truth. Rows hold their faces; the one picker only shows the target's."""

    def __init__(self):
        self.construction_failed = False
        self.sources = {}
        self.groups = {}
        self.next_id = 1
        self.restoring = False
        self.context = {}
        self.original_sources = []
        self.original_groups = []
        self.preview_error = None
        # A FEM region with a moving surface drives the model without a source.
        self.fem_driven = False
        self.activated = False
        # ("source" | "group", row_id) whose faces the picker shows and edits.
        self.target = None
        # Zone row ids in table order, for deleting a table row.
        self.group_order = []
        self.notice = ""
        # Read-only texts as last set, so an unchanged text is not set again.
        self.shown = {}
        # The live Command (set by WGLink.py before construction).
        self.command = None


PICKER = "mesh_faces"
DRIVERS_TABLE = "mesh_sources_table"
ZONES_TABLE = "mesh_groups_table"


def _mesh_faces(entities):
    faces = {}
    for entity in entities:
        for face in wglink_core._items(entity.faces, strict=True) if hasattr(entity, "faces") else [entity]:
            faces.setdefault(wglink_send._face_key(face), face)
    return list(faces.values())


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
        return f"Zone '{record.get('name') or 'unnamed'}'"
    label = wglink_author.source_role_label(record["role"])
    return f"{label} (from the WG design)" if record.get("instance_id") else label


def _count_text(kind, record, target):
    faces = len(record["faces"])
    text = _face_count_text(faces) if faces else ("not used" if kind == "source" else "no faces")
    return f"<b>{text} ◀</b>" if target else html.escape(text)


def _add_mesh_row(inputs, state, kind, record=None, faces=()):
    """One table row: label or name, mesh size, face count, Pick faces (and ✕ for a zone)."""

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
        role = wglink_author.resolve_source_choice(record.get("role", "HF"))
        rows[row_id]["role"] = role
        name = cells.addTextBoxCommandInput(f"mesh_source_label_{row_id}", "",
                                            html.escape(_row_label(kind, rows[row_id])), 1, True)
        size = record.get("size_mm", wglink_protocol.default_source_size_mm(role))
    else:
        name = cells.addStringValueInput(f"mesh_group_name_{row_id}", "", record.get("name", ""))
        size = record.get("size_mm", wglink_protocol.MESH_PRESETS["Fine"])
        rows[row_id].setdefault("group_id", uuid.uuid4().hex)
        state.group_order.append(row_id)
    value = cells.addValueInput(f"mesh_{kind}_size_{row_id}", "", "mm", host._mm_input(size))
    value.isEnabled = not linked
    value.tooltip = ("Mesh size on this driver's faces. Smaller is more accurate but slower."
                     if is_source else "Mesh size on this zone's faces.")
    count = cells.addTextBoxCommandInput(f"mesh_{kind}_count_{row_id}", "", "", 1, True)
    row_inputs = [name, value, count]
    if not linked:
        # Linked membership comes from WG; there is nothing to pick here.
        row_inputs.append(_mesh_button(cells, f"mesh_pick_{kind}_{row_id}", "Pick faces", full_width=False))
    if not is_source:
        remove = _mesh_button(cells, f"mesh_remove_group_{row_id}", "✕", full_width=False)
        remove.tooltip = "Remove this zone. Its faces go back to the wall size."
        row_inputs.append(remove)
    for column, item in enumerate(row_inputs):
        table.addCommandInput(item, index, column)
    return row_id


def _next_zone_name(state, preset):
    names = {str(record.get("name", "")).casefold() for record in state.groups.values()}
    serial = 1
    while f"{preset} zone {serial}".casefold() in names:
        serial += 1
    return f"{preset} zone {serial}"


def _set_target(inputs, state, kind, row_id):
    """Point the one picker at a row: clear it, load that row's faces, focus it."""

    state.target = (kind, row_id)
    record = _rows(state, kind)[row_id]
    picker = inputs.itemById(PICKER)
    restoring, state.restoring = state.restoring, True
    try:
        picker.clearSelection()
        for face in record["faces"]:
            picker.addSelection(face)
        for table_id, table_kind in ((DRIVERS_TABLE, "source"), (ZONES_TABLE, "group")):
            table = inputs.itemById(table_id)
            if table_kind != kind:
                table.selectedRow = -1
            elif kind == "group":
                table.selectedRow = state.group_order.index(row_id)
            else:
                table.selectedRow = list(state.sources).index(row_id)
        picker.hasFocus = True
    finally:
        state.restoring = restoring
    inputs.itemById("mesh_target").formattedText = (
        f"Picking faces for: <b>{html.escape(_row_label(kind, record))}</b>")


def _face_count_text(count):
    return f"{count} {wglink_protocol._plural(count, 'face', 'faces')}"


def _read_zone_names(inputs, state):
    """Zone names as typed, so notices and labels use what the user sees."""

    for row_id, record in state.groups.items():
        item = inputs.itemById(f"mesh_group_name_{row_id}")
        if item is not None:
            record["name"] = str(item.value).strip()


def _picked(inputs, state):
    """Apply the picker's contents to the target row.

    A face picked here that another row holds moves here, and the notice says
    so. A whole body (zones only) adds its faces that no other row holds; a
    linked source's faces never move, they change in WG.
    """

    _read_zone_names(inputs, state)
    kind, row_id = state.target
    target = _rows(state, kind)[row_id]
    owners = {wglink_send._face_key(face): (other_kind, record)
              for other_kind in ("source", "group")
              for other_id, record in _rows(state, other_kind).items()
              if (other_kind, other_id) != state.target for face in record["faces"]}
    entities = host._selected_entities(inputs, PICKER)
    bodies = [entity for entity in entities if hasattr(entity, "faces")]
    notices, reload_picker = [], bool(bodies)
    body_faces = []
    if bodies and kind == "source":
        notices.append("A driver takes faces, not whole bodies.")
    elif bodies:
        body_faces = _mesh_faces(bodies)
        stayed = [face for face in body_faces if wglink_send._face_key(face) in owners]
        if stayed:
            notices.append(f"{_face_count_text(len(stayed))} of that body already in other rows stayed there.")
    candidates = _mesh_faces([
        *(entity for entity in entities if not hasattr(entity, "faces")),
        *(face for face in body_faces if wglink_send._face_key(face) not in owners),
    ])
    kept, moved = [], {}
    for face in candidates:
        key = wglink_send._face_key(face)
        owner_kind, owner = owners.get(key, (None, None))
        if owner is not None and owner.get("instance_id"):
            notices.append(f"{_row_label(owner_kind, owner)} keeps its faces; change them in WG.")
            reload_picker = True
            continue
        if owner is not None:
            owner["faces"] = [member for member in owner["faces"] if wglink_send._face_key(member) != key]
            label = _row_label(owner_kind, owner)
            moved[label] = moved.get(label, 0) + 1
        kept.append(face)
    unchanged = [wglink_send._face_key(face) for face in kept] == [
        wglink_send._face_key(face) for face in target["faces"]]
    target["faces"] = kept
    notices.extend(f"Moved {_face_count_text(count)} from {label} to {_row_label(kind, target)}."
                   for label, count in moved.items())
    if notices or not unchanged:
        # A repeated report of the same picks (Fusion can send one per
        # programmatic change) must not wipe the notice of the real change.
        state.notice = " ".join(notices)
    if reload_picker:
        # Show exactly what the row now holds: faces, never a body or a refused face.
        _set_target(inputs, state, kind, row_id)


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


MESH_DIALOG_RETRY = "Close and reopen Sources & Mesh… to retry before saving."


def _build_mesh_dialog(inputs, state):
    preview = getattr(state.command, "executePreview", None)
    if preview is not None:
        handler = MeshPreviewHandler(state)
        preview.add(handler)
        host._handlers.append(handler)
    state.restoring = True
    try:
        design = wglink_core._design(host._app())
        # Resolve saved settings before the fail-soft source preview can read
        # them. A failed read leaves construction guarded until the next open.
        try:
            saved = wglink_send.read_mesh_settings(design)
        except wglink_core.WgLinkError as exc:
            raise wglink_core.WgLinkError(f"{exc} {MESH_DIALOG_RETRY}") from exc
        try:
            wglink_send._stamp_attributes(design)
            report = wglink_send.preflight_scope(
                host._app(), {"source_identity": host._source_identity_enabled(), "display_automatic_domain": True,
                       "automatic_domain": host._manifest_feature_enabled(wglink_inbox.AUTOMATIC_DOMAIN)},
                authoring_context=state.context,
            )
        except wglink_send.SourceIdentityReadError as exc:
            raise wglink_core.WgLinkError(f"{exc} {MESH_DIALOG_RETRY}") from exc
        state.preview_error = report.get("source_error")
        state.fem_driven = report.get("fem_driven") is True
        sources = state.context.get("sources", [])
        mesh = wglink_mesh.snapshot(sources, saved)
        for input_id, title, expanded in (
            ("mesh_sources", "Drivers", True), ("mesh_walls", "Walls", True),
            ("mesh_groups", "Size zones", True), ("mesh_advanced", "Advanced", False),
            ("mesh_estimate_section", "Estimate", True), ("mesh_validation_section", "Validation", True),
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
                    DRIVERS_TABLE if is_source else ZONES_TABLE, title, 0, "5:2:3:2" if is_source else "5:2:3:2:1")
                table.maximumVisibleRows = 6 if is_source else 4
                table.minimumVisibleRows = 4 if is_source else 1
                table.isFullWidth = True
                if not is_source:
                    for preset, size in wglink_protocol.MESH_PRESETS.items():
                        add = _mesh_button(table.commandInputs, f"mesh_add_group_{preset}",
                                           f"+ {preset} zone ({size:g} mm)", full_width=False)
                        table.addToolbarCommandInput(add)
            if input_id == "mesh_groups":
                # The one face picker, right under the tables it fills.
                inputs.addTextBoxCommandInput("mesh_target", "", "", 1, True).isFullWidth = True
                picker = inputs.addSelectionInput(
                    PICKER, "Faces",
                    "Click faces in the model; click a selected face again to remove it. "
                    "A zone also takes whole bodies.")
                host._add_selection_filters(picker, "Faces", "SolidBodies", "SurfaceBodies")
                picker.setSelectionLimits(0, 0)
                inputs.addTextBoxCommandInput("mesh_notice", "", "", 2, True).isFullWidth = True
        walls = inputs.itemById("mesh_walls").children
        wall = walls.addValueInput("mesh_wall", "Walls", "mm", host._mm_input(mesh["wall_size_mm"]))
        wall.tooltip = "Mesh size for every surface that is not a source or in a surface group."
        advanced = inputs.itemById("mesh_advanced").children
        grading = advanced.addValueInput("mesh_grading", "Grading distance", "mm", host._mm_input(mesh["grading_distance_mm"]))
        grading.tooltip = "Distance over which the mesh grows from each source's size to the wall size."
        symmetry = _mesh_dropdown(advanced, "mesh_symmetry", "Symmetry", ("Automatic", "Off"),
                                 "Off" if mesh["symmetry"] == "off" else "Automatic")
        symmetry.tooltip = "Off solves the whole model: slower, more memory."
        _mesh_button(advanced, "mesh_reset", "Reset to defaults")
        estimate = inputs.itemById("mesh_estimate_section").children
        estimate.addTextBoxCommandInput("mesh_estimate", "", "", 4, True)
        inputs.itemById("mesh_validation_section").children.addTextBoxCommandInput("mesh_validation", "", "", 4, True)
        # One fixed row per role, in the protocol's order; a saved source fills
        # its role's row. Linked sources keep their own read-only rows.
        for source in sources:
            record = {**source, "size_mm": mesh["source_size_mm"][source["id"]]}
            state.original_sources.append(
                {**record, "original_faces": list(state.context["members"].get(source["id"], []))})
        for role in wglink_protocol.MESH_DEFAULTS["source_size_mm"]:
            saved_rows = [source for source in state.original_sources if not source.get("instance_id")
                          and wglink_author.resolve_source_choice(source["role"]) == role]
            record = {**saved_rows[0], "role": role} if saved_rows else {"role": role}
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
            # unreadable WGMESH override must not silently leave its group.
            label = wglink_core._appearance_name(face, strict=True) or ""
            if label.startswith("WGMESH_"):
                painted.setdefault(label[len("WGMESH_"):], []).append(face)
        for group_id in sorted(painted) if saved is None else ():
            # Hand-painted labels may lack a usable id. Preserve their face
            # membership, but mint a valid id so Save can recover or clear them.
            recovered_id = group_id if group_id.strip() and group_id == group_id.strip() else uuid.uuid4().hex
            definitions.append({"group_id": recovered_id, "name": f"Recovered {group_id.strip() or 'surface group'}",
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
                            else host._mm_value(inputs, f"mesh_source_size_{row_id}"))}))
    _read_zone_names(inputs, state)
    for row_id, record in state.groups.items():
        groups.append({**record, "size_mm": host._mm_value(inputs, f"mesh_group_size_{row_id}"),
                       "faces": list(record["faces"])})
    return sources, groups, {
        "wall_size_mm": host._mm_value(inputs, "mesh_wall"),
        "grading_distance_mm": host._mm_value(inputs, "mesh_grading"),
        "symmetry": "off" if host._selected_name(inputs, "mesh_symmetry", "Automatic") == "Off" else "auto",
        "settings_origin": "user",
    }


def _mesh_dialog_errors(sources, groups, mesh, state):
    """Save's blockers: the shared authoring rules over this dialog's rows."""
    rows = []
    for kind, records in (("source", sources), ("group", groups)):
        for row in records:
            selected = {wglink_send._face_key(face) for face in row["faces"]}
            missing = row.get("unselected_saved_faces", set()) - selected
            rows.append({
                "kind": kind,
                "label": _row_label(kind, row),
                "role": row.get("role"), "name": row.get("name"), "linked": bool(row.get("instance_id")),
                "size_mm": row["size_mm"], "face_keys": selected,
                "missing_saved_faces": min(len(missing), max(0, len(row["original_faces"]) - len(selected))),
            })
    findings = wglink_protocol.mesh_authoring_findings(
        rows, mesh, fem_driven=state.fem_driven, source_error=state.preview_error)
    return [finding["message"] for finding in findings]


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
    for kind in ("source", "group"):
        for row_id, record in _rows(state, kind).items():
            _show(inputs, state, f"mesh_{kind}_count_{row_id}",
                  _count_text(kind, record, state.target == (kind, row_id)))
    _show(inputs, state, "mesh_notice", html.escape(state.notice))
    sources, groups, mesh = _mesh_dialog_values(inputs, state)
    errors = _mesh_dialog_errors(sources, groups, mesh, state)
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
                learned = wglink_mesh.history.learn(wglink_workspace.ipc_folder(), host._active_document_id(),
                    wglink_send.mesh_geometry_key(wglink_core._design(host._app()), bodies, memberships), snapshot, measured)
            except OSError:
                host._log("Could not update mesh calibration.")
            display = wglink_mesh.estimate_display(estimate, symmetry=mesh["symmetry"], limits=limits,
                drawn_fraction=state.context.get("drawn_fraction", 1.0), learned=learned)
            colour = "#b77900" if display["colour"] == "amber" else "#287a38"
            estimate_text = f'<font color="{colour}">{html.escape(display["text"])}</font>'
            if display["ceiling"] is not None:
                warnings.append(wglink_protocol.finding_text(display["ceiling"]))
        except (wglink_mesh.MeshSettingsError, wglink_core.WgLinkError, OSError):
            estimate_text = f"An estimate is unavailable for this geometry. Limit {limit:,}."
    inputs.itemById("mesh_estimate").formattedText = estimate_text
    validation = inputs.itemById("mesh_validation")
    validation.formattedText = (("<b>Fix before saving:</b><br>" + "<br>".join(html.escape(error) for error in errors)) if errors else "")
    if warnings:
        validation.formattedText += ("<br>" if errors else "") + "<b>Warnings:</b><br>" + "<br>".join(html.escape(warning) for warning in warnings)
    validation.isVisible = bool(errors or warnings)
    return not errors


def _apply_mesh_dialog(inputs, state):
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
    app = host._app()
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
            native = host._native(face)
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
            lambda edit: _save_mesh_dialog(design, app, state, sources, groups, mesh))
    except wglink_core.WgLinkError as exc:
        raise wglink_author.AuthorError(str(exc)) from exc


def _save_mesh_dialog(design, app, state, sources, groups, mesh):
    if state.original_groups:
        wglink_send.paint_surface_group(app, design, state.original_groups, None)
    final_keys = {wglink_send._face_key(face) for source in sources if not source.get("instance_id") for face in source["faces"]}
    removed = [face for source in state.original_sources if not source.get("instance_id")
               for face in source["original_faces"] if wglink_send._face_key(face) not in final_keys]
    if removed:
        host._apply_source_faces(removed, wglink_author.CLEAR_SOURCE_LABEL, app=app, design=design)
    for source in sources:
        if source.get("instance_id"):
            continue
        unchanged = ({wglink_send._face_key(face) for face in source["faces"]} ==
                     {wglink_send._face_key(face) for face in source.get("original_faces", [])})
        original_role = source.get("selectors", {}).get("appearance_labels", [None])[0]
        if unchanged and original_role and wglink_author.resolve_source_choice(original_role) == source["role"]:
            # Keep legacy PORT_EXIT paint; explicitly Save repairs its stamps.
            wglink_send.assign_source_identity(design, source["faces"], source["role"])
        else:
            host._apply_source_faces(source["faces"], source["role"], app=app, design=design)
    all_source_faces = [face for source in sources for face in source["faces"]]
    for group in groups:
        wglink_send.paint_surface_group(app, design, group["faces"], group["group_id"], source_faces=all_source_faces)
    # Assignment can mint an identity. Store the ids Send will actually emit.
    context = {}
    report = wglink_send.preflight_scope(app, {"source_identity": host._source_identity_enabled()}, authoring_context=context)
    if report.get("source_error"):
        raise wglink_author.AuthorError(report["source_error"])
    for source in sources:
        matches = [current for current in context.get("sources", [])
                   if (current.get("instance_id") == source.get("instance_id") and
                       wglink_author.resolve_source_choice(current["role"]) == source["role"])]
        if len(matches) != 1:
            raise wglink_author.AuthorError(f"Could not save {source['role']} source. Review its faces in Sources & Mesh….")
        source["id"] = matches[0]["id"]
    # A source this dialog never showed (its paint could not be read when the
    # dialog opened) must not be dropped from the saved settings by this Save.
    shown = {(source.get("instance_id"), source["role"]) for source in sources}
    unseen = [current for current in context.get("sources", [])
              if (current.get("instance_id"), wglink_author.resolve_source_choice(current["role"])) not in shown]
    if unseen:
        raise wglink_author.AuthorError(
            f"{_row_label('source', {**unseen[0], 'role': wglink_author.resolve_source_choice(unseen[0]['role'])})} "
            "is in this document but was not shown here.")
    _, snapshot = _mesh_snapshot_values(sources, groups, mesh)
    wglink_send.save_mesh_settings(design, snapshot)
    host._source_authoring_generation += 1
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
            inputs = command.commandInputs if command is not None else host._command_inputs(args.inputs)
            input_id = args.input.id
            state = self.state
            if input_id == PICKER:
                if state.activated and state.target is not None:
                    _picked(inputs, state)
            elif input_id.startswith("mesh_add_group_"):
                preset = input_id[len("mesh_add_group_"):]
                row = _add_mesh_row(inputs, state, "group", {
                    "name": _next_zone_name(state, preset), "size_mm": wglink_protocol.MESH_PRESETS[preset]})
                _set_target(inputs, state, "group", row)
            elif input_id.startswith("mesh_remove_group_"):
                row = int(input_id.rsplit("_", 1)[1])
                inputs.itemById(ZONES_TABLE).deleteRow(state.group_order.index(row))
                state.group_order.remove(row)
                del state.groups[row]
                if state.target == ("group", row):
                    _set_target(inputs, state, *_initial_target(state))
            elif input_id == "mesh_reset":
                defaults = wglink_protocol.MESH_DEFAULTS
                inputs.itemById("mesh_wall").value = defaults["wall_size_mm"] / 10
                inputs.itemById("mesh_grading").value = defaults["grading_distance_mm"] / 10
                for row, source in state.sources.items():
                    if not source.get("instance_id"):
                        inputs.itemById(f"mesh_source_size_{row}").value = (
                            wglink_protocol.default_source_size_mm(source["role"]) / 10)
                for row in state.groups:
                    inputs.itemById(f"mesh_group_size_{row}").value = wglink_protocol.MESH_PRESETS["Fine"] / 10
            else:
                # Pick faces, or editing a row's name or size, makes that row the target.
                for kind in ("source", "group"):
                    prefix = f"mesh_pick_{kind}_", f"mesh_{kind}_size_", f"mesh_{kind}_name_"
                    if state.activated and input_id.startswith(prefix):
                        row = int(input_id.rsplit("_", 1)[1])
                        record = _rows(state, kind).get(row)
                        if record is not None and not record.get("instance_id") and state.target != (kind, row):
                            _set_target(inputs, state, kind, row)
            _sync_mesh_dialog(inputs, self.state)
        except Exception as exc:
            host._report_error("Updating Sources & Mesh…", f"{host.PANEL_NAME} error", exc)
        finally:
            self.state.restoring = False


class MeshPreviewHandler(adsk.core.CommandEventHandler):
    """Show the colours Save would paint, while the dialog is open.

    It runs the Save itself (``_apply_mesh_dialog``: the same writers and
    journal) inside Fusion's preview, which Fusion rolls back before the next
    preview, before Save and on Cancel. ``isValidResult`` stays False so Save
    always runs for real. Settings that cannot be saved yet simply show no
    colours; the dialog's own validation explains why.
    """

    def __init__(self, state):
        super().__init__()
        self.state = state

    def notify(self, args):
        args.isValidResult = False
        state = self.state
        if state.construction_failed or not state.activated or state.restoring:
            return
        try:
            _apply_mesh_dialog(args.command.commandInputs, state)
        except Exception as exc:  # noqa: BLE001 - a preview never interrupts the user
            state.preview_failure = repr(exc)


class MeshValidateInputsHandler(adsk.core.ValidateInputsEventHandler):
    def __init__(self, state):
        super().__init__()
        self.state = state

    def notify(self, args):
        try:
            args.areInputsValid = not self.state.construction_failed and _sync_mesh_dialog(
                host._command_inputs(args.inputs), self.state
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
            _set_target(inputs, state, *_initial_target(state))
            _sync_mesh_dialog(inputs, state)
        finally:
            state.restoring = False


class MeshDialogDestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        args.command.parentCommandDefinition.name = host.COMMANDS["source"][1]
