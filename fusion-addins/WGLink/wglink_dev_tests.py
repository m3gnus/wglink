"""Developer-only in-Fusion test runner, driven from a terminal.

Present only in a dev-synced install (``wglink_dev.json`` beside the add-in).
A request is a JSON file ``<ipc>/.dev-tests/requests/<id>.json`` holding
``{"id": ..., "scenario": ..., "modules": [...]}``; ``scripts/fusion_dev_test.py``
writes one and waits. WGLink.py polls with its one-shot timers and runs
``Runner.tick`` through its follow-up event, so every adsk call happens on
Fusion's main thread, outside any WGLink command. A scenario is a generator:
each ``yield`` hands Fusion back for one tick (about a second), so a command
started with ``execute()`` can open and the viewport can redraw. The result,
errors included, is written atomically to ``<ipc>/.dev-tests/results/<id>.json``
with viewport screenshots in ``results/<id>/``; nothing is raised into Fusion.

Scenarios:

- ``noop``: proves the runner and a two-tick scenario work.
- ``reload``: reloads this module and the helper modules a request names, in
  place, so a dev sync of them needs no add-in restart (WGLink.py does).
- ``sources-mesh-baseline``: Sources & Mesh… on an unsaved scratch box: picks
  for HF with live colour, Save, reopen, a size zone, a face moved between
  rows and back, row selection, a cleared driver, repeated and unchanged
  previews, and Cancel leaving the design exactly as it was; then the scratch
  design is closed and the user's own document checked unchanged.

This module imports only the standard library and ``wglink_inbox`` (its
atomic writer), so the terminal script can import it; scenarios reach every
Fusion-side module through ``context.wglink`` (WGLink.py's namespace).
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import sys
import traceback
from typing import Any, Callable, Iterator

if __package__:
    from . import wglink_inbox
else:
    import wglink_inbox

DEV_TESTS_DIRECTORY = ".dev-tests"
TICK_SECONDS = 1.0
MAX_TICKS = 240


def folders(ipc: Path) -> tuple[Path, Path]:
    root = Path(ipc) / DEV_TESTS_DIRECTORY
    return root / "requests", root / "results"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """The add-in's own atomic writer, after making the folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    wglink_inbox._write_json_atomically(path, json.loads(json.dumps(payload, default=str)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ScenarioFailed(Exception):
    """A scenario's own check failed; recorded as status "failed"."""


