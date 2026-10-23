"""Write WG's request inbox and read its capabilities and acknowledgements.

File delivery follows WG's docs/architecture/CAD-OPERATIONS.md contract.
Send and Solve each publish one request; Fusion-bound requests and the
heartbeat retain delivery version 3. This module does not inspect Fusion.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Mapping

if __package__:
    from . import wglink_protocol
else:
    import wglink_protocol


# The delivery version this add-in speaks, published in its heartbeat. WG
# refuses an add-in that reports anything lower. The WG-bound inbox has its
# own minimum, WG_REQUEST_SCHEMA_VERSION, checked alongside this value.
DELIVERY_VERSION = wglink_protocol.FUSION_REQUEST_DELIVERY_VERSION
# WG's advertisement of the delivery versions it reads.
CAPABILITIES_FILENAME = "wg-capabilities.json"
CAPABILITIES_SCHEMA_VERSION = 1
SOLVE_COMMAND_DELIVERY = "solveCommandDelivery"
# WG writes one acknowledgement file per request when it advertises this as an
# integer of at least 1: ``<ipc>/.wg-solve-acks/<requestId>.json``.
SOLVE_ACKNOWLEDGEMENT = "solveAcknowledgement"
ACK_DIRECTORY = ".wg-solve-acks"
ACK_SCHEMA_VERSION = 1
ACK_ACCEPTED = "accepted"
ACK_REFUSED = "refused"
# How often the acknowledgement is looked for, and how long after the request
# is written the add-in waits for one once WG has taken the file. An
# acknowledgement can lag the file's disappearance by about 30 delivery passes.
ACK_POLL_SECONDS = 5.0
ACK_WAIT_SECONDS = 120.0
FUSION_REQUEST_DELIVERY = "fusionRequestDelivery"
# WG reads returns that require ``source-identity-v1`` when it advertises this
# as an integer of at least 1. The add-in declares the feature only then: a WG
# that does not advertise it refuses the bundle as an unknown required feature.
SOURCE_IDENTITY = "sourceIdentity"
AUTOMATIC_DOMAIN = "automaticDomain"
DOCUMENT_UP = "documentUp"
# Optional: a WG that solves declared FEM regions (fem-regions-v1).
FEM_REGIONS = "femRegions"
# The schema-3 solve writer retained for the WG packaged-pair contract.
# New commands use WG_REQUEST_SCHEMA_VERSION; Fusion-bound delivery stays 3.
REQUEST_SCHEMA_VERSION = wglink_protocol.SOLVE_COMMAND_SCHEMA_VERSION
# The WG request inbox (M1 transfer contract, C2/C3): one file per Send or
# Solve, the two differing only in ``kind``. Written only for a WG whose
# ``solveCommandDelivery`` is at least this, because a schema-3 reader ignores
# ``kind`` and would start a solve for a plain Send.
WG_REQUEST_SCHEMA_VERSION = wglink_protocol.WG_REQUEST_SCHEMA_VERSION
KIND_RECEIVE_SNAPSHOT = "receive_snapshot"
KIND_PREPARE_AND_SOLVE = "prepare_and_solve"
WG_REQUEST_KINDS = (KIND_RECEIVE_SNAPSHOT, KIND_PREPARE_AND_SOLVE)
# One file per solve command, one per WG request.
SOLVE_REQUESTS_DIRECTORY = ".wg-solve-requests"
# An id that becomes part of a file name: this add-in's command ids.
_PLAIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")

# How long a request may wait untaken before the user is told. WG takes a file
# on its next pass while it runs, so a minute is far past normal. The request
# is never dropped or rerouted.
SOLVE_PICKUP_NOTICE_SECONDS = 60.0
REQUEST_NOT_TAKEN_MESSAGE = (
    "Waveguide Generator has not picked up the request from Fusion for a minute. "
    "Either WG is closed, or it is older than this Waveguide Generator add-in, or it is not "
    "collecting requests from Fusion. Open Waveguide Generator (update it if an "
    "older version is running). The request waits in the WGLink folder and is "
    "handled once WG takes it."
)
WG_NOT_COLLECTING_MESSAGE = (
    "Waveguide Generator is not collecting requests from Fusion: it has not said "
    "that it reads them. Either WG is not running with this WGLink folder, or "
    "its CAD Link request handling is switched off. Start Waveguide Generator, "
    "check its WGLink folder, then try again."
)
WG_OUTDATED_MESSAGE = (
    "This Waveguide Generator does not advertise the delivery capabilities this "
    "Waveguide Generator add-in needs (WG request inbox 4 and Fusion request delivery 3). "
    "Update Waveguide Generator; it installs the matching Fusion add-in."
)
WG_FUSION_OUTDATED_MESSAGE = (
    "This Waveguide Generator does not advertise the delivery capabilities this "
    "Waveguide Generator add-in needs (Fusion request delivery 3 under capability schema 1). "
    "Update Waveguide Generator; it installs the matching Fusion add-in."
)


# The same three causes, for requests a restart found still waiting (A8 brief 5):
# the pickup timer that would have said so did not survive the restart.
def requests_not_taken_at_startup_message(count: int) -> str:
    return (
        f"{count} request(s) from Fusion to Waveguide Generator that were waiting "
        "when the Waveguide Generator add-in started have not been picked up. Either WG is closed, or it "
        "is older than this Waveguide Generator add-in, or it is not collecting requests from "
        "Fusion. Open Waveguide Generator (update it if an older version is "
        "running). The requests wait in the WGLink folder and are handled once WG "
        "takes them."
    )


def written_to_inbox_text(request_id: str) -> str:
    return (
        f"Written to WG's inbox (request {str(request_id)[:8]}) -- waiting for WG to "
        "accept it. WG says here when it has."
    )


ACK_ACCEPTED_TEXT = (
    "WG accepted the request. Progress and results appear in Waveguide Generator."
)
ACK_UNCONFIRMED_TEXT = (
    "WG took the request but has not confirmed it. Check WG's CAD Link panel "
    "before sending this model again."
)


def wg_acknowledges(ipc_folder: Path) -> bool:
    """Whether WG advertises that it writes an acknowledgement for each request."""

    payload = _read_json(Path(ipc_folder) / CAPABILITIES_FILENAME)
    if not isinstance(payload, Mapping):
        return False
    schema = payload.get("schemaVersion")
    if isinstance(schema, bool) or schema != CAPABILITIES_SCHEMA_VERSION:
        return False
    if SOLVE_COMMAND_DELIVERY not in payload:
        return False
    value = payload.get(SOLVE_ACKNOWLEDGEMENT)
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def read_acknowledgement(
    ipc_folder: Path,
    request_id: str,
    *,
    kind: str | None = None,
    manifest_sha256: str | None = None,
) -> tuple[str, str | None] | None:
    """WG's outcome for one request: ``(outcome, reason)``, or None for "not yet".

    Opens, reads and closes the file at once (a held handle delays WG's own
    replace on Windows). A missing folder or file, a parse failure, another
    schema version, an outcome this add-in does not know, ids that are not this
    request's, or a ``kind`` / ``manifestSha256`` that names another request
    all read as "not yet". Every other field is ignored.
    """

    if not _PLAIN_ID.fullmatch(str(request_id)):
        return None
    payload = _read_json(Path(ipc_folder) / ACK_DIRECTORY / f"{request_id}.json")
    if not isinstance(payload, Mapping):
        return None
    schema = payload.get("schemaVersion")
    if isinstance(schema, bool) or schema != ACK_SCHEMA_VERSION:
        return None
    for field in ("commandId", "operationId"):
        if payload.get(field, request_id) != request_id:
            return None
    if kind is not None and payload.get("kind", kind) != kind:
        return None
    if manifest_sha256 is not None and payload.get("manifestSha256", manifest_sha256) != manifest_sha256:
        return None
    outcome = payload.get("outcome")
    if outcome not in (ACK_ACCEPTED, ACK_REFUSED):
        return None
    reason = payload.get("reason")
    return outcome, reason if isinstance(reason, str) and reason.strip() else None


class WgOutdatedError(RuntimeError):
    """WG does not read this add-in's delivery version; nothing was written."""


