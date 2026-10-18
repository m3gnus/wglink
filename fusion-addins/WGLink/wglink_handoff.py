"""Read, order and claim explicit WG Insert/Update handoffs and return requests.

No export bundles are watched. Fusion mutations belong to WGLink.py on the
main thread; this module only reads and claims request files.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
from typing import Any, Mapping, TypeVar
import uuid

if __package__:
    from .wglink_inbox import _PLAIN_ID, _read_json
    from . import wglink_protocol
else:
    from wglink_inbox import _PLAIN_ID, _read_json
    import wglink_protocol

REQUEST_SCHEMA_VERSION = wglink_protocol.FUSION_REQUEST_DELIVERY_VERSION
RETURN_REQUESTS_DIRECTORY = ".fusion-return-requests"
HANDOFFS_DIRECTORY = ".fusion-handoffs"
SEQUENCE_FIELD = "deliverySequence"
LEGACY_HANDOFF_FILENAME = ".fusion-handoff.json"
LEGACY_RETURN_REQUEST_FILENAME = ".fusion-return-request.json"
CLAIM_PREFIX = ".wglink-claim-"


@dataclass(frozen=True)
class PendingHandoff:
    """A completed WG export that the user explicitly sent to Fusion.

    Read from its own file, ``.fusion-handoffs/<requestId>.json``. The request
    id is its operation id, and the request must be claimed (``claim_request``)
    before it runs. An update names its exact target -- document and instance
    -- and the baseline it expects; an insert names neither instance nor
    baseline.
    """

    marker_path: Path
    request_id: str
    delivery_sequence: int
    bundle_path: str
    bundle_id: str
    export_id: str
    sequence: str
    design_id: str
    expected_document_id: str
    expected_instance_id: str
    expected_return_state_hash: str
    requested_at: str
    destination: dict[str, str] | None

    @property
    def operation_id(self) -> str:
        return self.request_id

    @property
    def exact_target(self) -> tuple[str, str] | None:
        """``(document_id, instance_id)`` for an update, None for an insert."""

        if self.expected_document_id and self.expected_instance_id:
            return self.expected_document_id, self.expected_instance_id
        return None


@dataclass(frozen=True)
class PendingReturnRequest:
    """A request from WG to export the active Fusion document back to WG."""

    marker_path: Path
    request_id: str
    delivery_sequence: int
    session_id: str
    design_id: str
    document_id: str
    instance_id: str
    expected_return_state_hash: str

    @property
    def operation_id(self) -> str:
        return self.request_id


_Pending = TypeVar("_Pending", PendingHandoff, PendingReturnRequest)


def _sequence(payload: Any) -> int | None:
    value = payload.get(SEQUENCE_FIELD) if isinstance(payload, Mapping) else None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


def _request_identity(payload: Any) -> tuple[str, int] | None:
    """``(request_id, sequence)`` of a request file WG wrote, or None.

    WG writes schema version 3, the request id as the operation id, and a
    positive sequence. A file without all of them is not one of this delivery
    version's requests, and is left where it is.
    """

    if (
        not isinstance(payload, Mapping)
        or payload.get("schemaVersion") != REQUEST_SCHEMA_VERSION
        or payload.get("target") != "fusion360"
    ):
        return None
    request_id = payload.get("requestId")
    sequence = _sequence(payload)
    if (
        not isinstance(request_id, str)
        or not _PLAIN_ID.fullmatch(request_id)
        or payload.get("operationId") != request_id
        or sequence is None
    ):
        return None
    return request_id, sequence


def read_return_request(
    marker_path: Path, *, session_id: str
) -> PendingReturnRequest | None:
    """Read one return request. It runs only in the add-in session it names."""

    return return_request_from_document(
        _read_json(marker_path), session_id=session_id, marker_path=marker_path
    )


def return_request_from_document(
    payload: Any, *, session_id: str, marker_path: Path
) -> PendingReturnRequest | None:
    """Parse one return request read from its version 3 file."""

    identity = _request_identity(payload)
    if identity is None or payload.get("sessionId") != session_id:
        return None
    return PendingReturnRequest(
        marker_path=marker_path,
        request_id=identity[0],
        delivery_sequence=identity[1],
        session_id=session_id,
        design_id=str(payload.get("designId") or ""),
        document_id=str(payload.get("documentId") or ""),
        instance_id=str(payload.get("instanceId") or ""),
        expected_return_state_hash=str(payload.get("expectedReturnStateHash") or ""),
    )


def read_pending_handoff(
    marker_path: Path, *, bundle_root: Path | None = None
) -> PendingHandoff | None:
    """Read one handoff without trusting an arbitrary bundle path."""

    return handoff_from_document(
        _read_json(marker_path), bundle_root=bundle_root, marker_path=marker_path
    )


def handoff_from_document(
    payload: Any, *, bundle_root: Path | None, marker_path: Path
) -> PendingHandoff | None:
    """Parse a version 3 handoff and validate its path against the bundle root."""

    identity = _request_identity(payload)
    if identity is None:
        return None
    bundle_id = payload.get("bundleId")
    export_id = payload.get("exportId")
    bundle_value = payload.get("bundlePath")
    if not all(isinstance(value, str) and value for value in (bundle_id, export_id, bundle_value)):
        return None
    try:
        allowed_bundle_root = (bundle_root or marker_path.parent).resolve()
        bundle_path = Path(str(bundle_value)).expanduser().absolute()
    except OSError:
        return None
    if bundle_path.parent.resolve() != allowed_bundle_root:
        return None
    # Keep the explicit basename (including a missing path) for core to refuse
    # visibly. Resolving it here would erase a symlink before the bundle reader
    # can reject it. Never substitute a discovered or latest bundle.
    sequence = payload.get("sequence")
    raw_destination = payload.get("destination")
    destination = None
    if isinstance(raw_destination, Mapping):
        kind = raw_destination.get("kind")
        value = raw_destination.get("value")
        if isinstance(kind, str) and isinstance(value, str) and kind and value:
            destination = {"kind": kind, "value": value}
    return PendingHandoff(
        marker_path=marker_path,
        request_id=identity[0],
        delivery_sequence=identity[1],
        bundle_path=str(bundle_path),
        bundle_id=str(bundle_id),
        export_id=str(export_id),
        sequence="" if sequence is None else str(sequence),
        design_id=str(payload.get("designId") or ""),
        expected_document_id=str(payload.get("expectedDocumentId") or ""),
        expected_instance_id=str(payload.get("expectedInstanceId") or ""),
        expected_return_state_hash=str(payload.get("expectedReturnStateHash") or ""),
        requested_at=str(payload.get("requestedAt") or ""),
        destination=destination,
    )


def _remove_claim(path: Path) -> bool:
    """Delete a claim this add-in made, and nothing else."""

    if not path.name.startswith(CLAIM_PREFIX):
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def acknowledge_return_request(request: PendingReturnRequest) -> bool:
    """Retire a return request that ran: delete its claim."""

    return _remove_claim(request.marker_path)


def acknowledge_handoff(handoff: PendingHandoff) -> bool:
    """Retire a handoff that ran: delete its claim."""

    return _remove_claim(handoff.marker_path)


def _request_files(directory: Path) -> list[Path]:
    """A request folder's requests: ``*.json`` names not starting with "."."""

    try:
        return sorted(
            path for path in directory.iterdir()
            if path.suffix == ".json" and not path.name.startswith(".") and path.is_file()
        )
    except OSError:
        return []


