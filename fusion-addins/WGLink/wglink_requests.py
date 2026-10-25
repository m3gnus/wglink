"""The one Send or Solve the add-in follows, and what its palette says about it.

A request is plain data (``new_request``): what was pressed, for which
document, what WG advertised when it was submitted, and the latest answer WG
wrote for it. ``poll`` advances it from files alone -- WG's request inbox, the
receipt and WG's request status (docs/REQUEST-STATUS.md) -- so it runs off
Fusion's main thread; WGLink.py hands each change to the main thread through
its follow-up event and only renders there. Nothing here touches ``adsk``.

The words come from one owner each: WG's states read through
``wglink_protocol.request_status_text`` (both repositories share it), and the
few states only Fusion knows -- exporting, waiting for WG to pick the model
up, a WG too old to report progress -- are worded here. Every string a user
can read passes ``wglink_protocol.user_text_violations``.
"""

from __future__ import annotations

import copy
import threading
import time
from pathlib import Path
from typing import Any, Mapping

if __package__:
    from . import wglink_inbox, wglink_protocol, wglink_status
else:
    import wglink_inbox
    import wglink_protocol
    import wglink_status


#: The add-in's own record of the request it follows, in the WGLink folder,
#: so a restart resumes following it without submitting anything again.
RECORD_FILENAME = ".fusion-request-status.json"
RECORD_SCHEMA = 1

SEND = wglink_inbox.KIND_RECEIVE_SNAPSHOT
SOLVE = wglink_inbox.KIND_PREPARE_AND_SOLVE

# What only Fusion knows about a request (``local``). WG's own answer, once
# there is one, takes over from these.
EXPORTING = "exporting"  # the command is exporting; nothing is written yet
SUBMITTED = "submitted"  # the request file is in WG's inbox
UNCONFIRMED = "unconfirmed"  # writing it reported trouble; it may have landed
NOT_TAKEN = "notTaken"  # still in the inbox after the pickup window
TAKEN = "taken"  # a WG without receipts took the file: all it will say
TAKEN_UNCONFIRMED = "takenUnconfirmed"  # taken, but no receipt within the wait
NOT_SENT = "notSent"  # the command ended before a request was written
_LOCAL_TERMINAL = frozenset({TAKEN, NOT_SENT})
_LOCAL = frozenset({EXPORTING, SUBMITTED, UNCONFIRMED, NOT_TAKEN, TAKEN, TAKEN_UNCONFIRMED, NOT_SENT})

#: How often a request still under way is looked at: while its palette is
#: showing, and while it is not. A finished request is not looked at.
POLL_VISIBLE_SECONDS = 2.0
POLL_HIDDEN_SECONDS = 5.0

APP_NAME = wglink_protocol.APP_NAME
_OPEN_WG = {"id": "openWg", "label": f"Open {APP_NAME}"}
_SEND_AGAIN = {"id": "sendAgain", "label": "Send again"}
_SOLVE_AGAIN = {"id": "sendAgain", "label": "Solve again"}
#: The actions the palette carries out. ``wglink_protocol`` names the first three.
ACTION_IDS = ("openWg", "openSourcesMesh", "showResults", "sendAgain")


def new_request(request_id: str, kind: str, document_name: object, *,
                document_id: object = None, submission_key: object = None,
                now: float | None = None) -> dict[str, Any]:
    """A request that has just been pressed: it is exporting, nothing is written."""

    return {
        "schema": RECORD_SCHEMA,
        "requestId": str(request_id),
        "kind": SOLVE if kind == SOLVE else SEND,
        "documentName": _name(document_name),
        "documentId": str(document_id) if document_id else None,
        "submissionKey": str(submission_key) if submission_key is not None else None,
        "pressedAt": float(time.time() if now is None else now),
        "submittedAt": None,
        "local": EXPORTING,
        "manifestSha256": None,
        "capabilities": {"acknowledgement": False, "requestStatus": False},
        "receipt": None,
        "status": None,
        "notes": [],
    }


