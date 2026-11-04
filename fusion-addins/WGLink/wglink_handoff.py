"""Read, order and claim explicit WG Insert/Update handoffs and return requests.

No export bundles are watched. Fusion mutations belong to WGLink.py on the
main thread; this module only reads and claims request files.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, TypeVar
import time
import uuid

if __package__:
    from .wglink_inbox import _PLAIN_ID, _read_json
    from . import wglink_protocol, wglink_status
else:
    from wglink_inbox import _PLAIN_ID, _read_json
    import wglink_protocol
    import wglink_status

REQUEST_SCHEMA_VERSION = wglink_protocol.FUSION_REQUEST_DELIVERY_VERSION
RETURN_REQUESTS_DIRECTORY = ".fusion-return-requests"
HANDOFFS_DIRECTORY = ".fusion-handoffs"
SEQUENCE_FIELD = "deliverySequence"
LEGACY_HANDOFF_FILENAME = ".fusion-handoff.json"
LEGACY_RETURN_REQUEST_FILENAME = ".fusion-return-request.json"
CLAIM_PREFIX = ".wglink-claim-"
# Opening a tab is an immediate action, not a mutation to replay after restart.
# The atomic request file records its publication time even for older publishers.
OPEN_DOCUMENT_MAX_AGE_SECONDS = 120


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


@dataclass(frozen=True)
class PendingOpenDocument:
    """Open a live cloud document, using the handoff's ordering and claim owner."""

    marker_path: Path
    request_id: str
    delivery_sequence: int
    session_id: str
    document_id: str

    @property
    def operation_id(self) -> str:
        return self.request_id


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

    return wglink_protocol.fusion_request_identity(payload)


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
) -> PendingHandoff | PendingOpenDocument | None:
    """Read one handoff without trusting an arbitrary bundle path."""

    return handoff_from_document(
        _read_json(marker_path), bundle_root=bundle_root, marker_path=marker_path
    )


def handoff_from_document(
    payload: Any, *, bundle_root: Path | None, marker_path: Path
) -> PendingHandoff | PendingOpenDocument | None:
    """Parse a version 3 handoff and validate its path against the bundle root."""

    identity = _request_identity(payload)
    if identity is None:
        return None
    if payload.get("kind") == wglink_protocol.OPEN_DOCUMENT_KIND:
        frame = wglink_protocol.validate_open_document_request(payload)
        if frame is None:
            return None
        return PendingOpenDocument(marker_path, identity[0], identity[1],
                                   frame["sessionId"], frame["documentId"])
    if payload.get("kind") is not None:
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


def acknowledge_handoff(handoff: PendingHandoff | PendingOpenDocument | UnusableHandoff) -> bool:
    """Retire a handled handoff: delete its claim."""

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


def has_requests(ipc: Path) -> bool:
    """File-only probe for explicit handoffs; never reads JSON or creates a folder."""
    return bool(_request_files(Path(ipc) / HANDOFFS_DIRECTORY))


def _request_file_state(path: Path) -> tuple[str, int, int]:
    stat = path.stat()
    return path.name, stat.st_mtime_ns, stat.st_size


def request_state(ipc: Path) -> tuple[tuple[str, int, int], ...]:
    """A cheap memo key: an unusable unchanged file gets no repeated wakeups."""
    state = []
    for path in _request_files(Path(ipc) / HANDOFFS_DIRECTORY):
        try:
            state.append(_request_file_state(path))
        except OSError:
            continue
    return tuple(state)


@dataclass(frozen=True)
class UnusableHandoff:
    """An unchanged file read successfully and proven not to be executable."""

    marker_path: Path
    request_id: str | None
    state: tuple[str, int, int]


_Pending = TypeVar("_Pending", PendingHandoff, PendingReturnRequest, PendingOpenDocument, UnusableHandoff)


def unusable_handoffs(ipc: Path, *, bundle_root: Path) -> list[UnusableHandoff]:
    """Prove unusability before suppressing wakeups; never memoize failed reads.

    WG names each atomic request file with its operation ID. Even malformed
    JSON can therefore report a correlated refusal using that filename. An
    unsafe filename is not an ID WG can publish, and supplies no correlation.
    """
    unusable = []
    for path in _request_files(Path(ipc) / HANDOFFS_DIRECTORY):
        try:
            before = _request_file_state(path)
            payload = _read_json(path, raise_io_errors=True)
            handoff = handoff_from_document(payload, bundle_root=bundle_root, marker_path=path)
            after = _request_file_state(path)
        except OSError:
            continue
        if handoff is not None or before != after:
            continue
        unusable.append(UnusableHandoff(
            marker_path=path,
            request_id=path.stem if _PLAIN_ID.fullmatch(path.stem) else None,
            state=after,
        ))
    return unusable


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
    ipc_folder: Path,
    *,
    bundle_root: Path | None,
    document_id: str | None = None,
    session_id: str | None = None,
) -> PendingHandoff | PendingOpenDocument | None:
    """Next handoff for this document and session; leave other destinations unclaimed.

    ``document_id=None`` permits an unfiltered inventory of insert/update handoffs.
    The consumer passes its live identity (or an empty string with no document),
    then checks again at execution. Cloud opens name one Fusion session and are
    filtered by ``session_id`` before claiming. Expired opens may be claimed
    by any session for refusal only, so a restart cannot strand them forever.
    """

    for path in _requests_in_order(Path(ipc_folder) / HANDOFFS_DIRECTORY):
        handoff = read_pending_handoff(path, bundle_root=bundle_root)
        if isinstance(handoff, PendingOpenDocument):
            if handoff.session_id != session_id and not open_document_expired(handoff):
                continue
            return handoff
        if handoff is not None and _matches_document(handoff, document_id):
            return handoff
    return None


