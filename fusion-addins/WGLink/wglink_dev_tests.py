"""Developer-only in-Fusion test runner, driven from a terminal.

Present only in a dev-synced install (``wglink_dev.json`` beside the add-in).
A request is a JSON file ``<ipc>/.dev-tests/requests/<id>.json`` holding
``{"id": ..., "scenario": ...}``; ``scripts/fusion_dev_test.py`` writes one
and waits. A worker thread only looks at the folder and fires a custom event;
every adsk call happens in that event's handler, on Fusion's main thread.
A scenario is a generator: each ``yield`` hands Fusion back for one tick
(about a second), so a command started with ``execute()`` can open and the
viewport can redraw before the next step. The result, including errors, is
written to ``<ipc>/.dev-tests/results/<id>.json``; nothing is raised into
Fusion.
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import sys
import threading
import traceback
from typing import Any, Callable, Iterator

DEV_TESTS_DIRECTORY = ".dev-tests"
TICK_SECONDS = 1.0
MAX_TICKS = 180


def folders(ipc: Path) -> tuple[Path, Path]:
    root = Path(ipc) / DEV_TESTS_DIRECTORY
    return root / "requests", root / "results"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(temporary, path)


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
            self.log(f"[WGLink dev tests] runner error:\n{traceback.format_exc()}")

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
            self.log(f"[WGLink dev tests] started {run.scenario} ({run.id})")
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
        self.log(f"[WGLink dev tests] {run.scenario} ({run.id}): {run.result['status']}")


class Watcher:
    """A thread that only reads the folder and fires the custom event."""

    def __init__(self, app: Any, event_id: str, runner: Runner) -> None:
        self.app = app
        self.event_id = event_id
        self.runner = runner
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="WGLinkDevTests", daemon=True)

    def _loop(self) -> None:
        while not self.stop_flag.wait(TICK_SECONDS):
            if self.runner.pending():
                try:
                    self.app.fireCustomEvent(self.event_id, "")
                except Exception:  # noqa: BLE001 - Fusion shutting down
                    pass

    def stop(self) -> None:
        self.stop_flag.set()
        if self.thread.is_alive():
            self.thread.join(timeout=TICK_SECONDS + 1)


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


def _items(collection: Any) -> list[Any]:
    if collection is None:
        return []
    if hasattr(collection, "item") and hasattr(collection, "count"):
        return [collection.item(index) for index in range(collection.count)]
    return list(collection)


def _bodies(design: Any) -> list[Any]:
    """Bodies in assembly context: root bodies, then every occurrence's proxies."""

    root = design.rootComponent
    bodies = list(_items(root.bRepBodies))
    for occurrence in _items(getattr(root, "allOccurrences", None)):
        bodies.extend(_items(occurrence.bRepBodies))
    return bodies


def _appearance(face: Any) -> str | None:
    try:
        appearance = face.appearance
        return str(appearance.name) if appearance is not None else None
    except Exception:  # noqa: BLE001
        return None


def snapshot(design: Any) -> dict[str, Any]:
    """What the spike must leave untouched, keyed by position, not token."""

    faces = {}
    for body_index, body in enumerate(_bodies(design)):
        for face_index, face in enumerate(_items(body.faces)):
            try:
                source = int(face.appearanceSourceType)
            except Exception:  # noqa: BLE001
                source = None
            faces[f"{body_index}:{getattr(body, 'name', '?')}:{face_index}"] = [source, _appearance(face)]
    attributes = []
    for attribute in _items(design.findAttributes("WGLink", "")):
        attributes.append([str(getattr(attribute, "name", "")), str(getattr(attribute, "value", ""))])
    timeline = getattr(design, "timeline", None)
    try:
        timeline_count = int(timeline.count) if timeline is not None else None
    except Exception:  # noqa: BLE001 - a direct-modeling design has none
        timeline_count = None
    return {
        "faces": faces,
        "attributes": sorted(attributes),
        "appearances": sorted(str(item.name) for item in _items(design.appearances)),
        "timeline": timeline_count,
    }