def submitted(request: Mapping[str, Any], *, manifest_sha256: str, acknowledgement: bool,
              request_status: bool, confirmed: bool, notes: list[str] | None = None,
              submission_key: object = None, now: float | None = None) -> dict[str, Any]:
    """The request once its file is written (``confirmed``) or may have been.

    ``acknowledgement`` and ``request_status`` are what WG advertised at this
    moment; the request keeps them, so a capability file that is briefly
    missing later never downgrades it.
    """

    found = copy.deepcopy(dict(request))
    found.update(
        local=SUBMITTED if confirmed else UNCONFIRMED,
        submittedAt=float(time.time() if now is None else now),
        manifestSha256=str(manifest_sha256),
        capabilities={"acknowledgement": bool(acknowledgement),
                      "requestStatus": bool(acknowledgement and request_status)},
        notes=[str(note) for note in (notes or [])],
    )
    if submission_key is not None:
        found["submissionKey"] = str(submission_key)
    return found


def not_sent(request: Mapping[str, Any]) -> dict[str, Any]:
    """The request whose command ended before anything reached WG's inbox."""

    found = copy.deepcopy(dict(request))
    found["local"] = NOT_SENT
    return found


def request_path(ipc: Path, request: Mapping[str, Any]) -> Path:
    return Path(ipc) / wglink_inbox.SOLVE_REQUESTS_DIRECTORY / f"{request.get('requestId')}.json"


def poll(request: Mapping[str, Any], ipc: Path, *, now: float | None = None) -> dict[str, Any]:
    """The request advanced by what WG has written since. Reads files only.

    The inbox file (is it still waiting?), the receipt, and -- when WG said it
    writes one -- the status. A status with a lower ``seq`` than the one
    already held is an older write and is ignored. Never raises: a request
    that cannot be looked at stays as it was.
    """

    found = copy.deepcopy(dict(request))
    try:
        _advance(found, Path(ipc), time.time() if now is None else float(now))
    except Exception:  # noqa: BLE001 - the next look tries again
        return copy.deepcopy(dict(request))
    return found


def _advance(request: dict[str, Any], ipc: Path, now: float) -> None:
    if terminal(request) or request.get("local") in (EXPORTING, NOT_SENT):
        return
    request_id = str(request.get("requestId"))
    kind = request.get("kind")
    manifest = request.get("manifestSha256")
    capabilities = request.get("capabilities") or {}
    if capabilities.get("requestStatus"):
        status = wglink_inbox.read_request_status(
            ipc, request_id, kind=kind, manifest_sha256=manifest)
        held = request.get("status")
        if status is not None and (not isinstance(held, Mapping) or status["seq"] >= held.get("seq", -1)):
            request["status"] = status
    if capabilities.get("acknowledgement") and request.get("receipt") is None:
        receipt = wglink_inbox.read_acknowledgement(
            ipc, request_id, kind=kind, manifest_sha256=manifest)
        if receipt is not None:
            request["receipt"] = {"outcome": receipt[0], "reason": receipt[1]}
    if request.get("status") is not None or request.get("receipt") is not None:
        return
    waited = now - float(request.get("submittedAt") or now)
    if request_path(ipc, request).exists():
        if waited >= wglink_inbox.SOLVE_PICKUP_NOTICE_SECONDS:
            request["local"] = NOT_TAKEN
    elif not capabilities.get("acknowledgement"):
        request["local"] = TAKEN  # all a WG without receipts will ever say
    elif waited >= wglink_inbox.ACK_WAIT_SECONDS:
        request["local"] = TAKEN_UNCONFIRMED
    elif request.get("local") == NOT_TAKEN:
        request["local"] = SUBMITTED  # taken after all; the receipt follows


def _wg_record(request: Mapping[str, Any]) -> dict[str, Any] | None:
    """WG's answer as a status record: the status itself, or one read off the receipt."""

    status = request.get("status")
    if isinstance(status, Mapping):
        return dict(status)
    receipt = request.get("receipt")
    if not isinstance(receipt, Mapping):
        return None
    record: dict[str, Any] = {
        "contract": wglink_protocol.REQUEST_STATUS_CONTRACT,
        "schemaVersion": wglink_protocol.REQUEST_STATUS_SCHEMA_VERSION,
        "seq": 0,
        "requestId": request.get("requestId"),
        "kind": request.get("kind"),
        "documentName": request.get("documentName"),
        "state": "refused" if receipt.get("outcome") == wglink_inbox.ACK_REFUSED else "accepted",
    }
    if record["state"] == "refused":
        # WG's own reason, shown only when it reads as words for a user (the
        # protocol's validator drops it otherwise; Diagnostics keeps it).
        record["failure"] = {"recorded": False, "message": receipt.get("reason")}
    return wglink_protocol.validate_request_status(record)


