"""The one Send or Solve the add-in follows, and what its palette says about it.

A request is plain data (``new_request``): what was pressed, for which
document, what WG advertised when it was submitted, the latest answer WG
wrote for it, and when the add-in last heard anything about it. ``poll``
advances it from files alone -- WG's request inbox, the receipt and WG's
request status (docs/REQUEST-STATUS.md) -- so it runs off Fusion's main
thread; WGLink.py hands each change to the main thread through its follow-up
event and only renders there. Nothing here touches ``adsk``.

The words come from one owner each: WG's states read through
``wglink_protocol.request_status_text`` (both repositories share it), and the
few states only Fusion knows -- exporting, waiting for WG to pick the model
up, a WG too old to report progress, a request WG has gone quiet about -- are
worded here. Every string a user can read passes
``wglink_protocol.user_text_violations``.

No state is a dead end. A request still under way always offers a way on
(Open Waveguide Generator, and Send or Solve again where the add-in cannot
tell what WG has), and one WG stops reporting on reads as quiet rather than
busy for ever. Following stops on its own, and a record too old to matter is
not resumed.
"""

from __future__ import annotations

import copy
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

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
#: WG's states in which something is visibly happening (an animated bar).
_WG_BUSY = frozenset({"accepted", "preparing", "queued", "solving"})

#: How often a request still under way is looked at: while its palette is
#: showing, while it is not, and once WG has gone quiet about it (or it
#: finished in a way WG may still take back, see ``RETRY_WATCH_SECONDS``).
POLL_VISIBLE_SECONDS = 2.0
POLL_HIDDEN_SECONDS = 5.0
POLL_QUIET_SECONDS = 30.0
#: WG rewrites the status of every request it has not finished once the file
#: is this old, each time with a higher ``seq`` (docs/REQUEST-STATUS.md).
WG_STATUS_RHYTHM_SECONDS = float(wglink_protocol.REQUEST_STATUS_REFRESH_SECONDS)
#: With no new ``seq`` for this long, a request WG is working on reads as
#: quiet: "has not reported for a while", with a way on.
QUIET_SECONDS = 5 * WG_STATUS_RHYTHM_SECONDS
#: Sooner when WG is gone: its capability file carries ``stoppedAt`` (a clean
#: shutdown), or a request whose status WG keeps fresh has missed two of its
#: rewrites (a crash).
QUIET_WITHOUT_WG_SECONDS = 2 * WG_STATUS_RHYTHM_SECONDS
#: With no news for this long, the add-in stops looking. The palette keeps
#: its last words and its ways on.
FOLLOW_LIMIT_SECONDS = 24 * 3600.0
#: WG's "Try again" moves a request that could not finish back to preparing:
#: it is looked at, slowly, for this long after it stopped.
RETRY_WATCH_SECONDS = 3600.0
#: A record whose last news is older than this is not resumed at start-up.
RECORD_MAX_AGE_SECONDS = 2 * 24 * 3600.0

APP_NAME = wglink_protocol.APP_NAME
_OPEN_WG = wglink_protocol.REQUEST_STATUS_OPEN_WG
_SEND_AGAIN = {"id": "sendAgain", "label": "Send again"}
_SOLVE_AGAIN = {"id": "sendAgain", "label": "Solve again"}
SEND_AGAIN_ID = _SEND_AGAIN["id"]
#: The actions the palette carries out: WG's shared ones, and Send/Solve again.
ACTION_IDS = wglink_protocol.REQUEST_STATUS_ACTION_IDS + (SEND_AGAIN_ID,)