def open_document_expired(request: PendingOpenDocument) -> bool:
    """Use the publication mtime, preserved by the existing claiming rename.

    A failed stat proves no expiry. Call again after claiming and immediately
    before opening; an expired request can only produce a terminal refusal.
    """
    try:
        return time.time() - request.marker_path.stat().st_mtime > OPEN_DOCUMENT_MAX_AGE_SECONDS
    except OSError:
        return False


def _matches_document(handoff: PendingHandoff, document_id: str | None) -> bool:
    return (document_id is None or not handoff.expected_document_id
            or handoff.expected_document_id == document_id)


def discard_superseded_handoffs(
    ipc_folder: Path, *, bundle_root: Path | None, document_id: str | None = None
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
        if isinstance((handoff := read_pending_handoff(path, bundle_root=bundle_root)), PendingHandoff)
        and _matches_document(handoff, document_id)
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
    """An inventoried claim, potentially still owned by a live process.

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
    session_id: str = ""
    kind: str = ""


def leftover_claims(ipc_folder: Path) -> list[LeftoverClaim]:
    """Inventory hidden claims; the caller must prove owner termination."""

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
            found.append(_claim_from_document(path, channel, payload))
    return found


def _claim_from_document(path: Path, channel: str, payload: Any) -> LeftoverClaim:
    fields = payload if isinstance(payload, Mapping) else {}
    return LeftoverClaim(
        path=path, channel=channel,
        request_id=str(fields.get("requestId") or ""),
        export_id=str(fields.get("exportId") or ""),
        design_id=str(fields.get("designId") or ""),
        expected_document_id=str(
            fields.get("recoveryDocumentId") or fields.get("expectedDocumentId")
            or fields.get("documentId") or ""
        ),
        expected_instance_id=str(fields.get("expectedInstanceId") or fields.get("instanceId") or ""),
        session_id=str(fields.get("sessionId") or ""),
        kind=str(fields.get("kind") or ""),
    )


def refresh_leftover_claim(claim: LeftoverClaim) -> LeftoverClaim | None:
    """Refresh inventory; confirmed absence raises FileNotFoundError.

    Reading is safe while the owner is live, but settlement still requires
    proof of termination before using any returned evidence.
    """
    try:
        payload = _read_json(claim.path, raise_io_errors=True)
    except FileNotFoundError:
        raise
    except OSError:
        return None
    if _request_identity(payload) is None:
        return None  # Unreadable or unrecognized content proves no outcome.
    return _claim_from_document(claim.path, claim.channel, payload)


def remove_leftover_claim(claim: LeftoverClaim) -> bool:
    return wglink_protocol.claim_owner_terminated(claim.path.name) and _remove_claim(claim.path)


def bind_handoff_destination(handoff: PendingHandoff, document_id: str | None) -> None:
    """Persist the newly created document identity before any model mutation.

    The status record writer supplies file fsync, atomic replacement and POSIX
    directory fsync. A failed binding stops execution and retains the claim.
    """
    path = handoff.marker_path
    payload = _read_json(path)
    if (not document_id or not path.name.startswith(CLAIM_PREFIX)
            or not isinstance(payload, Mapping)
            or _request_identity(payload) != (handoff.request_id, handoff.delivery_sequence)):
        raise OSError("Could not identify this insert's claimed destination. "
                      "Inspect the new Fusion document before sending again.")
    wglink_status.write_status_record(path.parent, path.name, {
        **payload, "recoveryDocumentId": document_id,
    })


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
    Unusable files are matched to their validated file state instead of JSON.
    """

    if not pending.request_id:
        return None
    source = pending.marker_path
    claim = source.with_name(
        f"{CLAIM_PREFIX}{pending.request_id}-{uuid.uuid4().hex[:12]}-"
        f"{wglink_protocol.claim_owner_suffix()}.json"
    )
    try:
        wglink_protocol.take_by_rename(source, claim)
    except OSError:
        return None
    if isinstance(pending, UnusableHandoff):
        try:
            matches = _request_file_state(claim)[1:] == pending.state[1:]
        except OSError:
            matches = False
    else:
        taken = _read_json(claim)
        matches = (
            isinstance(taken, Mapping)
            and taken.get("requestId") == pending.request_id
            and _sequence(taken) == pending.delivery_sequence
        )
    if not matches:
        try:
            wglink_protocol.take_by_rename(claim, source)
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
        wglink_protocol.take_by_rename(claim, source)
    except OSError:
        return False
    return True
