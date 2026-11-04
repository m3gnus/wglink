"""Publish and remove this Fusion session's .fusion-status.json heartbeat.

Payload construction is independent of Fusion; WGLink.py supplies inert model
evidence on the main thread. The writer preserves the durable file contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping

if __package__:
    from . import wglink_protocol
else:
    import wglink_protocol

FUSION_STATUS_FILENAME = ".fusion-status.json"
DELIVERY_VERSION = wglink_protocol.FUSION_REQUEST_DELIVERY_VERSION
IMMUTABLE_BUNDLES = 1


def write_fusion_status(
    bundle_root: Path,
    *,
    session_id: str,
    document_name: str | None,
    document_id: str | None = None,
    adapter_version: str | None = None,
    workspace_root: Path | None = None,
    links: Iterable[Mapping[str, Any]],
    updated_at: datetime | None = None,
    diagnostics: Mapping[str, Any] | None = None,
    applying_operation: Mapping[str, Any] | None = None,
) -> Path:
    """Atomically publish the active Fusion document as inert JSON.

    The payload is :func:`fusion_status_payload`; the writer is
    :func:`write_fusion_status_payload`.
    """

    root = bundle_root.expanduser().resolve()
    if not root.is_dir():
        raise OSError(f"WGLink folder is unavailable: {root}")
    payload = fusion_status_payload(
        session_id=session_id,
        document_name=document_name,
        document_id=document_id,
        adapter_version=adapter_version,
        workspace_root=workspace_root,
        links=links,
        updated_at=updated_at,
        diagnostics=diagnostics,
        applying_operation=applying_operation,
    )
    return write_fusion_status_payload(root, payload)


def fusion_status_payload(
    *,
    session_id: str,
    document_name: str | None,
    document_id: str | None = None,
    adapter_version: str | None = None,
    workspace_root: Path | None = None,
    links: Iterable[Mapping[str, Any]],
    updated_at: datetime | None = None,
    diagnostics: Mapping[str, Any] | None = None,
    applying_operation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The heartbeat object: the active Fusion document as inert JSON.

    The object is written to ``.fusion-status.json``.

    ``deliveryVersion`` tells WG which delivery version this add-in speaks; WG
    refuses an add-in that reports less than its own. ``applying_operation``
    is the WG operation the document is marked as applying, from
    ``wglink_core.applying_operation``: one whose evidence never followed is an
    interrupted mutation, and WG reports it as needing recovery.

    ``diagnostics`` is advisory and additive under heartbeat schema 1: it
    carries what the last tick cost and which source the add-in is running, so
    a change can be shown to be live -- and a slow heartbeat measured -- from
    outside Fusion, without a debugger and without shipping a build. An older
    WG client ignores the key, exactly as it ignores ``linkName``.
    """

    allowed = {
        "instance_id": "instanceId",
        "bundle_path": "bundlePath",
        "design_id": "designId",
        "lineage_id": "lineageId",
        "edit_version": "editVersion",
        "design_hash": "designHash",
        "design_name": "designName",
        # The user's own label for this link, or null when they never set one.
        # Additive under heartbeat schema 1: an older WG client ignores it and
        # keeps showing designName, exactly as it does today.
        "link_name": "linkName",
        "formula": "formula",
        "config_present": "configPresent",
        "parameter_count": "parameterCount",
        "parameter_drift_count": "parameterDriftCount",
        "local_body_state": "localBodyState",
        "body_fingerprint_hash": "bodyFingerprintHash",
        "document_signature_hash": "documentSignatureHash",
        "document_body_count": "documentBodyCount",
        "source_state_hash": "sourceStateHash",
        # The cheap revision token the document is at now, and the token the
        # published measurement was taken at. The periodic heartbeat reads
        # cached state only, so these two are how a consumer tells a current
        # measurement from an older observation of the same document without
        # anyone inspecting geometry: equal means current, unequal means
        # cached-and-moved, and a null measured token means there is no
        # measurement to offer. Both are property reads. Additive under
        # heartbeat schema 1: an older WG client ignores them, exactly as it
        # ignores linkName.
        "geometry_revision_token": "geometryRevisionToken",
        "measured_revision_token": "measuredRevisionToken",
        "export_id": "exportId",
        "export_sequence": "exportSequence",
    }
    copied = []
    for link in links:
        instance_id = str(link.get("instance_id") or "")
        if not instance_id:
            continue
        record = {
            wire_name: str(link.get(source_name) or "") or None
            for source_name, wire_name in allowed.items()
        }
        record["configPresent"] = record["configPresent"] == "true"
        try:
            record["parameterCount"] = int(record["parameterCount"] or 0)
        except (TypeError, ValueError):
            record["parameterCount"] = 0
        raw_drifted_parameters = link.get("drifted_parameters")
        record["driftedParameters"] = sorted(
            name
            for name in (
                raw_drifted_parameters
                if isinstance(raw_drifted_parameters, list)
                else []
            )
            if isinstance(name, str)
        )
        record["parameterDriftCount"] = len(record["driftedParameters"])
        try:
            record["documentBodyCount"] = int(record["documentBodyCount"] or 0)
        except (TypeError, ValueError):
            record["documentBodyCount"] = 0
        transform_hash = link.get("transform_hash")
        if isinstance(transform_hash, str) and transform_hash:
            record["transformHash"] = transform_hash
        # The WG operation that last updated this link, stamped beside its
        # export id: the reconciliation evidence. Omitted, never invented,
        # for a link no WG operation has updated. Additive under schema 1.
        operation_id = link.get("operation_id")
        if isinstance(operation_id, str) and operation_id:
            record["operationId"] = operation_id
        for source_name, wire_name in (
            ("body_object_ids", "bodyObjectIds"),
            ("source_ids", "sourceIds"),
            ("drive_channel_ids", "driveChannelIds"),
        ):
            values = link.get(source_name)
            if (
                isinstance(values, list)
                and values
                and all(isinstance(value, str) and bool(value) for value in values)
            ):
                record[wire_name] = sorted(set(values))
        copied.append(record)
    timestamp = (updated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload = {
        "schemaVersion": 1,
        "cadApplication": "fusion360",
        "sessionId": str(session_id),
        "adapterVersion": str(adapter_version or "") or None,
        "deliveryVersion": DELIVERY_VERSION,
        "immutableBundles": IMMUTABLE_BUNDLES,
        "workspaceRoot": (
            str(workspace_root.expanduser().resolve())
            if workspace_root is not None
            else None
        ),
        "updatedAt": timestamp.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "document": (
            {"name": document_name, "id": document_id, "links": copied}
            if document_name is not None
            else None
        ),
    }
    operation_id = (
        str(applying_operation.get("operation_id") or "")
        if isinstance(applying_operation, Mapping)
        else ""
    )
    if payload["document"] is not None and operation_id:
        payload["document"]["applyingOperation"] = {
            "operationId": operation_id,
            **{
                wire: str(applying_operation.get(name) or "") or None
                for name, wire in (
                    ("kind", "kind"),
                    ("instance_id", "instanceId"),
                    ("export_id", "exportId"),
                )
            },
        }
        for name in ("phase", "startedAt"):
            value = applying_operation.get(name)
            if isinstance(value, str) and value:
                payload["document"]["applyingOperation"][name] = value
    if diagnostics:
        payload["diagnostics"] = json.loads(json.dumps(diagnostics, default=str))
    return payload


def write_fusion_status_payload(bundle_root: Path, payload: Mapping[str, Any]) -> Path:
    """Atomically write a :func:`fusion_status_payload` object as the heartbeat file."""

    return write_status_record(bundle_root, FUSION_STATUS_FILENAME, payload)


def write_status_record(bundle_root: Path, filename: str, payload: Mapping[str, Any]) -> Path:
    """The one atomic writer for this add-in's own records in the WGLink folder.

    The heartbeat and the request the add-in is following (``wglink_requests``).
    ``filename`` is a plain name directly in ``bundle_root``.
    """

    if Path(filename).name != filename or not filename:
        raise ValueError("A status record's file name must be a plain name")
    root = bundle_root.expanduser().resolve()
    if not root.is_dir():
        raise OSError(f"WGLink folder is unavailable: {root}")
    marker = root / filename
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{filename}.", dir=root
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
        if os.name == "posix":
            # Recovery journals may be removed after this returns: persist
            # the directory entry as well as the already fsynced file data.
            directory = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return marker


def remove_fusion_status(bundle_root: Path, *, session_id: str) -> bool:
    """Remove only this add-in session's heartbeat, never a replacement's."""

    marker = bundle_root.expanduser().resolve() / FUSION_STATUS_FILENAME
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping) or payload.get("sessionId") != session_id:
            return False
        marker.unlink()
    except (OSError, ValueError, TypeError):
        return False
    return True