def _requests_in_order(directory: Path) -> list[Path]:
    """Request files in ``deliverySequence`` order, then by name.

    A file without a valid sequence is not WG's, and is left out.
    """

    ordered: list[tuple[int, str, Path]] = []
    for path in _request_files(directory):
        sequence = _sequence(_read_json(path))
        if sequence is not None:
            ordered.append((sequence, path.name, path))
    return [path for _sequence_value, _name, path in sorted(ordered)]


def next_return_request(
    ipc_folder: Path, *, session_id: str
) -> PendingReturnRequest | None:
    """The next return request for this session, in sequence order, or None.

    The result must be claimed before it runs.
    """

    for path in _requests_in_order(Path(ipc_folder) / RETURN_REQUESTS_DIRECTORY):
        request = read_return_request(path, session_id=session_id)
        if request is not None:
            return request
    return None


def next_pending_handoff(
    ipc_folder: Path, *, bundle_root: Path | None
) -> PendingHandoff | None:
    """The next handoff, in sequence order, or None; claim it before it runs."""

    for path in _requests_in_order(Path(ipc_folder) / HANDOFFS_DIRECTORY):
        handoff = read_pending_handoff(path, bundle_root=bundle_root)
        if handoff is not None:
            return handoff
    return None


def discard_superseded_handoffs(
    ipc_folder: Path, *, bundle_root: Path | None
) -> list[str]:
    """Drop every unstarted update a newer update for the same target replaces.

    WG's supersession policy (CAD-OPERATIONS.md, "Ordering"): an update that has
    not started may be superseded only by a newer one for the same exact
    target, the same document and instance. WG withdraws the older file when it
    publishes the newer one; this covers the file it could not remove, on
    Windows while this add-in held it open. An insert is never superseded.
    Each superseded file is claimed first, so a request is either run or
    dropped, never both. Returns the request ids dropped.
    """

    handoffs = [
        handoff
        for path in _requests_in_order(Path(ipc_folder) / HANDOFFS_DIRECTORY)
        if (handoff := read_pending_handoff(path, bundle_root=bundle_root)) is not None
    ]
    newest: dict[tuple[str, str], PendingHandoff] = {}
    for handoff in handoffs:
        target = handoff.exact_target
        if target is not None:
            newest[target] = handoff
    dropped: list[str] = []
    for handoff in handoffs:
        target = handoff.exact_target
        if target is None or newest[target] is handoff:
            continue
        claimed = claim_request(handoff)
        if claimed is not None and _remove_claim(claimed.marker_path):
            dropped.append(handoff.request_id)
    return dropped