class Run:
    def __init__(self, request_id: str, scenario: str, results: Path,
                 request: dict[str, Any] | None = None) -> None:
        self.request = dict(request or {})
        self.id = request_id
        self.scenario = scenario
        self.folder = results / request_id
        self.result: dict[str, Any] = {
            "id": request_id, "scenario": scenario, "status": "running",
            "startedAt": _now(), "steps": [], "checks": [],
        }

    def check(self, name: str, ok: bool, detail: object = None) -> bool:
        self.result["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)


class Runner:
    """Picks up requests and advances at most one scenario per tick."""

    def __init__(self, ipc: Path, scenarios: dict[str, Callable[[Run], Iterator[None]]],
                 log: Callable[[str], None]) -> None:
        self.requests, self.results = folders(ipc)
        self.scenarios = scenarios
        self.log = log
        self.run: Run | None = None
        self.steps: Iterator[None] | None = None
        self.ticks = 0

    def pending(self) -> bool:
        if self.run is not None:
            return True
        try:
            return any(self.requests.glob("*.json"))
        except OSError:
            return False

    def tick(self) -> None:
        try:
            if self.run is None:
                self._start_next()
            if self.run is not None:
                self._advance()
        except Exception:  # noqa: BLE001 - the runner itself must never raise into Fusion
            self.log(f"[dev tests] runner error:\n{traceback.format_exc()}")

    def _start_next(self) -> None:
        try:
            requests = sorted(self.requests.glob("*.json"), key=lambda path: path.stat().st_mtime)
        except OSError:
            return
        for path in requests:
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                request = None
            try:
                path.unlink()
            except OSError:
                pass
            if not isinstance(request, dict):
                continue
            request_id = str(request.get("id") or path.stem)
            run = Run(request_id, str(request.get("scenario") or ""), self.results, request)
            scenario = self.scenarios.get(run.scenario)
            if scenario is None:
                run.result.update(status="error", error=f"unknown scenario {run.scenario!r}; "
                                  f"known: {sorted(self.scenarios)}")
                self._finish(run)
                continue
            self.run, self.steps, self.ticks = run, scenario(run), 0
            self.log(f"[dev tests] started {run.scenario} ({run.id})")
            return

    def _advance(self) -> None:
        run = self.run
        self.ticks += 1
        try:
            if self.ticks > MAX_TICKS:
                raise ScenarioFailed(f"timed out after {MAX_TICKS} ticks")
            next(self.steps)
            return
        except StopIteration:
            failed = [check["name"] for check in run.result["checks"] if not check["ok"]]
            run.result["status"] = "failed" if failed else "passed"
            if failed:
                run.result["error"] = "failed checks: " + ", ".join(failed)
        except ScenarioFailed as exc:
            run.result.update(status="failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            run.result.update(status="error", error=repr(exc), traceback=traceback.format_exc())
        try:
            self.steps.close()
        except Exception:  # noqa: BLE001
            pass
        self.run = self.steps = None
        self._finish(run)

    def _finish(self, run: Run) -> None:
        run.result["finishedAt"] = _now()
        write_json(self.results / f"{run.id}.json", run.result)
        self.log(f"[dev tests] {run.scenario} ({run.id}): {run.result['status']}")


# -- scenarios -------------------------------------------------------------------


def noop(context: Any) -> Callable[[Run], Iterator[None]]:
    def scenario(run: Run) -> Iterator[None]:
        app = context.app()
        run.result["fusionVersion"] = str(getattr(app, "version", "?"))
        document = getattr(app, "activeDocument", None)
        run.result["document"] = str(getattr(document, "name", None))
        yield  # proves the multi-tick path
        run.check("main thread tick", True)

    return scenario


def snapshot(wglink: dict[str, Any], design: Any) -> dict[str, Any]:
    """What a scenario must leave untouched: faces' paint, WGLink attributes,
    appearance assets and the timeline, keyed by position (tokens can change)."""

    core, send = wglink["wglink_core"], wglink["wglink_send"]
    faces = {}
    for body_index, body in enumerate(send.design_bodies(design)):
        for face_index, face in enumerate(core._items(body.faces)):
            try:
                source = int(face.appearanceSourceType)
            except Exception:  # noqa: BLE001
                source = None
            faces[f"{body_index}:{getattr(body, 'name', '?')}:{face_index}"] = [source, core._appearance_name(face)]
    attributes = sorted([str(getattr(attribute, "name", "")), str(getattr(attribute, "value", ""))]
                        for attribute in core._items(design.findAttributes(core.ATTRIBUTE_GROUP, "")))
    try:
        timeline = int(design.timeline.count)
    except Exception:  # noqa: BLE001 - a direct-modeling design has none
        timeline = None
    return {"faces": faces, "attributes": attributes, "timeline": timeline,
            "appearances": sorted(str(item.name) for item in core._items(design.appearances))}


def _diff(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    changes = []
    for key in sorted(set(before["faces"]) | set(after["faces"])):
        if before["faces"].get(key) != after["faces"].get(key):
            changes.append(f"face {key}: {before['faces'].get(key)} -> {after['faces'].get(key)}")
    for name in ("attributes", "appearances", "timeline"):
        if before[name] != after[name]:
            changes.append(f"{name}: {before[name]} -> {after[name]}")
    return changes


def screenshot(app: Any, run: Run, step: str) -> str:
    index = len(run.result["steps"]) + 1
    path = run.folder / f"{index:02d}-{step}.png"
    try:
        run.folder.mkdir(parents=True, exist_ok=True)
        saved = app.activeViewport.saveAsImageFile(str(path), 1200, 800)
    except Exception as exc:  # noqa: BLE001
        return f"not saved: {exc!r}"
    return str(path) if saved else "not saved: Fusion returned False"


def scratch_box(app: Any) -> tuple[Any, Any, Any]:
    """A new, unsaved design holding one 40 x 40 x 20 mm box: (document, design, body)."""

    import adsk.core
    import adsk.fusion

    document = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    design = adsk.fusion.Design.cast(app.activeProduct)
    root = design.rootComponent
    sketch = root.sketches.add(root.xYConstructionPlane)
    sketch.sketchCurves.sketchLines.addTwoPointRectangle(
        adsk.core.Point3D.create(0, 0, 0), adsk.core.Point3D.create(4, 4, 0))
    extrude = root.features.extrudeFeatures.addSimple(
        sketch.profiles.item(0), adsk.core.ValueInput.createByReal(2),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    body = extrude.bodies.item(0)
    body.name = "Scratch horn"
    return document, design, body


def latest_dialog_state(context: Any, handler_type: str) -> tuple[Any, Any] | None:
    """The newest (handler, state) of a WGLink dialog, from the add-in's handler list."""

    for handler in reversed(list(context.handlers)):
        if type(handler).__name__ == handler_type and getattr(handler, "state", None) is not None:
            return handler, handler.state
    return None


class _ChangedArgs:
    """What Fusion hands an inputChanged handler, for one scripted change."""

    def __init__(self, item: Any) -> None:
        self.input = item
        self.inputs = item.commandInputs


def _text(inputs: Any, input_id: str) -> str | None:
    item = inputs.itemById(input_id)
    try:
        return str(item.formattedText) if item is not None else None
    except Exception:  # noqa: BLE001
        return None


def sources_mesh_baseline(context: Any) -> Callable[[Run], Iterator[None]]:
    """Sources & Mesh… on a scratch box, driven through its own handlers (see the module doc)."""

    def scenario(run: Run) -> Iterator[None]:
        app, ui, wglink = context.app(), context.ui, context.wglink
        core, author = wglink["wglink_core"], wglink["wglink_author"]
        dialog_module = wglink["wglink_mesh_dialog"]
        source_id = wglink["COMMANDS"]["source"][0]
        colour = {role: author.role_appearance_name(role) for role in ("HF", "LF")}
        user_document = app.activeDocument
        user_before = snapshot(wglink, context.design()) if user_document is not None else None
        messages: list[list[str]] = []
        original_message = wglink["_message"]
        wglink["_message"] = lambda text, title="": messages.append([str(title), str(text)])
        run.result["messages"] = messages
        document = None
        handler = state = inputs = None
        try:
            document, design, body = scratch_box(app)
            yield
            faces = core._items(body.faces)
            top = max(faces, key=lambda f: float(f.pointOnFace.z))
            side = max(faces, key=lambda f: float(f.pointOnFace.x))
            back = min(faces, key=lambda f: float(f.pointOnFace.x))
            run.result["scratchDocument"] = str(document.name)

            def appearance(face):
                return core._appearance_name(face)

            def open_dialog(label: str) -> Iterator[None]:
                nonlocal handler, state, inputs
                known = latest_dialog_state(context, "MeshInputChangedHandler")
                ui.commandDefinitions.itemById(source_id).execute()
                for _ in range(15):
                    yield
                    found = latest_dialog_state(context, "MeshInputChangedHandler")
                    if found is not None and found != known and found[1].activated:
                        handler, state = found
                        inputs = state.command.commandInputs
                        return
                raise ScenarioFailed(f"Sources & Mesh… did not open ({label})")

            def record(step: str, **extra: Any) -> dict[str, Any]:
                entry = {"step": step, "top": appearance(top), "side": appearance(side), "back": appearance(back),
                         "activeCommand": str(ui.activeCommand), "messages": list(messages), **extra}
                if inputs is not None and ui.activeCommand == source_id:
                    entry.update(validation=_text(inputs, "mesh_validation"), notice=state.notice,
                                 target=list(state.target or ()), previewFailure=state.preview_failure)
                entry["screenshot"] = screenshot(app, run, step)
                run.result["steps"].append(entry)
                return entry

            def row(role):
                return next(row_id for row_id, record_ in state.sources.items()
                            if record_["role"] == role and not record_.get("instance_id"))

            def faces_in(kind, row_id):
                return list((state.sources if kind == "source" else state.groups)[row_id]["faces"])

            def changed(input_id):
                handler.notify(_ChangedArgs(inputs.itemById(input_id)))

            def preview():
                # Fusion previews after a user's change; a scripted change asks.
                try:
                    state.command.doExecutePreview()
                except Exception as exc:  # noqa: BLE001
                    run.result.setdefault("previewErrors", []).append(repr(exc))

            def pick(*entities):
                picker = inputs.itemById(dialog_module.PICKER)
                for entity in entities:
                    run.check("Fusion accepted a scripted pick", bool(picker.addSelection(entity)))
                changed(dialog_module.PICKER)
                preview()

            def save() -> Iterator[None]:
                state.command.doExecute(True)
                for _ in range(10):
                    yield
                    if ui.activeCommand != source_id:
                        return
                raise ScenarioFailed("Save (doExecute) did not close Sources & Mesh…")

            record("scratch box")
            # 1. Pick the top face for HF: Save's colour shows live, then Save keeps it.
            yield from open_dialog("first")
            step = record("opened")
            run.check("a fresh model shows guidance, not an error",
                      "Fix before saving" not in (step["validation"] or ""), step["validation"])
            run.check("the picker starts on the HF row", state.target == ("source", row("HF")))
            pick(top)
            yield
            step = record("HF picked", hf=len(faces_in("source", row("HF"))))
            run.check("HF row holds the face", step["hf"] == 1)
            run.check("live: the HF colour shows before Save", step["top"] == colour["HF"], step["top"])
            run.check("live: the picker keeps the face through the preview",
                      inputs.itemById(dialog_module.PICKER).selectionCount == 1)
            preview()
            yield
            run.check("two previews in a row give the same colours", appearance(top) == colour["HF"])
            yield from save()
            yield
            step = record("saved")
            run.check("Save paints the HF colour", step["top"] == colour["HF"], step["top"])
            run.check("no message box after Save", not messages, messages)

            # 2. Reopen; a size edit keeps the target, the focus row and the colours.
            yield from open_dialog("reopen")
            shown = inputs.itemById(dialog_module.PICKER)
            run.check("reopened: HF holds the saved face and the picker shows it",
                      faces_in("source", row("HF")) == [top] and shown.selectionCount == 1)
            size = inputs.itemById(f"mesh_source_size_{row('HF')}")
            size.value = dialog_module.wglink_dialog_inputs.mm_to_internal(5)
            changed(size.id)
            preview()
            yield
            step = record("HF size edited")
            run.check("a size edit keeps the target", state.target == ("source", row("HF")))
            run.check("a size edit keeps the live colour", step["top"] == colour["HF"], step["top"])

            # 3. Row selection: does Fusion report a table's selected row?
            table = inputs.itemById(dialog_module.DRIVERS_TABLE)
            mf_index = list(state.sources).index(row("MF"))
            table.selectedRow = mf_index
            yield
            run.result["fusionReportsSelectedRow"] = state.target == ("source", row("MF"))
            if state.target != ("source", row("MF")):
                handler.notify(_ChangedArgs(table))
            yield
            step = record("MF row selected")
            run.check("selecting a row targets it", state.target == ("source", row("MF")))
            run.check("only one table shows a selected row",
                      inputs.itemById(dialog_module.ZONES_TABLE).selectedRow == -1
                      and table.selectedRow == mf_index)

            # 4. A size zone; a face picked for another row moves, and says so, and back.
            changed("mesh_add_group_Coarse")
            zone = next(reversed(state.groups))
            run.check("a new size zone becomes the target", state.target == ("group", zone))
            pick(side, top)
            yield
            step = record("zone picks", zone=len(faces_in("group", zone)), hf=len(faces_in("source", row("HF"))))
            run.check("the HF face moved into the size zone", (step["zone"], step["hf"]) == (2, 0), step)
            run.check("the move is announced", "Moved 1 face from HF" in state.notice, state.notice)
            changed(f"mesh_pick_source_{row('HF')}")
            run.check("retargeting clears the old notice", state.notice == "", state.notice)
            pick(top)
            yield
            zone_colour = author.group_appearance_name(state.groups[zone]["group_id"])
            step = record("HF face moved back")
            run.check("live: both rows show their colours",
                      (step["top"], step["side"]) == (colour["HF"], zone_colour), step)
            # 5. A driver row can be cleared.
            changed(f"mesh_pick_source_{row('LF')}")
            pick(back)
            changed(f"mesh_clear_source_{row('LF')}")
            preview()
            yield
            step = record("LF cleared", lf=len(faces_in("source", row("LF"))))
            run.check("a cleared driver row holds no faces and says so",
                      step["lf"] == 0 and "is not used" in state.notice, state.notice)
            run.check("live: the cleared face loses the LF colour", step["back"] != colour["LF"], step["back"])
            yield from save()
            yield
            step = record("saved with a size zone")
            run.check("Save keeps HF and paints the size zone", (step["top"], step["side"]) == (colour["HF"], zone_colour))
            run.check("still no message box", not messages, messages)

            # 6. Live colour is only a preview: Cancel leaves the design exactly as it was.
            before_cancel = snapshot(wglink, design)
            source_generation = wglink["wglink_send"]._source_authoring_generation
            mesh_generation = wglink["wglink_send"]._mesh_authoring_generation
            yield from open_dialog("cancel")
            changed(f"mesh_pick_source_{row('LF')}")
            pick(back)
            yield
            step = record("LF picked, not saved")
            run.check("live: the LF pick shows the LF colour", step["back"] == colour["LF"], step["back"])
            ui.terminateActiveCommand()
            yield
            yield
            record("cancelled")
            changes = _diff(before_cancel, snapshot(wglink, design))
            run.check("Cancel leaves the design as it was before the dialog", not changes, changes[:20])
            run.check("Cancel leaves authoring counters unchanged",
                      (wglink["wglink_send"]._source_authoring_generation,
                       wglink["wglink_send"]._mesh_authoring_generation) ==
                      (source_generation, mesh_generation))
        finally:
            wglink["_message"] = original_message
            if document is not None:
                try:
                    if ui.activeCommand == source_id:
                        ui.terminateActiveCommand()
                    run.result["scratchClosed"] = bool(document.close(False))
                except Exception as exc:  # noqa: BLE001
                    run.result["scratchClosed"] = repr(exc)
        if user_before is not None:
            yield
            changes = _diff(user_before, snapshot(wglink, context.design()))
            run.check("the user's own document is unchanged", not changes, changes[:20])

    return scenario


DEFAULT_RELOAD = ("wglink_dev_tests",)


def reload(context: Any) -> Callable[[Run], Iterator[None]]:
    """Reload this module (and ``{"modules": [...]}`` helpers) inside Fusion.

    Helper modules reload in place, so WGLink.py's references see the new
    code. WGLink.py itself cannot be reloaded this way: that needs a restart.
    """

    def scenario(run: Run) -> Iterator[None]:
        package = context.package
        extra = [str(name) for name in run.request.get("modules", []) if name not in DEFAULT_RELOAD]
        for name in [*extra, *DEFAULT_RELOAD]:
            module = sys.modules.get(f"{package}.{name}")
            if module is None:
                importlib.import_module(f"{package}.{name}")
            else:
                importlib.reload(module)
        run.result["reloaded"] = [*extra, *DEFAULT_RELOAD]
        fresh = sys.modules[f"{package}.wglink_dev_tests"]
        context.runner.scenarios = fresh.scenarios(context)
        run.result["scenarios"] = sorted(context.runner.scenarios)
        yield

    return scenario


def scenarios(context: Any) -> dict[str, Callable[[Run], Iterator[None]]]:
    found = {"noop": noop(context)}
    if getattr(context, "package", None):
        found["reload"] = reload(context)
        found["sources-mesh-baseline"] = sources_mesh_baseline(context)
    return found