class WgNotCollectingError(RuntimeError):
    """WG advertises no request inbox at all; nothing was written."""


class WriteOutcomeUnknown(OSError):
    """The rename into the final name raised: the file may or may not have landed.

    Everything before the rename -- staging, writing, syncing -- failing is a
    known failure: the final name was never touched. The rename itself raising
    is not, because a rename can land and still report an error, and WG may
    take the file the moment it lands (M1 transfer contract C5, Amendment A1).
    """


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _write_json_atomically(path: Path, payload: Mapping[str, Any]) -> Path:
    """Stage under a name that starts with "." and ends in .tmp, then rename.

    Neither WG nor this add-in takes such a name, so a reader sees the file
    whole or not at all.
    """

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name.lstrip('.')}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(temporary, path)
        except OSError as exc:
            raise WriteOutcomeUnknown(*exc.args) from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # A held staging file must not replace the error that matters;
            # its hidden name is never read as a request.
            pass
    return path


def wg_delivery_version(ipc_folder: Path, name: str) -> int:
    """The delivery version WG advertises for ``name``; 1 when it names none.

    The capability file's reading rules: fields this add-in does not know are
    ignored, and a missing or unreadable file, a ``schemaVersion`` it does not
    know, or a value that is not an integer of at least 2 all read as 1.
    """

    payload = _read_json(Path(ipc_folder) / CAPABILITIES_FILENAME)
    if not isinstance(payload, Mapping):
        return 1
    schema = payload.get("schemaVersion")
    if (
        isinstance(schema, bool)
        or not isinstance(schema, int)
        or schema != CAPABILITIES_SCHEMA_VERSION
    ):
        return 1
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 2:
        return 1
    return value


