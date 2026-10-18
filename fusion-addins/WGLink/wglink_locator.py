"""Read the immutable-bundle latest locator without changing bundle contents."""

from __future__ import annotations

import json
import ntpath
from pathlib import Path

if __package__:
    from .wglink_bundle import WgLinkError, read_bundle
else:
    from wglink_bundle import WgLinkError, read_bundle

LATEST_SUFFIX = ".latest.json"
LOCATOR_KEYS = frozenset({
    "schemaVersion", "designId", "exportId", "bundleId", "sequence", "bundlePath",
})


def _basename(value: object) -> bool:
    return (
        isinstance(value, str) and bool(value)
        and not any(part in value for part in ("/", "\\", "..", "\x00", ":"))
        and value != "." and not ntpath.isabs(value)
    )


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate locator key {key!r}")
        result[key] = value
    return result


def latest_bundle(folder: Path, design_id: str):
    """Return (path, validated bundle), or None only when no locator exists.

    The normal bundle reader verifies checksums and rejects symlinked contents.
    Its JSON remains usable after releasing any owned extraction.
    """

    if not _basename(design_id):
        raise WgLinkError("Invalid latest locator design id; re-export from WG.")
    locator = folder / (design_id + LATEST_SUFFIX)
    try:
        # lstat distinguishes absence from a dangling symlink or access failure.
        try:
            locator.lstat()
        except FileNotFoundError:
            return None
        if locator.is_symlink():
            raise ValueError("locator is a symlink")
        payload = json.loads(locator.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
        if not isinstance(payload, dict) or set(payload) != LOCATOR_KEYS:
            raise ValueError("unexpected locator keys")
        if type(payload["schemaVersion"]) is not int or payload["schemaVersion"] != 1:
            raise ValueError("unsupported schemaVersion")
        if type(payload["sequence"]) is not int or payload["sequence"] < 1:
            raise ValueError("sequence must be a positive integer")
        if any(not isinstance(payload[key], str) or not payload[key]
               for key in ("designId", "exportId", "bundleId")):
            raise ValueError("missing locator identity")
        if payload["designId"] != design_id:
            raise ValueError("designId does not match the locator filename")
        name = payload["bundlePath"]
        if not _basename(name) or name != payload["exportId"] + ".wglink":
            raise ValueError("bundlePath must name its immutable export basename")
        path = folder / name
        if path.is_symlink() or path.resolve().parent != folder.resolve() or not path.is_dir():
            raise ValueError("bundlePath is missing, outside the folder, or a symlink")
        bundle = read_bundle(path)
        bundle.close()
        manifest_bundle = bundle.manifest.get("bundle")
        if (
            bundle.identity.design_id != design_id
            or bundle.identity.export_id != payload["exportId"]
            or bundle.identity.export_sequence != payload["sequence"]
            or not isinstance(manifest_bundle, dict)
            or manifest_bundle.get("id") != payload["bundleId"]
        ):
            raise ValueError("locator identity does not match the bundle manifest")
        return path, bundle
    except (OSError, ValueError, WgLinkError) as exc:
        raise WgLinkError(
            f"Invalid latest locator {locator.name!r}: {exc}. Re-export from WG; "
            "nothing in the document was changed."
        ) from exc
