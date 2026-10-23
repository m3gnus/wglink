"""Fusion command shell for the head-less WGLink adapter."""

from __future__ import annotations

import hashlib
import html
import importlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import sys
import threading
import time
import traceback
import types
import uuid

import adsk.core


ADDIN_DIR = Path(__file__).resolve().parent
_watch_session_id = str(uuid.uuid4())
_registration_package_name = (
    f"_hornlab_wglink_registration_{_watch_session_id.replace('-', '_')}"
)


def _load_registration_package() -> dict[str, types.ModuleType]:
    """Load every helper under this registration's private package namespace.

    Fusion caches ordinary imports process-wide even though it loads every
    registered add-in entry point as a separate module. A private package keeps
    a newer or older WGLink checkout from borrowing any executable helper from
    the registration that happened to start first.
    """

    package = types.ModuleType(_registration_package_name)
    package.__file__ = str(ADDIN_DIR / "__init__.py")
    package.__package__ = _registration_package_name
    package.__path__ = [str(ADDIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[_registration_package_name] = package
    loaded: dict[str, types.ModuleType] = {}
    for name in (
        "wglink_activity",
        "wglink_locator",
        "wglink_workspace",
        "wglink_author",
        "wglink_fem",
        "wglink_bundle",
        "wglink_core",
        "wglink_return",
        "wglink_send",
        "wglink_inbox",
        "wglink_handoff",
        "wglink_status",
        "wglink_launcher",
        "wglink_legacy_queue",
    ):
        loaded[name] = importlib.import_module(f"{_registration_package_name}.{name}")
    return loaded


_registration_modules = _load_registration_package()
wglink_activity = _registration_modules["wglink_activity"]
wglink_workspace = _registration_modules["wglink_workspace"]
wglink_author = _registration_modules["wglink_author"]
wglink_fem = _registration_modules["wglink_fem"]
wglink_core = _registration_modules["wglink_core"]
wglink_send = _registration_modules["wglink_send"]
wglink_mesh = wglink_send.wglink_mesh
wglink_protocol = wglink_send.wglink_protocol
wglink_inbox = _registration_modules["wglink_inbox"]
wglink_handoff = _registration_modules["wglink_handoff"]
wglink_status = _registration_modules["wglink_status"]
wglink_launcher = _registration_modules["wglink_launcher"]
wglink_legacy_queue = _registration_modules["wglink_legacy_queue"]
format_measurement_mm = _registration_modules["wglink_bundle"].format_measurement_mm


PANEL_ID = "hornlab_wglink_panel"
PANEL_NAME = "Waveguide Generator"
BROWSE_FOLDER = "Browse for a bundle folder…"
BROWSE_ZIP = "Browse for a zipped .wglink file…"
SETTINGS_PATH = Path.home() / ".hornlab" / "WGLink" / "settings.json"
COMMANDS = {
    "source": (
        "hornlab_wglink_set_source",
        "Sources & Mesh…",
        "Set sources, mesh sizes and surface groups in this document.",
    ),
    "declare": (
        "hornlab_wglink_declare_body",
        "Declare Body…",
        "Classify a body as the exterior WG solves, or leave it out of the return.",
    ),
    "fem": (
        "hornlab_wglink_declare_fem_region",
        "Declare FEM Region…",
        "Mark a solid body of air as a FEM region and name its driver, interface "
        "and termination faces for a coupled FEM/BEM solve.",
    ),
    "solve": (
        "hornlab_wglink_solve",
        "Solve in WG",
        "Export the assembly and ask Waveguide Generator to prepare and solve it.",
    ),
    "insert": (
        "hornlab_wglink_insert",
        "Insert",
        "Insert the full WG viewport model from a .wglink bundle.",
    ),
    "update": (
        "hornlab_wglink_update",
        "Update",
        "Rebuild a managed link in place from its current bundle.",
    ),
    "send": (
        "hornlab_wglink_send",
        "Send to WG",
        "Export the displayed acoustic assembly as a validated .wgreturn bundle.",
    ),
    "detach": (
        "hornlab_wglink_detach",
        "Detach",
        "Remove Waveguide Generator link data without changing bodies or features.",
    ),
}
# The commands a user starts. Everything else is maintenance or recovery and
# lives under one dropdown; the automatic handoff already covers the ordinary
# Insert and Update.
#
# Sources & Mesh is promoted even though it is authoring rather than an export:
# a model with no painted source face cannot be sent at all, and the convention
# is invisible -- it used to be "rename a Fusion appearance to exactly HF".
# Filed under a dropdown named "Manage Link…" it would read as maintenance
# for an existing link, which is exactly what a from-scratch model does not
# have. Declare Body stays in the dropdown: it is the remedy for one refusal,
# and that refusal names it.
PROMOTED_COMMANDS = ("source", "solve", "send")

_handlers: list[object] = []
_definitions: list[object] = []
_controls: list[object] = []
_panel = None
# True only after this registration committed a complete panel + file request service
# transaction. The process-wide lease is separately checked before touching
# shared UI during shutdown, because an expired owner may already be replaced.
_owned = False

WATCH_INTERVAL_SECONDS = 4.0
_installed_source_cache: dict[str, object] | None = None
_geometry_state_cache: dict[str, object] | None = None
# At most one pending geometry refresh per add-in instance. A refresh is asked
# for by something a user or WG did -- startup, a document switch, a finished
# command -- and is paid for once on the next tick that may run it. The
# periodic heartbeat never asks: a queue of requests, or a timer that requested
# its own, would reinstate exactly the four-second main-thread load this
# design removes.
_geometry_refresh_pending: dict[str, str | None] | None = None
# When a document with no usable observation may be asked about again, and
# when each was last attempted. A request can be spent without answering
# anything -- the document stopped being nameable between the tick that asked
# and the tick that would have paid, the measurement threw -- and a request
# asked once and lost is a document that reports no state for the rest of the
# session. This is the retry.
#
# **A rate, not a budget.** A fixed number of attempts is D1's symptom behind a
# counter: ``_measure_geometry_state`` swallows every exception and returns
# empty hashes, so a document that is still loading or regenerating looks
# exactly like one that can never be measured, spends the allowance in a few
# fast ticks, and then publishes an empty ``documentSignatureHash`` for ever
# however measurable it becomes. A rate costs an unmeasurable document about
# one attempt a minute and never gives up. A measurement that produces a
# signature hash clears the entry, and the add-in stops asking entirely.
GEOMETRY_REFRESH_RETRY_SECONDS = 60.0
_GEOMETRY_REFRESH_ATTEMPTS_TRACKED = 64
_geometry_refresh_attempts: dict[str, float] = {}
OWNER_LEASE_SECONDS = WATCH_INTERVAL_SECONDS * 3
WATCH_EVENT_ID = f"hornlab_wglink_file_requests_{_watch_session_id}"
_candidate_event_id = f"hornlab_wglink_owner_candidate_{_watch_session_id}"

# Executable add-in modules are registration-local. This deliberately tiny
# data-only broker is the one shared object: it serializes which registration
# may service WGLink's machine-local IPC inside this Fusion process. The lease
# is renewed by the owner's worker without making Fusion API calls.
_RUNTIME_MODULE_NAME = "_hornlab_wglink_ipc_owner_runtime_v1"
_runtime_candidate = types.ModuleType(_RUNTIME_MODULE_NAME)
_runtime_candidate.lock = threading.RLock()  # type: ignore[attr-defined]
_runtime_candidate.owner = None  # type: ignore[attr-defined]
_runtime_candidate.phase = None  # type: ignore[attr-defined]
_runtime_candidate.renewed_at = 0.0  # type: ignore[attr-defined]
_runtime_candidate.generation = 0  # type: ignore[attr-defined]
_runtime_candidate.watch_event_id = None  # type: ignore[attr-defined]
_ipc_owner_runtime = sys.modules.setdefault(_RUNTIME_MODULE_NAME, _runtime_candidate)
if not hasattr(_ipc_owner_runtime, "watch_event_id"):
    _ipc_owner_runtime.watch_event_id = None

_watch_event = None
_watch_handler = None
_watch_stop: threading.Event | None = None
_watch_thread: threading.Thread | None = None
_candidate_event = None
_candidate_handler = None
_candidate_stop: threading.Event | None = None
_candidate_thread: threading.Thread | None = None
_replaced_watch_event_id: str | None = None
# Set while a WGLink command runs. The heartbeat keeps ticking, but the handler
# refuses to raise a prompt over a command that is mid-execution.
_command_busy = False
# The marker remains on disk when Insert refuses, so remember that refusal for
# this Fusion session instead of showing the same error every four seconds. A
# later Send carries a new export id and is attempted normally.
_handoff_attempted_id: str | None = None
# A refused return request also remains on disk. Suppress repeated attempts for
# that exact request while allowing a later request id through normally.
_return_request_attempted_id: str | None = None
# The WG round trip this add-in last worked on, and which attempt that was,
# published in the heartbeat's diagnostics. One correlation id per round trip:
# WG's operation id for its own requests (the export id for a handoff from a
# WG that predates them), the command id for a solve command.
_request_trace: dict[str, str] | None = None
# Whether this session has settled the claims an interrupted one left behind,
# and whether it has told the user WG is too old to exchange requests with.
_claims_swept = False
_wg_outdated_noticed = False
# Outcomes the per-request trace would overwrite within one tick -- a request
# dropped as superseded, a leftover claim settled -- kept for the heartbeat's
# ``diagnostics.recentOutcomes``, newest last.
_recent_outcomes: list[dict[str, str]] = []
_RECENT_OUTCOMES_LIMIT = 16
# Interrupted file claims wait until their exact target document is active.
# They are settled read-only and never replayed.
_deferred_file_claims: list[object] = []
_legacy_claims_pending = True

# -- the activation boundary (STEP2 section 4) ---------------------------------
#
# One gate, read once when this registration starts, and checked where each
# engine starts -- never threaded through the tick. On, WGLink coordinates as
# it always has: the four-second watch tick and owner-candidate promotion.
# Off, neither starts or registers, so nothing between the user's
# commands can inspect the document, publish status or apply a WG request.
#
# The gate is off by default. The owner may enable it with a JSON boolean in
# WGLink's own settings file (``SETTINGS_PATH``); Fusion does not inherit a
# shell's environment:
#
#     {"automatic_coordination": true}
#
# Missing and invalid values stay off. Restart Fusion (or the add-in) for a
# change to take effect.
ACTIVATION_SETTING = "automatic_coordination"
_activation: dict[str, object] = {
    "automaticCoordination": False,
    "setting": "default",
    "settingsKey": ACTIVATION_SETTING,
}
# Main-thread time a command asks for after it returns. The event is part of
# the transfer path, so it is registered whatever the gate says: it carries
# the pickup check a Send or Solve schedules (M1 transfer contract C6), and,
# with coordination off, the command's own follow-up -- the geometry refresh
# it requested, status for WG, and settling a claim start-up could not. Each
# job carries the cause of the command that scheduled it across the hop
# (``wglink_activity``).
FOLLOWUP_EVENT_ID = f"hornlab_wglink_followup_{_watch_session_id}"
_followup_event = None
_followup_handler = None
_followups: list[object] = []
# The pickup check's one-shot timer adds its job from its own thread.
_followups_lock = threading.Lock()
_timers: list[threading.Timer] = []
_backend_admission = wglink_launcher.BackendAdmission()
_backend_progress = None


def _note_outcome(channel: str, request_id: str, outcome: str) -> None:
    _recent_outcomes.append({"channel": channel, "requestId": request_id, "outcome": outcome})
    del _recent_outcomes[:-_RECENT_OUTCOMES_LIMIT]


def _modal(text: str, title: str) -> None:
    """A message box shown from the watch tick, which no tick may run inside."""

    global _command_busy
    _command_busy = True
    try:
        _message(text, title)
    finally:
        _release_command_busy()


def _release_command_busy() -> None:
    """Clear ``_command_busy`` and re-drive whatever waited behind it.

    The only way the flag is cleared. Work deferred behind a busy holder --
    a pickup check or a command's follow-up -- is queued rather
    than dropped, and nothing but this re-raises it: the tick does not drain
    follow-ups, and with coordination off there is no tick.
    """

    global _command_busy
    _command_busy = False
    _command_finished()

# What one IPC channel did for a single watch tick. The distinction exists
# because suppressing a repeat error is not the same as doing work: a refused
# handoff keeps its marker and its export id for the whole Fusion session, so a
# channel that reported "handled" on every later tick monopolised the
# dispatcher and starved the return-request and survey channels behind it.
IDLE = "idle"  # nothing was pending on this channel
HANDLED = "handled"  # the channel used this tick -- later channels wait
SUPPRESSED = "suppressed"  # already reported, no work done -- carry on

# A request runs once and is then spent: nothing in Fusion retries it, and
# deleting a file would not help. Say what does.
_HANDOFF_RETRY_HINT = (
    "The Waveguide Generator add-in will not retry this handoff by itself. Send the model from WG again."
)
_RETURN_RETRY_HINT = (
    "The Waveguide Generator add-in will not retry this request by itself. Ask WG for the model again."
)
# How every WG round trip is delivered now: one file per request.
DELIVERY = "perRequest"
INSERT_HANDOFF_TTL_SECONDS = 30 * 60


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _refusal_text(reason: str, hint: str) -> str:
    return f"{reason}\n\n{hint}"


def _begin_request(channel: str, correlation_id: str, delivery: str) -> dict[str, str]:
    """Start one attempt at a WG round trip, named for the heartbeat."""

    global _request_trace
    _request_trace = {
        "channel": channel,
        "correlationId": correlation_id,
        "attemptId": str(uuid.uuid4()),
        "delivery": delivery,
        "outcome": "running",
    }
    return _request_trace


def _lease_now() -> float:
    return time.monotonic()


def _claim_ipc_lease() -> bool:
    """Atomically claim an absent or expired process-local IPC lease."""

    global _replaced_watch_event_id
    now = _lease_now()
    with _ipc_owner_runtime.lock:
        owner = _ipc_owner_runtime.owner
        expired = (
            owner is not None
            and now - float(_ipc_owner_runtime.renewed_at) > OWNER_LEASE_SECONDS
        )
        if owner not in (None, _watch_session_id) and not expired:
            return False
        if owner != _watch_session_id:
            _ipc_owner_runtime.generation += 1
            _replaced_watch_event_id = (
                str(_ipc_owner_runtime.watch_event_id)
                if _ipc_owner_runtime.watch_event_id
                else None
            )
        _ipc_owner_runtime.owner = _watch_session_id
        _ipc_owner_runtime.phase = "constructing"
        _ipc_owner_runtime.renewed_at = now
        _ipc_owner_runtime.watch_event_id = None
        return True


def _activate_ipc_lease() -> bool:
    """Commit the lease after panel construction and file request service registration."""

    with _ipc_owner_runtime.lock:
        if _ipc_owner_runtime.owner != _watch_session_id:
            return False
        _ipc_owner_runtime.phase = "active"
        # The watch thread renews the lease, and only it. With coordination off
        # there is no such thread, so the lease is committed without a clock:
        # an owner in this process holds it until its own stop() releases it,
        # and a second registration waits rather than taking over an owner
        # that is merely idle. Every registration's expiry test reads
        # ``now - renewed_at``, which never exceeds the timeout for infinity.
        _ipc_owner_runtime.renewed_at = _lease_now() if _coordinating() else math.inf
        _ipc_owner_runtime.watch_event_id = WATCH_EVENT_ID
        return True


def _renew_ipc_lease() -> bool:
    with _ipc_owner_runtime.lock:
        if (
            _ipc_owner_runtime.owner != _watch_session_id
            or _ipc_owner_runtime.phase != "active"
        ):
            return False
        _ipc_owner_runtime.renewed_at = _lease_now()
        return True


def _owns_ipc_lease() -> bool:
    with _ipc_owner_runtime.lock:
        return _ipc_owner_runtime.owner == _watch_session_id


def _owns_active_ipc_lease() -> bool:
    with _ipc_owner_runtime.lock:
        return (
            _ipc_owner_runtime.owner == _watch_session_id
            and _ipc_owner_runtime.phase == "active"
        )


def _release_ipc_lease() -> bool:
    with _ipc_owner_runtime.lock:
        if _ipc_owner_runtime.owner != _watch_session_id:
            return False
        _ipc_owner_runtime.owner = None
        _ipc_owner_runtime.phase = None
        _ipc_owner_runtime.renewed_at = 0.0
        _ipc_owner_runtime.watch_event_id = None
        return True


def _ipc_lease_snapshot() -> dict[str, object]:
    """A deterministic diagnostic surface for lifecycle tests and support."""

    with _ipc_owner_runtime.lock:
        return {
            "owner": _ipc_owner_runtime.owner,
            "phase": _ipc_owner_runtime.phase,
            "renewed_at": _ipc_owner_runtime.renewed_at,
            "generation": _ipc_owner_runtime.generation,
            "watch_event_id": _ipc_owner_runtime.watch_event_id,
        }


def _fingerprint_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _app() -> object:
    return adsk.core.Application.get()


def _ui() -> object | None:
    app = _app()
    return app.userInterface if app else None


def _message(text: str, title: str = PANEL_NAME) -> None:
    ui = _ui()
    if ui:
        ui.messageBox(text, title)


def _confirm_detach() -> bool:
    """Ask before irreversibly removing the selected link's WG identity."""

    ui = _ui()
    message_box = getattr(ui, "messageBox", None) if ui is not None else None
    try:
        yes_no = adsk.core.MessageBoxButtonTypes.YesNoButtonType
        question = adsk.core.MessageBoxIconTypes.QuestionIconType
        accepted = adsk.core.DialogResults.DialogYes
    except AttributeError:
        # A command shell without Fusion's modal API must fail closed. The
        # head-less core API remains available to callers that explicitly ask
        # for detach without going through this UI flow.
        return False
    if not callable(message_box):
        return False
    answer = message_box(
        "Detach permanently removes this link's WG identity. Geometry stays in "
        "the Fusion document, but it cannot be re-attached. The only way back "
        "is to insert a fresh copy from Waveguide Generator.\n\nDetach this link?",
        f"{PANEL_NAME} — confirm Detach",
        yes_no,
        question,
    )
    return answer == accepted


def _confirm_clear_fem() -> bool:
    """Offer recovery on the command's main thread even when FEM UI is hidden."""

    ui = _ui()
    message_box = getattr(ui, "messageBox", None) if ui is not None else None
    try:
        yes_no = adsk.core.MessageBoxButtonTypes.YesNoButtonType
        question = adsk.core.MessageBoxIconTypes.QuestionIconType
        accepted = adsk.core.DialogResults.DialogYes
    except AttributeError:
        return False
    if not callable(message_box):
        return False
    answer = message_box(
        "This design declares FEM regions, which this Waveguide Generator does not offer. "
        "Clear the FEM declarations and continue without them?",
        f"{PANEL_NAME} — clear FEM declarations",
        yes_no,
        question,
    )
    return answer == accepted


def _confirm_source_adoption(role: str, faces: int) -> bool:
    """Ask before assigning a random identity to paint in an unsaved document.

    Asked once per painted role group, because that is the only unit the
    question is answerable in: a document can be unambiguous in one role and
    ambiguous in another, and only the unambiguous one may be adopted at all.
    One question covering the whole document would put a single answer over
    several different situations.

    Fails closed, exactly as ``_confirm_detach`` does. A shell without Fusion's
    modal API cannot ask, so it refuses -- which is what it did before adoption
    existed.
    """

    ui = _ui()
    message_box = getattr(ui, "messageBox", None) if ui is not None else None
    try:
        yes_no = adsk.core.MessageBoxButtonTypes.YesNoButtonType
        question = adsk.core.MessageBoxIconTypes.QuestionIconType
        accepted = adsk.core.DialogResults.DialogYes
    except AttributeError:
        return False
    if not callable(message_box):
        return False
    answer = message_box(
        f"{faces} face(s) painted {role} carry no WG source identity.\n\n"
        f"Adopt them as this document's {role} source?\n\n"
        f"This unsaved document has no stable lineage yet. {role} will get a "
        "new source identity; Waveguide Generator may ask for its setup. "
        "Save the document to keep source identities.",
        f"{PANEL_NAME} — adopt {role} source",
        yes_no,
        question,
    )
    if answer != accepted:
        return False
    # The heartbeat's cached source ids are about to change. Bumping on the
    # answer rather than on the write over-invalidates when the write then
    # rolls back, which is the safe direction: a key that moves too often costs
    # one measurement, one that moves too rarely serves a stale identity.
    global _source_authoring_generation
    _source_authoring_generation += 1
    return True


def _log(text: str) -> None:
    """Put diagnostics where diagnostics belong: Fusion's Text Commands palette."""

    try:
        palette = _ui().palettes.itemById("TextCommands")
    except Exception:  # noqa: BLE001 - an unavailable palette must not raise
        palette = None
    if palette is not None:
        try:
            palette.writeText(text)
            return
        except Exception:  # noqa: BLE001
            pass
    try:
        print(text)
    except Exception:  # noqa: BLE001 - logging is never worth a second failure
        pass


def _report_error(action: str, title: str, exc: BaseException | None = None) -> None:
    """Log the traceback, show one human line.

    A raw ``traceback.format_exc()`` in a modal states the add-in's internals
    and nothing the user can act on, and the one sentence that identifies the
    failure is buried in the middle of it.
    """

    _log(f"[{PANEL_NAME}] {action}\n{traceback.format_exc()}")
    _message(wglink_author.failure_message(action, exc), title)


def _load_settings() -> dict[str, object]:
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - settings are optional
        return {}
    return value if isinstance(value, dict) else {}


def _save_settings(settings: dict[str, object]) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(
            json.dumps(settings, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 - a picker preference must not fail a command
        pass


def _read_activation() -> dict[str, object]:
    """The activation gate as the owner set it, read from the settings file.

    Called once per start. Only an explicit JSON ``true`` enables automatic
    coordination. Invalid values are reported and leave the gate off.
    """

    settings = _load_settings()
    state: dict[str, object] = {"settingsKey": ACTIVATION_SETTING}
    if ACTIVATION_SETTING not in settings:
        return {**state, "automaticCoordination": False, "setting": "default"}
    value = settings[ACTIVATION_SETTING]
    if isinstance(value, bool):
        return {**state, "automaticCoordination": value, "setting": "settings"}
    return {**state, "automaticCoordination": False, "setting": "invalid"}


def _coordinating() -> bool:
    """Whether this registration runs automatic coordination (read at start)."""

    return _activation.get("automaticCoordination") is True


def _discovered_bundles() -> list:
    try:
        return wglink_workspace.discover_bundles()
    except Exception:  # noqa: BLE001 - discovery is a convenience, never a gate
        return []


def _resolve_source(source_kind: str) -> tuple[str, str | None]:
    """Turn a dropdown choice into either a chosen bundle or a dialog to open.

    Returns ``(kind, path)``: a path means the workspace already answered the
    question, and ``None`` means fall through to the picker.
    """

    if source_kind in {BROWSE_FOLDER, BROWSE_ZIP}:
        return source_kind, None
    for bundle in _discovered_bundles():
        if bundle.label() == source_kind:
            return BROWSE_FOLDER, str(bundle.path)
    # A workspace that changed under an open dialog: ask rather than guess.
    return BROWSE_FOLDER, None


def _choose_bundle(title: str, source_kind: str) -> str | None:
    ui = _ui()
    if ui is None:
        return None
    settings = _load_settings()
    default_folder = wglink_workspace.bundle_folder()
    previous = Path(
        str(settings.get("last_bundle_folder", default_folder or Path.home()))
    ).expanduser()
    if not previous.is_dir() and default_folder is not None:
        previous = default_folder
    initial = previous if previous.is_dir() else previous.parent
    if source_kind == BROWSE_ZIP:
        dialog = ui.createFileDialog()
        dialog.title = title
        dialog.isMultiSelectEnabled = False
        dialog.filter = "Waveguide Generator bundles (*.wglink);;All files (*.*)"
        if initial.exists():
            dialog.initialDirectory = str(initial)
        if dialog.showOpen() != adsk.core.DialogResults.DialogOK:
            return None
        selected = Path(dialog.filename)
    else:
        dialog = ui.createFolderDialog()
        dialog.title = title
        if initial.exists():
            dialog.initialDirectory = str(initial)
        if dialog.showDialog() != adsk.core.DialogResults.DialogOK:
            return None
        selected = Path(dialog.folder)
    settings["last_bundle_folder"] = str(selected.parent)
    _save_settings(settings)
    return str(selected)


def _input(command_inputs: object, name: str) -> object | None:
    try:
        return command_inputs.itemById(name)
    except Exception:  # noqa: BLE001
        return None


def _command_inputs(inputs):
    """Events for grouped inputs can supply only that group's collection."""
    command = getattr(inputs, "command", None)
    return command.commandInputs if command is not None else inputs


def _input_value(command_inputs: object, name: str) -> object | None:
    item = _input(command_inputs, name)
    return item.value if item else None


def _selected_name(command_inputs: object, name: str, default: str) -> str:
    item = _input(command_inputs, name)
    try:
        return str(item.selectedItem.name)
    except Exception:  # noqa: BLE001
        return default


def _document_link_choices() -> list[tuple[str, str]]:
    """``(label, instance_id)`` for every managed link in the active document."""

    choices = []
    try:
        links = _document_links()
    except LinksNotInspected:
        # A chooser only shows links; the command it opens refuses on its own.
        links = []
    for link in links:
        instance_id = str(link.get("instance_id") or "")
        if not instance_id:
            continue
        # The user's own label wins over WG's name for the design: this list is
        # how they recognise their own link, and the design name is WG's.
        name = str(link.get("link_name") or link.get("design_name") or "").strip()
        choices.append((f"{name} · {instance_id}" if name else instance_id, instance_id))
    return choices


def _command_options(command_inputs: object) -> dict[str, object]:
    result: dict[str, object] = {}
    chosen = _selected_name(command_inputs, "instance_choice", "")
    if chosen:
        # The label carries the link's display name for recognition; the id is
        # the part the head-less API takes.
        result["instance_id"] = chosen.rsplit(" · ", 1)[-1].strip()
    link_name = str(_input_value(command_inputs, "link_name") or "").strip()
    if link_name:
        result["link_name"] = link_name
    return result


def _send_selection(command_inputs: object) -> object:
    item = _input(command_inputs, "send_selection")
    try:
        if item.selectionCount:
            return item.selection(0).entity
    except Exception:  # noqa: BLE001
        pass
    return "root"


def _source_identity_enabled() -> bool:
    """Whether this return declares ``source-identity-v1``.

    Only when WG advertises it: a WG that does not would refuse the bundle as an
    unknown required feature. Every path that builds sources -- Send, Solve, a
    return WG asked for, the preflight and the heartbeat's return-state token --
    asks here, so they all carry the same source ids.
    """

    try:
        return wglink_inbox.wg_source_identity(wglink_workspace.ipc_folder())
    except Exception:  # noqa: BLE001 - an unreadable capability declares nothing
        return False


def _manifest_feature_enabled(name: str) -> bool:
    try:
        return wglink_inbox.wg_manifest_feature(
            wglink_workspace.ipc_folder(), name
        )
    except Exception:  # noqa: BLE001 - an unreadable capability declares nothing
        return False


def _require_fem_regions() -> None:
    if not _manifest_feature_enabled(wglink_inbox.FEM_REGIONS):
        raise wglink_core.WgLinkError(
            wglink_fem.UNAVAILABLE_MESSAGE
        )


def _required_manifest_features() -> dict[str, bool]:
    """Manifest features required before exporting any Send or Solve."""

    missing = [
        label
        for capability, label in (
            (wglink_inbox.AUTOMATIC_DOMAIN, "automatic CAD domains"),
            (wglink_inbox.DOCUMENT_UP, "Fusion's modelling orientation"),
        )
        if not _manifest_feature_enabled(capability)
    ]
    if missing:
        raise wglink_core.WgLinkError(
            "The Waveguide Generator add-in needs a newer Waveguide Generator before it can "
            "Send or Solve: the running WG does not advertise "
            + " and ".join(missing)
            + ". Update and restart WG, then try again."
        )
    if not _manifest_feature_enabled(wglink_inbox.MESH_SIZING):
        raise wglink_core.WgLinkError(
            wglink_send.wglink_protocol.MESH_SIZING_UNAVAILABLE_MESSAGE
        )
    # FEM regions are optional: a design that declares none sends to a compatible WG,
    # and wglink_send refuses one that does when WG cannot solve them.
    features = {
        "automatic_domain": True,
        "document_up": True,
    }
    if _manifest_feature_enabled(wglink_inbox.FEM_REGIONS):
        features["fem_regions"] = True
    return features


def _no_workspace_text(action: str = "send again") -> str:
    """Why there is no WGLink folder to use, told apart the way WG tells it.

    A folder that was chosen and has since been moved or deleted is not the
    same situation as one never chosen, and a user told "choose one" when he
    already did has no way to learn which folder WG is still pointing at.
    """

    try:
        missing = wglink_workspace.missing_workspace()
    except Exception:  # noqa: BLE001 - the generic refusal is still true
        missing = None
    if missing is not None:
        return (
            "The WGLink folder Waveguide Generator is set to no longer exists:\n"
            f"{missing}\n\n"
            "It may have been moved, renamed or deleted. Choose the WGLink folder "
            f"again in WG under Settings → CAD Link, then {action}."
        )
    return (
        "Waveguide Generator has no selected WGLink folder. Choose one in "
        f"WG under Settings → CAD Link, then {action}."
    )


def _send_backend_options() -> dict[str, object]:
    # WG only ingests from its own workspace, so there is one correct
    # destination and the UI no longer asks. Collision-safe naming is kept:
    # a return WG has not ingested yet is not in content-addressed storage,
    # so overwriting one loses evidence.
    output = wglink_workspace.return_folder()
    if output is None:
        raise wglink_core.WgLinkError(_no_workspace_text())
    return {
        "output_folder": str(output),
        "capture_document": wglink_workspace.capture_document(),
        "source_identity": _source_identity_enabled(),
        **_required_manifest_features(),
    }


def _send_options(command_inputs: object, *, backend_ready: bool = True) -> dict[str, object]:
    options: dict[str, object] = {
        "selection": _send_selection(command_inputs),
        "overwrite": False,
    }
    if backend_ready:
        options.update(_send_backend_options())
    anchor_input = _input(command_inputs, "anchor_instance_id")
    try:
        if anchor_input.isVisible:
            anchor = str(anchor_input.selectedItem.name).strip()
            if anchor:
                options["anchor_instance_id"] = anchor
    except Exception:  # noqa: BLE001
        pass
    return options


def _sync_anchor_choices(command_inputs: object) -> None:
    anchor = _input(command_inputs, "anchor_instance_id")
    if anchor is None:
        return
    try:
        report = wglink_send.inspect_scope(
            _app(), {"selection": _send_selection(command_inputs)}
        )
        instance_ids = list(report.get("instance_ids", []))
    except Exception:  # noqa: BLE001 - execute will present the actionable refusal
        instance_ids = []
    anchor.isVisible = len(instance_ids) > 1
    try:
        previous = str(anchor.selectedItem.name) if anchor.selectedItem else None
    except Exception:  # noqa: BLE001
        previous = None
    try:
        anchor.listItems.clear()
        for index, instance_id in enumerate(instance_ids):
            anchor.listItems.add(str(instance_id), str(instance_id) == previous or (previous is None and index == 0))
    except Exception:  # noqa: BLE001
        pass


def _sync_preflight(command_inputs: object) -> None:
    """Refresh the read-only summary the Send and Solve dialogs show.

    Everything Fusion-specific happens here, on the main thread; the wording is
    composed by wglink_author from plain numbers.
    """

    box = _input(command_inputs, "preflight")
    if box is None:
        return
    try:
        options: dict[str, object] = {
            "selection": _send_selection(command_inputs),
            "source_identity": _source_identity_enabled(),
            **_required_manifest_features(),
        }
        anchor = _input(command_inputs, "anchor_instance_id")
        try:
            if anchor is not None and anchor.isVisible and anchor.selectedItem:
                options["anchor_instance_id"] = str(anchor.selectedItem.name)
        except Exception:  # noqa: BLE001 - an untouched dropdown has no choice yet
            pass
        text = wglink_author.preflight_summary(
            wglink_send.preflight_scope(_app(), options)
        ).html()
    except Exception as exc:  # noqa: BLE001 - a preview never blocks the dialog
        text = wglink_author.preflight_unavailable(exc)
    for attribute in ("formattedText", "text"):
        try:
            setattr(box, attribute, text)
            return
        except Exception:  # noqa: BLE001 - older text boxes expose only one
            continue


def _sync_help(command_inputs: object, box_id: str, choice_id: str, text_of) -> None:
    box = _input(command_inputs, box_id)
    if box is None:
        return
    try:
        text = text_of(_selected_name(command_inputs, choice_id, ""))
    except wglink_author.AuthorError as exc:
        text = str(exc)
    for attribute in ("formattedText", "text"):
        try:
            setattr(box, attribute, text)
            return
        except Exception:  # noqa: BLE001
            continue


def _add_selection_filters(selection: object, *names: str) -> None:
    """Apply what this Fusion knows, rather than failing the whole dialog.

    A filter name an older Fusion does not recognise would otherwise raise
    before the execute handler is attached, leaving a button that does nothing.
    """

    for name in names:
        try:
            selection.addSelectionFilter(name)
        except Exception:  # noqa: BLE001
            pass


def _selected_entities(command_inputs: object, name: str) -> list[object]:
    item = _input(command_inputs, name)
    entities: list[object] = []
    try:
        for index in range(item.selectionCount):
            entities.append(item.selection(index).entity)
    except Exception:  # noqa: BLE001 - an empty selection is refused by the plan
        pass
    return entities


def _face_descriptors(faces: list[object]) -> list[dict[str, object]]:
    """Copy what the plan needs off live faces. Main thread only."""

    descriptors = []
    for index, face in enumerate(faces):
        try:
            area = float(face.area) * 100.0
        except Exception:  # noqa: BLE001 - an unread area only drops the total
            area = None
        descriptors.append({
            "index": index,
            "current_role": wglink_send._face_role(face),
            "area_mm2": area,
        })
    return descriptors


def _native(entity: object) -> object:
    # An occurrence proxy exposes an empty attribute collection, so a
    # declaration has to be written on the native body, which is also where the
    # export reads it back from.
    return getattr(entity, "nativeObject", None) or entity


def _body_descriptors(bodies: list[object]) -> list[dict[str, object]]:
    """Copy what the plan needs off live bodies. Main thread only."""

    descriptors = []
    for index, body in enumerate(bodies):
        descriptors.append({
            "index": index,
            "name": str(getattr(body, "name", "") or f"body {index + 1}"),
            "current": wglink_send.read_declaration(_native(body)),
            "body_kind": "solid" if bool(getattr(body, "isSolid", False)) else "surface",
        })
    return descriptors


def _apply_source_faces(faces: list[object], role: str) -> dict[str, object]:
    app = _app()
    design = wglink_core._design(app)
    plan = wglink_author.plan_source_assignment(_face_descriptors(faces), role)
    wglink_author.apply_face_appearance_plan(
        faces, plan, lambda name: wglink_core._named_appearance(app, design, name)
    )
    # The source identity is authored here, and whatever WG advertises, so a
    # document is ready when WG starts reading identities. The preflight and the
    # heartbeat never author one. Send authors one in exactly one case: a role
    # group painted before source identities existed, where no face carries the
    # attribute at all, and only after ``_confirm_source_adoption`` has asked --
    # see ``wglink_send._adopt_painted_source``. Every selected face is stamped,
    # including one that already carried the role: re-running this command on a
    # source WGLink refused is how that source is reassigned. Clear takes the
    # identity off every selected face, including one whose paint was already
    # removed by hand, which is the only way to take such a stale face out.
    global _source_authoring_generation
    _source_authoring_generation += 1
    summary = plan.summary
    # The dialog owns paint/attribute rollback and the final failure message.
    if plan.role is not None:
        wglink_send.assign_source_identity(
            design, [faces[index] for index in (*plan.paint, *plan.unchanged)], plan.role
        )
        removed = 0
    else:
        removed = wglink_send.clear_source_identity(design, faces)
    if plan.role is None:
        if removed and not plan.clear:
            summary = (
                f"Removed a stale WG source identity from {removed} face(s) that no "
                "longer carried a WG source role."
            )
    return {"summary": summary}


def _apply_body_declaration(command_inputs: object) -> dict[str, object]:
    """Write or remove the return classification the export scope reads."""

    bodies = _selected_entities(command_inputs, "declare_bodies")
    plan = wglink_author.plan_body_declaration(
        _body_descriptors(bodies),
        _selected_name(command_inputs, "declaration", ""),
    )
    for index in plan.write:
        wglink_send.declare_body(_native(bodies[index]), plan.declaration)
    for index in plan.clear:
        wglink_send.clear_declaration(_native(bodies[index]))
    return {"summary": plan.summary}


def _tag_summary(tag: object) -> str:
    if not isinstance(tag, dict):
        return "Tag: not reported"
    return (
        f"Tag: {tag.get('role', '?')} on {tag.get('tagged_faces', 0)} face(s), "
        f"{float(tag.get('area_mm2', 0.0)):.3f} mm²"
    )


def _warnings(report: dict[str, object]) -> str:
    warnings = report.get("warnings")
    if not isinstance(warnings, list) or not warnings:
        return ""
    return "\n\nWarnings:\n- " + "\n- ".join(str(item) for item in warnings)


def _summary(operation: str, report: dict[str, object]) -> str:
    if operation in {"source", "declare", "fem"}:
        # The plan already states what changed, in the plan's own words.
        return str(report.get("summary", ""))
    if operation == "insert":
        deviation = report.get("deviation", {})
        # State both names once, here: the label the link now carries and WG's
        # own name for the design, which the parameter namespace follows. The
        # two are allowed to differ and a user who has not been told they are
        # different things reads the namespace as a mistake.
        link_name = str(report.get("link_name") or "")
        design_name = str(report.get("design_name") or "")
        naming = f"WG design: {design_name or '?'}\n"
        if link_name:
            naming = f"Link name: {link_name}\n" + naming
        naming += f"Parameters: {report.get('parameter_prefix', '?')}*\n"
        return (
            f"Inserted Waveguide Generator instance {report.get('instance_id', '?')}.\n"
            f"{naming}"
            f"Wrapper: {report.get('wrapper', '?')}\n"
            f"{_tag_summary(report.get('tag'))}\n"
            f"Deviation: mean {format_measurement_mm(deviation.get('mean_mm'))}, "
            f"max {format_measurement_mm(deviation.get('max_mm'))}"
            f"{_warnings(report)}"
        )
    if operation == "update":
        deviation = report.get("deviation", {})
        return (
            "The Waveguide Generator add-in finished updating the linked model.\n"
            f"Fit points moved: {report.get('fit_points_moved', 0)}\n"
            f"Sections done: {report.get('sections_done', 0)}\n"
            f"Health regressions: {len(report.get('regressed', []))}\n"
            f"{_tag_summary(report.get('tag'))}\n"
            f"Deviation max: {format_measurement_mm(deviation.get('max_mm'))}"
            f"{_warnings(report)}"
        )
    if operation in {"send", "solve"}:
        scope = report.get("scope", {})
        status = scope.get("status", "?") if isinstance(scope, dict) else "?"
        request_id = str(report.get("request_id") or "")
        if report.get("solve_requested"):
            # Handed off: WG owns progress and results from here, so this is
            # the one confirmation Fusion shows -- not a second progress bar
            # racing WG's own.
            if report.get("acknowledgement_expected"):
                closing = wglink_inbox.written_to_inbox_text(request_id)
            else:
                closing = (
                    f"Sent to WG (request {request_id[:8]}). Progress and results "
                    "appear in Waveguide Generator."
                    if request_id
                    else "Sent to WG. Progress and results appear in Waveguide Generator."
                )
        elif report.get("acknowledgement_expected"):
            closing = wglink_inbox.written_to_inbox_text(request_id)
        else:
            sent = (
                f"Sent to Waveguide Generator (request {request_id[:8]}). "
                if request_id
                else ""
            )
            closing = sent + "Waveguide Generator will open this return."
        if report.get("delivery_unconfirmed"):
            closing = _unconfirmed_text(request_id)
        message = (
            f"Return bundle written: {report.get('bundle_path', '?')}\n"
            f"Return ID: {report.get('return_id', '?')}\n"
            f"Scope: {status}\n"
            f"Sources: {len(report.get('sources', []))}\n\n"
            f"{closing}"
        )
        adopted = report.get("adopted_sources")
        if isinstance(adopted, list) and adopted:
            message += f"\n\n{', '.join(str(role) for role in adopted)}: painted sources identified for this document."
            if report.get("save_source_identities"):
                message += " Save the document to keep source identities."
        if status == "degraded" and isinstance(scope, dict):
            skipped = scope.get("skipped", [])
            names = []
            if isinstance(skipped, list):
                for record in skipped:
                    if not isinstance(record, dict) or record.get("kind") == "construction":
                        continue
                    names.append(
                        str(record.get("path") or record.get("name") or record.get("object_id") or "unnamed body")
                    )
            message += "\n\nDEGRADED EXPORT — skipped bodies:\n- " + "\n- ".join(names or ["none reported"])
        if report.get("delivery_note"):
            message += f"\n\n{report['delivery_note']}"
        return message
    return (
        f"Detached Waveguide Generator instance {report.get('instance_id', '?')}.\n"
        f"Attributes removed: {report.get('attributes_removed', 0)}\n"
        "Bodies and features changed: 0"
        f"{_warnings(report)}"
    )


#: How many times a request write is tried before its outcome is settled (C5, A1).
#: Known failures are retried too: the same id and bytes, so a retry is harmless.
WG_REQUEST_WRITE_ATTEMPTS = 3
#: The pause before the n-th retry is n times this: 0.45 s of pauses in all.
#: That bounds the pauses, not the filesystem calls around them, which run on
#: the calling (main) thread and have no deadline of their own.
WG_REQUEST_RETRY_PAUSE_SECONDS = 0.15


def _retry_pause(seconds: float) -> None:
    time.sleep(seconds)


def _write_wg_request(ipc: Path, **fields: object) -> tuple[Path, bool]:
    """Write one request; retry the same bytes; say whether it is confirmed.

    The request is planned once (``plan_wg_request``): WG's capability decides
    its schema and the exact payload here, before the first attempt, and every
    attempt writes that same payload -- A1's "same id and fields". A retry
    never re-decides the schema, so a capability change mid-retry cannot
    change what is written, and nothing but that payload can confirm it.

    Returns ``(path, True)`` when the request is known to be in WG's inbox as
    planned, and ``(path, False)`` when that is **unconfirmed** (M1 transfer
    contract C5, Amendment A1): a rename raised (``WriteOutcomeUnknown`` -- it
    may have landed, and WG may have claimed, accepted or refused it at once),
    every retry failed too, and the inbox does not hold exactly the planned
    request. Absence proves nothing then, so the caller must never say "not
    asked" or advise a new request.

    Raises only for what is known: a refusal while planning (WG outdated, WG
    not collecting) -- nothing was attempted -- or an ``OSError`` from every
    attempt when none of them reached the rename, so nothing was written.
    """

    path, payload = wglink_inbox.plan_wg_request(ipc, **fields)
    unknown = False
    last: OSError | None = None
    for attempt in range(WG_REQUEST_WRITE_ATTEMPTS):
        if attempt:
            _retry_pause(WG_REQUEST_RETRY_PAUSE_SECONDS * attempt)
        try:
            return wglink_inbox.publish_wg_request(path, payload), True
        except OSError as exc:
            last = exc
            unknown = unknown or isinstance(exc, wglink_inbox.WriteOutcomeUnknown)
    if wglink_inbox.landed_request(path, payload) is not None:
        return path, True
    if unknown:
        return path, False
    assert last is not None
    raise last


def _unconfirmed_text(request_id: str) -> str:
    return (
        f"The request to Waveguide Generator (request {request_id[:8]}) is "
        "unconfirmed. Writing it into WG's request folder reported an error, and "
        "the Waveguide Generator add-in could not read it back there -- but WG may already have it: WG "
        "takes a request the moment it lands. Check WG's CAD Link panel before "
        "sending this model again. If the request is there, it is being handled; "
        "sending again would make a second request."
    )


def _submit_to_wg(report: dict[str, object], kind: str) -> str:
    """Hand the return this command just published to WG: one file, one id.

    Send and Solve both come here and differ only in ``kind`` (M1 transfer
    contract C1/C2). There is one path in every configuration: no outbox
    item is created for new work, whatever the activation gate says. The
    outcome is synchronous -- the returned request id, or a ``WgLinkError``
    naming why WG was not asked -- and one follow-up, the pickup check, says
    so later if WG never takes the file.
    """

    channel = "solveCommand" if kind == wglink_inbox.KIND_PREPARE_AND_SOLVE else "snapshot"
    action = "solve again" if channel == "solveCommand" else "send again"
    ipc = wglink_workspace.ipc_folder(create=True)
    workspace = wglink_workspace.workspace_root()
    if ipc is None or workspace is None:
        raise wglink_core.WgLinkError(_no_workspace_text(action))
    bundle = Path(str(report.get("bundle_path") or ""))
    not_asked = (
        "The return bundle is in the WGLink folder, but Waveguide Generator was "
        "not asked to take it. "
    )
    request_id = str(uuid.uuid4())
    try:
        relative, manifest = wglink_inbox.return_reference(bundle, workspace)
        path, confirmed = _write_wg_request(
            ipc,
            kind=kind,
            command_id=request_id,
            return_id=str(report.get("return_id") or "") if channel == "solveCommand" else None,
            bundle_relative=relative,
            manifest_sha256=manifest,
            requested_at=wglink_inbox.utc_timestamp(),
        )
    except (wglink_inbox.WgOutdatedError, wglink_inbox.WgNotCollectingError) as exc:
        _begin_request(channel, request_id, DELIVERY)["outcome"] = "refused"
        raise wglink_core.WgLinkError(f"{not_asked}{exc}") from exc
    except OSError as exc:
        _begin_request(channel, request_id, DELIVERY)["outcome"] = "writeFailed"
        raise wglink_core.WgLinkError(
            f"{not_asked}The Waveguide Generator add-in could not write the request for WG: {exc}. "
            f"Check the WGLink folder is writable, then {action}."
        ) from exc
    if confirmed:
        _begin_request(channel, request_id, DELIVERY)["outcome"] = "requested"
    else:
        _begin_request(channel, request_id, DELIVERY)["outcome"] = "unconfirmed"
        report["delivery_unconfirmed"] = True
    # Unconfirmed included: if the file is there after a minute, WG never took it.
    if wglink_inbox.wg_acknowledges(ipc):
        # WG will say whether it accepted or refused: say only "written" until it does.
        report["acknowledgement_expected"] = True
        _schedule_pickup_check(
            path, channel, request_id, ack=(ipc, kind, manifest)
        )
    else:
        _schedule_pickup_check(path, channel, request_id)
    return request_id


def _schedule_pickup_check(
    path: Path,
    channel: str,
    request_id: str,
    ack: tuple[Path, str, str] | None = None,
) -> None:
    """The one follow-up a Send or Solve makes: did WG take the file? (C6)

    A one-shot timer, then the follow-up event onto the main thread, counted
    under the command that wrote the request. It reads whether one file still
    exists and nothing else -- no document, no link, no geometry -- and it runs
    whether or not automatic coordination is on.

    Against a WG that advertises acknowledgements (``ack`` is the inbox, the
    request kind and its manifest hash), the same timer machinery re-arms every
    few seconds until WG's acknowledgement file appears or the wait ends: one
    timer at a time, no thread of its own.
    """

    if ack is None:
        _start_pickup_timer(
            wglink_inbox.SOLVE_PICKUP_NOTICE_SECONDS,
            wglink_activity.carrying(lambda: _pickup_check(path, channel, request_id)),
        )
        return
    started = time.monotonic()
    _start_pickup_timer(
        wglink_inbox.ACK_POLL_SECONDS,
        wglink_activity.carrying(
            lambda: _acknowledgement_check(path, channel, request_id, ack, started)
        ),
    )


def _acknowledgement_check(
    path: Path,
    channel: str,
    request_id: str,
    ack: tuple[Path, str, str],
    started: float,
) -> None:
    """One look for WG's acknowledgement; re-arms itself until there is an answer.

    accepted -> "WG accepted" (logged, no prompt); refused -> WG's reason verbatim. The request file
    still waiting after the pickup window is the existing "not picked up" notice.
    The file gone for the whole wait with no acknowledgement is "taken, not
    confirmed" -- never "refused", never "solving".
    """

    ipc, kind, manifest = ack
    wglink_activity.record(wglink_activity.PICKUP_CHECK)
    result = wglink_inbox.read_acknowledgement(
        ipc, request_id, kind=kind, manifest_sha256=manifest
    )
    if result is not None:
        outcome, reason = result
        if outcome == wglink_inbox.ACK_ACCEPTED:
            _note_outcome(channel, request_id, "accepted")
            # Good news is not worth a prompt: WG shows the operation itself.
            _log(f"[{PANEL_NAME}] request {request_id[:8]}: {wglink_inbox.ACK_ACCEPTED_TEXT}")
        else:
            _note_outcome(channel, request_id, "refused")
            _modal(
                reason or "WG refused the request.",
                f"{PANEL_NAME} request refused",
            )
        return
    elapsed = time.monotonic() - started
    if path.exists():
        if elapsed >= wglink_inbox.SOLVE_PICKUP_NOTICE_SECONDS:
            _note_outcome(channel, request_id, "notTaken")
            _modal(wglink_inbox.REQUEST_NOT_TAKEN_MESSAGE, f"{PANEL_NAME} request waiting")
            return
    elif elapsed >= wglink_inbox.ACK_WAIT_SECONDS:
        _note_outcome(channel, request_id, "takenUnconfirmed")
        _modal(wglink_inbox.ACK_UNCONFIRMED_TEXT, f"{PANEL_NAME} request not confirmed")
        return
    _start_pickup_timer(
        wglink_inbox.ACK_POLL_SECONDS,
        wglink_activity.carrying(
            lambda: _acknowledgement_check(path, channel, request_id, ack, started)
        ),
    )


def _start_pickup_timer(delay: float, job: object) -> None:
    """A one-shot timer that hands ``job`` to the main thread through the follow-up event."""

    def due() -> None:
        with _followups_lock:
            _followups.append(job)
        try:
            _raise_followup_event()
        except Exception:  # noqa: BLE001 - a torn-down event: the add-in is stopping
            pass

    _start_timer(delay, due)


def _start_timer(delay: float, function: object) -> object:
    """A one-shot daemon timer, remembered so stop() can cancel it."""

    timer = threading.Timer(delay, function)
    timer.name = "WGLinkPickupCheck"
    timer.daemon = True
    _timers[:] = [item for item in _timers if item.is_alive()]
    _timers.append(timer)
    timer.start()
    return timer


def _pickup_check(path: Path, channel: str, request_id: str) -> None:
    wglink_activity.record(wglink_activity.PICKUP_CHECK)
    if not path.exists():
        _note_outcome(channel, request_id, "taken")
        return
    _note_outcome(channel, request_id, "notTaken")
    _modal(wglink_inbox.REQUEST_NOT_TAKEN_MESSAGE, f"{PANEL_NAME} request waiting")


#: At most this many inbox entries are looked at when WGLink starts.
STARTUP_RECHECK_LIMIT = 256


def _recheck_requests_at_startup() -> None:
    """The pickup check a restart lost (C6; A8 brief 5). Start-up only, bounded.

    A Send or Solve's pickup check is an in-memory timer, so stopping WGLink
    or Fusion within the minute loses it, and a request WG never takes would
    then wait unreported. At start-up this looks once at WG's inbox for
    WGLink's request files still under their final name -- files WG has not
    claimed -- and reads nothing else: no document, no link, no geometry. It
    runs whatever the activation gate says (C8), under the ``startup`` cause.

    One check, one notice at most. Every file already older than the pickup
    window is checked now; if any is younger (written within the last minute,
    or migrated from an earlier queue just now) the one check waits until the
    youngest has had its full window, so WG gets the same minute a pickup
    check always gives it. Nothing is waiting: no check, and no timer.
    """

    ipc = wglink_workspace.ipc_folder()
    if ipc is None:
        return
    try:
        pending = wglink_inbox.untaken_requests(ipc, limit=STARTUP_RECHECK_LIMIT)
        if not pending:
            return
        paths = [path for path, _age in pending]
        remaining = wglink_inbox.SOLVE_PICKUP_NOTICE_SECONDS - min(age for _path, age in pending)
        job = wglink_activity.carrying(lambda: _startup_pickup_check(paths))
        if remaining <= 0.0:
            job()
        else:
            _start_pickup_timer(remaining, job)
    except Exception as exc:  # noqa: BLE001 - a notice must never stop start-up
        _log(f"[{PANEL_NAME}] could not re-check waiting requests at start-up: {exc}")


def _startup_pickup_check(paths: list[Path]) -> None:
    wglink_activity.record(wglink_activity.PICKUP_CHECK)
    waiting = [path for path in paths if path.exists()]
    for path in paths:
        _note_outcome("startup", path.stem, "notTaken" if path in waiting else "taken")
    if waiting:
        _modal(
            wglink_inbox.requests_not_taken_at_startup_message(len(waiting)),
            f"{PANEL_NAME} request waiting",
        )


#: What an item that start-up could not convert still has: it is kept, and the
#: next start-up writes it again under its own id -- never a new one.
OUTBOX_KEPT_TEXT = (
    "Nothing was dropped. They stay in the WGLink folder, and the Waveguide Generator add-in tries again "
    "the next time it starts, under the same request ids, so Waveguide Generator "
    "never runs one twice. Restart Fusion once the folder is writable."
)


def _convert_outbox_at_startup() -> None:
    """Migrate earlier queued requests into the file inbox once at startup."""

    ipc = wglink_workspace.ipc_folder()
    if ipc is None:
        return
    try:
        # Final answers from an earlier session are never sent again. Show an
        # outstanding refusal before removing its item, so a restart cannot
        # silently lose the notice the old session had not displayed yet.
        queue = wglink_legacy_queue.LegacyQueue(ipc)
        items, _foreign = queue.scan()
        for item in items:
            answer = item.get("answer")
            if not isinstance(answer, dict):
                continue
            operation_id = item["operationId"]
            outcome = str(answer.get("outcome") or "rejected")
            if outcome != "delivered" and not queue.notice_shown(operation_id):
                if _ui() is None:
                    continue
                _modal(
                    _delivery_notice_text({**item, **answer}),
                    f"{PANEL_NAME} earlier delivery to WG",
                )
                queue.mark_notice_shown(operation_id)
            _note_outcome("outbox", operation_id, outcome)
            queue.delete(operation_id)
        result = wglink_legacy_queue.convert_outbox(
            ipc,
            solve_files=wglink_inbox,
        )
    except Exception as exc:  # noqa: BLE001 - the commands must still load
        _log(f"[{PANEL_NAME}] could not convert queued deliveries: {exc}")
        _message(
            "The Waveguide Generator add-in could not hand the requests an earlier session queued for "
            f"Waveguide Generator over at start-up: {exc}\n\n"
            + OUTBOX_KEPT_TEXT,
            f"{PANEL_NAME} earlier requests waiting",
        )
        return
    for operation_id in result["converted"]:
        _note_outcome("outbox", operation_id, "converted")
    for operation_id in result["abandoned"]:
        _note_outcome("outbox", operation_id, "notDelivered")
    for operation_id in result["failed"]:
        _note_outcome("outbox", operation_id, "conversionFailed")
    if result["failed"]:
        _message(
            f"{len(result['failed'])} request(s) an earlier session queued for "
            "Waveguide Generator could not be moved into WG's request folder at "
            "start-up: the WGLink folder could not be written, or a file in it "
            "was held by another program.\n\n" + OUTBOX_KEPT_TEXT,
            f"{PANEL_NAME} earlier requests waiting",
        )
    if result["abandoned"]:
        _message(
            f"{len(result['abandoned'])} earlier Send(s) to Waveguide Generator were "
            "never delivered, and this Waveguide Generator cannot take a Send from a "
            "file. Update Waveguide Generator, then send those models again from "
            "Fusion. Their returns are still in the WGLink folder.",
            f"{PANEL_NAME} earlier sends not delivered",
        )


def _delivery_notice_text(notice: dict[str, object]) -> str:
    """Describe the final answer, including a locally expired delivery."""

    what = "Solve in WG" if notice.get("kind") == wglink_legacy_queue.KIND_SOLVE else "Send to WG"
    sent = str(notice.get("createdAt") or "earlier")
    operation = str(notice.get("operationId") or "?")
    outcome = notice.get("outcome")
    if outcome == "rejected":
        reason = str(notice.get("reason") or "")
        detail = str(notice.get("message") or "") or "WG gave no message"
        return (
            f"Waveguide Generator did not accept the return from {what} (sent {sent}, "
            f"operation {operation}): {detail}"
            f"{f' [{reason}]' if reason else ''}\n\n"
            "The Waveguide Generator add-in does not send it again by itself. Send it again from Fusion if you "
            "still want it; that is a new request."
        )
    if outcome == "conflict":
        return (
            f"Waveguide Generator refused the return from {what} (sent {sent}): its "
            f"operation id {operation} already names a different request, so nothing "
            "changed in WG. Send it again from Fusion."
        )
    return (
        f"The return from {what} (sent {sent}, operation {operation}) could not be "
        "delivered to Waveguide Generator within 7 days and was removed from the Waveguide Generator add-in's "
        "queue. Send it again from Fusion if you still want it."
    )


def _show_export_progress(operation: str, message: str = "Surveying the assembly…") -> object | None:
    """Open Fusion's modeless progress surface for the slow return path."""

    ui = _ui()
    factory = getattr(ui, "createProgressDialog", None) if ui is not None else None
    if not callable(factory):
        return None
    try:
        dialog = factory()
        dialog.isCancelButtonShown = False
        dialog.isBackgroundTranslucent = False
        dialog.show(
            "Solve in WG" if operation == "solve" else "Send to WG",
            message,
            0,
            3,
            0,
        )
        return dialog
    except Exception:  # noqa: BLE001 - progress is advisory, export is not
        return None


def _update_export_progress(dialog: object | None, value: int, message: str) -> None:
    if dialog is None:
        return
    try:
        dialog.message = message
        dialog.progressValue = value
        adsk.doEvents()
    except Exception:  # noqa: BLE001 - an old Fusion progress API must not abort export
        pass


def _hide_export_progress(dialog: object | None) -> None:
    if dialog is None:
        return
    try:
        dialog.hide()
    except Exception:  # noqa: BLE001
        pass


def _require_compatible_wg(*, wg_inbox: bool = False) -> None:
    """Require the command's channel before export or mutation."""

    ipc = wglink_workspace.ipc_folder()
    try:
        if ipc is None:
            if wglink_workspace.missing_workspace() is not None:
                raise wglink_core.WgLinkError(_no_workspace_text())
            raise wglink_inbox.WgNotCollectingError(wglink_inbox.WG_NOT_COLLECTING_MESSAGE)
        if wg_inbox:
            wglink_inbox.require_wg_delivery(ipc)
        else:
            wglink_inbox.require_fusion_delivery(ipc)
    except (wglink_inbox.WgOutdatedError, wglink_inbox.WgNotCollectingError) as exc:
        raise wglink_core.WgLinkError(str(exc)) from exc


class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self, operation: str):
        super().__init__()
        self.operation = operation
        # The Declare FEM Region dialog's group bookkeeping, set when it is built.
        self.fem_state: FemDialogState | None = None
        self.mesh_state: MeshDialogState | None = None

    def notify(self, args: object) -> None:
        # Everything this command inspects or changes, including the follow-up
        # it schedules below, is counted under the command that asked for it.
        with wglink_activity.because(wglink_activity.command_cause(self.operation)):
            self._execute(args)

    def _execute(self, args: object, *, send_options: dict[str, object] | None = None,
                 admission: wglink_launcher.BackendAdmission | None = None) -> None:
        global _command_busy
        progress = None
        # Whether this command got as far as producing a report. A user who
        # cancelled the bundle picker or the detach confirmation changed
        # nothing, so there is nothing for the next tick to look at.
        ran = False
        # Keep file requests from running over an explicit command.
        exported = False
        _command_busy = True
        try:
            adsk.doEvents()
            if admission is not None and admission.cancelled:
                return
            if self.operation in {"send", "solve"} and send_options is None:
                if self._admit_backend(args):
                    return
            if self.operation in {"send", "solve", "insert", "update"}:
                _require_compatible_wg(wg_inbox=self.operation in {"send", "solve"})
            inputs = args.command.commandInputs if args is not None else None
            options = _command_options(inputs) if inputs is not None else {}
            if self.operation == "insert":
                kind, chosen = _resolve_source(
                    _selected_name(inputs, "bundle_source", BROWSE_FOLDER)
                )
                path = chosen or _choose_bundle(
                    "Select a .wglink bundle to insert", kind
                )
                if path is None:
                    return
                report = wglink_core.insert(_app(), path, options)
            elif self.operation == "update":
                report = wglink_core.update(_app(), None, options)
            elif self.operation in {"send", "solve"}:
                progress = _show_export_progress(self.operation)
                _update_export_progress(
                    progress, 1, "Exporting STEP and validating the return bundle…"
                )
                if admission is not None and admission.cancelled:
                    return
                report = wglink_send.send(
                    _app(),
                    send_options if send_options is not None else _send_options(inputs),
                    confirm_adoption=_confirm_source_adoption,
                    confirm_clear_fem=_confirm_clear_fem,
                )
                exported = True
                if report.get("adopted_sources") and not report.get("save_source_identities"):
                    global _source_authoring_generation
                    _source_authoring_generation += 1
                # One path for both: the return is published, then one request
                # file hands it to WG. Send and Solve differ only in its kind.
                solve = self.operation == "solve"
                _update_export_progress(
                    progress,
                    2,
                    "Return written. Asking Waveguide Generator to solve…"
                    if solve
                    else "Return written. Handing it to Waveguide Generator…",
                )
                if admission is not None and admission.cancelled:
                    return
                report = dict(report)
                report["request_id"] = _submit_to_wg(
                    report,
                    wglink_inbox.KIND_PREPARE_AND_SOLVE
                    if solve
                    else wglink_inbox.KIND_RECEIVE_SNAPSHOT,
                )
                report["solve_requested"] = solve
                _update_export_progress(progress, 3, "Return ready in Waveguide Generator.")
            elif self.operation == "source":
                report = _apply_mesh_dialog(inputs, self.mesh_state)
            elif self.operation == "declare":
                report = _apply_body_declaration(inputs)
            elif self.operation == "fem":
                _require_fem_regions()
                report = _apply_fem_region(inputs, self.fem_state)
            elif self.operation == "detach":
                if not _confirm_detach():
                    return
                report = wglink_core.detach(_app(), options)
            else:
                raise RuntimeError(f"Unknown Waveguide Generator operation: {self.operation}")
            ran = True
            _message(
                _summary(self.operation, report),
                f"{PANEL_NAME} request unconfirmed"
                if isinstance(report, dict) and report.get("delivery_unconfirmed")
                else PANEL_NAME,
            )
        except (wglink_core.WgLinkError, wglink_author.AuthorError) as exc:
            # A Send or Solve refused after its export ran (WG outdated or not
            # collecting, a failed request write) may already have changed the
            # document -- an adopted source -- and has published a return.
            # It is owed a refresh and a status like any command that ran.
            if exported:
                ran = True
            _message(str(exc), f"{PANEL_NAME} refused")
        except Exception as exc:  # noqa: BLE001 - UI boundary; core remains head-less
            _report_error(COMMANDS[self.operation][1], f"{PANEL_NAME} error", exc)
        finally:
            _hide_export_progress(progress)
            # Cleared here; re-driven by the _command_finished() below, after
            # this command has scheduled its own follow-up.
            _command_busy = False
            # This command may have moved the geometry, and a command the user ran
            # is an explicit cause. One coalesced refresh, paid for on the next
            # tick, is how a linked document's published state becomes current
            # again without a timer measuring anything. A cancelled command
            # asks for nothing.
            if ran:
                _request_geometry_refresh("command", _active_document_id())
                if not _coordinating():
                    # No tick will pay for that refresh or publish the
                    # result, so this command schedules both itself.
                    _schedule_followup(_after_command)
            _command_finished()

    def _admit_backend(self, args: object) -> bool:
        """Snapshot dialog choices on main; defer installed admission and resume here.

        CommandInputs cease to be valid when execute returns. Only the copied
        choices survive; their Fusion entity references are accessed on main.
        """

        global _backend_progress
        data_dir = wglink_workspace.data_dir()
        if data_dir is None:
            return False
        ipc = wglink_workspace.ipc_folder()
        if ipc is None:
            ipc = data_dir / wglink_workspace.IPC_SUBDIRECTORY
        argv = wglink_launcher.launcher_argv(ipc)
        if argv is None:
            # Keep the registered folder's spelling: a symlink is unmanaged.
            argv = wglink_launcher.installed_marker_argv(Path(__file__).parent)
        try:
            if argv is None:
                # Preserve the visible validation error for an advertised but
                # invalid entry when the managed install cannot rescue it.
                wglink_launcher.launcher_argv(ipc, strict=True)
        except wglink_launcher.LauncherError as exc:
            raise wglink_core.WgLinkError(str(exc)) from exc
        if argv is None:
            return False
        try:
            if (ipc / wglink_inbox.CAPABILITIES_FILENAME).exists():
                wglink_inbox.require_wg_delivery(ipc)
        except wglink_inbox.WgNotCollectingError:
            pass  # An installed launcher can start or adopt this same folder.
        except wglink_inbox.WgOutdatedError as exc:
            raise wglink_core.WgLinkError(str(exc)) from exc
        if _followup_event is None:
            raise wglink_core.WgLinkError("The Waveguide Generator add-in's main-thread event is unavailable. Start WG manually, then try again.")
        app = _app()
        document_id = _active_document_id()
        document = app.activeDocument
        options = _send_options(args.command.commandInputs, backend_ready=False)
        # IPC comes exclusively from workspace.data_dir(), not the CAD workspace.
        override = wglink_workspace.launcher_data_dir()
        admission = _backend_admission

        def starting() -> None:
            global _backend_progress
            if not admission.cancelled and _backend_progress is None:
                _backend_progress = _show_export_progress(self.operation, "Starting Waveguide Generator…")
                if _backend_progress is None:
                    _log("Starting Waveguide Generator…")

        def resume(error: str | None) -> None:
            global _backend_progress
            _hide_export_progress(_backend_progress)
            _backend_progress = None
            if admission.cancelled:
                return
            if error is not None:
                _modal(f"Could not start Waveguide Generator. Start WG manually, then try again.\n\n{error}", f"{PANEL_NAME} refused")
                return
            if (_active_document_id() != document_id
                    or (document_id is None and app.activeDocument != document)):
                _modal("The active Fusion document changed while WG was starting. Send or Solve again from the intended document.", f"{PANEL_NAME} refused")
                return
            # Read settings and negotiate capabilities after WG has created its
            # first workspace and advertisement, before any export.
            try:
                options.pop("fem_regions", None)
                options.update(_send_backend_options())
            except wglink_core.WgLinkError as exc:
                _modal(str(exc), f"{PANEL_NAME} refused")
                return
            if not admission.cancelled:
                self._execute(None, send_options=options, admission=admission)

        starting = wglink_activity.carrying(starting)
        resume = wglink_activity.carrying(resume)
        event = _followup_event

        def dispatch(job: object) -> None:
            # fireCustomEvent is Fusion's thread-safe bridge. Do not acquire an
            # Application, inspect a document, or use UI from this worker.
            receipt = wglink_launcher.DispatchReceipt(job)
            with _followups_lock:
                if _followup_event is not event:
                    raise wglink_launcher.LauncherError("The Waveguide Generator add-in's main-thread event is stopping.")
                _followups.append(receipt)
            try:
                if app.fireCustomEvent(FOLLOWUP_EVENT_ID) is False:
                    receipt.wait(lambda: admission.cancelled,
                                 min(time.monotonic() + wglink_launcher.DISPATCH_TIMEOUT,
                                     admission.dispatch_deadline))
            except Exception:
                if not receipt.cancel():
                    return  # Actual receipt wins over a misleading API result.
                with _followups_lock:
                    if receipt in _followups:
                        _followups.remove(receipt)
                raise

        try:
            admission.request(ipc, data_dir, argv, override, resume, dispatch, starting)
        except wglink_launcher.LauncherError as exc:
            if not admission.has_pending_callbacks:
                # A lost completion cannot hide its modeless status from a
                # worker. The next command reports it and clears it on main.
                _hide_export_progress(_backend_progress)
                _backend_progress = None
            raise wglink_core.WgLinkError(str(exc)) from exc
        return True


# ---------------------------------------------------------- Declare FEM Region
#
# Modelled on the FEM export dialog of the Fusion-to-MSH add-in: one target
# body, a default size, and any number of named surface groups, each with a
# kind, a local size and its faces, validated live so OK stays disabled until
# the declaration is complete. Unlike that add-in nothing is meshed here: the
# declaration is stored on the body and its faces, and WG meshes at Send.

FEM_CLEAR_LABEL = "Clear the FEM region on this body"
FEM_DECLARE_LABEL = "Declare or update the FEM region"


class FemDialogState:
    def __init__(self) -> None:
        self.group_ids: list[int] = []
        self.next_group_id = 1
        self.region_id: str | None = None
        self.restoring = False


def _fem_input_id(prefix: str, group_id: int) -> str:
    return f"fem_{prefix}_{group_id}"


def _mm_input(value_mm: float) -> object:
    # Fusion value inputs hold internal units (cm) whatever unit they display.
    return adsk.core.ValueInput.createByReal(float(value_mm) / wglink_fem.CM_TO_MM)


def _mm_value(command_inputs: object, input_id: str) -> float | None:
    item = _input(command_inputs, input_id)
    try:
        return float(item.value) * wglink_fem.CM_TO_MM
    except Exception:  # noqa: BLE001 - an unreadable value is refused by the plan
        return None


def _build_fem_dialog(inputs: object, state: FemDialogState) -> None:
    body = inputs.addSelectionInput(
        "fem_body", "Air body", "Select one closed solid body of air."
    )
    _add_selection_filters(body, "SolidBodies")
    body.setSelectionLimits(1, 1)
    action = inputs.addDropDownCommandInput(
        "fem_action", "Action", adsk.core.DropDownStyles.TextListDropDownStyle
    )
    action.listItems.add(FEM_DECLARE_LABEL, True)
    action.listItems.add(FEM_CLEAR_LABEL, False)
    inputs.addStringValueInput("fem_name", "Region name", "")
    inputs.addValueInput(
        "fem_size", "Mesh size", "mm", _mm_input(wglink_fem.DEFAULT_REGION_SIZE_MM)
    )
    loss = inputs.addDropDownCommandInput(
        "fem_loss", "Bulk loss factor", adsk.core.DropDownStyles.TextListDropDownStyle
    )
    for preset in wglink_fem.BULK_LOSS_PRESETS:
        loss.listItems.add(f"{preset:g}", preset == wglink_fem.DEFAULT_BULK_LOSS_FACTOR)
    inputs.addTextBoxCommandInput(
        "fem_note",
        "",
        "Faces in no group are rigid walls at the region size. Add a group for "
        "each driver diaphragm, each opening to the exterior air (interface), "
        "and any face that needs its own size. Hide the body before Send.",
        3,
        True,
    )
    groups = inputs.addGroupCommandInput("fem_groups", "Surface groups")
    groups.isExpanded = True
    add = groups.children.addBoolValueInput("fem_add_group", "Add group", False, "", False)
    add.isFullWidth = True
    remove = groups.children.addBoolValueInput(
        "fem_remove_group", "Remove last group", False, "", False
    )
    remove.isFullWidth = True
    remove.isEnabled = False
    inputs.addTextBoxCommandInput(
        "fem_validation", "", "<b>Select one solid body of air.</b>", 2, True
    )


def _add_fem_group(
    inputs: object,
    state: FemDialogState,
    *,
    name: str = "",
    kind: str = "moving",
    size_mm: float = wglink_fem.DEFAULT_GROUP_SIZE_MM,
    role: str = "LF",
    faces: list[object] | None = None,
    lining: dict[str, object] | None = None,
    relative_level_db: float = wglink_fem.DEFAULT_RELATIVE_LEVEL_DB,
) -> None:
    container = _input(inputs, "fem_groups")
    if container is None:
        return
    group_id = state.next_group_id
    state.next_group_id += 1
    state.group_ids.append(group_id)
    group = container.children.addGroupCommandInput(
        _fem_input_id("group", group_id), name or f"Surface group {group_id}"
    )
    group.isExpanded = True
    children = group.children
    children.addStringValueInput(_fem_input_id("name", group_id), "Name", name)
    kinds = children.addDropDownCommandInput(
        _fem_input_id("kind", group_id), "Kind",
        adsk.core.DropDownStyles.TextListDropDownStyle,
    )
    for label, value in wglink_fem.KIND_CHOICES:
        kinds.listItems.add(label, value == kind)
    roles = children.addDropDownCommandInput(
        _fem_input_id("role", group_id), "Drive role",
        adsk.core.DropDownStyles.TextListDropDownStyle,
    )
    for value in wglink_fem.DRIVE_ROLES:
        roles.listItems.add(value, value == role)
    roles.isVisible = kind == "moving"
    children.addValueInput(
        _fem_input_id("level", group_id), "Relative level (dB)", "",
        adsk.core.ValueInput.createByReal(relative_level_db),
    )
    children.addBoolValueInput(
        _fem_input_id("lining", group_id), "Absorbing lining", True, "", lining is not None,
    )
    children.addValueInput(
        _fem_input_id("thickness", group_id), "Lining thickness", "mm",
        _mm_input(float((lining or {}).get("thickness_mm", wglink_fem.DEFAULT_LINING_THICKNESS_MM))),
    )
    children.addValueInput(
        _fem_input_id("resistivity", group_id), "Flow resistivity (Pa·s/m²)", "",
        adsk.core.ValueInput.createByReal(float((lining or {}).get(
            "flow_resistivity_pa_s_per_m2", wglink_fem.DEFAULT_FLOW_RESISTIVITY_PA_S_PER_M2,
        ))),
    )
    _sync_fem_group_options(inputs, group_id)
    children.addValueInput(_fem_input_id("size", group_id), "Mesh size", "mm", _mm_input(size_mm))
    selection = children.addSelectionInput(
        _fem_input_id("faces", group_id), "Faces", "Select faces of the air body."
    )
    _add_selection_filters(selection, "Faces")
    selection.setSelectionLimits(0, 0)
    for face in faces or ():
        try:
            selection.addSelection(face)
        except Exception:  # noqa: BLE001 - a stale face is reported by validation
            pass
    remove = _input(inputs, "fem_remove_group")
    if remove is not None:
        remove.isEnabled = True


def _sync_fem_group_options(inputs: object, group_id: int) -> None:
    kind = wglink_fem.resolve_kind(
        _selected_name(inputs, _fem_input_id("kind", group_id), "")
    )
    for prefix in ("role", "level", "lining", "thickness", "resistivity"):
        item = _input(inputs, _fem_input_id(prefix, group_id))
        if item is not None:
            item.isVisible = kind == ("moving" if prefix in ("role", "level") else "rigid")
            if prefix in ("thickness", "resistivity"):
                item.isEnabled = bool(_input_value(inputs, _fem_input_id("lining", group_id)))


def _remove_fem_group(inputs: object, state: FemDialogState, group_id: int) -> None:
    group = _input(inputs, _fem_input_id("group", group_id))
    if group is not None:
        group.deleteMe()
    state.group_ids = [item for item in state.group_ids if item != group_id]
    remove = _input(inputs, "fem_remove_group")
    if remove is not None:
        remove.isEnabled = bool(state.group_ids)


def _restore_fem_region(inputs: object, state: FemDialogState, body: object) -> None:
    """Fill the dialog from the body's stored region, like a saved preset."""

    state.restoring = True
    try:
        for group_id in list(state.group_ids):
            _remove_fem_group(inputs, state, group_id)
        region = wglink_send.read_fem_region(body)
        state.region_id = region.get("region_id") if region else None
        name = _input(inputs, "fem_name")
        if name is not None:
            name.value = str((region or {}).get("name") or getattr(body, "name", "") or "")
        size = _input(inputs, "fem_size")
        if size is not None:
            size.value = float(
                (region or {}).get("mesh_size_mm") or wglink_fem.DEFAULT_REGION_SIZE_MM
            ) / wglink_fem.CM_TO_MM
        loss = _input(inputs, "fem_loss")
        wanted = float(((region or {}).get("medium") or {}).get("bulk_loss_factor", 0.0))
        try:
            for index in range(loss.listItems.count):
                item = loss.listItems.item(index)
                item.isSelected = abs(float(item.name) - wanted) < 1e-12
        except Exception:  # noqa: BLE001
            pass
        if region is None:
            return
        faces = wglink_send.fem_region_faces(body, str(region["region_id"]))
        for group in region.get("groups") or ():
            _add_fem_group(
                inputs,
                state,
                name=str(group.get("name") or ""),
                kind=str(group.get("kind") or "rigid"),
                size_mm=float(group.get("mesh_size_mm") or wglink_fem.DEFAULT_GROUP_SIZE_MM),
                role=str(group.get("drive_role") or "LF"),
                faces=faces.get(str(group.get("group_id")), []),
                lining=group.get("lining"),
                relative_level_db=float(group.get("relative_level_db", wglink_fem.DEFAULT_RELATIVE_LEVEL_DB)),
            )
    finally:
        state.restoring = False


def _fem_group_rows(inputs: object, state: FemDialogState, body: object) -> list[dict[str, object]]:
    target = _native(body) if body is not None else None
    rows = []
    for group_id in state.group_ids:
        faces = []
        for face in _selected_entities(inputs, _fem_input_id("faces", group_id)):
            native = _native(face)
            try:
                matches = target is not None and _native(face.body) == target
            except Exception:  # noqa: BLE001
                matches = False
            faces.append({
                "key": wglink_send.face_identity(native),
                "body_matches": matches,
                "area_cm2": getattr(native, "area", None),
                "face": face,
            })
        row = {
            "name": str(_input_value(inputs, _fem_input_id("name", group_id)) or ""),
            "kind": _selected_name(inputs, _fem_input_id("kind", group_id), ""),
            "drive_role": _selected_name(inputs, _fem_input_id("role", group_id), ""),
            "mesh_size_mm": _mm_value(inputs, _fem_input_id("size", group_id)),
            "faces": faces,
        }
        kind = wglink_fem.resolve_kind(row["kind"])
        if kind == "moving":
            level = _input(inputs, _fem_input_id("level", group_id))
            row["relative_level_db"] = (level.value if level is not None
                                        else wglink_fem.DEFAULT_RELATIVE_LEVEL_DB)
        if kind == "rigid" and _input_value(inputs, _fem_input_id("lining", group_id)):
            row["lining"] = {
                "model": "miki",
                "thickness_mm": _mm_value(inputs, _fem_input_id("thickness", group_id)),
                "flow_resistivity_pa_s_per_m2": _input_value(inputs, _fem_input_id("resistivity", group_id)),
            }
        rows.append(row)
    return rows


def _other_region_ids(body: object) -> list[str]:
    target = _native(body)
    ids = []
    try:
        found = wglink_core._design(_app()).findAttributes(
            wglink_core.ATTRIBUTE_GROUP, wglink_fem.REGION_ATTRIBUTE
        )
    except Exception:  # noqa: BLE001 - uniqueness is re-checked at Send
        return ids
    for attribute in found or ():
        try:
            if attribute.parent == target:
                continue
            region = wglink_fem.read_region_attribute(attribute.value)
        except Exception:  # noqa: BLE001
            continue
        if region is not None:
            ids.append(str(region["region_id"]))
    return ids


def _plan_fem_region(inputs: object, state: FemDialogState) -> tuple[object, "wglink_fem.RegionDeclaration", list[dict[str, object]]]:
    bodies = _selected_entities(inputs, "fem_body")
    if not bodies:
        raise wglink_fem.FemDeclarationError("Select one closed solid body of air.")
    body = bodies[0]
    rows = _fem_group_rows(inputs, state, body)
    plan = wglink_fem.plan_region_declaration(
        name=_input_value(inputs, "fem_name"),
        mesh_size_mm=_mm_value(inputs, "fem_size"),
        body_is_solid=bool(getattr(body, "isSolid", False)),
        groups=rows,
        other_region_ids=_other_region_ids(body),
        region_id=state.region_id,
        medium={"bulk_loss_factor": _selected_name(inputs, "fem_loss", "0")},
    )
    return body, plan, rows


def _fem_clearing(inputs: object) -> bool:
    return _selected_name(inputs, "fem_action", FEM_DECLARE_LABEL) == FEM_CLEAR_LABEL


def _fem_validation_error(inputs: object, state: FemDialogState) -> str | None:
    if _fem_clearing(inputs):
        return None if _selected_entities(inputs, "fem_body") else "Select the body to clear."
    try:
        _plan_fem_region(inputs, state)
    except wglink_fem.FemDeclarationError as exc:
        return str(exc)
    return None


def _sync_fem_validation(inputs: object, state: FemDialogState) -> bool:
    error = _fem_validation_error(inputs, state)
    box = _input(inputs, "fem_validation")
    if box is not None:
        box.formattedText = (
            f"<b>Fix before OK:</b> {html.escape(error)}" if error else "<b>Ready.</b>"
        )
    return error is None


def _apply_fem_region(inputs: object, state: FemDialogState | None) -> dict[str, object]:
    state = state or FemDialogState()
    if _fem_clearing(inputs):
        bodies = _selected_entities(inputs, "fem_body")
        if not bodies:
            raise wglink_author.AuthorError("Select the body to clear.")
        cleared = wglink_send.clear_fem_region(bodies[0])
        return {"summary": "Cleared the FEM region and its surface groups."
                if cleared else "That body was not a FEM region, so nothing changed."}
    try:
        body, plan, rows = _plan_fem_region(inputs, state)
    except wglink_fem.FemDeclarationError as exc:
        raise wglink_author.AuthorError(str(exc)) from exc
    faces_by_group = {
        group.group_id: [face["face"] for face in row["faces"]]
        for group, row in zip(plan.groups, rows)
    }
    wglink_send.declare_fem_region(body, plan, faces_by_group)
    return {"summary": plan.summary}


class FemInputChangedHandler(adsk.core.InputChangedEventHandler):
    def __init__(self, state: FemDialogState):
        super().__init__()
        self.state = state

    def notify(self, args: object) -> None:
        with wglink_activity.because(wglink_activity.command_cause("fem")):
            self._changed(args)

    def _changed(self, args: object) -> None:
        if self.state.restoring:
            return
        try:
            changed = getattr(args, "input", None)
            input_id = str(getattr(changed, "id", ""))
            command = getattr(changed, "parentCommand", None)
            inputs = command.commandInputs if command is not None else args.inputs
            if input_id == "fem_add_group":
                _add_fem_group(inputs, self.state)
            elif input_id == "fem_remove_group" and self.state.group_ids:
                _remove_fem_group(inputs, self.state, self.state.group_ids[-1])
            elif input_id == "fem_body":
                bodies = _selected_entities(inputs, "fem_body")
                if bodies:
                    _restore_fem_region(inputs, self.state, bodies[0])
            elif input_id.startswith(("fem_kind_", "fem_lining_")):
                group_id = int(input_id.rsplit("_", 1)[-1])
                _sync_fem_group_options(inputs, group_id)
            _sync_fem_validation(inputs, self.state)
        except Exception as exc:  # noqa: BLE001 - the dialog must keep working
            _log(f"Declare FEM Region input update failed: {exc}")


class FemValidateInputsHandler(adsk.core.ValidateInputsEventHandler):
    def __init__(self, state: FemDialogState):
        super().__init__()
        self.state = state

    def notify(self, args: object) -> None:
        try:
            args.areInputsValid = _sync_fem_validation(_command_inputs(args.inputs), self.state)
        except Exception:  # noqa: BLE001 - execute repeats the validation
            try:
                args.areInputsValid = False
            except Exception:  # noqa: BLE001
                pass


class CommandInputChangedHandler(adsk.core.InputChangedEventHandler):
    def __init__(self, operation: str = "dialog"):
        super().__init__()
        self.operation = operation

    def notify(self, args: object) -> None:
        # A dialog's refresh is part of the command whose dialog it is.
        with wglink_activity.because(wglink_activity.command_cause(self.operation)):
            self._changed(args)

    def _changed(self, args: object) -> None:
        try:
            changed = getattr(args, "input", None)
            input_id = str(getattr(changed, "id", ""))
            inputs = args.inputs
            if input_id == "send_selection":
                _sync_anchor_choices(inputs)
                _sync_preflight(inputs)
            elif input_id == "anchor_instance_id":
                _sync_preflight(inputs)
            elif input_id == "refresh_preflight":
                # Fusion body/occurrence visibility can change while this modal
                # is open without producing a command-input event. Re-survey on
                # an explicit read-only action so the displayed body inventory
                # and anchor choices cannot remain stale.
                _sync_anchor_choices(inputs)
                _sync_preflight(inputs)
                try:
                    changed.value = False
                except Exception:  # noqa: BLE001 - old Fusion buttons may be write-only
                    pass
            elif input_id == "declaration":
                _sync_help(
                    inputs,
                    "declaration_help",
                    "declaration",
                    wglink_author.declaration_help_text,
                )
        except Exception:  # noqa: BLE001 - execute remains the validation boundary
            pass


# -- Sources & Mesh ----------------------------------------------------------

MESH_DIALOG_TITLE = "Sources & Mesh — Waveguide Generator"


class MeshDialogState:
    def __init__(self):
        self.sources = {}
        self.groups = {}
        self.next_id = 1
        self.restoring = False
        self.context = {}
        self.original_sources = []
        self.original_groups = []
        self.preview_error = None
        self.activated = False


def _mesh_faces(entities):
    faces = {}
    for entity in entities:
        for face in wglink_core._items(entity.faces) if hasattr(entity, "faces") else [entity]:
            faces.setdefault(wglink_send._face_key(face), face)
    return list(faces.values())


def _mesh_members(inputs, prefix, row_id):
    return _mesh_faces(_selected_entities(inputs, f"mesh_{prefix}_faces_{row_id}"))


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
        item.isSelected = item.name == label


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
    _add_selection_filters(selection, *(('Faces',) if is_source else ('Faces', 'SolidBodies', 'SurfaceBodies')))
    selection.setSelectionLimits(0, 0)
    if linked:
        # Fusion does not disable selection inputs with isEnabled. Keep linked
        # membership in the record and show a read-only face count instead.
        selection.isVisible = False
        children.addTextBoxCommandInput(f"mesh_linked_faces_{row_id}", "Faces", str(len(faces)), 1, True)
    elif state.activated:
        _restore_mesh_selections(selection, rows[row_id])
    value = children.addValueInput(f"mesh_{kind}_size_{row_id}", "Mesh size", "mm", _mm_input(size))
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


def _build_mesh_dialog(inputs, state):
    state.restoring = True
    try:
        design = wglink_core._design(_app())
        report = wglink_send.preflight_scope(
            _app(), {"source_identity": _source_identity_enabled(), "display_automatic_domain": True},
            authoring_context=state.context,
        )
        state.preview_error = report.get("source_error")
        saved = wglink_send.read_mesh_settings(design)
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
        wall = walls.addValueInput("mesh_wall", "Walls", "mm", _mm_input(mesh["wall_size_mm"]))
        wall.tooltip = "Mesh size for every surface that is not a source or in a surface group."
        advanced = inputs.itemById("mesh_advanced").children
        grading = advanced.addValueInput("mesh_grading", "Grading distance", "mm", _mm_input(mesh["grading_distance_mm"]))
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
        definitions = list(saved["surface_groups"] if saved else [])
        known = {group["group_id"] for group in definitions}
        painted = {}
        for face in all_faces:
            label = wglink_core._appearance_name(face) or ""
            if label.startswith("WGMESH_"):
                painted.setdefault(label[len("WGMESH_"):], []).append(face)
        for group_id in sorted(painted.keys() - known):
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
                else _selected_name(inputs, f"mesh_source_role_{row_id}", "HF"))
        sources.append({**record, "id": record.get("id", f"new-{row_id}"), "role": role,
                        "faces": faces, "size_mm": (record["size_mm"] if record.get("instance_id")
                            else _mm_value(inputs, f"mesh_source_size_{row_id}"))})
    for row_id, record in state.groups.items():
        groups.append({**record, "name": str(inputs.itemById(f"mesh_group_name_{row_id}").value).strip(),
                       "size_mm": _mm_value(inputs, f"mesh_group_size_{row_id}"),
                       "faces": (_mesh_members(inputs, "group", row_id) if state.activated else record["original_faces"])})
    return sources, groups, {
        "wall_size_mm": _mm_value(inputs, "mesh_wall"),
        "grading_distance_mm": _mm_value(inputs, "mesh_grading"),
        "symmetry": "off" if _selected_name(inputs, "mesh_symmetry", "Automatic") == "Off" else "auto",
        "settings_origin": "user",
    }


def _mesh_dialog_errors(sources, groups, mesh, state):
    errors = [state.preview_error] if state.preview_error else []
    if not sources:
        errors.append("There must be at least one source.")
    def size_error(label, value):
        if value is None or not math.isfinite(value) or value <= 0:
            errors.append(f"{label}: size must be greater than 0.")
    size_error("Walls", mesh["wall_size_mm"])
    size_error("Grading distance", mesh["grading_distance_mm"])
    source_keys, group_keys, roles, names = {}, {}, {}, {}
    for kind, rows in (("Source", sources), ("Group", groups)):
        for row in rows:
            label = (f"Source {row['row_id']} ({row['role']})" if kind == "Source"
                     else f"Group {row['row_id']} ({row['name'] or 'unnamed'})")
            size_error(label, row["size_mm"])
            if not row["faces"]:
                errors.append(f"{label}: select at least one face.")
            selected = {wglink_send._face_key(face) for face in row["faces"]}
            missing = row.get("unselected_saved_faces", set()) - selected
            missing_count = min(len(missing), max(0, len(row["original_faces"]) - len(selected)))
            if missing_count:
                errors.append(f"{label}: {missing_count} saved faces could not be selected (hidden or changed). Reselect them.")
            if kind == "Source":
                if not row.get("instance_id"):
                    if row["role"] in roles:
                        errors.append(f"{label}: same role as {roles[row['role']]}. Use one source per role.")
                    roles[row["role"]] = label
                other = source_keys
            else:
                if not row["name"]:
                    errors.append(f"{label}: give this surface group a name.")
                name = row["name"].casefold()
                if name in names:
                    errors.append(f"{label}: duplicate group name with {names[name]}. Use unique group names.")
                names[name] = label
                for key in selected & source_keys.keys():
                    errors.append(f"{label}: a face is also in {source_keys[key]}. A face cannot be in both a source and a surface group.")
                other = group_keys
            for key in selected & other.keys():
                plural = "sources" if kind == "Source" else "surface groups"
                errors.append(f"{label}: a face is also in {other[key]}. A face cannot be in two {plural}.")
            other.update({key: label for key in selected})
    return list(dict.fromkeys(errors))


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
    estimate_text = "Approximate estimate is available when the settings are valid. Limit 22,000."
    if not errors:
        try:
            measured, snapshot = _mesh_snapshot_values(sources, groups, mesh)
            area = sum(wglink_send._face_area(face) for face in _mesh_faces(state.context.get("estimate_bodies", [])))
            estimate = wglink_mesh.estimate_triangles(
                area, measured, snapshot, solved_fraction=0.25 if mesh["symmetry"] == "auto" else 1.0
            )
            count, full = estimate["triangles"], estimate["drawn_triangles"]
            estimate_text = f"≈ {count:,} triangles for the part WG solves"
            if mesh["symmetry"] == "auto":
                estimate_text += f" (assuming quarter symmetry; WG checks symmetry; up to {full:,} if none)"
            estimate_text += ". Limit 22,000. (approximate)"
            if count > 18000:
                estimate_text = f'<font color="#b77900">{estimate_text}</font>'
            if count > 22000:
                warnings.append("The approximate estimate for the part WG solves is above the 22,000 triangle limit. Larger sizes give fewer triangles.")
        except (wglink_mesh.MeshSettingsError, wglink_core.WgLinkError):
            estimate_text = "Approximate estimate unavailable for this geometry. Limit 22,000."
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
    sources, groups, mesh = _mesh_dialog_values(inputs, state)
    errors = _mesh_dialog_errors(sources, groups, mesh, state)
    if errors:
        raise wglink_author.AuthorError("Fix before saving: " + " ".join(errors))
    # Validate the entire snapshot before the first document edit.
    _mesh_snapshot_values(sources, groups, mesh)
    app = _app()
    design = wglink_core._design(app)
    faces = _mesh_faces([
        *(face for source in state.original_sources if not source.get("instance_id")
          for face in source["original_faces"]),
        *state.original_groups,
        *(face for row in [*sources, *groups] if not row.get("instance_id") for face in row["faces"]),
    ])
    # Source reassignment can also repair stamps outside the selected faces.
    # Reuse its transaction journal for a single atomic dialog Save.
    stamps = wglink_send._StampEdit()
    for attribute in wglink_core._items(design.findAttributes(
            wglink_core.ATTRIBUTE_GROUP, wglink_send.SOURCE_IDENTITY_ATTRIBUTE)):
        stamps._remember(attribute.parent)
    for face in faces:
        native = _native(face)
        stamps.remember_appearance(native)
        stamps._remember(native)
        stamps.remember_attribute(native, wglink_send.GROUP_ORIGINAL_APPEARANCE_ATTRIBUTE)
    stamps.remember_attribute(design.rootComponent, wglink_mesh.SETTINGS_ATTRIBUTE)
    try:
        return _save_mesh_dialog(design, app, state, sources, groups, mesh)
    except Exception as exc:
        problems = stamps.rollback()
        if problems:
            raise wglink_author.AuthorError(f"Could not save sources and mesh: {exc} Undo this command in Fusion; some changes could not be restored.") from exc
        raise wglink_author.AuthorError(f"Could not save sources and mesh: {exc} This dialog's changes were restored.") from exc


def _save_mesh_dialog(design, app, state, sources, groups, mesh):
    if state.original_groups:
        wglink_send.paint_surface_group(app, design, state.original_groups, None)
    final_keys = {wglink_send._face_key(face) for source in sources if not source.get("instance_id") for face in source["faces"]}
    removed = [face for source in state.original_sources if not source.get("instance_id")
               for face in source["original_faces"] if wglink_send._face_key(face) not in final_keys]
    if removed:
        _apply_source_faces(removed, wglink_author.CLEAR_SOURCE_LABEL)
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
            _apply_source_faces(source["faces"], source["role"])
    all_source_faces = [face for source in sources for face in source["faces"]]
    for group in groups:
        wglink_send.paint_surface_group(app, design, group["faces"], group["group_id"], source_faces=all_source_faces)
    # Assignment can mint an identity. Store the ids Send will actually emit.
    context = {}
    report = wglink_send.preflight_scope(app, {"source_identity": _source_identity_enabled()}, authoring_context=context)
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
    global _source_authoring_generation
    _source_authoring_generation += 1
    return {"summary": "Saved sources and mesh settings in this document."}


class MeshInputChangedHandler(adsk.core.InputChangedEventHandler):
    def __init__(self, state):
        super().__init__()
        self.state = state

    def notify(self, args):
        if self.state.restoring:
            return
        self.state.restoring = True
        try:
            command = args.input.parentCommand
            inputs = command.commandInputs if command is not None else _command_inputs(args.inputs)
            input_id = args.input.id
            for kind, rows in (("source", self.state.sources), ("group", self.state.groups)):
                if input_id == f"mesh_add_{kind}":
                    record = None
                    if kind == "source":
                        used = {_selected_name(inputs, f"mesh_source_role_{row}", "HF") for row in rows}
                        record = {"role": next((role for role in wglink_protocol.MESH_DEFAULTS["source_size_mm"] if role not in used), "HF")}
                    _add_mesh_row(inputs, self.state, kind, record)
                elif input_id.startswith(f"mesh_remove_{kind}_"):
                    row = int(input_id.rsplit("_", 1)[1])
                    if not rows[row].get("instance_id"):
                        inputs.itemById(f"mesh_{kind}_{row}").deleteMe()
                        del rows[row]
            if input_id.startswith("mesh_group_preset_"):
                row = int(input_id.rsplit("_", 1)[1])
                name = _selected_name(inputs, input_id, "Custom").split()[0]
                if name in wglink_protocol.MESH_PRESETS:
                    inputs.itemById(f"mesh_group_size_{row}").value = wglink_protocol.MESH_PRESETS[name] / 10
            elif input_id.startswith("mesh_group_size_"):
                row = int(input_id.rsplit("_", 1)[1])
                size = _mm_value(inputs, input_id)
                label = next((f"{name} {value:g} mm" for name, value in wglink_protocol.MESH_PRESETS.items() if value == size), "Custom")
                _mesh_select(inputs.itemById(f"mesh_group_preset_{row}"), label)
            elif input_id == "mesh_reset":
                defaults = wglink_protocol.MESH_DEFAULTS
                inputs.itemById("mesh_wall").value = defaults["wall_size_mm"] / 10
                inputs.itemById("mesh_grading").value = defaults["grading_distance_mm"] / 10
                for row, source in self.state.sources.items():
                    if not source.get("instance_id"):
                        role = _selected_name(inputs, f"mesh_source_role_{row}", "HF")
                        inputs.itemById(f"mesh_source_size_{row}").value = wglink_protocol.default_source_size_mm(role) / 10
                for row in self.state.groups:
                    inputs.itemById(f"mesh_group_size_{row}").value = wglink_protocol.MESH_PRESETS["Fine"] / 10
                    label = f"Fine {wglink_protocol.MESH_PRESETS['Fine']:g} mm"
                    _mesh_select(inputs.itemById(f"mesh_group_preset_{row}"), label)
            _sync_mesh_dialog(inputs, self.state)
        except Exception as exc:
            _report_error("Updating Sources & Mesh…", f"{PANEL_NAME} error", exc)
        finally:
            self.state.restoring = False


class MeshValidateInputsHandler(adsk.core.ValidateInputsEventHandler):
    def __init__(self, state):
        super().__init__()
        self.state = state

    def notify(self, args):
        try:
            args.areInputsValid = _sync_mesh_dialog(_command_inputs(args.inputs), self.state)
        except Exception:
            args.areInputsValid = False


class MeshDialogActivateHandler(adsk.core.CommandEventHandler):
    def __init__(self, state):
        super().__init__()
        self.state = state

    def notify(self, args):
        state = self.state
        if state.activated:
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
            _sync_mesh_dialog(inputs, state)
        finally:
            state.restoring = False


class MeshDialogDestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        args.command.parentCommandDefinition.name = COMMANDS["source"][1]


class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self, operation: str):
        super().__init__()
        self.operation = operation

    def notify(self, args: object) -> None:
        # Building the dialog reads the document (pre-flight, link choices):
        # that is this command's work.
        with wglink_activity.because(wglink_activity.command_cause(self.operation)):
            self._created(args)

    def _created(self, args: object) -> None:
        try:
            if self.operation == "fem":
                _require_fem_regions()
            inputs = args.command.commandInputs
            if self.operation in {"send", "solve"}:
                selection = inputs.addSelectionInput(
                    "send_selection",
                    "Assembly scope",
                    "Leave empty for the root, or select one occurrence subtree.",
                )
                selection.addSelectionFilter("Occurrences")
                selection.setSelectionLimits(0, 1)
                # The return folder is WG's setting, like the bundle folder:
                # WG only ingests from its own workspace, so a return written
                # anywhere else is invisible to it. Head-less callers may still
                # pass output_folder and overwrite.
                anchor = inputs.addDropDownCommandInput(
                    "anchor_instance_id",
                    "Solver anchor instance",
                    adsk.core.DropDownStyles.TextListDropDownStyle,
                )
                anchor.isVisible = False
                _sync_anchor_choices(inputs)
                # State the export before it happens: what goes in, whether it
                # is linked, which sources drive it, and -- for an unlinked
                # model, whose assembly frame WG solves as-is -- how far the
                # model sits from that frame. A missing source used to be a
                # dead-end modal raised after the user pressed OK.
                inputs.addTextBoxCommandInput("preflight", "Pre-flight", "", 8, True)
                _sync_preflight(inputs)
                inputs.addBoolValueInput(
                    "refresh_preflight",
                    "Refresh body inventory",
                    False,
                    "",
                    False,
                )
                changed = CommandInputChangedHandler(self.operation)
                args.command.inputChanged.add(changed)
                _handlers.append(changed)
            if self.operation == "source":
                command = args.command
                command.parentCommandDefinition.name = MESH_DIALOG_TITLE
                command.isExecutedWhenPreEmpted = False
                command.okButtonText = "Save"
                destroy = MeshDialogDestroyHandler()
                command.destroy.add(destroy)
                _handlers.append(destroy)
                command.setDialogInitialSize(660, 720)
                command.setDialogMinimumSize(420, 420)
                state = MeshDialogState()
                _build_mesh_dialog(inputs, state)
                activate = MeshDialogActivateHandler(state)
                command.activate.add(activate)
                changed = MeshInputChangedHandler(state)
                command.inputChanged.add(changed)
                validate = MeshValidateInputsHandler(state)
                command.validateInputs.add(validate)
                handler = CommandExecuteHandler(self.operation)
                handler.mesh_state = state
                command.execute.add(handler)
                _handlers.extend((activate, changed, validate, handler))
                return
            if self.operation == "declare":
                bodies = inputs.addSelectionInput(
                    "declare_bodies",
                    "Bodies",
                    "Select the bodies to classify for the WG return.",
                )
                _add_selection_filters(
                    bodies, "SolidBodies", "SurfaceBodies", "MeshBodies"
                )
                bodies.setSelectionLimits(1, 0)
                declaration = inputs.addDropDownCommandInput(
                    "declaration",
                    "Declaration",
                    adsk.core.DropDownStyles.TextListDropDownStyle,
                )
                for index, choice in enumerate(wglink_author.declaration_choices()):
                    declaration.listItems.add(choice, index == 0)
                inputs.addTextBoxCommandInput(
                    "declaration_help",
                    "",
                    wglink_author.declaration_help_text(
                        wglink_author.declaration_choices()[0]
                    ),
                    3,
                    True,
                )
                changed = CommandInputChangedHandler(self.operation)
                args.command.inputChanged.add(changed)
                _handlers.append(changed)
            if self.operation == "insert":
                source = inputs.addDropDownCommandInput(
                    "bundle_source",
                    "Bundle source",
                    adsk.core.DropDownStyles.TextListDropDownStyle,
                )
                # Bundles already sitting in WG's workspace come first, so the
                # ordinary case needs no file dialog and no second copy of a
                # folder the user set once in WG.
                discovered = _discovered_bundles()
                for index, bundle in enumerate(discovered):
                    source.listItems.add(bundle.label(), index == 0)
                source.listItems.add(BROWSE_FOLDER, not discovered)
                source.listItems.add(BROWSE_ZIP, False)
                # A label for this placement, in the user's own words. It is
                # display only: the parameter namespace, the wrapper component
                # and every identifier still come from the bundle, because an
                # already-linked document depends on them for its lifetime.
                inputs.addStringValueInput("link_name", "Link name", "")
                inputs.addTextBoxCommandInput(
                    "link_name_help",
                    "",
                    "Optional. Leave empty to use the WG design name. This "
                    "names the link in menus and in the timeline only; the "
                    "wg_&lt;name&gt;_* parameters keep the bundle's namespace.",
                    3,
                    True,
                )
            if self.operation == "fem":
                state = FemDialogState()
                _build_fem_dialog(inputs, state)
                changed = FemInputChangedHandler(state)
                args.command.inputChanged.add(changed)
                _handlers.append(changed)
                validate = FemValidateInputsHandler(state)
                args.command.validateInputs.add(validate)
                _handlers.append(validate)
                handler = CommandExecuteHandler(self.operation)
                handler.fem_state = state
                args.command.execute.add(handler)
                _handlers.append(handler)
                return
            if self.operation not in {"insert", "send", "solve", "source", "declare"}:
                # A document with one link needs no choice, and the old free
                # text field required a separate diagnostic call just to learn
                # the id.
                links = _document_link_choices()
                if len(links) > 1:
                    chooser = inputs.addDropDownCommandInput(
                        "instance_choice",
                        "Managed link",
                        adsk.core.DropDownStyles.TextListDropDownStyle,
                    )
                    for index, (label, _instance_id) in enumerate(links):
                        chooser.listItems.add(label, index == 0)
            # force and allow_root_fallback stay head-less-only: every refusal
            # names the recovery path, and the root fallback already warns in
            # its report.
            handler = CommandExecuteHandler(self.operation)
            args.command.execute.add(handler)
            _handlers.append(handler)
        except Exception as exc:  # noqa: BLE001
            _report_error("Building a dialog for the Waveguide Generator add-in", f"{PANEL_NAME} command error", exc)


def _delete_quietly(entity: object) -> None:
    try:
        if entity and entity.isValid:
            entity.deleteMe()
    except Exception:  # noqa: BLE001 - tolerate duplicate/stale add-in registrations
        pass


def _workspace(ui: object) -> object:
    workspace = ui.workspaces.itemById("FusionSolidEnvironment")
    if workspace is None:
        raise RuntimeError("Could not find Fusion's Design workspace.")
    return workspace


# How old a cached observation may get before a refresh that is waiting on the
# throttle must be allowed through. It is no longer a ceiling on what may be
# *published*: a cache-only heartbeat that refused to publish anything past a
# minute left a linked idle document with no baseline at all, and WG cannot ask
# for a model whose state it was never told. What may be published is governed
# by the revision tokens instead, which say exactly how current an observation
# is without anyone inspecting geometry.
GEOMETRY_STATE_MAX_AGE_SECONDS = 60.0
GEOMETRY_STATE_DUTY_CYCLE = 12.0
# The throttle may never outlive the observation it is protecting. While these
# two disagreed -- 120 against 60 -- an expensive document postponed its
# refresh for two minutes, and the state that refresh was waiting to replace
# had aged out one minute earlier.
GEOMETRY_STATE_MAX_WAIT_SECONDS = GEOMETRY_STATE_MAX_AGE_SECONDS


# Bumped by Sources & Mesh:: a stamp is an attribute write, which moves neither
# the timeline nor a body revision, and the heartbeat's source ids depend on it.
_source_authoring_generation = 0


def _geometry_change_key(design: object, records: dict) -> tuple:
    """A key that moves when the geometry the heartbeat measures moves.

    Every entry here is a property read. Nothing evaluates a surface, and that
    is the whole point: ``body.volume`` and ``face.area`` are *computed* on a
    dense NURBS body, not looked up, so recomputing them on a four-second timer
    is what makes Fusion unusable on a linked document. The timeline count
    catches every modelling operation in a parametric design; the per-body
    revision and visibility catch an edit or a hide that leaves the count
    alone; the stored export id and edit version catch an Update.
    """

    automatic_domain = _manifest_feature_enabled(wglink_inbox.AUTOMATIC_DOMAIN)
    document_up = _manifest_feature_enabled(wglink_inbox.DOCUMENT_UP)
    parts: list[object] = [
        _source_authoring_generation,
        _source_identity_enabled(),
        automatic_domain,
        document_up,
        wglink_send._mesh_authoring_generation,
        wglink_core._attribute_value(
            getattr(design, "rootComponent", None), wglink_send.wglink_mesh.SETTINGS_ATTRIBUTE
        ),
    ]
    if document_up:
        try:
            parts.append(wglink_send.fusion_document_up(_app()))
        except Exception:  # noqa: BLE001 - an unreadable preference still changes the key
            parts.append("unreadable-document-up")
    try:
        parts.append(int(design.timeline.count))
    except Exception:  # noqa: BLE001 - a product without a timeline still keys
        parts.append(-1)
    for instance_id in sorted(records):
        record = records[instance_id]
        payload = record.get("payload") or {}
        body = record.get("body")
        parts.append((
            str(instance_id),
            str(payload.get("export_id") or ""),
            str(payload.get("edit_version") or ""),
            bool(record.get("managed_object_clash")),
            str(getattr(body, "revisionId", "") or "") if body is not None else "",
            bool(getattr(body, "isVisible", False)) if body is not None else False,
        ))
    return tuple(parts)


def _measure_geometry_state(app: object, records: dict) -> dict[str, object]:
    """The part of the heartbeat that costs real geometry evaluation."""

    # Counted before the advisory ``except`` below, which turns a measurement
    # that ran and failed into empty strings indistinguishable from none.
    wglink_activity.record(wglink_activity.GEOMETRY_MEASUREMENT)
    first_instance = next(iter(sorted(records)), None)
    try:
        return_state = wglink_send.return_state(
            app,
            {
                "selection": "root",
                **({"anchor_instance_id": first_instance} if first_instance else {}),
                "source_identity": _source_identity_enabled(),
                "automatic_domain": _manifest_feature_enabled(
                    wglink_inbox.AUTOMATIC_DOMAIN
                ),
                "document_up": _manifest_feature_enabled(wglink_inbox.DOCUMENT_UP),
            },
        )
        document_signature_hash = str(return_state.get("hash") or "")
        document_body_count = str(return_state.get("body_count") or "")
        source_state_hash = str(return_state.get("source_hash") or "")
        raw_instance_identities = return_state.get("instance_identities")
        instance_identities = (
            raw_instance_identities
            if isinstance(raw_instance_identities, dict)
            else {}
        )
    except Exception:  # noqa: BLE001 - advisory heartbeat may omit the token
        document_signature_hash = ""
        document_body_count = ""
        source_state_hash = ""
        instance_identities = {}
    bodies: dict[str, dict[str, object]] = {}
    for instance_id, record in records.items():
        try:
            if record.get("managed_object_clash"):
                # Audit refuses a duplicated instance id outright. The advisory
                # heartbeat says it cannot tell rather than picking one of them
                # and publishing that body's fingerprint as the link's.
                local_body_state = "unknown"
                body_fingerprint = None
            else:
                local_body_state = wglink_core._local_body_state(record)
                body = record.get("body")
                body_fingerprint = (
                    wglink_core._body_fingerprint(body) if body is not None else None
                )
        except Exception:  # noqa: BLE001 - advisory status may degrade to unknown
            local_body_state = "unknown"
            body_fingerprint = None
        bodies[str(instance_id)] = {
            "local_body_state": str(local_body_state),
            "body_fingerprint_hash": (
                _fingerprint_hash(body_fingerprint) if body_fingerprint else ""
            ),
        }
    return {
        "document_signature_hash": document_signature_hash,
        "document_body_count": document_body_count,
        "source_state_hash": source_state_hash,
        "instance_identities": instance_identities,
        "bodies": bodies,
    }


def _geometry_revision_token(design: object, records: dict) -> str:
    """The cheap revision token: a hash of property reads, and nothing else.

    :func:`_geometry_change_key` evaluates no surface -- it reads the timeline
    count, each managed body's ``revisionId`` and visibility, the stored export
    id and edit version, and the source-authoring generation. Hashing it gives
    one short string that moves when the geometry the measurement describes may
    have moved, at no cost to Fusion's main thread.

    It is published beside the measurement's own token so a consumer can tell,
    without anyone inspecting anything, whether the measured state it holds is
    still current. That is what lets the periodic heartbeat be cache-only.
    """

    try:
        return _fingerprint_hash(_geometry_change_key(design, records))
    except Exception:  # noqa: BLE001 - a document we cannot key has no token
        return ""


def _with_revision_tokens(
    state: dict[str, object], current: str, measured: str
) -> dict[str, object]:
    """Label a measured state with when it was taken, and where we are now."""

    return {
        **state,
        "geometry_revision_token": current,
        "measured_revision_token": measured,
    }


def _request_geometry_refresh(reason: str, document_id: str | None) -> None:
    """Ask for one geometry inspection of one document, from an explicit cause.

    Coalescing is the whole point: the slot holds at most one request per
    add-in instance and a second is dropped, not queued. Startup, a document
    switch and a burst of finished commands therefore cost one measurement
    between them, not one each.

    The request names the document it was asked for. A slot carrying only a
    reason is serviced against whatever document happens to be active when the
    tick lands, so asking about a linked document and then switching away paid
    for a walk of the document the user moved to -- which is neither what was
    asked for nor something WGLink has any business inspecting.

    Coalescing means one slot, not "the first request wins". A pending request
    for a *different* document is replaced: it is about a document nobody is
    asking about any more, and leaving it there would block the live question
    behind a dead one until a tick dropped it for mismatch -- after which
    nothing would ask again, and the document would never report a state.
    """

    global _geometry_refresh_pending
    pending = _geometry_refresh_pending
    if pending is not None and pending.get("document_id") == document_id:
        return
    _geometry_refresh_pending = {"reason": str(reason), "document_id": document_id}


def _linked_records_now() -> dict | None:
    """The active document's resolved WGLink records, or ``None`` for no links.

    Stored-attribute reads and inventory resolution -- the same call the tick
    already makes in :func:`_document_links`, and nothing that evaluates a
    surface. It exists so a refresh can be refused *before* it costs anything.

    Raises :class:`LinksNotInspected` when the records cannot be read: that is
    not "no links", and the caller records it as an attempt rather than
    deciding the document is unlinked.
    """

    app = _app()
    design = app.activeProduct if app else None
    if design is None or not isinstance(getattr(design, "objectType", ""), str):
        return None
    if "Design" not in str(design.objectType):
        return None
    try:
        records = wglink_core._resolved_link_records(design)
    except Exception as exc:  # noqa: BLE001 - unreadable is "not inspected"
        raise LinksNotInspected(_NOT_INSPECTED_TEXT) from exc
    return records or None


def _service_geometry_refresh(*, throttle: bool = True) -> None:
    """Pay for a requested inspection on the main thread, at most once.

    The tick is the only place a Fusion API call may run, so it is where a
    refresh is carried out -- but it is the carrier, never the cause. With
    nothing pending this returns immediately, which is what makes the
    heartbeat-caused inspection count zero.

    Three things can spend a request without measuring, and all three matter:

    * the active document is not the one the request named. The answer would
      be about the wrong document, so there is nothing to measure.
    * the active document has no WGLink records. ``_measure_geometry_state``
      walks the root export scope whatever the document is, so servicing a
      request here would walk a model WGLink has never touched -- the exact
      cost the unlinked fast path exists to avoid, arriving by another door.
    * the measurement ran. That is the ordinary case.

    The duty cycle survives here, and only here: it no longer defers a
    measurement the heartbeat wanted (the heartbeat wants none), it bounds what
    a stream of explicit causes may cost on a document where one measurement is
    expensive. It is capped at ``GEOMETRY_STATE_MAX_AGE_SECONDS`` so it can
    never outlive the observation it is protecting, and it is never applied
    against a cache entry belonging to a different document -- switching away
    from an expensive model must not throttle the one switched to.

    ``throttle=False`` is for a carrier that runs once and has no next tick to
    defer to: a command's own follow-up with coordination off. The command is
    the explicit cause, so it pays now rather than leaving the request
    pending for a tick that will never come.
    """

    global _geometry_refresh_pending
    pending = _geometry_refresh_pending
    if pending is None:
        return
    document_id = _active_document_id()
    if document_id is None or pending.get("document_id") != document_id:
        _geometry_refresh_pending = None
        return
    cached = _geometry_state_cache
    if throttle and cached is not None and cached["key"][0] == document_id:
        wait = min(
            float(cached["cost_ms"]) / 1000.0 * GEOMETRY_STATE_DUTY_CYCLE,
            GEOMETRY_STATE_MAX_WAIT_SECONDS,
        )
        if time.monotonic() - float(cached["at"]) < wait:
            return
    # Spent here, once, for every outcome that is not "still waiting": a
    # request left standing after the tick decided not to answer it is
    # retried on the next tick and the next, which is a periodic inspection
    # attempt wearing an explicit cause's clothes.
    _geometry_refresh_pending = None
    try:
        linked = _linked_records_now() is not None
    except LinksNotInspected:
        # Could not read, which is not "no links": record the attempt, so the
        # document is asked about again at the retry rate instead of being
        # treated as one WGLink never touched.
        linked = False
        _geometry_refresh_attempts[document_id] = time.monotonic()
    if not linked:
        return
    if len(_geometry_refresh_attempts) >= _GEOMETRY_REFRESH_ATTEMPTS_TRACKED:
        # A session's worth of documents, not a leak. The oldest entries are
        # for documents nothing is asking about any more.
        _geometry_refresh_attempts.clear()
    _geometry_refresh_attempts[document_id] = time.monotonic()
    _fresh_geometry_state()


def _has_usable_observation(document_id: str) -> bool:
    """Whether the cache holds a measurement of this document worth publishing.

    "Worth publishing" means it carries a signature hash. A measurement that
    produced none is an answer in the same sense that silence is: WG cannot
    publish a return request or an exact-target handoff from it, so the
    document is no better off than if nothing had ever been measured.
    """

    cached = _geometry_state_cache
    return bool(
        cached is not None
        and cached["key"][0] == document_id
        and str(cached["state"].get("document_signature_hash") or "")
    )


def _note_active_document(document_id: str | None, *, linked: bool) -> None:
    """The document-change notification: bounded work, never a scan.

    The cached measurement is keyed by document id, so a switch cannot publish
    the previous document's fingerprint whatever happens here. What this adds
    is the bounded half: one coalesced refresh so the active document reports a
    measured state, without a timer ever deciding to walk anything.

    The condition is deliberately **"this document has no usable observation"**
    rather than "the document changed". Both fire on a switch -- a switch
    leaves the single cache slot holding the other document -- but only the
    first survives a request that is spent without answering, and requests are
    spent without answering in more ways than are comfortable: the add-in
    loaded before the user opened a model, a Drawing was active at load, the
    document stopped being nameable between the tick that asked and the tick
    that would have paid, the measurement threw. Every one of those leaves a
    linked document publishing an empty ``documentSignatureHash``, which WG
    cannot act on and no WG-side action can repair. A rule keyed on change
    asks once and never learns that the answer never arrived.

    It is not a retry loop either. ``GEOMETRY_REFRESH_RETRY_SECONDS`` is the
    rate at which a document with no observation may be asked about again, so
    one that cannot be measured costs about one attempt a minute rather than
    one every four seconds -- and, unlike a fixed allowance, never stops being
    asked about, because a document that is still loading is indistinguishable
    from one that can never be measured and both recover. A measurement that
    yields a hash clears the entry and this stops asking for good. An idle
    document with an observation, or one whose observation is merely stale,
    asks for nothing.

    An unlinked document schedules nothing: there is no link state to publish
    for it, and ``return_state`` on the root scope is the cost that merely
    enabling WGLink must not impose on an unrelated model. A document this
    add-in cannot name schedules nothing either -- there is nowhere to cache
    the answer, and two unnameable documents would share the entry.
    """

    if not linked or document_id is None:
        return
    if _geometry_refresh_pending is not None or _has_usable_observation(document_id):
        return
    attempted = _geometry_refresh_attempts.get(document_id)
    if attempted is not None and (
        time.monotonic() - attempted < GEOMETRY_REFRESH_RETRY_SECONDS
    ):
        return
    _request_geometry_refresh("document-seen", document_id)


def _unavailable_geometry_state() -> dict[str, object]:
    """The shape of "this add-in cannot say", in the measured state's own keys.

    Same shape as a failed :func:`_measure_geometry_state`, so every consumer
    already degrades correctly: no signature token, no body fingerprints, and
    ``local_body_state`` reads ``unknown``. Publishing this is always allowed;
    publishing a *different* document's measurement never is.
    """

    return {
        "document_signature_hash": "",
        "document_body_count": "",
        "source_state_hash": "",
        "instance_identities": {},
        "bodies": {},
    }


def _geometry_state(
    app: object,
    design: object,
    records: dict,
    document_id: str | None,
    timings: dict[str, float] | None = None,
    *,
    force: bool = False,
) -> tuple[dict[str, object], str]:
    """Cached state for a heartbeat; a measurement only when asked explicitly.

    **The periodic heartbeat reads the cache and nothing else.** Every verdict
    it can produce -- ``cached``, ``stale``, ``unavailable`` -- is a statement
    about a measurement somebody already paid for. It never returns
    ``measured``, because ``force`` is the only path that measures, and no
    timer sets it. ``_measure_geometry_state`` walks the root export scope and
    evaluates every included face and body on Fusion's main thread, so a
    four-second timer that can reach it is a permanent load on a linked dense
    document however cleverly it is throttled. Change detection made that load
    rarer; it did not make the tick cache-only, and on a first tick, a document
    switch or a restart it did not help at all.

    ``stale`` publishes the measurement it has rather than nothing, and says so
    through the revision tokens. This is deliberate: the cached token is a real
    measurement of *this* document that is no longer current, and refusing to
    publish anything would leave WG unable to ask for a model at all -- its
    return request requires a baseline. Publishing it is safe because the
    guard that matters re-measures: :func:`_require_live_state` forces a
    measurement before any mutation, so a moved model is refused, never
    overwritten, and that refusal refreshes this cache for the next tick.

    **There is no age at which a cached observation is withdrawn.** There used
    to be: past ``GEOMETRY_STATE_MAX_AGE_SECONDS`` this returned "cannot tell",
    because the old wire had no way to say how old an observation was, so a
    minute was the point past which publishing one was misleading. The revision
    tokens say it exactly, and withdrawing the observation instead is strictly
    worse: a linked document left idle for a minute published an empty
    ``document_signature_hash`` for ever, WG refuses to publish a return
    request or an exact-target handoff without one, and nothing in WG's UI can
    cause a measurement -- so the user could not ask for the model at all.
    Nothing renews a cache the heartbeat may not measure into; the age ceiling
    only decided how long an unusable state took to arrive.

    Cached state belongs to the document it was measured in. The key carries
    ``document_id``, so a switch is ``unavailable`` -- empty tokens, empty
    hashes, ``unknown`` body state -- and never the previous document's
    measurement wearing the new document's identity.

    ``force`` is for an explicit cause -- a guarded update or return WG asked
    for, or a refresh :func:`_service_geometry_refresh` was asked for -- which
    happens once and can pay for one measurement. It also settles a pending
    refresh: the request has been answered.
    """

    global _geometry_state_cache, _geometry_refresh_pending
    started = time.perf_counter()
    revision_token = _geometry_revision_token(design, records)
    key = (document_id, revision_token)
    if not force:
        cached = _geometry_state_cache
        state = _unavailable_geometry_state()
        verdict = "unavailable"
        measured_token = ""
        age = None
        if cached is not None:
            age = time.monotonic() - float(cached["at"])
            # ``None`` is "cannot name this document", not a name. Two
            # documents this add-in cannot name would otherwise share one
            # cache entry, which is how one document's measurement reaches
            # another document's links. Nothing writes such an entry; this is
            # what makes that a property rather than a coincidence.
            if document_id is not None and cached["key"][0] == document_id:
                state = dict(cached["state"])
                if str(state.get("document_signature_hash") or ""):
                    measured_token = str(cached["key"][1])
                    # An empty revision token means the key could not be
                    # computed, not that nothing moved. Comparing one unknown
                    # with another reported ``cached`` -- a full measurement
                    # presented as current, on the strength of two blanks
                    # matching.
                    verdict = (
                        "cached"
                        if revision_token and cached["key"] == key
                        else "stale"
                    )
                else:
                    # A measurement that produced no document-level baseline.
                    # What it does carry -- each managed body's state, which is
                    # how a deleted body still reads ``missing`` -- is real and
                    # is published. The currency claim is not: reporting
                    # ``cached`` with a token beside an empty signature hash
                    # said "this measurement is current" while carrying no
                    # measurement for anything to be current about.
                    measured_token = ""
                    verdict = "stale"
        if timings is not None:
            timings["geometry_state_ms"] = round(
                (time.perf_counter() - started) * 1000.0, 1
            )
            # No cache, no age. A sentinel would be a number nobody can read.
            if age is not None:
                timings["geometry_state_age_s"] = round(age, 1)
        return _with_revision_tokens(state, revision_token, measured_token), verdict
    state = _measure_geometry_state(app, records)
    cost_ms = (time.perf_counter() - started) * 1000.0
    measured = str(state.get("document_signature_hash") or "")
    # A document that cannot be named has no key to file this under. Every
    # unnameable document would read back the same entry, which is the
    # substitution this whole design exists to prevent -- and the read path
    # refuses such an entry anyway, so writing one only evicted the last
    # usable observation.
    if document_id is not None and (
        measured or not _has_usable_observation(document_id)
    ):
        # A measurement that produced no document-level baseline is still worth
        # keeping when there is nothing better: it carries each managed body's
        # state, which is how a deleted body reads ``missing``. It must not
        # replace *this document's* own baseline -- across documents it still
        # can, because the cache is one slot and the active document owns it.
        _geometry_state_cache = {
            "key": key,
            "at": time.monotonic(),
            "cost_ms": cost_ms,
            "state": state,
        }
    if document_id is not None and measured:
        # A baseline arrived. Whatever went wrong before it, this document is
        # no longer one that needs asking about.
        _geometry_refresh_attempts.pop(document_id, None)
    _geometry_refresh_pending = None
    if timings is not None:
        timings["geometry_state_ms"] = round(cost_ms, 1)
        timings["geometry_state_age_s"] = 0.0
    return _with_revision_tokens(state, revision_token, revision_token), "measured"


class LinksNotInspected(wglink_core.WgLinkError):
    """The document's links could not be read, so there is no evidence either way."""


_NOT_INSPECTED_TEXT = (
    "The Waveguide Generator add-in could not read this Fusion document's WG links, so it cannot tell "
    "whether this change already happened. Nothing was changed."
)


def _link_evidence(snapshot: dict[str, object]) -> list[dict[str, object]] | None:
    """The links a snapshot read, or None when it did not read them.

    "Not inspected" is its own state. Reconciliation decides from link evidence
    whether an interrupted operation applied, and an empty list read as "no
    evidence" would call an applied operation never-started. A snapshot built
    by :func:`_fusion_snapshot` always says whether it looked; a hand-made one
    that carries a ``links`` list is taken as having looked.
    """

    if snapshot.get("links_inspected") is False or "links" not in snapshot:
        return None
    return list(snapshot.get("links") or [])


def _document_links(timings: dict[str, float] | None = None) -> list[dict[str, object]]:
    """Copy each managed link's identity out of the document as plain strings.

    Raises :class:`LinksNotInspected` when the document's links cannot be
    read. It used to return ``[]``, and every caller then read "could not
    read" as "there are no links": reconciliation called an applied operation
    never-started, and an insert's already-linked guard passed. A caller that
    only *shows* links catches it; one that decides from them refuses.

    Called on Fusion's main thread only. What it returns is deliberately inert
    data: the file request worker runs on another thread and must never hold a live Fusion
    object.

    Identity comes from stored attributes and costs nothing, so it is read on
    every tick. The measured half -- the export fingerprint and the body
    fingerprints -- goes through :func:`_geometry_state`, which on this path
    reads the cache and never measures. Called on a timer it measures no
    geometry, but it does resolve every managed link through the Fusion API
    (STEP2 section 3), which is why no timer calls it while automatic
    coordination is off.

    ``timings`` collects the wall clock of each phase in milliseconds, which is
    the only way to say what this costs on a real document without attaching a
    debugger to Fusion.
    """

    def _record(name: str, started: float) -> None:
        if timings is not None:
            timings[name] = round((time.perf_counter() - started) * 1000.0, 1)

    app = _app()
    design = app.activeProduct if app else None
    # A document with nothing to report needs no notification and no document
    # id: :func:`_note_active_document` acts only on a linked document, so
    # asking here would cost a Fusion round trip to reach an immediate return.
    if design is None or not isinstance(getattr(design, "objectType", ""), str):
        return []
    if "Design" not in str(design.objectType):
        return []
    resolve_started = time.perf_counter()
    try:
        # The resolved inventory, not the raw attribute grouping: a record from
        # `_link_records` alone carries no managed body, so the heartbeat used
        # to publish `missing` and no fingerprint for links Audit reports as
        # intact.
        records = wglink_core._resolved_link_records(design)
    except Exception as exc:  # noqa: BLE001 - unreadable is "not inspected", never "no links"
        _record("resolve_links_ms", resolve_started)
        raise LinksNotInspected(_NOT_INSPECTED_TEXT) from exc
    _record("resolve_links_ms", resolve_started)
    if not records:
        # A heartbeat has no link state or optimistic-concurrency token to
        # publish for an unrelated document.  Calling ``return_state`` here
        # walks the root export scope and evaluates every included face and
        # body on Fusion's main thread, so merely enabling WGLink could stall
        # any complex model even though WGLink had never touched it.
        if timings is not None:
            timings["geometry_state"] = "not-linked"
            timings["geometry_state_ms"] = 0.0
        return []
    document_id = _active_document_id()
    _note_active_document(document_id, linked=True)
    state, verdict = _geometry_state(app, design, records, document_id, timings)
    if timings is not None:
        timings["geometry_state"] = verdict
        timings["geometry_refresh_pending"] = _geometry_refresh_pending is not None
    instance_identities = state["instance_identities"]
    body_states = state["bodies"]
    links: list[dict[str, object]] = []
    per_link_started = time.perf_counter()
    for instance_id, record in records.items():
        payload = record.get("payload") or {}
        try:
            stored_config = json.loads(str(payload.get("config_json") or ""))
            config_present = isinstance(stored_config, dict) and bool(stored_config)
        except (TypeError, ValueError):
            config_present = False
        try:
            parameter_expressions = json.loads(
                str(payload.get("parameter_expressions") or "{}")
            )
            parameter_count = (
                len(parameter_expressions)
                if isinstance(parameter_expressions, dict)
                else 0
            )
        except (TypeError, ValueError):
            parameter_count = 0
        try:
            # Drift is a comparison of stored and live parameter expressions:
            # a handful of string reads, so it stays on every tick and never
            # goes stale behind the cache.
            parameter_drift = wglink_core._parameter_drift(design, record)
            drifted_parameters = sorted(
                str(item["name"]) for item in parameter_drift
            )
        except Exception:  # noqa: BLE001 - advisory status may degrade to unknown
            drifted_parameters = []
        measured = body_states.get(str(instance_id), {})
        link = {
            "instance_id": str(instance_id),
            "bundle_path": str(payload.get("bundle_path") or ""),
            "design_id": str(payload.get("design_id") or ""),
            "lineage_id": str(payload.get("lineage_id") or ""),
            "edit_version": str(payload.get("edit_version") or ""),
            "design_hash": str(payload.get("design_hash") or ""),
            "design_name": str(payload.get("design_name") or ""),
            "link_name": str(payload.get("link_name") or ""),
            "formula": str(payload.get("formula") or ""),
            "config_present": "true" if config_present else "false",
            "parameter_count": str(parameter_count),
            "parameter_drift_count": str(len(drifted_parameters)),
            "drifted_parameters": drifted_parameters,
            "local_body_state": str(measured.get("local_body_state") or "unknown"),
            "body_fingerprint_hash": str(measured.get("body_fingerprint_hash") or ""),
            "document_signature_hash": state["document_signature_hash"],
            "document_body_count": state["document_body_count"],
            "source_state_hash": state["source_state_hash"],
            # The cheap revision token the document is at now, and the one the
            # published measurement was taken at. Equal means the measured
            # state is current; unequal means it is a cached observation of an
            # earlier revision; an empty measured token means there is no
            # measurement to offer. Neither costs a geometry evaluation, and
            # neither is ever presented as a fresh measurement.
            "geometry_revision_token": str(
                state.get("geometry_revision_token") or ""
            ),
            "measured_revision_token": str(
                state.get("measured_revision_token") or ""
            ),
            "export_id": str(payload.get("export_id") or ""),
            "export_sequence": str(payload.get("export_sequence") or ""),
            "operation_id": str(payload.get("operation_id") or ""),
        }
        identity = instance_identities.get(str(instance_id))
        if isinstance(identity, dict):
            for name in ("body_object_ids", "source_ids", "drive_channel_ids"):
                value = identity.get(name)
                if isinstance(value, list):
                    link[name] = value
            transform_hash = identity.get("transform_hash")
            if isinstance(transform_hash, str) and transform_hash:
                link["transform_hash"] = transform_hash
        links.append(link)
    _record("per_link_ms", per_link_started)
    return links


def _active_document_id() -> str | None:
    """A durable identity for the active document, saved or not.

    A saved document has one: its data file id. An unsaved one has no data
    file, and neither Python's identity for the ``Document`` object nor its
    address is a substitute. Fusion's bindings construct a fresh proxy for a
    property read, so ``document is other`` and ``id(document)`` answer about a
    temporary wrapper rather than about the document -- a stable answer and an
    unstable one are both accidents of when that wrapper was collected. This
    repository already identifies Fusion entities by ``entityToken`` for
    exactly that reason (``wglink_core._entity_token``), and the root
    component's token is that same handle for the document holding it. It is
    hashed because a raw token is long and belongs to Fusion, not to the wire.

    ``None`` means this add-in cannot name the active document. It is not an
    identity and is never treated as one: nothing is cached under it, nothing
    is refreshed for it, and the heartbeat answers "cannot tell" -- because two
    documents that cannot be named would otherwise share one cache entry, which
    is how one document's measurement reaches another's links.
    """

    app = _app()
    document = getattr(app, "activeDocument", None) if app else None
    if document is None:
        return None
    try:
        native_id = str(document.dataFile.id or "").strip()
        if native_id:
            return f"fusion:{native_id}"
    except Exception:  # noqa: BLE001 - unsaved local document
        pass
    try:
        token = str(app.activeProduct.rootComponent.entityToken or "").strip()
    except Exception:  # noqa: BLE001 - a product with no root component
        token = ""
    if not token:
        return None
    return "local:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _fresh_geometry_state() -> dict[str, object] | None:
    """Measure the live document now, ignoring the heartbeat cache.

    ``None`` means the live state could not be measured at all -- no
    application, no Design product, or an inventory this add-in cannot read.
    A dict whose ``document_signature_hash`` is empty means the measurement ran
    and declined to produce a token. Both are "cannot confirm" to a guard.

    This deliberately goes through :func:`_geometry_state` rather than calling
    ``return_state`` directly, so the token it produces is computed by exactly
    the code, with exactly the anchor, that produced the token WG is holding.
    A guard that measured under a different convention would compare two
    honest hashes of two different things and refuse every unchanged document.
    The measurement it takes also refreshes the cache, so the tick that
    services the operation does not leave a stale entry behind it.
    """

    app = _app()
    design = app.activeProduct if app else None
    if design is None or not isinstance(getattr(design, "objectType", ""), str):
        return None
    if "Design" not in str(design.objectType):
        return None
    try:
        records = wglink_core._resolved_link_records(design)
    except Exception:  # noqa: BLE001 - a document we cannot read has no evidence
        return None
    try:
        state, _verdict = _geometry_state(
            app, design, records, _active_document_id(), force=True
        )
    except Exception:  # noqa: BLE001 - an unmeasurable document confirms nothing
        return None
    return state


def _require_live_state(expected_hash: str, changed_message: str) -> None:
    """Refuse an explicit guarded operation unless the live model still matches.

    The heartbeat cache is advisory by construction: it exists so an idle
    document does not pay a geometry evaluation every four seconds, and it
    deliberately hands back a token that can be seconds old. Comparing WG's
    expected token against *that* token compares a stale value with itself, so
    a document edited after WG displayed its status passed the one guard whose
    entire purpose is to catch that edit -- and ``wglink_core.update`` then
    rebuilt sketches and parameters over it.

    An update or a return happens once, at the user's request, so it can pay
    for one measurement. Idle ticks still cannot, and still do not.
    """

    state = _fresh_geometry_state()
    current_state_hash = (
        str(state.get("document_signature_hash") or "") if state is not None else ""
    )
    if not current_state_hash:
        raise wglink_core.WgLinkError(
            "The Waveguide Generator add-in could not measure this Fusion document, so it cannot confirm "
            "the model still matches what WG displayed."
        )
    if current_state_hash != expected_hash:
        raise wglink_core.WgLinkError(changed_message)


def _installed_source() -> dict[str, object]:
    """Which source this add-in is running, when a dev sync left a marker.

    Validating a WGLink change otherwise means push, pin, package and install,
    and nothing on the way back says which build Fusion actually loaded.
    ``scripts/dev_sync_wglink.py`` writes this marker beside the add-in, so the
    heartbeat can state it and one ``cat`` of the status file proves a restart
    picked the edit up. Absent -- an ordinary managed install -- it is silent.
    """

    global _installed_source_cache
    if _installed_source_cache is not None:
        return _installed_source_cache
    marker = Path(__file__).resolve().parent / "wglink_dev.json"
    payload: dict[str, object] = {}
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            payload = {
                key: value[key]
                for key in ("sourceRoot", "sourceCommit", "syncedAt", "treeHash")
                if key in value
            }
    except (OSError, ValueError, TypeError):
        payload = {}
    _installed_source_cache = payload
    return payload


def _applying_operation() -> dict[str, str] | None:
    """The WG operation the active document is marked as applying, or None."""

    app = _app()
    design = app.activeProduct if app else None
    if design is None or "Design" not in str(getattr(design, "objectType", "")):
        return None
    try:
        return wglink_core.applying_operation(design)
    except Exception:  # noqa: BLE001 - an unreadable marker is no evidence
        return None


def _fusion_snapshot() -> dict[str, object]:
    """Read the active document and managed-link state once on the main thread."""

    started = time.perf_counter()
    timings: dict[str, float] = {}
    app = _app()
    product = app.activeProduct if app else None
    active_document = getattr(app, "activeDocument", None) if app else None
    links_inspected = True
    if active_document is None and product is None:
        document_name = None
        links: list[dict[str, object]] = []
    else:
        document_name = str(getattr(active_document, "name", "") or "Untitled")
        try:
            links = _document_links(timings)
        except LinksNotInspected:
            # Carried as its own state: nothing downstream may read the empty
            # list as "this document has no links" (see ``_link_evidence``).
            links, links_inspected = [], False
    timings["snapshot_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    diagnostics: dict[str, object] = {"lastTickMs": timings}
    if _coordinating():
        diagnostics["watchIntervalSeconds"] = WATCH_INTERVAL_SECONDS
    source = _installed_source()
    if source:
        diagnostics["source"] = source
    return {
        "document_name": document_name,
        "document_id": _active_document_id(),
        "links": links,
        "links_inspected": links_inspected,
        "applying_operation": _applying_operation() if document_name is not None else None,
        "diagnostics": diagnostics,
    }


def _publish_fusion_status(snapshot: dict[str, object] | None = None) -> bool:
    """Best-effort owner presence for WG; every Fusion read stays on this thread.

    Counted on entry, whoever calls it: with automatic coordination off it is
    reached only from start-up and from a command's follow-up, never a clock.
    """

    wglink_activity.record(wglink_activity.STATUS_PUBLICATION)
    if not _owns_active_ipc_lease():
        return False

    folder = wglink_workspace.ipc_folder(create=True)
    if folder is None:
        return False
    current = snapshot if snapshot is not None else _fusion_snapshot()
    if current.get("links_inspected") is False and current.get("document_name") is not None:
        # The document's links could not be read. Publishing its link list
        # empty would tell WG "this document has no links"; the last status
        # stands instead, and ages out on WG's side as it would with no add-in.
        return False
    diagnostics = dict(current.get("diagnostics") or {})
    # How this add-in is configured, from its own mouth, so a comparison run
    # records its configuration from the components rather than from notes.
    diagnostics["activation"] = dict(_activation)
    diagnostics["activity"] = dict(wglink_activity.LOG.as_payload())
    if _request_trace is not None:
        diagnostics["lastRequest"] = dict(_request_trace)
    if _recent_outcomes:
        diagnostics["recentOutcomes"] = [dict(item) for item in _recent_outcomes]
    try:
        payload = wglink_status.fusion_status_payload(
            session_id=_watch_session_id,
            document_name=current["document_name"],
            document_id=current["document_id"],
            adapter_version=wglink_send.ADAPTER_VERSION,
            workspace_root=wglink_workspace.workspace_root(),
            links=current["links"],
            diagnostics=diagnostics,
            applying_operation=current.get("applying_operation"),
        )
    except Exception:  # noqa: BLE001 - presence must never block CAD commands
        return False
    try:
        wglink_status.write_fusion_status_payload(folder, payload)
    except Exception:  # noqa: BLE001 - presence must never block CAD commands
        return False
    return True


def _pending_handoff() -> wglink_handoff.PendingHandoff | None:
    ipc = wglink_workspace.ipc_folder(create=True)
    bundles = wglink_workspace.bundle_folder()
    if ipc is None or bundles is None:
        return None
    for request_id in wglink_handoff.discard_superseded_handoffs(ipc, bundle_root=bundles):
        # Visible in the heartbeat: which request was dropped, and why.
        _note_outcome("handoff", request_id, "superseded")
    return wglink_handoff.next_pending_handoff(ipc, bundle_root=bundles)


def _pending_return_request() -> wglink_handoff.PendingReturnRequest | None:
    ipc = wglink_workspace.ipc_folder(create=True)
    if ipc is None:
        return None
    return wglink_handoff.next_return_request(ipc, session_id=_watch_session_id)


def _apply_pending_return_request(
    snapshot: dict[str, object] | None = None,
) -> str:
    """Export the current Fusion body/tags after an explicit WG request.

    Returns one of ``IDLE``, ``HANDLED`` or ``SUPPRESSED``. A request already
    attempted this session is ``SUPPRESSED``: the user has seen its refusal and
    must not see it again, but nothing ran, so suppression does not consume
    the file request tick.

    The request is claimed before it runs, and its claim is deleted afterwards
    whatever the outcome: it runs at most once. It names its exact target and
    the baseline WG displayed, and both are checked against the live document.
    """

    global _return_request_attempted_id
    request = _pending_return_request()
    if request is None:
        return IDLE
    if _return_request_attempted_id == request.request_id:
        return SUPPRESSED
    claimed = wglink_handoff.claim_request(request)
    if claimed is None:
        # Gone, or held open by WG on Windows: the next tick looks again.
        return IDLE
    request = claimed
    _return_request_attempted_id = request.request_id
    try:
        _execute_return_request(request, snapshot)
    finally:
        wglink_handoff.acknowledge_return_request(request)
    return HANDLED


def _execute_return_request(
    request: wglink_handoff.PendingReturnRequest,
    snapshot: dict[str, object] | None = None,
) -> str:
    """Run one claimed file request; its caller removes the claim afterwards."""

    global _command_busy
    trace = _begin_request("returnRequest", request.request_id, DELIVERY)
    outcome = "failed"
    _command_busy = True
    try:
        # WG reads the exported bundle directly; this request never writes its
        # Send/Solve inbox. Manifest features are checked below before export.
        _require_compatible_wg()
        if (
            not request.design_id
            or not request.document_id
            or not request.instance_id
            or not request.expected_return_state_hash
        ):
            raise wglink_core.WgLinkError(
                "WG's return request does not name an exact document, link and "
                "baseline. Refresh CAD Link and try again."
            )
        document_id = (
            snapshot["document_id"] if snapshot is not None else _active_document_id()
        )
        if request.document_id != document_id:
            raise wglink_core.WgLinkError(
                "The active Fusion document changed after WG requested the model. Reopen CAD Link and try again."
            )
        links = _evidence_or_refuse(snapshot)
        matching = [
            link for link in links
            if link.get("design_id") == request.design_id
            and link.get("instance_id") == request.instance_id
        ]
        if len(matching) != 1:
            raise wglink_core.WgLinkError(
                "The active Fusion document no longer contains the exact WG link requested by WG."
            )
        # Measured here, not read off ``matching[0]``: the snapshot's token
        # comes from the heartbeat cache and can equal the stale token WG is
        # holding while the model has already moved.
        _require_live_state(
            str(request.expected_return_state_hash),
            "The Fusion model changed after WG displayed its status. Refresh CAD Link and try again.",
        )
        output = wglink_workspace.return_folder()
        if output is None:
            raise wglink_core.WgLinkError(
                "Waveguide Generator has no selected WGLink folder."
            )
        options: dict[str, object] = {
            "selection": "root",
            "output_folder": str(output),
            "overwrite": True,
            "request_id": request.request_id,
            "anchor_instance_id": request.instance_id,
            "capture_document": wglink_workspace.capture_document(),
            "source_identity": _source_identity_enabled(),
            **_required_manifest_features(),
        }
        # This path holds ``_command_busy`` while adoption may prompt the user.
        wglink_send.send(_app(), options, confirm_adoption=_confirm_source_adoption,
                         confirm_clear_fem=_confirm_clear_fem)
        outcome = "applied"
    except wglink_core.WgLinkError as exc:
        outcome = "refused"
        _message(
            _refusal_text(str(exc), _RETURN_RETRY_HINT),
            f"{PANEL_NAME} return to WG refused",
        )
    except Exception as exc:  # noqa: BLE001 - main-thread add-in boundary
        _report_error("Returning this model to WG", f"{PANEL_NAME} return to WG error", exc)
    finally:
        trace["outcome"] = outcome
        _release_command_busy()
    return outcome


def _evidence_or_refuse(snapshot: dict[str, object] | None) -> list[dict[str, object]]:
    """Link evidence for a decision: the snapshot's, or a live read; never a guess."""

    if snapshot is None:
        return _document_links()
    links = _link_evidence(snapshot)
    if links is None:
        raise LinksNotInspected(_NOT_INSPECTED_TEXT)
    return links


def _reconciled(
    operation_id: str, export_id: str, links: list[dict[str, object]]
) -> bool:
    """Whether a link already carries this operation's evidence (a read)."""

    return any(
        link.get("export_id") == export_id and link.get("operation_id") == operation_id
        for link in links
    )


def _linked_to(handoff: object, links: list[dict[str, object]]) -> list[dict[str, object]]:
    """The links in ``links`` that already hold this handoff's design."""

    try:
        pending_path = Path(handoff.bundle_path).resolve()
    except OSError:
        pending_path = None
    return [
        link
        for link in links
        if (handoff.design_id and link.get("design_id") == handoff.design_id)
        or (
            not handoff.design_id
            and pending_path is not None
            and link.get("bundle_path")
            and Path(str(link.get("bundle_path"))).expanduser().resolve() == pending_path
        )
    ]


_ALREADY_LINKED_TEXT = (
    "This WG design is already linked in the active Fusion document. Choose that "
    "link in WG's CAD Link panel and send the update from there, so WG names the "
    "exact instance to change."
)


def _require_insert_target(
    handoff: object, new_document_id: str | None = None
) -> None:
    """Re-read the live document immediately before an insert's first write."""

    active_document_id = _active_document_id()
    # A new-document destination was validated while no document existed. At
    # this boundary validate its request identity and TTL again, then compare
    # the document created for it below; do not reinterpret that new document
    # as an unrelated active-document destination.
    issue_document_id = None if new_document_id is not None else active_document_id
    issue = _insert_handoff_issue(handoff, issue_document_id)
    if issue is not None:
        outcome, reason = issue
        if outcome == "expired":
            raise _ExpiredInsertError(reason)
        raise wglink_core.WgLinkError(reason)
    if handoff.expected_document_id and handoff.expected_document_id != active_document_id:
        raise wglink_core.WgLinkError(
            "The active Fusion document changed after WG prepared this insert. "
            "Refresh CAD Link and try again."
        )
    destination = getattr(handoff, "destination", None)
    if destination is not None:
        expected = (
            destination.get("value")
            if destination.get("kind") == "document"
            else new_document_id
        )
        if not expected or expected != active_document_id:
            raise wglink_core.WgLinkError(
                "The active Fusion document changed from this insert's destination. "
                "Refresh CAD Link and try again."
            )
    if _linked_to(handoff, _document_links()):
        raise wglink_core.WgLinkError(_ALREADY_LINKED_TEXT)


def _interrupted(operation_id: str, applying: dict[str, str] | None) -> bool:
    """Whether this operation began mutating and left no evidence."""

    return applying is not None and applying.get("operation_id") == operation_id


_RECOVERY_REQUIRED_TEXT = (
    "Update interrupted — recovery required. The Waveguide Generator add-in started this change in "
    "Fusion and it did not finish, so it will not run it again. Undo the partial "
    "change, or repair the link, then send the model from WG again."
)


class _ExpiredInsertError(wglink_core.WgLinkError):
    """An insert crossed its TTL after it was claimed but before mutation."""


def _design_ready() -> bool:
    app = _app()
    design = app.activeProduct if app else None
    return bool(design and "Design" in str(getattr(design, "objectType", "")))


def _ensure_design_ready() -> bool:
    """Create a Fusion Design document for an explicit WG handoff if needed."""

    if _design_ready():
        return True
    app = _app()
    if app is None:
        return False
    try:
        app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    except Exception as exc:  # noqa: BLE001 - surfaced as an actionable refusal
        raise wglink_core.WgLinkError(
            f"Could not create a Fusion Design document for this waveguide: {exc}"
        ) from exc
    return _design_ready()


def _insert_handoff_issue(
    handoff: object, document_id: object
) -> tuple[str, str] | None:
    """Return an insert's terminal outcome and reason before it can mutate."""

    if getattr(handoff, "expected_instance_id", ""):
        return None
    requested_at = str(getattr(handoff, "requested_at", "") or "")
    if requested_at:
        try:
            requested = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
            age = (_utc_now() - requested.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            age = 0.0
        if age > INSERT_HANDOFF_TTL_SECONDS:
            return (
                "expired",
                "This insert request expired after 30 minutes and was not applied. "
                "Send the model from WG again.",
            )
    destination = getattr(handoff, "destination", None)
    if destination is None:
        return None
    kind = destination.get("kind")
    value = destination.get("value")
    if kind == "document" and value == document_id:
        return None
    if (
        kind == "new_document"
        and value == getattr(handoff, "request_id", None)
        and document_id is None
    ):
        return None
    return (
        "refused",
        "This insert names a destination other than the active Fusion document. "
        "Refresh CAD Link and send the model from WG again.",
    )


def _apply_pending_handoff(snapshot: dict[str, object] | None = None) -> str:
    """Insert or update the bundle named by WG's explicit CAD action.

    Returns one of ``IDLE``, ``HANDLED`` or ``SUPPRESSED``. A request already
    attempted this session is ``SUPPRESSED``, and that must not read as "this
    tick is spoken for" -- a return request behind it would
    then never be looked at again.

    The handoff is claimed before it runs and its claim deleted afterwards,
    whatever the outcome: it runs at most once, under its request id. WG's
    contract orders what happens in between (CAD-OPERATIONS.md, "Fusion-bound
    mutations"): the exact target first; then reconciliation, which only
    reads; then, for this operation only, an interrupted mutation, which is
    never repeated; and only then the baseline, re-checked inside Update
    immediately before its first write.
    """

    global _command_busy, _handoff_attempted_id
    handoff = _pending_handoff()
    if handoff is None:
        return IDLE

    attempt_key = handoff.request_id
    if _handoff_attempted_id == attempt_key:
        return SUPPRESSED
    correlation_id = handoff.operation_id
    claimed = wglink_handoff.claim_request(handoff)
    if claimed is None:
        return IDLE
    handoff = claimed
    # Destination is a live guard, not heartbeat-cached identity. The snapshot
    # may already be stale if the user switched documents during this tick.
    issue = _insert_handoff_issue(handoff, _active_document_id())
    if issue is not None:
        wglink_handoff.acknowledge_handoff(handoff)
        _handoff_attempted_id = attempt_key
        outcome, reason = issue
        _begin_request("handoff", correlation_id, DELIVERY)["outcome"] = outcome
        _message(reason, f"{PANEL_NAME} automatic insert refused")
        return HANDLED
    try:
        _require_compatible_wg()
        # Read before _ensure_design_ready can create a destination document.
        # Core checks again at insert/update, immediately before using it.
        wglink_core._handoff_bundle(handoff.bundle_path, {
            "expected_bundle_id": handoff.bundle_id,
            "expected_export_id": handoff.export_id,
        })
        ready = _ensure_design_ready()
    except wglink_core.WgLinkError as exc:
        wglink_handoff.acknowledge_handoff(handoff)
        _handoff_attempted_id = attempt_key
        _begin_request("handoff", correlation_id, DELIVERY)["outcome"] = "refused"
        _message(
            _refusal_text(str(exc), _HANDOFF_RETRY_HINT),
            f"{PANEL_NAME} automatic open refused",
        )
        return HANDLED
    if not ready:
        wglink_handoff.release_claim(handoff)
        return IDLE
    new_document_id = None
    destination = getattr(handoff, "destination", None)
    if (
        not getattr(handoff, "expected_instance_id", "")
        and destination is not None
        and destination.get("kind") == "new_document"
    ):
        new_document_id = _active_document_id()
    _handoff_attempted_id = attempt_key
    try:
        _execute_handoff(handoff, snapshot, new_document_id=new_document_id)
    finally:
        wglink_handoff.acknowledge_handoff(handoff)
    return HANDLED


def _execute_handoff(
    handoff: wglink_handoff.PendingHandoff,
    snapshot: dict[str, object] | None = None,
    *,
    new_document_id: str | None = None,
) -> str:
    """Run one claimed file request; its caller removes the claim afterwards."""

    global _command_busy
    document_id = (
        snapshot["document_id"] if snapshot is not None else _active_document_id()
    )
    applying = (
        snapshot.get("applying_operation")
        if snapshot is not None and "applying_operation" in snapshot
        else _applying_operation()
    )
    trace = _begin_request("handoff", handoff.operation_id, DELIVERY)
    outcome = "failed"
    operation = "update" if handoff.expected_instance_id else "insert"
    _command_busy = True
    try:
        _require_compatible_wg()
        # Reconciliation below decides from these whether this operation
        # already applied; with no reading of them there is no deciding.
        links = _evidence_or_refuse(snapshot)
        if handoff.expected_document_id and handoff.expected_document_id != document_id:
            raise wglink_core.WgLinkError(
                "The active Fusion document changed after WG prepared this "
                f"{operation}. Refresh CAD Link and try again."
            )
        if operation == "update":
            # The exact target first: the document (above), the instance, and
            # the model state WG measured.
            if not handoff.expected_document_id or not handoff.expected_return_state_hash:
                raise wglink_core.WgLinkError(
                    "WG sent an update without the exact Fusion document and the "
                    "model state it expects, so the Waveguide Generator add-in will not change the model."
                )
            selected = [
                link
                for link in links
                if link.get("instance_id") == handoff.expected_instance_id
                and (not handoff.design_id or link.get("design_id") == handoff.design_id)
            ]
            if len(selected) != 1:
                raise wglink_core.WgLinkError(
                    "The active Fusion document no longer contains exactly one WG "
                    "link with the instance selected in WG. Refresh CAD Link and "
                    "try again."
                )
            link = selected[0]
            if _reconciled(handoff.operation_id, handoff.export_id, [link]):
                # This operation's own evidence is on the exact link: it
                # applied, and its write is why the document moved. A read.
                outcome = "reconciled"
            elif _interrupted(handoff.operation_id, applying):
                outcome = "recoveryRequired"
                raise wglink_core.WgLinkError(_RECOVERY_REQUIRED_TEXT)
            elif link.get("export_id") == handoff.export_id:
                # The export got there another way -- a manual Update, or
                # another operation. There is nothing left to do.
                outcome = "alreadyCurrent"
            else:
                expected = str(handoff.expected_return_state_hash)
                wglink_core.update(
                    _app(),
                    handoff.bundle_path,
                    {
                        "instance_id": link["instance_id"],
                        # Stamped beside the export identity as Update's last
                        # write: the evidence a redelivery is reconciled against.
                        "operation_id": handoff.operation_id,
                        "expected_bundle_id": handoff.bundle_id,
                        "expected_export_id": handoff.export_id,
                        # Measured live, not read off ``link``: the snapshot's
                        # token comes from the heartbeat cache and can equal the
                        # stale token WG holds while the model has moved. The
                        # precondition runs after Update's last read and before
                        # its first write.
                        "precondition": lambda: _require_live_state(
                            expected,
                            "The Fusion model changed after WG prepared this update. Refresh CAD Link and choose a sync direction again.",
                        ),
                    },
                )
                outcome = "applied"
        elif _reconciled(handoff.operation_id, handoff.export_id, links):
            # A redelivered insert: its own evidence is on the link it made.
            outcome = "reconciled"
        elif _interrupted(handoff.operation_id, applying):
            outcome = "recoveryRequired"
            raise wglink_core.WgLinkError(_RECOVERY_REQUIRED_TEXT)
        elif _linked_to(handoff, links):
            raise wglink_core.WgLinkError(_ALREADY_LINKED_TEXT)
        else:
            wglink_core.insert(
                _app(),
                handoff.bundle_path,
                {
                    "allow_root_fallback": True,
                    "operation_id": handoff.operation_id,
                    "expected_bundle_id": handoff.bundle_id,
                    "expected_export_id": handoff.export_id,
                    # The document and "no link of this design yet", read live
                    # again immediately before the insert's first write.
                    "precondition": lambda: _require_insert_target(handoff, new_document_id),
                },
            )
            outcome = "applied"

        # An automatic send already has two visible success signals: the model
        # appears and the browser reports the completed bundle. A modal report
        # here blocked Fusion on every send and made the ordinary Part Design
        # root-fallback warning look like an error. Keep detailed reports for
        # the manual Insert command; automatic success stays silent. Genuine
        # refusals and unexpected failures below still demand attention.
    except _ExpiredInsertError as exc:
        outcome = "expired"
        _message(
            _refusal_text(str(exc), _HANDOFF_RETRY_HINT),
            f"{PANEL_NAME} automatic insert refused",
        )
    except wglink_core.WgLinkError as exc:
        if outcome != "recoveryRequired":
            outcome = (
                "recoveryRequired"
                if _interrupted(handoff.operation_id, _applying_operation())
                else "refused"
            )
        _message(
            _refusal_text(str(exc), _HANDOFF_RETRY_HINT),
            f"{PANEL_NAME} automatic {operation} refused",
        )
    except Exception as exc:  # noqa: BLE001 - main-thread add-in boundary
        if _interrupted(handoff.operation_id, _applying_operation()):
            outcome = "recoveryRequired"
        _report_error(
            f"The automatic {operation}", f"{PANEL_NAME} automatic {operation} error", exc
        )
    finally:
        trace["outcome"] = outcome
        _release_command_busy()
    return outcome


def _sweep_leftover_claims(snapshot: dict[str, object]) -> None:
    """Settle the claims an interrupted session left behind, once per session.

    A claim is hidden from every listing, so without this nothing ever looks at
    one again. None is ever run: a return request named the session that
    claimed it, and a handoff is reconciled against the document, read-only.
    Its evidence on a link means it applied; the applying marker without
    evidence means it was interrupted, which needs recovery and is reported;
    neither means it never started, and WG's user sends it again.

    **A claim whose document is not the active one is kept, not discarded.**
    Reconciliation is a read of that exact document, so with another one open
    none of the three answers above can be established. Reporting "never
    started" there would cancel an operation that may have applied, or may
    need recovery, and it would collapse *target document unavailable* into
    *operation never started*. Such a claim stays on disk and is settled when
    its document becomes active, in this session or a later one.
    """

    global _claims_swept
    if snapshot.get("document_id") is None:
        # Fusion is still opening: a claim for a document is settled against
        # that document, so wait until one is active.
        return
    if _link_evidence(snapshot) is None:
        # The document is open but its links were not read. Settling now would
        # judge every claim against no evidence -- an applied operation would
        # read as never started -- so nothing is settled and nothing is spent.
        return
    _claims_swept = True
    ipc = wglink_workspace.ipc_folder(create=True)
    if ipc is None:
        return
    _settle_file_claims(wglink_handoff.leftover_claims(ipc), snapshot)


def _settle_file_claims(
    claims: list, snapshot: dict[str, object], *, announce: bool = True
) -> None:
    """Settle what this document can answer for; keep the rest for later."""

    deferred: list[object] = []
    evidence = _link_evidence(snapshot)
    for claim in claims:
        reconcilable = claim.channel == "handoff" and bool(claim.request_id)
        if reconcilable and (
            evidence is None
            or (
                claim.expected_document_id
                and claim.expected_document_id != snapshot.get("document_id")
            )
        ):
            # Kept, never discarded: with no reading of this document's links
            # there is no evidence either way.
            deferred.append(claim)
            continue
        outcome = "discarded"
        if reconcilable:
            if _reconciled(claim.request_id, claim.export_id, evidence or []):
                outcome = "reconciled"
            elif _interrupted(claim.request_id, snapshot.get("applying_operation")):
                outcome = "recoveryRequired"
                _modal(
                    _refusal_text(_RECOVERY_REQUIRED_TEXT, _HANDOFF_RETRY_HINT),
                    f"{PANEL_NAME} update interrupted",
                )
        if wglink_handoff.remove_leftover_claim(claim):
            _note_outcome(claim.channel, claim.request_id or claim.path.name, outcome)
    _deferred_file_claims[:] = deferred
    if deferred and announce:
        _log(
            f"The Waveguide Generator add-in is holding {len(deferred)} interrupted WG update(s) whose Fusion "
            "document is not open. Open that document to let the Waveguide Generator add-in settle them; it "
            "never re-runs one."
        )


def _notice_outdated_wg() -> None:
    """Say once per session that WG is too old to exchange requests with."""

    global _wg_outdated_noticed
    if _wg_outdated_noticed:
        return
    ipc = wglink_workspace.ipc_folder(create=True)
    if ipc is None:
        return
    found = wglink_handoff.outdated_wg_requests(ipc)
    if not found:
        return
    _wg_outdated_noticed = True
    _note_outcome("wgRequest", found[0], "wgOutdated")
    _modal(wglink_inbox.WG_OUTDATED_MESSAGE, f"{PANEL_NAME} cannot read this request")


def _legacy_claim_outcome(entry: dict, snapshot: dict[str, object]) -> str | None:
    """Read model or journal evidence; uncertainty never proves success."""

    operation_id = entry["operationId"]
    request = entry["request"]
    mutating = entry["kind"] != "request_return"
    export_id = str(request.get("exportId") or "")
    destination = request.get("destination")
    if not isinstance(destination, dict):
        destination = {}
    expected_document = request.get("expectedDocumentId") or (
        destination.get("value") if destination.get("kind") == "document" else None
    )
    document_matches = not expected_document or expected_document == snapshot.get("document_id")
    links = _link_evidence(snapshot) if document_matches else None
    if mutating and export_id and links is not None and _reconciled(operation_id, export_id, links):
        return "reconciled"
    record = entry.get("outcome") if entry["state"] == "outcome" else None
    if isinstance(record, dict):
        outcome = record.get("outcome")
        if outcome in ("applied", "reconciled"):
            if not mutating:
                return "applied" if outcome == "applied" else "failed"
            evidence = record.get("evidence")
            if (export_id and isinstance(evidence, dict)
                    and evidence.get("operationId") == operation_id
                    and evidence.get("exportId") == export_id):
                # WG checks a reconciled report against its target document.
                # Keep a known completion until that document is open.
                return "reconciled" if document_matches else None
        elif outcome in wglink_legacy_queue.CLAIM_OUTCOMES:
            if outcome == "recoveryRequired" and not mutating:
                return "failed"
            # A journaled failure during a mutation may have left partial work.
            return "recoveryRequired" if mutating and outcome == "failed" else str(outcome)
    if mutating and document_matches and _interrupted(operation_id, snapshot.get("applying_operation")):
        return "recoveryRequired"
    if entry["state"] == "claiming":
        # This state was never handed to Fusion's main thread.
        return "discarded"
    return "recoveryRequired" if mutating else "failed"


def _settle_legacy_claims(snapshot: dict[str, object]) -> None:
    """Publish retired HTTP claims through the file outcome channel, read-only.

    Each bounded batch fits WG's recent-outcome window. A claim is removed
    only after the atomic, fsynced status publication succeeds. A held claim
    whose exact outcome is already in that file is only removed, not reported
    again. Unknown mutations need recovery; an interrupted return is failed.
    """

    global _legacy_claims_pending
    ipc = wglink_workspace.ipc_folder()
    if ipc is None:
        return
    journal = wglink_legacy_queue.LegacyClaims(ipc)
    entries = journal.scan(_RECENT_OUTCOMES_LIMIT)
    _legacy_claims_pending = bool(entries)
    if not entries:
        return
    try:
        path = Path(ipc) / wglink_status.FUSION_STATUS_FILENAME
        if path.is_symlink() or path.stat().st_size > 256 * 1024:
            raise ValueError("Unsafe status file")
        previous = json.loads(path.read_text(encoding="utf-8"))
        published = previous.get("diagnostics", {}).get("recentOutcomes", [])
        if previous.get("schemaVersion") != 1 or previous.get("cadApplication") != "fusion360":
            published = []
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        published = []
    if not isinstance(published, list):
        published = []
    # Preserve the already published outcomes if a fresh registration is
    # cleaning up a held journal. Deduplicate by correlation, newest last.
    for item in published[-_RECENT_OUTCOMES_LIMIT:]:
        if (isinstance(item, dict) and item.get("channel") in ("handoff", "returnRequest")
                and isinstance(item.get("requestId"), str)
                and item.get("outcome") in wglink_legacy_queue.CLAIM_OUTCOMES
                and not any(existing["channel"] == item["channel"]
                            and existing["requestId"] == item["requestId"] for existing in _recent_outcomes)):
            _recent_outcomes.append(dict(item))
    ready = []
    refusal = None
    for entry in entries:
        channel = "returnRequest" if entry["kind"] == "request_return" else "handoff"
        earlier = next((item for item in reversed(published)
                        if isinstance(item, dict) and item.get("channel") == channel
                        and item.get("requestId") == entry["operationId"]
                        and item.get("outcome") in wglink_legacy_queue.CLAIM_OUTCOMES), None)
        # Once published, a held journal is cleanup work. A later undo or
        # document switch must not change its already reported completion.
        outcome = earlier["outcome"] if earlier is not None else _legacy_claim_outcome(entry, snapshot)
        if outcome == "reconciled":
            request = entry["request"]
            destination = request.get("destination")
            if not isinstance(destination, dict):
                destination = {}
            expected = request.get("expectedDocumentId") or (
                destination.get("value") if destination.get("kind") == "document" else None
            )
            if expected and expected != snapshot.get("document_id"):
                # A fresh report in another document would no longer be
                # acceptable evidence to WG. Keep the journal until it is.
                continue
        if outcome is None:
            continue
        # WG reads mutation refusals from the single lastRequest slot. Keep
        # further refusals journaled for a later pass rather than overwriting
        # their only readable report in this same batch.
        if outcome == "refused" and channel == "handoff":
            if refusal is not None:
                continue
            refusal = entry["operationId"]
        item = {"channel": channel, "requestId": entry["operationId"], "outcome": outcome}
        _recent_outcomes[:] = [existing for existing in _recent_outcomes
                              if (existing["channel"], existing["requestId"])
                              != (channel, entry["operationId"])]
        if item not in published:
            _note_outcome(channel, entry["operationId"], outcome)
        else:
            _recent_outcomes.append(item)
            del _recent_outcomes[:-_RECENT_OUTCOMES_LIMIT]
        _begin_request(channel, entry["operationId"], DELIVERY)["outcome"] = outcome
        ready.append(entry)
    if ready:
        if refusal is not None:
            _begin_request("handoff", refusal, DELIVERY)["outcome"] = "refused"
        report = snapshot
        if snapshot.get("links_inspected") is False:
            # No reading means no document observation. An outcome can still
            # report recovery without advertising an empty link list.
            report = {**snapshot, "document_name": None, "document_id": None, "links": []}
        if not _publish_fusion_status(report):
            return
    for entry in ready:
        journal.delete(entry["operationId"])
    _legacy_claims_pending = bool(journal.scan(1))


def _settle_claims(snapshot: dict[str, object]) -> None:
    """Settle interrupted claims read-only: once per session, then as documents open.

    Bounded and never a replay, and counted as claim settlement whichever
    carrier -- start-up, the tick, a command's follow-up -- reaches it.
    """

    with wglink_activity.because(wglink_activity.CAUSE_CLAIM_SETTLEMENT):
        if _legacy_claims_pending:
            _settle_legacy_claims(snapshot)
        if not _claims_swept:
            _sweep_leftover_claims(snapshot)
        elif _deferred_file_claims and snapshot.get("document_id") is not None:
            # An interrupted claim whose document was not open is settled as
            # soon as it is -- read-only, and never re-run.
            _settle_file_claims(list(_deferred_file_claims), snapshot, announce=False)


def _claims_outstanding() -> bool:
    return not _claims_swept or bool(_deferred_file_claims) or _legacy_claims_pending


def _on_watch_tick() -> None:
    """Service explicit file requests and publish status on the main thread."""

    global _command_busy
    if _command_busy:
        return
    # The one place a requested inspection may run: Fusion's API needs the main
    # thread, and this event is the only main thread WGLink has. The tick is
    # the carrier, not the cause -- with nothing pending it returns at once,
    # which is why periodic activity alone inspects no geometry.
    _service_geometry_refresh()
    snapshot = _fusion_snapshot()
    try:
        _settle_claims(snapshot)
        _notice_outdated_wg()
        # Only a channel that did work claims the tick. A suppressed refusal
        # must not starve the other file channel.
        if _apply_pending_handoff(snapshot) == HANDLED:
            return
        if _apply_pending_return_request(snapshot) == HANDLED:
            return
    finally:
        _publish_fusion_status(snapshot)


class WatchEventHandler(adsk.core.CustomEventHandler):
    def notify(self, _args: object) -> None:
        try:
            # A queued event from an expired owner must not service a request
            # after a candidate has promoted and replaced its panel.
            if not _owns_active_ipc_lease():
                return
            with wglink_activity.because(wglink_activity.CAUSE_TICK):
                _on_watch_tick()
        except Exception as exc:  # noqa: BLE001
            _report_error("Servicing WG file requests", f"{PANEL_NAME} file request error", exc)


class CandidateEventHandler(adsk.core.CustomEventHandler):
    """Try to promote an adopted registration after lease loss."""

    def notify(self, _args: object) -> None:
        try:
            # The candidate thread's timer raised this: a recovery path that
            # started on its own. Counted apart from start-up, and between
            # commands, so Gate AR sees it (it cannot run with the gate off).
            with wglink_activity.because(wglink_activity.CAUSE_PROMOTION):
                _attempt_promotion()
        except Exception as exc:  # noqa: BLE001
            _report_error("Promoting the Waveguide Generator add-in's standby registration", f"{PANEL_NAME} recovery error", exc)


class FollowupEventHandler(adsk.core.CustomEventHandler):
    """Main-thread time a command asked for; registered only with coordination off.

    Declares no cause of its own. Each job carries the cause of the command
    that scheduled it, so anything this handler did outside a job would count
    as ``unattributed`` -- which is the point.
    """

    def notify(self, _args: object) -> None:
        try:
            if not _owns_active_ipc_lease():
                return
            _run_followups()
        except Exception as exc:  # noqa: BLE001
            _report_error("Finishing a command in the Waveguide Generator add-in", f"{PANEL_NAME} error", exc)


def _schedule_followup(job: object) -> None:
    """Run ``job`` on the main thread after the current handler returns.

    Bound here to the cause declared here -- the command running now -- so the
    event hop does not turn the command's own follow-up into anonymous work.
    """

    with _followups_lock:
        _followups.append(wglink_activity.carrying(job))
    _raise_followup_event()


def _raise_followup_event() -> None:
    if _followup_event is None:
        return
    app = _app()
    if app is not None:
        app.fireCustomEvent(FOLLOWUP_EVENT_ID)


def _run_followups() -> None:
    """Run what commands scheduled, unless a WGLink command holds the thread.

    Fusion can deliver this event inside a running command (``adsk.doEvents``
    in a progress update, a modal). The jobs then stay queued, and that
    command's completion re-drives them (:func:`_command_finished`) -- there
    is no tick to retry them any more.
    """

    with _followups_lock:
        jobs = list(_followups)
        _followups.clear()
    for index, job in enumerate(jobs):
        if _command_busy:
            # A command holds the thread -- from the start, or because a job
            # raised a modal that is still up. Keep the rest for its end.
            with _followups_lock:
                _followups[:0] = jobs[index:]
            return
        try:
            job()
        except Exception as exc:  # noqa: BLE001 - one follow-up must not stop the rest
            _log(f"[{PANEL_NAME}] a command's follow-up failed: {exc}\n{traceback.format_exc()}")


def _command_finished() -> None:
    """A WGLink command or modal released the main thread: re-drive what waited.

    ``_command_busy`` defers work rather than dropping it. The watch tick used
    to be the retry; this is the retry that does not need one. Queued follow-ups
    are re-raised when the command releases the thread.
    """

    if _followups:
        _raise_followup_event()


def _after_command() -> None:
    """What a finished command owes WG and itself when no tick runs.

    Runs as a follow-up under that command's cause: pay for the geometry
    refresh the command requested, publish status once, and -- only while an
    interrupted claim is still unsettled -- settle it against the document the
    user is now in, read-only.
    """

    # No tick will come back for a request the duty cycle defers, so this
    # command pays for its own refresh now (review M1).
    _service_geometry_refresh(throttle=False)
    snapshot = _fusion_snapshot()
    if _claims_outstanding():
        _settle_claims(snapshot)
    _publish_fusion_status(snapshot)


def _watch_loop(app: object, stop: threading.Event) -> None:
    """Renew the lease and raise the owner's event without reading Fusion."""

    while not stop.wait(WATCH_INTERVAL_SECONDS):
        if not _renew_ipc_lease():
            return
        try:
            app.fireCustomEvent(WATCH_EVENT_ID)
        except Exception:  # noqa: BLE001 - a torn-down event ends the thread
            return


def _candidate_loop(app: object, stop: threading.Event) -> None:
    """Raise only this registration's private promotion event."""

    while not stop.wait(WATCH_INTERVAL_SECONDS):
        try:
            app.fireCustomEvent(_candidate_event_id)
        except Exception:  # noqa: BLE001 - a torn-down event ends the thread
            return


def _start_candidate(app: object) -> None:
    """Wait without publishing an unserviceable Fusion session id."""

    global _candidate_event, _candidate_handler, _candidate_stop, _candidate_thread
    if _candidate_event is not None:
        return
    _candidate_event = app.registerCustomEvent(_candidate_event_id)
    if _candidate_event is None:
        return
    _candidate_handler = CandidateEventHandler()
    _candidate_event.add(_candidate_handler)
    _candidate_stop = threading.Event()
    _candidate_thread = threading.Thread(
        target=_candidate_loop,
        args=(app, _candidate_stop),
        name="WGLinkOwnerCandidate",
        daemon=True,
    )
    _candidate_thread.start()


def _stop_candidate(app: object) -> None:
    global _candidate_event, _candidate_handler, _candidate_stop, _candidate_thread
    if _candidate_stop is not None:
        _candidate_stop.set()
    if _candidate_thread is not None and _candidate_thread.is_alive():
        _candidate_thread.join(timeout=WATCH_INTERVAL_SECONDS + 1)
    if _candidate_event is not None and _candidate_handler is not None:
        try:
            _candidate_event.remove(_candidate_handler)
        except Exception:  # noqa: BLE001
            pass
    try:
        if app:
            app.unregisterCustomEvent(_candidate_event_id)
    except Exception:  # noqa: BLE001
        pass
    _candidate_event = _candidate_handler = _candidate_stop = _candidate_thread = None


def _start_watch(app: object) -> bool:
    global _watch_event, _watch_handler, _watch_stop, _watch_thread
    global _handoff_attempted_id, _return_request_attempted_id, _request_trace
    global _claims_swept, _wg_outdated_noticed, _legacy_claims_pending
    _handoff_attempted_id = None
    _return_request_attempted_id = None
    _request_trace = None
    _claims_swept = False
    _legacy_claims_pending = True
    _wg_outdated_noticed = False
    _recent_outcomes.clear()
    _deferred_file_claims.clear()
    _followups.clear()
    # The transfer path's carrier, in both gate states (C8).
    if not _start_followups(app):
        return False
    if not _coordinating():
        return True
    _watch_event = app.registerCustomEvent(WATCH_EVENT_ID)
    if _watch_event is None:
        return False
    _watch_handler = WatchEventHandler()
    _watch_event.add(_watch_handler)
    _watch_stop = threading.Event()
    _watch_thread = threading.Thread(
        target=_watch_loop,
        args=(app, _watch_stop),
        name="WGLinkFileRequests",
        daemon=True,
    )
    _watch_thread.start()
    return True


def _start_followups(app: object) -> bool:
    """The one event a command raises for itself, registered in both gate states.

    With coordination off it is the whole of what starts: no thread, no timer,
    and no watch event. It is raised only by a command's own completion
    (:func:`_schedule_followup`) or by the one-shot pickup timer a Send or
    Solve starts (:func:`_schedule_pickup_check`), or that start-up starts
    for a request it found still waiting (:func:`_recheck_requests_at_startup`).
    """

    global _followup_event, _followup_handler, _backend_admission
    _backend_admission = wglink_launcher.BackendAdmission()
    _followup_event = app.registerCustomEvent(FOLLOWUP_EVENT_ID)
    if _followup_event is None:
        return False
    _followup_handler = FollowupEventHandler()
    _followup_event.add(_followup_handler)
    return True


def _stop_followups(app: object) -> None:
    global _followup_event, _followup_handler, _backend_progress
    _backend_admission.close()
    _hide_export_progress(_backend_progress)
    _backend_progress = None
    if _followup_event is not None and _followup_handler is not None:
        try:
            _followup_event.remove(_followup_handler)
        except Exception:  # noqa: BLE001
            pass
    if _followup_event is not None:
        try:
            if app:
                app.unregisterCustomEvent(FOLLOWUP_EVENT_ID)
        except Exception:  # noqa: BLE001
            pass
    for timer in list(_timers):
        timer.cancel()
    _timers.clear()
    _followup_event = _followup_handler = None
    with _followups_lock:
        _followups.clear()


def _stop_watch(app: object) -> None:
    global _watch_event, _watch_handler, _watch_stop, _watch_thread
    global _handoff_attempted_id, _return_request_attempted_id, _request_trace
    global _claims_swept, _wg_outdated_noticed
    _stop_followups(app)
    _deferred_file_claims.clear()
    if _watch_stop is not None:
        _watch_stop.set()
    if _watch_thread is not None and _watch_thread.is_alive():
        _watch_thread.join(timeout=WATCH_INTERVAL_SECONDS + 1)
    if _watch_event is not None and _watch_handler is not None:
        try:
            _watch_event.remove(_watch_handler)
        except Exception:  # noqa: BLE001
            pass
    try:
        if app:
            app.unregisterCustomEvent(WATCH_EVENT_ID)
    except Exception:  # noqa: BLE001
        pass
    folder = wglink_workspace.ipc_folder()
    if folder is not None:
        try:
            wglink_status.remove_fusion_status(
                folder, session_id=_watch_session_id
            )
        except Exception:  # noqa: BLE001 - shutdown cleanup is best effort
            pass
    _watch_event = _watch_handler = _watch_stop = _watch_thread = None
    _handoff_attempted_id = None
    _return_request_attempted_id = None
    _request_trace = None
    _claims_swept = False
    _wg_outdated_noticed = False
    _recent_outcomes.clear()


def _icon_folder(operation: str) -> str:
    """Fusion's per-command resource folder, or '' to fall back to no icon.

    Passing a path that holds no PNGs makes Fusion draw an empty button, so an
    incomplete checkout degrades to the unadorned text button it had before.
    """

    folder = ADDIN_DIR / "resources" / operation
    return str(folder) if (folder / "16x16.png").is_file() else ""


MANAGE_DROPDOWN_ID = "hornlab_wglink_manage"
MANAGE_DROPDOWN_NAME = wglink_send.MANAGE_DROPDOWN_NAME


def _manage_dropdown(panel: object) -> object | None:
    """The container for the maintenance and recovery commands.

    An older Fusion without ``addDropDown`` degrades to the flat panel every
    command used to live on, which is worse looking but never broken.
    """

    try:
        return panel.controls.addDropDown(
            MANAGE_DROPDOWN_NAME, _icon_folder("manage"), MANAGE_DROPDOWN_ID
        )
    except Exception:  # noqa: BLE001 - fall back to the flat panel
        return None


def _clear_registration_state() -> None:
    """Forget local handles without mutating UI another lease owner may own."""

    global _panel, _owned
    _controls.clear()
    _definitions.clear()
    _handlers.clear()
    _panel = None
    _owned = False


def _delete_owned_ui(ui: object | None) -> None:
    """Delete the panel transaction while this registration holds the lease."""

    global _panel, _owned
    for control in reversed(_controls):
        _delete_quietly(control)
    _controls.clear()
    for definition in reversed(_definitions):
        _delete_quietly(definition)
    _definitions.clear()
    if ui:
        for command_id, _name, _description in COMMANDS.values():
            _delete_quietly(ui.commandDefinitions.itemById(command_id))
    _delete_quietly(_panel)
    _panel = None
    _owned = False
    _handlers.clear()


def _build_owner(app: object, ui: object) -> None:
    """Build panel + file request service, then commit the already-claimed owner lease."""

    global _panel, _owned, _replaced_watch_event_id
    if _replaced_watch_event_id:
        try:
            app.unregisterCustomEvent(_replaced_watch_event_id)
        except Exception:  # noqa: BLE001 - the expired event may already be gone
            pass
        _replaced_watch_event_id = None
    workspace = _workspace(ui)
    stale_panel = workspace.toolbarPanels.itemById(PANEL_ID)
    if stale_panel:
        # This is either an orderly owner's incomplete panel or the dead panel
        # left by an expired lease. The claimant is the only registration
        # allowed to replace it.
        _delete_quietly(stale_panel)
    _panel = workspace.toolbarPanels.add(
        PANEL_ID,
        PANEL_NAME,
        "SolidScriptsAddinsPanel",
        False,
    )
    try:
        # Insert and Update are ordinarily automatic: WG's Send to CAD writes a
        # handoff this add-in applies on its own. They, and the recovery
        # commands, are demoted under one dropdown so the panel reads as the two
        # actions a user actually starts.
        manage = None
        fem_regions_enabled = _manifest_feature_enabled(wglink_inbox.FEM_REGIONS)
        for operation, (command_id, name, description) in COMMANDS.items():
            stale = ui.commandDefinitions.itemById(command_id)
            if stale:
                _delete_quietly(stale)
            if operation == "fem" and not fem_regions_enabled:
                continue
            definition = ui.commandDefinitions.addButtonDefinition(
                command_id,
                name,
                description,
                _icon_folder(operation),
            )
            if definition is None:
                raise RuntimeError(f"Fusion refused command definition {command_id!r}")
            created = CommandCreatedHandler(operation)
            definition.commandCreated.add(created)
            _handlers.append(created)
            _definitions.append(definition)
            if operation in PROMOTED_COMMANDS:
                _controls.append(_panel.controls.addCommand(definition))
                continue
            if manage is None:
                manage = _manage_dropdown(_panel)
                if manage is not None:
                    _controls.append(manage)
            target = manage.controls if manage is not None else _panel.controls
            _controls.append(target.addCommand(definition))
        if not _start_watch(app):
            raise RuntimeError("Fusion refused the Waveguide Generator add-in's owner event")
        if not _activate_ipc_lease():
            raise RuntimeError("The Waveguide Generator add-in's IPC owner lease was lost during startup")
        _owned = True
        # Migrate earlier queued requests once, in either gate state.
        _convert_outbox_at_startup()
        # The pickup checks a restart lost, whatever the gate says (C6, C8).
        _recheck_requests_at_startup()
        if not _coordinating():
            _start_without_coordination()
            return
        # No separate startup refresh. Loading the add-in is not the event that
        # matters -- seeing a linked document is, and a document stamped at
        # load time is routinely the wrong one or none at all, because WGLink
        # loads before the user opens a model. :func:`_note_active_document`
        # asks when the document is actually seen, on this very publish when
        # one is already open and on the first tick that finds one otherwise.
        snapshot = _fusion_snapshot()
        try:
            with wglink_activity.because(wglink_activity.CAUSE_CLAIM_SETTLEMENT):
                _settle_legacy_claims(snapshot)
        except Exception as exc:  # noqa: BLE001 - an unreadable old journal never blocks loading
            _log(f"[{PANEL_NAME}] could not settle requests claimed by an earlier version: {exc}")
        _publish_fusion_status(snapshot)
    except Exception:
        _stop_watch(app)
        _delete_owned_ui(ui)
        raise


def _start_without_coordination() -> None:
    """Start-up with the gate off: bounded, once, and then nothing until a command.

    What start-up still owes is done here, once: read the
    document if one is open, settle interrupted claims against it read-only
    (a claim for a document that is not open is kept, as always), and publish
    one status so WG knows which add-in is present and how it is configured.
    """

    _log(
        f"[{PANEL_NAME}] automatic coordination is off "
        f"({ACTIVATION_SETTING}: {_activation.get('setting')}). "
        "The Waveguide Generator add-in acts only when you run one of its commands."
    )
    try:
        snapshot = _fusion_snapshot()
        _settle_claims(snapshot)
        _publish_fusion_status(snapshot)
    except Exception as exc:  # noqa: BLE001 - the commands must still load
        _log(f"[{PANEL_NAME}] start-up handling failed: {exc}\n{traceback.format_exc()}")


def _attempt_promotion() -> None:
    """Promote this standby after an orderly release or expired heartbeat."""

    if _owned or not _claim_ipc_lease():
        return
    app = _app()
    ui = app.userInterface if app else None
    if ui is None:
        _release_ipc_lease()
        return
    try:
        _build_owner(app, ui)
    except Exception:
        _release_ipc_lease()
        raise
    _stop_candidate(app)


def run(_context: object) -> None:
    with wglink_activity.because(wglink_activity.CAUSE_STARTUP):
        _run()


def _run() -> None:
    global _panel, _owned, _activation
    try:
        app = _app()
        ui = app.userInterface if app else None
        if ui is None:
            return
        if _owned and _owns_active_ipc_lease():
            return
        # The activation gate, read once for this start and not again.
        _activation = _read_activation()
        if not _claim_ipc_lease():
            _panel = _workspace(ui).toolbarPanels.itemById(PANEL_ID)
            _owned = False
            if _coordinating():
                _start_candidate(app)
            else:
                # Candidate promotion is coordination (engine B): a standby
                # with the gate off waits for a restart rather than a timer.
                _log(
                    f"[{PANEL_NAME}] another registration of the Waveguide Generator add-in owns this Fusion "
                    "session, and automatic coordination is off, so this one does "
                    "not take over by itself. It stays idle until it is started "
                    "again (Stop and Run WGLink in Scripts and Add-Ins, or restart "
                    "Fusion) after the owner has stopped."
                )
            return
        try:
            _build_owner(app, ui)
        except Exception:
            _release_ipc_lease()
            raise
    except Exception as exc:  # noqa: BLE001
        _report_error("Starting the Waveguide Generator add-in", f"{PANEL_NAME} add-in start error", exc)


def stop(_context: object) -> None:
    with wglink_activity.because(wglink_activity.CAUSE_SHUTDOWN):
        _stop()


def _stop() -> None:
    try:
        app = _app()
        _stop_candidate(app)
        lease_owner = _owns_ipc_lease()
        try:
            _stop_watch(app)
            if _owned and lease_owner:
                _delete_owned_ui(app.userInterface if app else None)
            else:
                # An expired former owner may retain handles to objects already
                # replaced by its promoted successor. Never delete shared UI unless
                # the lease still proves authority.
                _clear_registration_state()
        finally:
            if lease_owner:
                _release_ipc_lease()
    except Exception as exc:  # noqa: BLE001
        _report_error("Stopping the Waveguide Generator add-in", f"{PANEL_NAME} add-in stop error", exc)