def _diff(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    changes = []
    for key in sorted(set(before["faces"]) | set(after["faces"])):
        if before["faces"].get(key) != after["faces"].get(key):
            changes.append(f"face {key}: {before['faces'].get(key)} -> {after['faces'].get(key)}")
    for name in ("attributes", "appearances", "timeline"):
        if before[name] != after[name]:
            changes.append(f"{name}: {before[name]} -> {after[name]}")
    return changes




# -- helpers for driving a real WGLink dialog ------------------------------------


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


def top_face(body: Any) -> Any:
    return max(_items(body.faces), key=lambda face: float(face.pointOnFace.z))


def latest_dialog_state(context: Any, handler_type: str) -> tuple[Any, Any] | None:
    """The newest (handler, state) of a WGLink dialog, from the add-in's handler list."""

    for handler in reversed(list(context.handlers)):
        if type(handler).__name__ == handler_type and getattr(handler, "state", None) is not None:
            return handler, handler.state
    return None


def notify_changed(handler: Any, item: Any) -> None:
    """Deliver one inputChanged to the dialog's own handler, as a user's click does."""

    handler.notify(_ChangedArgs(item))


class _ChangedArgs:
    def __init__(self, item: Any) -> None:
        self.input = item
        self.inputs = item.commandInputs


def _text(inputs: Any, input_id: str) -> str | None:
    item = inputs.itemById(input_id)
    if item is None:
        return None
    try:
        return str(item.formattedText)
    except Exception:  # noqa: BLE001
        return None


# -- scenarios -------------------------------------------------------------------


def sources_mesh_baseline(context: Any) -> Callable[[Run], Iterator[None]]:
    """Today's Sources & Mesh… on a scratch box: Add Source, pick the top face, Save, reopen.

    Runs in a new unsaved design and closes it without saving, so the user's
    own document is never edited; its state is compared before and after.
    The "Saved" message box is captured instead of shown, so nothing waits for
    a click.
    """

    def scenario(run: Run) -> Iterator[None]:
        app, ui, wglink = context.app(), context.ui, context.wglink
        source_id = wglink["COMMANDS"]["source"][0]
        role_name = wglink["wglink_author"].role_appearance_name("HF")
        user_document = app.activeDocument
        user_before = snapshot(context.design()) if user_document is not None else None
        messages: list[list[str]] = []
        original_message = wglink["_message"]
        wglink["_message"] = lambda text, title="": messages.append([str(title), str(text)])
        run.result["messages"] = messages
        document = None
        try:
            document, design, body = scratch_box(app)
            yield
            face = top_face(body)
            run.result["scratchDocument"] = str(document.name)

            def open_dialog(label: str) -> Iterator[None]:
                known = latest_dialog_state(context, "MeshInputChangedHandler")
                ui.commandDefinitions.itemById(source_id).execute()
                for _ in range(15):
                    yield
                    found = latest_dialog_state(context, "MeshInputChangedHandler")
                    if found is not None and found != known and found[1].activated:
                        run.result.setdefault("opened", []).append(label)
                        return
                raise ScenarioFailed(f"Sources & Mesh… did not open ({label})")

            def record(step: str, inputs: Any = None, **extra: Any) -> dict[str, Any]:
                entry = {
                    "step": step,
                    "faceAppearance": _appearance(face),
                    "activeCommand": str(ui.activeCommand),
                    "messages": list(messages),
                    **extra,
                }
                if inputs is not None:
                    entry["validation"] = _text(inputs, "mesh_validation")
                    entry["estimate"] = _text(inputs, "mesh_estimate")
                entry["screenshot"] = screenshot(app, run, step)
                run.result["steps"].append(entry)
                return entry

            record("scratch box")
            yield from open_dialog("first")
            handler, state = latest_dialog_state(context, "MeshInputChangedHandler")
            inputs = state.command.commandInputs
            record("opened", inputs, sources=len(state.sources))
            notify_changed(handler, inputs.itemById("mesh_add_source"))
            row = max(state.sources)
            picker = inputs.itemById(f"mesh_source_faces_{row}")
            run.check("new source row is HF",
                      inputs.itemById(f"mesh_source_role_{row}").selectedItem.name == "HF")
            picker.hasFocus = True
            run.check("Fusion accepted the pick", bool(picker.addSelection(face)))
            notify_changed(handler, picker)
            yield
            step = record("HF face picked", inputs, selectionCount=picker.selectionCount)
            run.check("picker holds the face", step["selectionCount"] == 1)

            state.command.doExecute(True)
            for _ in range(10):
                yield
                if ui.activeCommand != source_id:
                    break
            else:
                raise ScenarioFailed("Save (doExecute) did not close Sources & Mesh…")
            yield
            step = record("saved")
            run.check("face carries the HF appearance after Save", step["faceAppearance"] == role_name,
                      step["faceAppearance"])
            run.check("Save reported success", any("Saved" in text for _title, text in messages), messages)

            yield from open_dialog("reopen")
            handler, state = latest_dialog_state(context, "MeshInputChangedHandler")
            inputs = state.command.commandInputs
            rows = {row_id: record_["role"] for row_id, record_ in state.sources.items()}
            hf_rows = [row_id for row_id, role in rows.items()
                       if wglink["wglink_author"].resolve_source_choice(role) == "HF"]
            picker = inputs.itemById(f"mesh_source_faces_{hf_rows[0]}") if hf_rows else None
            picked = []
            if picker is not None:
                picked = [picker.selection(i).entity for i in range(picker.selectionCount)]
            yield
            step = record("reopened", inputs, sources=rows, selectionCount=len(picked))
            run.check("reopened dialog has the HF source", bool(hf_rows), rows)
            run.check("reopened HF picker shows the saved face",
                      len(picked) == 1 and picked[0] == face, len(picked))
            ui.terminateActiveCommand()
            yield
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
            changes = _diff(user_before, snapshot(context.design()))
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