def wg_source_identity(ipc_folder: Path | None) -> bool:
    """Whether WG advertises that it reads ``source-identity-v1`` returns.

    The same reading rules as the delivery versions: a missing or unreadable
    file, a ``schemaVersion`` this add-in does not know, or a value that is not
    an integer of at least 1 (a boolean is not one) all mean "do not declare it".
    """

    if ipc_folder is None:
        return False
    payload = _read_json(Path(ipc_folder) / CAPABILITIES_FILENAME)
    if not isinstance(payload, Mapping):
        return False
    schema = payload.get("schemaVersion")
    if (
        isinstance(schema, bool)
        or not isinstance(schema, int)
        or schema != CAPABILITIES_SCHEMA_VERSION
    ):
        return False
    value = payload.get(SOURCE_IDENTITY)
    return not isinstance(value, bool) and isinstance(value, int) and value >= 1


def wg_manifest_feature(ipc_folder: Path | None, name: str) -> bool:
    """Whether WG advertises a version-1 optional return-manifest feature."""

    if ipc_folder is None:
        return False
    payload = _read_json(Path(ipc_folder) / CAPABILITIES_FILENAME)
    if not isinstance(payload, Mapping):
        return False
    schema = payload.get("schemaVersion")
    if isinstance(schema, bool) or schema != CAPABILITIES_SCHEMA_VERSION:
        return False
    value = payload.get(name)
    return not isinstance(value, bool) and isinstance(value, int) and value >= 1


def wg_speaks_delivery_version(ipc_folder: Path) -> bool:
    """WG must read schema-4 Send/Solve and publish delivery-3 Fusion requests.

    Ignore product version strings and optional features. Unknown capability
    fields are additive; booleans, strings and unreadable advertisements do
    not establish compatibility.
    """

    folder = Path(ipc_folder)
    return (
        wg_delivery_version(folder, SOLVE_COMMAND_DELIVERY) >= WG_REQUEST_SCHEMA_VERSION
        and wg_delivery_version(folder, FUSION_REQUEST_DELIVERY) >= DELIVERY_VERSION
    )


def _require_capabilities(ipc_folder: Path, outdated_message: str) -> Mapping[str, Any]:
    """Separate an unavailable advertisement from an unsupported schema."""

    payload = _read_json(Path(ipc_folder) / CAPABILITIES_FILENAME)
    if payload is None:
        raise WgNotCollectingError(WG_NOT_COLLECTING_MESSAGE)
    if not isinstance(payload, Mapping):
        raise WgOutdatedError(outdated_message)
    schema = payload.get("schemaVersion")
    if (
        isinstance(schema, bool)
        or not isinstance(schema, int)
        or schema != CAPABILITIES_SCHEMA_VERSION
    ):
        raise WgOutdatedError(outdated_message)
    return payload


