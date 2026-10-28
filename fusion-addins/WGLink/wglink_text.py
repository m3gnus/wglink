"""Fusion wording and the boundary between diagnostics and messages.

Shared rule/status sentences stay in wglink_protocol, the byte-for-byte WG
counterpart. Internal exception strings are diagnostic evidence, never UI copy.
"""
from __future__ import annotations

import logging
import traceback
from typing import Callable, Sequence

if __package__:
    from . import wglink_protocol
    from .wglink_bundle import WgLinkError
else:
    import wglink_protocol
    from wglink_bundle import WgLinkError


class UserFacing(WgLinkError):
    """An intentional model message, with its stable diagnostic code."""

    def __init__(self, code: str, text: str, next_step: str = "", *, detail: str | None = None):
        self.code, self.text, self.next_step = code, text, next_step
        # Preserve the original evidence for logs and headless callers.
        super().__init__(detail if detail is not None else " ".join(filter(None, (text, next_step))))


# Every safety check still raises at the same point. Only its presentation changes.
PROBLEMS = {
    "mirrored_placement": ("This model contains a mirrored WG link.", "Insert a fresh copy from WG and move or rotate it into position."),
    "scaled_placement": ("This model contains a scaled or skewed WG link.", "Remove the scale or skew and send again."),
    "body_inclusion": ("Fusion would send different bodies from those selected.", "Hide unwanted bodies themselves in the browser, then send again."),
    "unclassified_visible_surface": ("A visible surface body has no purpose set.", "If it is a helper, hide the body itself in the browser; otherwise select it and use Advanced → Declare Body… → Exterior shell."),
    "scope_invalid": ("This model has no usable exterior in the selected scope.", "Review the visible bodies and Assembly scope, then send again."),
    "scope_selection": ("This selection cannot be sent on its own.", "Leave Assembly scope empty to send the whole assembly."),
    "anchor_choice": ("This model needs a reference WG link.", "Choose a Reference link in the Send or Solve dialog."),
    "copied_link": ("This model contains copies of the same WG link.", "Remove the unwanted copy or insert a fresh link from WG."),
    "link_placement": ("The position of a WG link could not be read.", "Check its component and placement in Fusion, then send again."),
    "link_datums": ("A WG link's reference geometry could not be identified.", "Insert a fresh copy of the link from WG."),
    "source_setup": ("A driver could not be identified on this model.", "Open Sources & Mesh… and select its faces again."),
    "source_changed": ("A driver's faces have changed or are outside this selection.", "Review its faces in Sources & Mesh…, then save."),
    "source_identity_unreadable": ("The saved driver setup could not be read.", "Open Sources & Mesh… and select its faces again."),
    "source_name": ("A saved driver name cannot be used.", "Open Sources & Mesh… and save the driver setup again."),
    "no_drive": (wglink_protocol.NO_DRIVE_MESSAGE, wglink_protocol.PREFLIGHT_RULES["no_drive"][1]),
    "mesh_settings": ("The saved mesh settings could not be used.", "Open Sources & Mesh… and save them again."),
    "size_zone": ("A size zone's faces could not be used.", "Review the size zones in Sources & Mesh…, then save."),
    "fem_setup": ("A FEM region could not be used.", "Review it in Declare FEM Region… before sending."),
    "fem_visibility": ("A FEM air volume would also be sent as exterior geometry.", "Hide its occurrence in the browser before sending."),
    "domain_setup": ("The model does not match the chosen half or quarter.", "Check the cut or send the full model."),
    "export_body_count": ("Fusion exported a different number of bodies from those selected.", "Check body visibility in the browser, then send again."),
    "export_folder": ("This model could not be saved for WG.", "Check that the WGLink folder is writable, then send again."),
    "export_busy": ("This model is already being sent.", "Wait for it to finish, then try again."),
    "mesh_outdated": (wglink_protocol.MESH_SIZING_UNAVAILABLE_MESSAGE, ""),
    "fem_unavailable": ("This Waveguide Generator does not offer FEM regions.", "Update it or turn FEM regions on, then try again."),
    "already_linked": ("This WG design is already linked in the active Fusion document.", "Select the link in WG’s CAD Link panel and send the update from there."),
    "expired": ("The model is ready, but the time to insert it has expired.", "Send the model from WG again."),
    "wg_outdated": ("This Waveguide Generator needs updating to work with Fusion.", "Update Waveguide Generator; it installs the matching Fusion add-in."),
    "wg_not_collecting": ("Waveguide Generator is not ready to receive this model.", "Open Waveguide Generator and check Settings → CAD Link, then try again."),
    "startup": ("Waveguide Generator could not start.", "Start Waveguide Generator manually, then try again."),
    "document_changed": ("The active Fusion document changed while WG was starting.", "Send or Solve again from the intended document."),
    "model_changed": ("This model changed after WG checked it.", "Refresh CAD Link in WG, then try again."),
    "workspace": ("This model has no usable WGLink folder.", "Choose the folder in WG under Settings → CAD Link, then try again."),
    "delivery_write": ("This model was saved, but Waveguide Generator has not received it.", "Check that the WGLink folder is writable, then send again."),
    "delivery": ("Waveguide Generator could not receive this model.", "Send or Solve again from Fusion."),
    "solve_settings": ("Some solve settings in the Waveguide Generator panel cannot be used.", "Correct the highlighted settings in the panel, then solve again."),
    "recovery": ("The model's previous update was interrupted.", "Undo the interrupted update or insert a fresh copy from WG."),
}

