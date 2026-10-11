"""Drain requests queued by earlier WGLink versions into the file inbox.

This compatibility reader runs once at startup. It never creates queue items,
starts a worker, reads CAD state, or contacts WG. A failed write or removal keeps
an unanswered item for the next startup under the same operation id.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from itertools import islice
import os
from pathlib import Path
import re
import time
import tempfile
from typing import Any


KIND_SOLVE = "prepare_and_solve"
KIND_SNAPSHOT = "receive_snapshot"
OUTBOX_DIRECTORY = ".wglink-outbox"
CLAIM_DIRECTORY = ".wglink-live-claims"
CLAIM_KINDS = ("insert_link", "update_link", "request_return")
CLAIM_OUTCOMES = (
    "applied", "reconciled", "refused", "superseded", "discarded", "recoveryRequired", "failed",
)
_SCAN_LIMIT = 400
_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
READ_RETRY_SECONDS = 0.5
DELETE_RETRY_SECONDS = 0.5
_RETRY_STEP_SECONDS = 0.01


def valid_item(item: object) -> bool:
    if not isinstance(item, Mapping):
        return False
    return (
        type(item.get("schemaVersion")) is int
        and item["schemaVersion"] == 1
        and isinstance(item.get("operationId"), str)
        and _OPERATION_ID.fullmatch(item["operationId"]) is not None
        and item.get("kind") in (KIND_SOLVE, KIND_SNAPSHOT)
        and all(isinstance(item.get(name), str) and item[name] for name in (
            "bundlePath", "manifestSha256", "requestedAt", "createdAt",
        ))
        and (item.get("answer") is None or isinstance(item["answer"], Mapping))
        and (item["kind"] != KIND_SOLVE or (
            isinstance(item.get("returnId"), str)
            and isinstance(item.get("fileWritten"), bool)
        ))
    )


def _read_item_file(path: Path) -> Any:
    deadline = time.monotonic() + READ_RETRY_SECONDS
    while True:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeError, ValueError, TypeError, FileNotFoundError):
            return None
        except OSError:
            if time.monotonic() >= deadline:
                return None
            time.sleep(_RETRY_STEP_SECONDS)


class LegacyQueue:
    """Read and remove old items; no API for producing or rewriting them."""

    def __init__(self, ipc_folder: Path) -> None:
        self.folder = Path(ipc_folder) / OUTBOX_DIRECTORY

    def path(self, operation_id: str) -> Path:
        if _OPERATION_ID.fullmatch(operation_id) is None:
            raise ValueError("not a request id")
        return self.folder / f"{operation_id}.json"

    def scan(self) -> tuple[list[dict[str, Any]], list[Path]]:
        items: list[dict[str, Any]] = []
        foreign: list[Path] = []
        try:
            paths = [path for path in self.folder.iterdir()
                     if path.suffix == ".json" and not path.name.startswith(".")]
        except FileNotFoundError:
            return items, foreign
        for path in paths[:_SCAN_LIMIT]:
            item = _read_item_file(path)
            if valid_item(item) and path.stem == item["operationId"]:
                items.append(dict(item))
            else:
                foreign.append(path)
        items.sort(key=lambda item: (item["createdAt"], item["operationId"]))
        return items, foreign

    def delete(self, operation_id: str) -> bool:
        path = self.path(operation_id)
        deadline = time.monotonic() + DELETE_RETRY_SECONDS
        while True:
            try:
                path.unlink(missing_ok=True)
                return True
            except OSError:
                if not path.exists():
                    return True
                if time.monotonic() >= deadline:
                    return False
                time.sleep(_RETRY_STEP_SECONDS)

    def notice_shown(self, operation_id: str) -> bool:
        return self.path(operation_id).with_suffix(".notice-shown").is_file()

    def mark_notice_shown(self, operation_id: str) -> None:
        """Keep the displayed notice acknowledged even if its item is held."""

        path = self.path(operation_id).with_suffix(".notice-shown")
        descriptor, name = tempfile.mkstemp(prefix=".notice-", dir=self.folder)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write("shown\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if os.name == "posix":
                directory = os.open(self.folder, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


class LegacyClaims:
    """Bounded reader for the retired claim journal; never delivers requests."""

    def __init__(self, ipc_folder: Path) -> None:
        self.folder = Path(ipc_folder) / CLAIM_DIRECTORY

    def scan(self, limit: int) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        try:
            if self.folder.is_symlink():
                return entries
            # Bound directory enumeration as well as file size and report count.
            with os.scandir(self.folder) as iterator:
                paths = [Path(item.path) for item in islice(iterator, _SCAN_LIMIT)]
            for path in sorted(paths):
                if path.suffix != ".json" or path.name.startswith(".") or path.is_symlink():
                    continue
                try:
                    if path.stat().st_size > 256 * 1024:
                        continue
                    entry = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue
                generation = entry.get("attemptGeneration")
                operation = entry.get("operationId")
                claim = entry.get("claimId")
                if (entry.get("schemaVersion") != 1
                        or entry.get("state") not in ("claiming", "claimed", "outcome")
                        or entry.get("kind") not in CLAIM_KINDS
                        or type(generation) is not int or generation < 0
                        or not isinstance(entry.get("request"), dict)
                        or not isinstance(operation, str) or not _OPERATION_ID.fullmatch(operation)
                        or path.stem != operation
                        or not isinstance(claim, str)
                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", claim)):
                    continue
                entries.append(entry)
                if len(entries) >= limit:
                    break
        except (OSError, UnicodeError, ValueError):
            pass
        return entries

    def delete(self, operation_id: str) -> bool:
        try:
            (self.folder / (operation_id + ".json")).unlink(missing_ok=True)
            return True
        except OSError:
            return False


def convert_outbox(ipc_folder: Path, *, solve_files: Any) -> dict[str, list[str]]:
    """Write unchanged ids and fields to WG before removing unanswered items.

    Schema 4 takes Send and Solve. Schema 3 takes only Solve; an earlier Send
    is removed with an explicit not-delivered notice. Without a compatible WG,
    nothing moves. Answered items and foreign files are left untouched.
    """

    result: dict[str, list[str]] = {
        "converted": [], "left": [], "abandoned": [], "failed": [],
    }
    advertised = solve_files.wg_request_capability(ipc_folder)
    queue = LegacyQueue(ipc_folder)
    items, _foreign = queue.scan()
    pending = [item for item in items if item.get("answer") is None]
    if advertised is None or advertised < solve_files.DELIVERY_VERSION:
        result["left"] = [item["operationId"] for item in pending]
        return result
    for item in pending:
        operation_id = item["operationId"]
        if item["kind"] == KIND_SNAPSHOT and advertised < solve_files.WG_REQUEST_SCHEMA_VERSION:
            result["abandoned" if queue.delete(operation_id) else "failed"].append(operation_id)
            continue
        try:
            if not (item["kind"] == KIND_SOLVE and item.get("fileWritten")):
                solve_files.write_wg_request(
                    ipc_folder,
                    kind=item["kind"],
                    command_id=operation_id,
                    return_id=item.get("returnId") if item["kind"] == KIND_SOLVE else None,
                    bundle_relative=item["bundlePath"],
                    manifest_sha256=item["manifestSha256"],
                    requested_at=item["requestedAt"],
                )
        except (OSError, ValueError, RuntimeError):
            result["failed"].append(operation_id)
            continue
        result["converted" if queue.delete(operation_id) else "failed"].append(operation_id)
    return result