def require_fusion_delivery(ipc_folder: Path) -> None:
    """Require only WG's Fusion-bound channel, independent of its Solve consumer."""

    payload = _require_capabilities(ipc_folder, WG_FUSION_OUTDATED_MESSAGE)
    value = payload.get(FUSION_REQUEST_DELIVERY)
    if isinstance(value, bool) or not isinstance(value, int) or value < DELIVERY_VERSION:
        raise WgOutdatedError(WG_FUSION_OUTDATED_MESSAGE)


def require_wg_delivery(ipc_folder: Path) -> None:
    """Require WG's Send/Solve inbox and Fusion delivery before exporting."""

    payload = _require_capabilities(ipc_folder, WG_OUTDATED_MESSAGE)
    if SOLVE_COMMAND_DELIVERY not in payload:
        raise WgNotCollectingError(WG_NOT_COLLECTING_MESSAGE)
    for name, minimum in (
        (SOLVE_COMMAND_DELIVERY, WG_REQUEST_SCHEMA_VERSION),
        (FUSION_REQUEST_DELIVERY, DELIVERY_VERSION),
    ):
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise WgOutdatedError(WG_OUTDATED_MESSAGE)


def write_solve_request(
    ipc_folder: Path,
    *,
    command_id: str,
    return_id: str,
    bundle_path: Path,
    workspace_root: Path,
    requested_at: datetime | None = None,
) -> Path:
    """Ask WG to ingest one exact return bundle and start a solve.

    Deliberately separate from ``wgreturn.json``: that manifest is immutable
    geometry evidence which WG re-reads whenever it re-lists the workspace, so
    an intent flag inside it would be re-observed and re-solved. A command with
    its own id can be spent exactly once.

    The manifest hash is recorded here, after the bundle has been published, so
    WG can refuse a bundle that changed between the publish and this write.

    The command is written as its own file,
    ``.wg-solve-requests/<commandId>.json``, so a second command never replaces
    one WG has not read yet. A WG that does not advertise this add-in's
    delivery version gets nothing: ``WgOutdatedError`` asks for it to be
    updated instead.
    """

    require_solve_delivery(ipc_folder)
    relative, manifest_sha256 = return_reference(bundle_path, workspace_root)
    return write_solve_request_fields(
        ipc_folder,
        command_id=command_id,
        return_id=return_id,
        bundle_relative=relative,
        manifest_sha256=manifest_sha256,
        requested_at=utc_timestamp(requested_at),
    )


def wg_request_capability(ipc_folder: Path) -> int | None:
    """What WG advertises for its request inbox: a version, or None for nothing.

    ``None`` -- no readable capability file, a schema this add-in does not
    know, or no ``solveCommandDelivery`` at all -- is "WG is not collecting
    requests" (C3): a WG whose consumer is off advertises nothing. A number
    below 4 is an older WG. ``wg_delivery_version`` folds both into 1; the two
    need different remedies, so this keeps them apart.
    """

    payload = _read_json(Path(ipc_folder) / CAPABILITIES_FILENAME)
    if not isinstance(payload, Mapping):
        return None
    schema = payload.get("schemaVersion")
    if isinstance(schema, bool) or schema != CAPABILITIES_SCHEMA_VERSION:
        return None
    if SOLVE_COMMAND_DELIVERY not in payload:
        return None
    return wg_delivery_version(Path(ipc_folder), SOLVE_COMMAND_DELIVERY)