def _old_wg_solve(request: Mapping[str, Any]) -> bool:
    """A Solve WG accepted that will say nothing more: it writes no status."""

    receipt = request.get("receipt")
    return (request.get("status") is None
            and isinstance(receipt, Mapping) and receipt.get("outcome") == wglink_inbox.ACK_ACCEPTED
            and request.get("kind") == SOLVE
            and not (request.get("capabilities") or {}).get("requestStatus"))


def terminal(request: Mapping[str, Any]) -> bool:
    """Whether nothing more will happen to the request unless someone acts."""

    record = _wg_record(request)
    if record is not None:
        if _old_wg_solve(request):
            return True
        if record.get("state") == "accepted" and request.get("kind") == SOLVE:
            return False  # WG writes the status next
        return wglink_protocol.request_status_terminal(record)
    return request.get("local") in _LOCAL_TERMINAL


def outcome(request: Mapping[str, Any]) -> str:
    """One word for diagnostics: WG's state, its receipt, or the local phase."""

    status = request.get("status")
    if isinstance(status, Mapping) and isinstance(status.get("state"), str):
        return str(status["state"])
    receipt = request.get("receipt")
    if isinstance(receipt, Mapping) and isinstance(receipt.get("outcome"), str):
        return str(receipt["outcome"])
    return str(request.get("local") or EXPORTING)


def view(request: Mapping[str, Any]) -> dict[str, Any]:
    """What the palette shows: ``{headline, detail, notes, options, actions,
    busy, progress, terminal, diagnostics}``.

    ``busy`` asks for an indeterminate bar, ``progress`` (0..1) for a filled
    one. ``diagnostics`` is the technical correlation, shown only under the
    palette's collapsed Diagnostics. Never raises.
    """

    try:
        return _view(request)
    except Exception:  # noqa: BLE001 - a user always gets a sentence
        return {"headline": f"Open {APP_NAME} to see this request.", "detail": None,
                "notes": [], "options": [], "actions": [dict(_OPEN_WG)], "busy": False,
                "progress": None, "terminal": True, "diagnostics": {}}


def _view(request: Mapping[str, Any]) -> dict[str, Any]:
    name = _name(request.get("documentName"))
    shown, inner = name or "This model", name or "this model"
    solve = request.get("kind") == SOLVE
    verb = "Solve" if solve else "Send"
    again = dict(_SOLVE_AGAIN if solve else _SEND_AGAIN)
    detail: str | None = None
    options: list[str] = []
    actions: list[dict[str, str]] = [dict(_OPEN_WG)]
    progress: float | None = None
    record = _wg_record(request)
    if _old_wg_solve(request):
        headline = f"{shown} · Accepted. Follow progress in {APP_NAME}."
    elif record is not None:
        text = wglink_protocol.request_status_text(record)
        headline, detail = text["headline"], text["detail"]
        options, actions = list(text["options"]), [dict(action) for action in text["actions"]]
        fraction = ((record.get("progress") or {}).get("fraction")
                    if record.get("state") == "solving" else None)
        progress = float(fraction) if isinstance(fraction, (int, float)) else None
    else:
        local = request.get("local")
        if local == EXPORTING:
            headline = f"{shown} · {verb} · Preparing the export…"
            actions = []
        elif local == SUBMITTED:
            headline = f"{shown} · Sent to {APP_NAME}. Waiting for it to pick up the model."
        elif local == UNCONFIRMED:
            headline = f"{APP_NAME} has not picked up {inner} yet."
            detail = (f"{APP_NAME} may already have it. Open {APP_NAME} and check "
                      f"before you {verb.lower()} again.")
            actions.append(again)
        elif local == NOT_TAKEN:
            headline = f"{APP_NAME} has not picked up {inner} yet."
            detail = (f"Open {APP_NAME}; it picks up the model while it runs. "
                      "If an older version is running, update it.")
            actions.append(again)
        elif local == TAKEN_UNCONFIRMED:
            headline = f"{APP_NAME} picked up {inner} but has not confirmed it yet."
            detail = f"Open {APP_NAME} and check before you {verb.lower()} again."
        elif local == TAKEN:
            headline = f"{shown} · {APP_NAME} picked it up. Follow progress in {APP_NAME}."
        elif local == NOT_SENT:
            headline = f"{shown} was not sent to {APP_NAME}."
            actions = [again]
        else:
            headline = f"{shown} · Open {APP_NAME} to see this request."
    finished = terminal(request)
    return {
        "headline": headline,
        "detail": detail,
        "notes": [str(note) for note in request.get("notes") or []],
        "options": options,
        "actions": actions,
        "busy": not finished and progress is None,
        "progress": progress,
        "terminal": finished,
        "diagnostics": diagnostics(request),
    }