@dataclass(frozen=True)
class LeftoverClaim:
    """A claim an earlier session made and never finished.

    Claims are hidden from every listing, so nothing else will ever look at
    one again. ``channel`` is ``"handoff"`` or ``"returnRequest"``; the other
    fields are what reconciliation reads, empty when the claim is unreadable.
    """

    path: Path
    channel: str
    request_id: str
    export_id: str
    design_id: str
    expected_document_id: str
    expected_instance_id: str


def leftover_claims(ipc_folder: Path) -> list[LeftoverClaim]:
    """Every claim left in WG's request folders by an interrupted session."""

    found: list[LeftoverClaim] = []
    for directory, channel in (
        (HANDOFFS_DIRECTORY, "handoff"),
        (RETURN_REQUESTS_DIRECTORY, "returnRequest"),
    ):
        try:
            paths = sorted(
                path for path in (Path(ipc_folder) / directory).iterdir()
                if path.name.startswith(CLAIM_PREFIX) and path.suffix == ".json"
            )
        except OSError:
            continue
        for path in paths:
            payload = _read_json(path)
            fields = payload if isinstance(payload, Mapping) else {}
            found.append(
                LeftoverClaim(
                    path=path,
                    channel=channel,
                    request_id=str(fields.get("requestId") or ""),
                    export_id=str(fields.get("exportId") or ""),
                    design_id=str(fields.get("designId") or ""),
                    expected_document_id=str(
                        fields.get("expectedDocumentId") or fields.get("documentId") or ""
                    ),
                    expected_instance_id=str(
                        fields.get("expectedInstanceId") or fields.get("instanceId") or ""
                    ),
                )
            )
    return found


def remove_leftover_claim(claim: LeftoverClaim) -> bool:
    return _remove_claim(claim.path)


def outdated_wg_requests(ipc_folder: Path) -> list[str]:
    """Requests an older WG wrote: a single-slot marker, or another schema.

    None of them is ever run. The capability file is not consulted: a WG that
    speaks version 3 removes such files at its start, before it advertises, so
    one beside a "3" was written after that -- by an older WG running on the
    same data folder since a downgrade, which never rewrites the file.
    """

    folder = Path(ipc_folder)
    found = [
        name
        for name in (LEGACY_HANDOFF_FILENAME, LEGACY_RETURN_REQUEST_FILENAME)
        if (folder / name).is_file()
    ]
    for directory in (HANDOFFS_DIRECTORY, RETURN_REQUESTS_DIRECTORY):
        for path in _request_files(folder / directory):
            payload = _read_json(path)
            if isinstance(payload, Mapping) and payload.get("schemaVersion") != REQUEST_SCHEMA_VERSION:
                found.append(f"{directory}/{path.name}")
    return found


def claim_request(pending: _Pending) -> _Pending | None:
    """Take a request file by renaming it to a hidden claim.

    Returns the request with ``marker_path`` naming the claim, or None when
    the rename failed -- the file is gone, or on Windows WG still has it
    open -- in which case the next pass tries again. What the rename took is
    the request: a claim that no longer holds the same request is put back.
    """

    source = pending.marker_path
    claim = source.with_name(
        f"{CLAIM_PREFIX}{pending.request_id}-{uuid.uuid4().hex[:12]}.json"
    )
    try:
        os.rename(source, claim)
    except OSError:
        return None
    taken = _read_json(claim)
    if (
        not isinstance(taken, Mapping)
        or taken.get("requestId") != pending.request_id
        or _sequence(taken) != pending.delivery_sequence
    ):
        try:
            os.rename(claim, source)
        except OSError:
            pass
        return None
    return replace(pending, marker_path=claim)


def release_claim(pending: _Pending) -> bool:
    """Put back a claim whose prerequisites are not ready, without consuming it."""

    claim = pending.marker_path
    if not claim.name.startswith(CLAIM_PREFIX):
        return False
    source = claim.with_name(f"{pending.request_id}.json")
    try:
        os.rename(claim, source)
    except OSError:
        return False
    return True