def wg_request_payload(
    *,
    kind: str,
    command_id: str,
    return_id: str | None,
    bundle_relative: str,
    manifest_sha256: str,
    requested_at: str,
) -> dict[str, Any]:
    """One schema-4 WG request (C2). ``kind`` is the only Send/Solve difference.

    ``returnId`` is present, as a string that may be empty, on a Solve, and
    absent on a Send.
    """

    if kind not in WG_REQUEST_KINDS:
        raise ValueError(f"Not a WG request kind: {kind!r}.")
    if not _PLAIN_ID.fullmatch(str(command_id)):
        raise ValueError(
            f"A request id must be a plain file name, got {command_id!r}."
        )
    payload: dict[str, Any] = {
        "schemaVersion": WG_REQUEST_SCHEMA_VERSION,
        "target": "waveguide-generator",
        "kind": kind,
        "commandId": str(command_id),
        "operationId": str(command_id),
        "bundlePath": str(bundle_relative),
        "manifestSha256": str(manifest_sha256),
        "requestedAt": str(requested_at),
    }
    if kind == KIND_PREPARE_AND_SOLVE:
        payload["returnId"] = str(return_id or "")
    return payload


#: A request file is a few hundred bytes; anything larger is not one of ours.
_REQUEST_READ_LIMIT = 64 * 1024


def plan_wg_request(
    ipc_folder: Path,
    *,
    kind: str,
    command_id: str,
    return_id: str | None,
    bundle_relative: str,
    manifest_sha256: str,
    requested_at: str,
) -> tuple[Path, dict[str, Any]]:
    """Decide, once, the exact request file this command writes: where, and what.

    WG must advertise WG request inbox 4 and Fusion request delivery 3.
    No schema-3 fallback is used for new commands. The payload returned is the
    one every attempt writes, unchanged; a retry never re-decides the schema.
    """

    require_wg_delivery(ipc_folder)
    fields = {
        "command_id": command_id,
        "bundle_relative": bundle_relative,
        "manifest_sha256": manifest_sha256,
        "requested_at": requested_at,
    }
    payload = wg_request_payload(kind=kind, return_id=return_id, **fields)
    return solve_request_path(ipc_folder, payload["commandId"]), payload


