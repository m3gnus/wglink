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


def _mesh_faces(entities):
    faces = {}
    for entity in entities:
        for face in wglink_core._items(entity.faces, strict=True) if hasattr(entity, "faces") else [entity]:
            faces.setdefault(wglink_send._face_key(face), face)
    return list(faces.values())


def _mesh_members(inputs, prefix, row_id):
    return _mesh_faces(host._selected_entities(inputs, f"mesh_{prefix}_faces_{row_id}"))


def _mesh_dropdown(inputs, input_id, name, choices, selected):
    dropdown = inputs.addDropDownCommandInput(
        input_id, name, adsk.core.DropDownStyles.TextListDropDownStyle
    )
    for choice in choices:
        dropdown.listItems.add(choice, choice == selected)
    return dropdown


def _mesh_select(dropdown, label):
    for index in range(dropdown.listItems.count):
        item = dropdown.listItems.item(index)
        if item.name == label:
            if not item.isSelected:
                item.isSelected = True  # Fusion deselects the previous item.
            return


def _mesh_button(inputs, input_id, name):
    button = inputs.addBoolValueInput(input_id, name, False, "", False)
    button.isFullWidth = True
    return button


def _add_mesh_row(inputs, state, kind, record=None, faces=()):
    record = dict(record or {})
    row_id = state.next_id
    state.next_id += 1
    is_source = kind == "source"
    rows = state.sources if is_source else state.groups
    linked = bool(record.get("instance_id")) if is_source else False
    rows[row_id] = {**record, "row_id": row_id, "original_faces": list(faces)}
    parent = inputs.itemById("mesh_sources" if is_source else "mesh_groups").children
    role = wglink_author.resolve_source_choice(record.get("role", "HF")) if is_source else None
    title = f"Source {row_id}" if is_source else f"Surface group {row_id}"
    group = parent.addGroupCommandInput(f"mesh_{kind}_{row_id}", title)
    group.isExpanded = True
    children = group.children
    if is_source:
        dropdown = _mesh_dropdown(children, f"mesh_source_role_{row_id}", "Role",
                                  wglink_author.SOURCE_ROLES, role)
        dropdown.isEnabled = not linked
        if linked:
            children.addTextBoxCommandInput(f"mesh_linked_{row_id}", "",
                                           "from the linked WG design", 1, True)
        size = record.get("size_mm", wglink_protocol.default_source_size_mm(role))
    else:
        children.addStringValueInput(f"mesh_group_name_{row_id}", "Name", record.get("name", ""))
        size = record.get("size_mm", wglink_protocol.MESH_PRESETS["Fine"])
        preset = next((f"{name} {value:g} mm" for name, value in wglink_protocol.MESH_PRESETS.items()
                       if value == size), "Custom")
        _mesh_dropdown(children, f"mesh_group_preset_{row_id}", "Preset",
                       [*(f"{name} {value:g} mm" for name, value in wglink_protocol.MESH_PRESETS.items()), "Custom"], preset)
        rows[row_id].setdefault("group_id", uuid.uuid4().hex)
    selection = children.addSelectionInput(
        f"mesh_{kind}_faces_{row_id}", "Faces" if is_source else "Faces or bodies",
        "Select the faces that drive this source." if is_source else "Select faces or bodies for this surface group.",
    )
    host._add_selection_filters(selection, *(('Faces',) if is_source else ('Faces', 'SolidBodies', 'SurfaceBodies')))
    selection.setSelectionLimits(0, 0)
    if linked:
        # Fusion does not disable selection inputs with isEnabled. Keep linked
        # membership in the record and show a read-only face count instead.
        selection.isVisible = False
        children.addTextBoxCommandInput(f"mesh_linked_faces_{row_id}", "Faces", str(len(faces)), 1, True)
    elif state.activated:
        _restore_mesh_selections(selection, rows[row_id])
        selection.hasFocus = True
    value = children.addValueInput(f"mesh_{kind}_size_{row_id}", "Mesh size", "mm", host._mm_input(size))
    value.isEnabled = not linked
    if not linked:
        _mesh_button(children, f"mesh_remove_{kind}_{row_id}", "Remove")
    return row_id


def _restore_mesh_selections(selection, record):
    # Stable API: discard preselection once on activate, then restore saved faces.
    selection.clearSelection()
    missing = []
    for face in record["original_faces"]:
        try:
            selected = selection.addSelection(face)
        except Exception:
            selected = False
        if not selected:
            missing.append(wglink_send._face_key(face))
    record["unselected_saved_faces"] = set(missing)


MESH_DIALOG_RETRY = "Close and reopen Sources & Mesh… to retry before saving."