COMMANDS = {
    "source": (
        "hornlab_wglink_set_source",
        "Sources & Mesh…",
        "Set up sources and mesh, then Send or Solve",
    ),
    "solve": (
        "hornlab_wglink_solve",
        "Solve in WG",
        "Send and solve now.",
    ),
    "requests": (
        "hornlab_wglink_requests",
        "Show Panel",
        "Show the Waveguide Generator panel: Sources & Mesh, Solve, and the progress of each run.",
    ),
    "send": (
        "hornlab_wglink_send",
        "Send to WG",
        "Open this model in WG to inspect or set up. Does not solve.",
    ),
    "declare": (
        "hornlab_wglink_declare_body",
        "Declare Body…",
        "Classify a body as the exterior WG solves, or leave it out of the model sent to WG.",
    ),
    "fem": (
        "hornlab_wglink_declare_fem_region",
        "Declare FEM Region…",
        "Mark a solid body of air as a FEM region and name its driver, interface "
        "and termination faces for a coupled FEM/BEM solve.",
    ),
    "insert": (
        "hornlab_wglink_insert",
        "Insert",
        "Insert the full WG viewport model from a WG model file.",
    ),
    "update": (
        "hornlab_wglink_update",
        "Update",
        "Rebuild a managed link in place from its current WG model.",
    ),
    "detach": (
        "hornlab_wglink_detach",
        "Detach",
        "Remove Waveguide Generator link data without changing bodies or features.",
    ),
}

EXPORT_CHECKING = "Exporting STEP and checking this model…"
EXPORT_SOLVING = "Model saved. Asking Waveguide Generator to solve…"
EXPORT_SENDING = "Model saved. Opening it in Waveguide Generator…"
OUTBOX_KEPT = "Your models are kept. Restart Fusion once the WGLink folder is writable to try again."

# Installed per registration; no Fusion access happens unless the UI calls us.
log: Callable[[str], None] = logging.getLogger(__name__).error
context: Callable[[], tuple[str, str | None]] = lambda: ("this model", None)


def problem(code: str, detail: object = "") -> UserFacing:
    text, next_step = PROBLEMS[code]
    return UserFacing(code, text, next_step, detail=str(detail) or None)


def user_message(value: BaseException | str | None, *, action: str = "use", document: str | None = None,
                 request_id: str | None = None, names: Sequence[str] = (), logger: Callable[[str], None] | None = None) -> str:
    """Render known wording; log full evidence and mask all other exceptions."""
    current_document, current_request = context()
    document = document or current_document or "this model"
    request_id = request_id or current_request
    sink = logger or log
    if isinstance(value, BaseException):
        seen = set()
        exc = value
        known = None
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            if isinstance(exc, UserFacing) and known is None:
                known = exc
            exc = exc.__cause__
        code = known.code if known is not None else type(value).__name__
        sink(f"[{wglink_protocol.APP_NAME}] code={code} request_id={request_id or '-'}\n"
             + "".join(traceback.format_exception(type(value), value, value.__traceback__)))
        if known is not None:
            text = " ".join(filter(None, (known.text, known.next_step)))
            if not wglink_protocol.user_text_violations(text, names=(document, *names)):
                return text
    elif isinstance(value, str):
        if not wglink_protocol.user_text_violations(value, names=(document, *names)):
            return value
        sink(f"[{wglink_protocol.APP_NAME}] code=unsafe_user_text request_id={request_id or '-'}\n{value}")
    return f"Waveguide Generator could not {action} {document}. This has been recorded."


def scope_problem(exc: BaseException) -> UserFacing:
    reasons = getattr(exc, "reasons", ())
    code = ("unclassified_visible_surface" if any(
        reason.get("reason_code") == "unclassified_visible_surface" for reason in reasons
    ) else "scope_invalid")
    return problem(code, exc)


def scope_message(exc: BaseException) -> str:
    return user_message(scope_problem(exc))


def role_label(role: object) -> str:
    return (wglink_protocol.driver_label(role) if wglink_protocol.canonical_source_role(role) == "PASSIVE_CARDIOID"
            else str(role))


def legacy_delivery_message(notice: dict) -> str:
    log(f"[{wglink_protocol.APP_NAME}] earlier_delivery request_id={notice.get('operationId', '-')} {notice!r}")
    return "Waveguide Generator could not receive an earlier model. Send or Solve again from Fusion."


def link_choices(links: list[dict]) -> list[tuple[str, str]]:
    """Recognisable labels without exposing the stable link identity."""
    choices = []
    for link in links:
        identity = str(link.get("instance_id") or "")
        if not identity:
            continue
        name = str(link.get("link_name") or link.get("design_name") or "").strip()
        label = f"Link {len(choices) + 1}"
        choices.append((f"{name} · {label}" if name else label, identity))
    return choices


ACTION_VERBS = {
    "source": "save settings for", "Sources & Mesh…": "save settings for",
    "send": "send", "Send to WG": "send", "solve": "solve", "Solve in WG": "solve",
    "insert": "insert", "Insert": "insert", "update": "update", "Update": "update",
    "detach": "detach", "Detach": "detach", "declare": "save settings for",
    "fem": "save settings for",
}


def action_verb(action: str) -> str:
    return ACTION_VERBS.get(action, "use")


def report_names(report: dict) -> tuple[str, ...]:
    return tuple(str(report[key]) for key in ("design_name", "link_name", "wrapper") if report.get(key))


def report_warnings(report: dict) -> str:
    warnings = report.get("warnings")
    if not isinstance(warnings, list) or not warnings:
        return ""
    shown = []
    for warning in warnings:
        text = str(warning)
        if wglink_protocol.user_text_violations(text, names=report_names(report)):
            _, request_id = context()
            log(f"[{wglink_protocol.APP_NAME}] code=model_warning request_id={request_id or '-'}\n{text}")
            text = "Some model checks could not be completed. The details have been recorded."
        if text not in shown:
            shown.append(text)
    return "\n\nWarnings:\n- " + "\n- ".join(shown)