def publish_wg_request(path: Path, payload: Mapping[str, Any]) -> Path:
    """Write a planned request atomically under its final name (one attempt)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    return _write_json_atomically(path, payload)


def write_wg_request(ipc_folder: Path, **fields: Any) -> Path:
    """Plan and write one Send or Solve into WG's request inbox (one attempt).

    See :func:`plan_wg_request` for the versioning rule. Nothing is written on
    a refusal.
    """

    path, payload = plan_wg_request(ipc_folder, **fields)
    return publish_wg_request(path, payload)


def _same_typed(found: Any, expected: Mapping[str, Any]) -> bool:
    """Equal key for key, value for value and *type* for type.

    Python's ``==`` calls ``4.0`` and ``True`` equal to ``4`` and ``1``; WG
    does not read them as a schema, so neither may confirm one.
    """

    return (
        isinstance(found, dict)
        and set(found) == set(expected)
        and all(
            type(found[key]) is type(value) and found[key] == value
            for key, value in expected.items()
        )
    )


def landed_request(path: Path, payload: Mapping[str, Any]) -> Path | None:
    """``path`` if it holds exactly ``payload`` -- the request this command planned.

    Asked only when a write's outcome is unknown (C5, Amendment A1). "Sent" may
    be reported only for a file equal to the complete request that was
    attempted: every protocol field, ``schemaVersion``, ``target``, ``kind``
    and ``returnId`` presence and value, each of the same JSON type. The
    schema-3 Solve file confirms only when schema 3 was what this command
    planned, never for a Send. A file larger than any request is not read
    whole and is not confirmation. None otherwise.
    """

    try:
        with open(path, "rb") as stream:
            raw = stream.read(_REQUEST_READ_LIMIT + 1)
    except OSError:
        return None
    if len(raw) > _REQUEST_READ_LIMIT:
        return None
    try:
        found = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not _same_typed(found, payload):
        return None
    return path



def untaken_requests(
    ipc_folder: Path,
    *,
    limit: int,
    now: float | None = None,
) -> list[tuple[Path, float]]:
    """WGLink's request files still in WG's inbox, with how long each has waited.

    Bounded: at most ``limit`` directory entries are looked at, and a file is
    read only if it is small. Only files under their final name count -- WG
    claims a file by renaming it to a hidden name, and staging names start
    with "." too -- and only those that parse as a WGLink request to WG under
    their own id. Reads the inbox and nothing else. The age is from the file's
    modification time, so a request converted from the outbox at this very
    start-up is young even though its ``requestedAt`` is old.
    """

    folder = Path(ipc_folder).expanduser().resolve() / SOLVE_REQUESTS_DIRECTORY
    moment = time.time() if now is None else float(now)
    found: list[tuple[Path, float]] = []
    try:
        entries = os.scandir(folder)
    except OSError:
        return found
    with entries:
        for examined, entry in enumerate(entries):
            if examined >= limit:
                break
            name = entry.name
            if name.startswith(".") or not name.endswith(".json"):
                continue
            stem = name[: -len(".json")]
            if not _PLAIN_ID.fullmatch(stem):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                status = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if status.st_size > _REQUEST_READ_LIMIT:
                continue
            payload = _read_json(Path(entry.path))
            if (
                not isinstance(payload, dict)
                or payload.get("target") != "waveguide-generator"
                or payload.get("commandId") != stem
                or payload.get("schemaVersion")
                not in (REQUEST_SCHEMA_VERSION, WG_REQUEST_SCHEMA_VERSION)
            ):
                continue
            found.append((Path(entry.path), max(0.0, moment - status.st_mtime)))
    return found

def require_solve_delivery(ipc_folder: Path) -> None:
    """Require both channels for the packaged-pair schema-3 solve writer."""

    require_wg_delivery(ipc_folder)


def utc_timestamp(moment: datetime | None = None) -> str:
    """A request's ``requestedAt``: UTC, whole seconds, ``Z``."""

    return (
        (moment or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def return_reference(bundle_path: Path, workspace_root: Path) -> tuple[str, str]:
    """How a delivery names a published return: workspace-relative path, manifest hash.

    WG's operation digest is made of exactly these two strings.
    """

    bundle = Path(bundle_path).expanduser().resolve()
    root = Path(workspace_root).expanduser().resolve()
    try:
        relative = bundle.relative_to(root)
    except ValueError as exc:
        raise OSError(
            f"Return bundle {bundle} is not inside the WGLink folder {root}."
        ) from exc
    digest = hashlib.sha256((bundle / "wgreturn.json").read_bytes()).hexdigest()
    return relative.as_posix(), f"sha256:{digest}"


def solve_request_path(ipc_folder: Path, command_id: str) -> Path:
    """Where the v3 solve file for ``command_id`` is written (and taken from by WG)."""

    return Path(ipc_folder).expanduser().resolve() / SOLVE_REQUESTS_DIRECTORY / f"{command_id}.json"


def solve_request_payload(
    *,
    command_id: str,
    return_id: str,
    bundle_relative: str,
    manifest_sha256: str,
    requested_at: str,
) -> dict[str, Any]:
    """The schema-3 solve file: no ``kind``, because a schema-3 reader means Solve."""

    return {
        "schemaVersion": REQUEST_SCHEMA_VERSION,
        "target": "waveguide-generator",
        "commandId": str(command_id),
        "operationId": str(command_id),
        "returnId": str(return_id),
        "bundlePath": str(bundle_relative),
        "manifestSha256": str(manifest_sha256),
        "requestedAt": str(requested_at),
    }


def write_solve_request_fields(
    ipc_folder: Path,
    *,
    command_id: str,
    return_id: str,
    bundle_relative: str,
    manifest_sha256: str,
    requested_at: str,
) -> Path:
    """Write the v3 solve file from a return reference already taken."""

    require_solve_delivery(ipc_folder)
    if not _PLAIN_ID.fullmatch(str(command_id)):
        raise ValueError(
            f"A solve command id must be a plain file name, got {command_id!r}."
        )
    payload = solve_request_payload(
        command_id=command_id,
        return_id=return_id,
        bundle_relative=bundle_relative,
        manifest_sha256=manifest_sha256,
        requested_at=requested_at,
    )
    path = solve_request_path(ipc_folder, payload["commandId"])
    path.parent.mkdir(parents=True, exist_ok=True)
    return _write_json_atomically(path, payload)