def _build_mesh_dialog(inputs, state):
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
            ("mesh_sources", "Sources", True), ("mesh_walls", "Walls", True),
            ("mesh_groups", "Surface Groups", True), ("mesh_advanced", "Advanced", False),
            ("mesh_estimate_section", "Estimate", True), ("mesh_validation_section", "Validation", True),
        ):
            group = inputs.addGroupCommandInput(input_id, title)
            group.isExpanded = expanded
        for kind, container, label in (("source", "mesh_sources", "Source"), ("group", "mesh_groups", "Group")):
            children = inputs.itemById(container).children
            if kind == "group":
                children.addTextBoxCommandInput("mesh_group_note", "",
                    "Optional: faces or bodies meshed finer or coarser than the walls.", 2, True)
            _mesh_button(children, f"mesh_add_{kind}", f"Add {label}")
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
        for source in sources:
            record = {**source, "size_mm": mesh["source_size_mm"][source["id"]]}
            faces = state.context["members"].get(source["id"], [])
            _add_mesh_row(inputs, state, "source", record, faces)
            state.original_sources.append({**record, "original_faces": list(faces)})
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
        faces = (record["original_faces"] if record.get("instance_id") or not state.activated
                 else _mesh_members(inputs, "source", row_id))
        role = (wglink_author.resolve_source_choice(record["role"]) if record.get("instance_id")
                else host._selected_name(inputs, f"mesh_source_role_{row_id}", "HF"))
        sources.append(wglink_mesh.normalize_source({**record, "id": record.get("id", f"new-{row_id}"), "role": role,
                        "faces": faces, "size_mm": (record["size_mm"] if record.get("instance_id")
                            else host._mm_value(inputs, f"mesh_source_size_{row_id}"))}))
    for row_id, record in state.groups.items():
        groups.append({**record, "name": str(inputs.itemById(f"mesh_group_name_{row_id}").value).strip(),
                       "size_mm": host._mm_value(inputs, f"mesh_group_size_{row_id}"),
                       "faces": (_mesh_members(inputs, "group", row_id) if state.activated else record["original_faces"])})
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
                "label": (f"Source {row['row_id']} ({row['role']})" if kind == "source"
                          else f"Group {row['row_id']} ({row['name'] or 'unnamed'})"),
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


def _sync_mesh_dialog(inputs, state):
    sources, groups, mesh = _mesh_dialog_values(inputs, state)
    errors = _mesh_dialog_errors(sources, groups, mesh, state)
    warnings = []
    wall = mesh["wall_size_mm"]
    for source in sources:
        if (source["size_mm"] is not None and wall is not None and math.isfinite(wall)
                and 0 < wall < source["size_mm"] and math.isfinite(source["size_mm"])):
            warnings.append(f"Source {source['row_id']} ({source['role']}): {source['size_mm']:g} mm is coarser than the walls ({mesh['wall_size_mm']:g} mm); it will be meshed at {mesh['wall_size_mm']:g} mm.")
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
    _, snapshot = _mesh_snapshot_values(sources, groups, mesh)
    wglink_send.save_mesh_settings(design, snapshot)
    host._source_authoring_generation += 1
    return {"summary": "Saved sources and mesh settings in this document."}


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
            for kind, rows in (("source", self.state.sources), ("group", self.state.groups)):
                if input_id == f"mesh_add_{kind}":
                    record = None
                    if kind == "source":
                        used = {host._selected_name(inputs, f"mesh_source_role_{row}", "HF") for row in rows}
                        record = {"role": next((role for role in wglink_protocol.MESH_DEFAULTS["source_size_mm"] if role not in used), "HF")}
                    _add_mesh_row(inputs, self.state, kind, record)
                elif input_id.startswith(f"mesh_remove_{kind}_"):
                    row = int(input_id.rsplit("_", 1)[1])
                    if not rows[row].get("instance_id"):
                        inputs.itemById(f"mesh_{kind}_{row}").deleteMe()
                        del rows[row]
            if input_id.startswith("mesh_group_preset_"):
                row = int(input_id.rsplit("_", 1)[1])
                name = host._selected_name(inputs, input_id, "Custom").split()[0]
                if name in wglink_protocol.MESH_PRESETS:
                    inputs.itemById(f"mesh_group_size_{row}").value = wglink_protocol.MESH_PRESETS[name] / 10
            elif input_id.startswith("mesh_group_size_"):
                row = int(input_id.rsplit("_", 1)[1])
                size = host._mm_value(inputs, input_id)
                label = next((f"{name} {value:g} mm" for name, value in wglink_protocol.MESH_PRESETS.items() if value == size), "Custom")
                _mesh_select(inputs.itemById(f"mesh_group_preset_{row}"), label)
            elif input_id == "mesh_reset":
                defaults = wglink_protocol.MESH_DEFAULTS
                inputs.itemById("mesh_wall").value = defaults["wall_size_mm"] / 10
                inputs.itemById("mesh_grading").value = defaults["grading_distance_mm"] / 10
                for row, source in self.state.sources.items():
                    if not source.get("instance_id"):
                        role = host._selected_name(inputs, f"mesh_source_role_{row}", "HF")
                        inputs.itemById(f"mesh_source_size_{row}").value = wglink_protocol.default_source_size_mm(role) / 10
                for row in self.state.groups:
                    inputs.itemById(f"mesh_group_size_{row}").value = wglink_protocol.MESH_PRESETS["Fine"] / 10
                    label = f"Fine {wglink_protocol.MESH_PRESETS['Fine']:g} mm"
                    _mesh_select(inputs.itemById(f"mesh_group_preset_{row}"), label)
            _sync_mesh_dialog(inputs, self.state)
        except Exception as exc:
            host._report_error("Updating Sources & Mesh…", f"{host.PANEL_NAME} error", exc)
        finally:
            self.state.restoring = False


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
            for kind, rows in (("source", state.sources), ("group", state.groups)):
                for row_id, record in rows.items():
                    if record.get("instance_id"):
                        continue
                    selection = inputs.itemById(f"mesh_{kind}_faces_{row_id}")
                    _restore_mesh_selections(selection, record)
            state.activated = True
            # Hidden linked rows cannot receive focus. Choose an editable row
            # once; subsequent clicks, Orbit/Pan and validation keep user focus.
            editable = [inputs.itemById(f"mesh_{kind}_faces_{row_id}")
                        for kind, rows in (("source", state.sources), ("group", state.groups))
                        for row_id, record in rows.items() if not record.get("instance_id")]
            if editable:
                editable[0].hasFocus = True
            _sync_mesh_dialog(inputs, state)
        finally:
            state.restoring = False


class MeshDialogDestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        args.command.parentCommandDefinition.name = host.COMMANDS["source"][1]