def diagnostics(request: Mapping[str, Any]) -> dict[str, Any]:
    """The technical correlation of one request, for Diagnostics and the log."""

    status = request.get("status") if isinstance(request.get("status"), Mapping) else {}
    job = status.get("result") or status.get("job") or {}
    receipt = request.get("receipt") if isinstance(request.get("receipt"), Mapping) else {}
    found = {
        "requestId": request.get("requestId"),
        "kind": request.get("kind"),
        "outcome": outcome(request),
        "local": request.get("local"),
        "seq": status.get("seq"),
        "jobId": job.get("jobId") if isinstance(job, Mapping) else None,
        "runNumber": job.get("runNumber") if isinstance(job, Mapping) else None,
        "receipt": receipt.get("outcome"),
        "receiptReason": receipt.get("reason"),
        "capabilities": dict(request.get("capabilities") or {}),
    }
    return {key: value for key, value in found.items() if value not in (None, {}, "")}


def _name(value: object) -> str | None:
    text = str(value).strip() if isinstance(value, str) else ""
    return text[:200] or None


# -- the add-in's own record ----------------------------------------------------


def save(ipc: Path, request: Mapping[str, Any]) -> Path:
    """Write the followed request atomically (``wglink_status``'s one writer)."""

    return wglink_status.write_status_record(Path(ipc), RECORD_FILENAME, dict(request))


def load(ipc: Path) -> dict[str, Any] | None:
    """The request a previous session was following, or None.

    A request whose export never finished cannot have reached WG: it reads as
    not sent. Nothing is ever submitted again from here.
    """

    found = wglink_inbox._read_json(Path(ipc) / RECORD_FILENAME)
    if (not isinstance(found, dict) or found.get("schema") != RECORD_SCHEMA
            or not isinstance(found.get("requestId"), str)
            or found.get("local") not in _LOCAL
            or not isinstance(found.get("capabilities"), dict)):
        return None
    if found.get("local") == EXPORTING:
        found["local"] = NOT_SENT
    if found.get("status") is not None:
        found["status"] = wglink_protocol.validate_request_status(
            found["status"], request_id=found["requestId"])
    if not isinstance(found.get("notes"), list):
        found["notes"] = []
    return found


class RequestTracker:
    """The one request the palette follows; shared by the main thread and the poll.

    Plain data under a lock. ``replace`` takes an update only for the request
    it holds, so a late answer for an earlier request never changes a newer
    one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._request: dict[str, Any] | None = None
        self.visible = False

    def current(self) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._request)

    def begin(self, request: Mapping[str, Any] | None) -> None:
        with self._lock:
            self._request = copy.deepcopy(dict(request)) if request is not None else None

    def replace(self, request: Mapping[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        """``(previous, taken)``: the update is taken only for the request held."""

        with self._lock:
            previous = copy.deepcopy(self._request)
            if previous is None or previous.get("requestId") != request.get("requestId"):
                return previous, False
            self._request = copy.deepcopy(dict(request))
            return previous, True

    def poll_interval(self) -> float | None:
        """Seconds to the next look, or None when nothing is under way."""

        request = self.current()
        if request is None or terminal(request) or request.get("local") == EXPORTING:
            return None
        return POLL_VISIBLE_SECONDS if self.visible else POLL_HIDDEN_SECONDS


__all__ = [
    "ACTION_IDS", "EXPORTING", "NOT_SENT", "NOT_TAKEN", "POLL_HIDDEN_SECONDS",
    "POLL_VISIBLE_SECONDS", "RECORD_FILENAME", "RequestTracker", "SEND", "SOLVE",
    "SUBMITTED", "TAKEN", "TAKEN_UNCONFIRMED", "UNCONFIRMED", "diagnostics", "load",
    "new_request", "not_sent", "outcome", "poll", "request_path", "save", "submitted",
    "terminal", "view",
]