def new_request(request_id: str, kind: str, document_name: object, *,
                document_id: object = None, submission_key: object = None,
                now: float | None = None) -> dict[str, Any]:
    """A request that has just been pressed: it is exporting, nothing is written."""

    moment = float(time.time() if now is None else now)
    return {
        "schema": RECORD_SCHEMA,
        "requestId": str(request_id),
        "kind": SOLVE if kind == SOLVE else SEND,
        "documentName": _name(document_name),
        "documentId": str(document_id) if document_id else None,
        "submissionKey": str(submission_key) if submission_key is not None else None,
        "pressedAt": moment,
        "submittedAt": None,
        "heardAt": moment,
        "quiet": False,
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

    moment = float(time.time() if now is None else now)
    found = copy.deepcopy(dict(request))
    found.update(
        local=SUBMITTED if confirmed else UNCONFIRMED,
        submittedAt=moment,
        heardAt=moment,
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


def replaces(previous: Mapping[str, Any] | None, request: Mapping[str, Any]) -> bool:
    """Whether ``request`` is the same press for the same document as ``previous``.

    The same command, and the same document by its id: two documents the
    add-in cannot name are never taken for one.
    """

    if previous is None or previous.get("requestId") == request.get("requestId"):
        return False
    document = previous.get("documentId")
    return (bool(document) and document == request.get("documentId")
            and previous.get("kind") == request.get("kind"))


def withdraw(ipc: Path, request: Mapping[str, Any]) -> bool:
    """Take back a request WG has not picked up, because a newer one replaces it.

    Only a file still under its own name in the inbox: WG claims a file by
    renaming it, so one WG already took is never touched. True when it was
    taken back.
    """

    if (request.get("receipt") is not None or request.get("status") is not None
            or request.get("local") not in (SUBMITTED, NOT_TAKEN, UNCONFIRMED)):
        return False
    try:
        request_path(ipc, request).unlink()
    except OSError:
        return False
    return True


def poll(request: Mapping[str, Any], ipc: Path, *, now: float | None = None) -> dict[str, Any]:
    """The request advanced by what WG has written since. Reads files only.

    The inbox file (is it still waiting?), the receipt, and -- when WG said it
    writes one -- the status. A status with a ``seq`` no higher than the one
    already held is not news. Never raises: a request that cannot be looked
    at stays as it was.
    """

    found = copy.deepcopy(dict(request))
    try:
        _advance(found, Path(ipc), time.time() if now is None else float(now))
    except Exception:  # noqa: BLE001 - the next look tries again
        return copy.deepcopy(dict(request))
    return found


def _advance(request: dict[str, Any], ipc: Path, now: float) -> None:
    if not following(request, now):
        return
    request_id = str(request.get("requestId"))
    kind = request.get("kind")
    manifest = request.get("manifestSha256")
    capabilities = request.get("capabilities") or {}
    heard = False
    if capabilities.get("requestStatus"):
        status = wglink_inbox.read_request_status(
            ipc, request_id, kind=kind, manifest_sha256=manifest)
        held = request.get("status")
        held_seq = held.get("seq", -1) if isinstance(held, Mapping) else -1
        # An equal seq is not older (the contract): it is taken. Only a higher
        # one is news from WG.
        if status is not None and status["seq"] >= held_seq:
            request["status"] = status
            heard = status["seq"] > held_seq
    if capabilities.get("acknowledgement") and request.get("receipt") is None:
        receipt = wglink_inbox.read_acknowledgement(
            ipc, request_id, kind=kind, manifest_sha256=manifest)
        if receipt is not None:
            request["receipt"] = {"outcome": receipt[0], "reason": receipt[1]}
            heard = True
    if request.get("status") is None and request.get("receipt") is None:
        before = request.get("local")
        _advance_local(request, ipc, now)
        heard = heard or request.get("local") != before
    if heard:
        request["heardAt"] = now
    request["wgStopped"] = wglink_inbox.wg_stopped(ipc)
    request["quiet"] = _quiet(request, now)


def _advance_local(request: dict[str, Any], ipc: Path, now: float) -> None:
    """What the inbox alone says. Absence alone never reads as picked up when
    the add-in could not confirm the write: the file may never have landed."""

    local = request.get("local")
    if local == UNCONFIRMED:
        return
    acknowledges = (request.get("capabilities") or {}).get("acknowledgement")
    waited = now - float(request.get("submittedAt") or now)
    if request_path(ipc, request).exists():
        if waited >= wglink_inbox.SOLVE_PICKUP_NOTICE_SECONDS:
            request["local"] = NOT_TAKEN
    elif not acknowledges:
        request["local"] = TAKEN  # written for certain, then taken: all this WG will say
    elif waited >= wglink_inbox.ACK_WAIT_SECONDS:
        request["local"] = TAKEN_UNCONFIRMED
    elif local == NOT_TAKEN:
        request["local"] = SUBMITTED  # taken after all; the receipt follows


def _quiet_eligible(request: Mapping[str, Any]) -> bool:
    """WG has the request and is meant to be working on it (not asking the user)."""

    record = _wg_record(request)
    return record is not None and not terminal(request) and record.get("state") != "needsYou"


def _silent(request: Mapping[str, Any], now: float) -> float:
    return now - float(request.get("heardAt") or now)


def _quiet(request: Mapping[str, Any], now: float) -> bool:
    """WG is working on the request, but has said nothing about it for a while."""

    if not _quiet_eligible(request):
        return False
    silent = _silent(request, now)
    if silent >= QUIET_SECONDS:
        return True
    if request.get("wgStopped"):
        return True  # WG shut down; a restart rewrites the status at once
    # A status WG keeps fresh that missed two rewrites: WG is not running.
    fresh = (request.get("capabilities") or {}).get("requestStatus")
    return bool(fresh) and silent > QUIET_WITHOUT_WG_SECONDS


def is_quiet(request: Mapping[str, Any], now: float | None = None) -> bool:
    """Quiet by age alone (``QUIET_SECONDS`` without news), whatever was saved."""

    moment = time.time() if now is None else float(now)
    return _quiet_eligible(request) and _silent(request, moment) >= QUIET_SECONDS


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


def following(request: Mapping[str, Any], now: float | None = None) -> bool:
    """Whether the request is still worth looking at.

    Not while exporting, not once nothing was sent, and not after a day
    without news. A finished request is not, except one WG could not finish,
    for an hour: WG's "Try again" moves it back to preparing.
    """

    if request.get("local") in (EXPORTING, NOT_SENT):
        return False
    moment = time.time() if now is None else float(now)
    silent = moment - float(request.get("heardAt") or moment)
    if silent > FOLLOW_LIMIT_SECONDS:
        return False
    if terminal(request):
        record = _wg_record(request) or {}
        return record.get("state") == "couldNotFinish" and silent < RETRY_WATCH_SECONDS
    return True


def outcome(request: Mapping[str, Any]) -> str:
    """One word for diagnostics: WG's state, its receipt, or the local phase."""

    status = request.get("status")
    if isinstance(status, Mapping) and isinstance(status.get("state"), str):
        return str(status["state"])
    receipt = request.get("receipt")
    if isinstance(receipt, Mapping) and isinstance(receipt.get("outcome"), str):
        return str(receipt["outcome"])
    return str(request.get("local") or EXPORTING)


def repeat_question(request: Mapping[str, Any]) -> str:
    """The question for a second press of the same model while its request is under way."""

    name = _name(request.get("documentName")) or "this model"
    verb = "Solve" if request.get("kind") == SOLVE else "Send"
    has_it = (request.get("status") is not None or request.get("receipt") is not None
              or request.get("local") in (TAKEN, TAKEN_UNCONFIRMED))
    if has_it and verb == "Solve":
        said = f"{APP_NAME} is already working on {name}."
    elif has_it:
        said = f"{APP_NAME} already has {name}."
    else:
        said = f"{name} is still waiting for {APP_NAME} to pick it up."
    return (f"{said} {verb} it again?\n\n"
            f"Yes: {verb} again.\nNo: Show its progress.")


def view(request: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """What the palette shows: ``{headline, detail, notes, options, actions,
    busy, progress, terminal, diagnostics}``.

    ``busy`` asks for an indeterminate bar, ``progress`` (0..1) for a filled
    one. ``diagnostics`` is the technical correlation, shown only under the
    palette's collapsed Diagnostics. Never raises.
    """

    try:
        return _view(request, time.time() if now is None else float(now))
    except Exception:  # noqa: BLE001 - a user always gets a sentence
        return dict(wglink_protocol.request_status_fallback_text(), notes=[], busy=False,
                    progress=None, terminal=True, diagnostics={})


def _view(request: Mapping[str, Any], now: float) -> dict[str, Any]:
    name = _name(request.get("documentName"))
    shown, inner = name or "This model", name or "this model"
    solve = request.get("kind") == SOLVE
    verb = "Solve" if solve else "Send"
    again = dict(_SOLVE_AGAIN if solve else _SEND_AGAIN)
    detail: str | None = None
    options: list[str] = []
    actions: list[dict[str, str]] = [dict(_OPEN_WG)]
    progress: float | None = None
    busy = False
    record = _wg_record(request)
    # Quiet once looked at, or by age alone: a request no longer looked at
    # (a day without news) still reads as quiet, never as busy.
    if request.get("quiet") or is_quiet(request, now):
        headline = f"{APP_NAME} has not reported on {inner} for a while."
        detail = f"Open {APP_NAME} to check it, or {verb.lower()} it again."
        actions.append(again)
    elif _old_wg_solve(request):
        headline = f"{shown} · Accepted. Follow progress in {APP_NAME}."
    elif record is not None:
        text = wglink_protocol.request_status_text(record)
        headline, detail = text["headline"], text["detail"]
        options, actions = list(text["options"]), [dict(action) for action in text["actions"]]
        state = record.get("state")
        fraction = (record.get("progress") or {}).get("fraction") if state == "solving" else None
        progress = float(fraction) if isinstance(fraction, (int, float)) else None
        busy = state in _WG_BUSY and progress is None and not terminal(request)
    else:
        local = request.get("local")
        if local == EXPORTING:
            headline = f"{shown} · {verb} · Preparing the export…"
            actions, busy = [], True
        elif local == SUBMITTED:
            headline = f"{shown} · Sent to {APP_NAME}. Waiting for it to pick up the model."
            busy = True
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
            actions.append(again)
        elif local == TAKEN:
            headline = (f"{shown} · {APP_NAME} picked it up. Follow progress in {APP_NAME}."
                        if solve else f"{shown} · {APP_NAME} has it.")
        elif local == NOT_SENT:
            headline = f"{shown} was not sent to {APP_NAME}."
            actions = [again]
        else:
            text = wglink_protocol.request_status_fallback_text()
            headline, actions = text["headline"], [dict(action) for action in text["actions"]]
    return {
        "headline": headline,
        "detail": detail,
        "notes": [str(note) for note in request.get("notes") or []],
        "options": options,
        "actions": actions,
        "busy": busy,
        "progress": progress,
        "terminal": terminal(request),
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
        "quiet": request.get("quiet") or None,
        "seq": status.get("seq"),
        "updatedAt": status.get("updatedAt"),
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


def load(ipc: Path, *, now: float | None = None) -> dict[str, Any] | None:
    """The request a previous session was following, or None.

    None too for a record whose last news is older than
    ``RECORD_MAX_AGE_SECONDS``. A request whose export never finished cannot
    have reached WG: it reads as not sent. Nothing is ever submitted again
    from here.
    """

    found = wglink_inbox._read_json(Path(ipc) / RECORD_FILENAME)
    if (not isinstance(found, dict) or found.get("schema") != RECORD_SCHEMA
            or not isinstance(found.get("requestId"), str)
            or found.get("local") not in _LOCAL
            or not isinstance(found.get("capabilities"), dict)):
        return None
    moment = time.time() if now is None else float(now)
    heard = found.get("heardAt") or found.get("submittedAt") or found.get("pressedAt")
    if not isinstance(heard, (int, float)) or moment - float(heard) > RECORD_MAX_AGE_SECONDS:
        return None
    found["heardAt"] = float(heard)
    if found.get("local") == EXPORTING:
        found["local"] = NOT_SENT
    if found.get("status") is not None:
        found["status"] = wglink_protocol.validate_request_status(
            found["status"], request_id=found["requestId"])
    if not isinstance(found.get("notes"), list):
        found["notes"] = []
    found["quiet"] = bool(found.get("quiet"))
    return found


class RequestTracker:
    """The one request the palette follows; shared by the main thread and the poll.

    Plain data under a lock. ``replace`` takes an update only for the request
    it holds, so a late answer for an earlier request never changes a newer
    one. A ``save`` passed in runs under the same lock, so the record on disk
    is always the one held, whichever thread wrote last.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._request: dict[str, Any] | None = None
        self.visible = False

    def current(self) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._request)

    def begin(self, request: Mapping[str, Any] | None,
              save: Callable[[dict[str, Any]], object] | None = None) -> None:
        with self._lock:
            self._request = copy.deepcopy(dict(request)) if request is not None else None
            if save is not None and self._request is not None:
                save(copy.deepcopy(self._request))

    def replace(self, request: Mapping[str, Any],
                save: Callable[[dict[str, Any]], object] | None = None
                ) -> tuple[dict[str, Any] | None, bool]:
        """``(previous, taken)``: the update is taken only for the request held."""

        with self._lock:
            previous = copy.deepcopy(self._request)
            if previous is None or previous.get("requestId") != request.get("requestId"):
                return previous, False
            self._request = copy.deepcopy(dict(request))
            if save is not None:
                save(copy.deepcopy(self._request))
            return previous, True

    def poll_interval(self, now: float | None = None) -> float | None:
        """Seconds to the next look, or None when there is nothing to look at."""

        request = self.current()
        moment = time.time() if now is None else float(now)
        if request is None or not following(request, moment):
            return None
        silent = moment - float(request.get("heardAt") or moment)
        if request.get("quiet") or terminal(request) or silent >= QUIET_SECONDS:
            return POLL_QUIET_SECONDS
        return POLL_VISIBLE_SECONDS if self.visible else POLL_HIDDEN_SECONDS


__all__ = [
    "ACTION_IDS", "EXPORTING", "NOT_SENT", "NOT_TAKEN", "POLL_HIDDEN_SECONDS",
    "POLL_QUIET_SECONDS", "POLL_VISIBLE_SECONDS", "QUIET_SECONDS", "RECORD_FILENAME",
    "is_quiet", "replaces",
    "RequestTracker", "SEND", "SEND_AGAIN_ID", "SOLVE", "SUBMITTED", "TAKEN",
    "TAKEN_UNCONFIRMED", "UNCONFIRMED", "diagnostics", "following", "load", "new_request",
    "not_sent", "outcome", "poll", "repeat_question", "request_path", "save", "submitted",
    "terminal", "view", "withdraw",
]
