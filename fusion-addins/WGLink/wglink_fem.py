"""Declare FEM regions in Fusion and describe them for a WG coupled solve.

Deliberately free of ``adsk``, like :mod:`wglink_author`: WGLink.py owns the
Declare FEM Region dialog and every attribute write; this module owns the
decisions -- what a valid declaration is, how it is stored on the body and its
faces, and what ``scope.fem_regions`` (feature ``fem-regions-v1``) says.

A FEM region is one closed solid body of air (a rear chamber, a phase plug, a
compression-driver front cavity). WG meshes it with tetrahedra and couples it
to the BEM exterior. Its surface groups name boundary conditions:

* ``moving``: a diaphragm, driven by one WG drive channel;
* ``interface``: where the bounded air meets the exterior BEM air (a port exit
  or a horn throat); WG makes the exterior mesh conform to it;
* ``plane-wave-termination``: an anechoic tube end;
* ``rigid``: an explicitly named wall with local mesh sizing.

Every face that belongs to no group is a rigid wall at the region's size.

Storage follows the source-identity idiom: the region (settings and group
definitions) is a JSON attribute on the native body, and each face carries a
small JSON attribute naming its group, so face membership follows the face
through later edits instead of depending on a token or a face index. Faces are
described to WG by geometry -- area, centroid, normal and bounding box in
millimetres -- because the STEP member carries no stable face identity; WG
matches them against the imported surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
import uuid
from typing import Any, Mapping, Sequence

if __package__:
    from . import wglink_author, wglink_protocol
else:
    import wglink_author
    import wglink_protocol


class FemDeclarationError(Exception):
    """A refusal the user can act on; WGLink.py shows it like an AuthorError."""


REGION_ATTRIBUTE = "fem_region"
FACE_ATTRIBUTE = "fem_surface_group"
ATTRIBUTE_SCHEMA = 1

FEM_REGIONS_FEATURE = wglink_protocol.FEM_REGIONS_FEATURE
FEM_REGION_PURPOSE = wglink_protocol.FEM_REGION_PURPOSE
SURFACE_KINDS = wglink_protocol.FEM_SURFACE_KINDS
CAPABILITY = "femRegions"
UNAVAILABLE_MESSAGE = (
    "This Waveguide Generator does not offer FEM regions "
    "(update it, or turn FEM regions on). Send or Solve again to clear the FEM "
    "declarations and continue without them."
)
EXPERIMENTAL_NOTE = "Experimental: geometry-checked only and not yet acoustically qualified."
GROUP_AREA_TOLERANCE = 0.01

# Fusion's internal length unit is the centimetre.
CM_TO_MM = 10.0

DEFAULT_REGION_SIZE_MM = 8.0
DEFAULT_GROUP_SIZE_MM = 3.0
DEFAULT_SOUND_SPEED_M_PER_S = 343.0
DEFAULT_DENSITY_KG_PER_M3 = 1.21
DEFAULT_BULK_LOSS_FACTOR = 0.0
# The presets Boundary Lab offers for bulk loss (0 is lossless air).
BULK_LOSS_PRESETS = (0.0, 0.002, 0.005, 0.01, 0.02, 0.05)

KIND_CHOICES = (
    ("Driver diaphragm (moving)", "moving"),
    ("Interface to exterior air", "interface"),
    ("Plane-wave termination", "plane-wave-termination"),
    ("Rigid wall (local size)", "rigid"),
)
DRIVE_ROLES = ("LF", "MF", "HF")


def kind_labels() -> tuple[str, ...]:
    return tuple(label for label, _kind in KIND_CHOICES)


def resolve_kind(choice: object) -> str:
    value = str(choice if choice is not None else "").strip()
    for label, kind in KIND_CHOICES:
        if value.casefold() in {label.casefold(), kind}:
            return kind
    raise FemDeclarationError(
        f"{value!r} is not a surface group kind. Choose one of "
        + ", ".join(label for label, _ in KIND_CHOICES)
        + "."
    )


def drive_channel_for_role(role: str) -> str:
    """The drive channel a moving group joins; the same one sources use."""

    if role not in DRIVE_ROLES:
        raise FemDeclarationError(
            f"A driver diaphragm needs a drive role ({', '.join(DRIVE_ROLES)})."
        )
    return wglink_author.source_ids(role, set())[1]


def identifier(name: str, *, fallback: str) -> str:
    """A protocol id: lower-case letters, digits and hyphens, at most 32 bytes."""

    text = re.sub(r"[^a-z0-9]+", "-", str(name).casefold()).strip("-")
    text = text[: wglink_protocol.FEM_ID_MAX_BYTES].strip("-")
    return text or fallback


def _unique(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used:
        tail = f"-{suffix}"
        candidate = base[: wglink_protocol.FEM_ID_MAX_BYTES - len(tail)] + tail
        suffix += 1
    used.add(candidate)
    return candidate


def _positive(value: object, label: str) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise FemDeclarationError(f"{label} must be a number.") from None
    if not math.isfinite(number) or number <= 0:
        raise FemDeclarationError(f"{label} must be greater than zero.")
    return number


# ------------------------------------------------------------- declaration


@dataclass(frozen=True)
class GroupDeclaration:
    group_id: str
    name: str
    kind: str
    mesh_size_mm: float
    drive_role: str | None
    face_keys: tuple[Any, ...]
    declared_area_mm2: float
    face_ids: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "group_id": self.group_id,
            "name": self.name,
            "kind": self.kind,
            "mesh_size_mm": self.mesh_size_mm,
            "declared_area_mm2": self.declared_area_mm2,
            "declared_face_count": len(self.face_keys),
            "declared_face_ids": list(self.face_ids),
        }
        if self.drive_role is not None:
            record["drive_role"] = self.drive_role
        return record


@dataclass(frozen=True)
class RegionDeclaration:
    region_id: str
    name: str
    mesh_size_mm: float
    medium: Mapping[str, float]
    groups: tuple[GroupDeclaration, ...]

    def body_attribute(self) -> str:
        """The JSON stored on the native body."""

        return json.dumps(
            {
                "schema": ATTRIBUTE_SCHEMA,
                "region_id": self.region_id,
                "name": self.name,
                "mesh_size_mm": self.mesh_size_mm,
                "medium": dict(self.medium),
                "groups": [group.payload() for group in self.groups],
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def face_attribute(self, group: GroupDeclaration, index: int = 0) -> str:
        """The JSON stored on each face of ``group``."""

        return face_attribute(self.region_id, group.group_id, group.face_ids[index])

    @property
    def summary(self) -> str:
        groups = len(self.groups)
        noun = "surface group" if groups == 1 else "surface groups"
        faces = sum(len(group.face_keys) for group in self.groups)
        return (
            f"Declared FEM region {self.name!r} ({self.mesh_size_mm:g} mm) with "
            f"{groups} {noun} on {faces} face{'s' if faces != 1 else ''}. Every other "
            "face of the body is a rigid wall."
        )


def face_attribute(region_id: str, group_id: str, face_id: str | None = None) -> str:
    return json.dumps(
        {"schema": ATTRIBUTE_SCHEMA, "region_id": region_id, "group_id": group_id,
         **({"face_id": face_id} if face_id is not None else {})},
        sort_keys=True,
        separators=(",", ":"),
    )


def plan_region_declaration(
    *,
    name: object,
    mesh_size_mm: object,
    body_is_solid: bool,
    groups: Sequence[Mapping[str, Any]],
    other_region_ids: Sequence[str] = (),
    region_id: str | None = None,
    medium: Mapping[str, Any] | None = None,
) -> RegionDeclaration:
    """Validate the dialog and decide what to store.

    ``groups`` entries carry ``name``, ``kind`` (label or kind), ``mesh_size_mm``,
    ``drive_role`` (for moving groups) and ``faces``: hashable keys of the
    selected faces, each paired with ``area_cm2`` and ``body_matches`` so the
    planner can refuse faces of another body without touching Fusion. Errors come out in the
    order a user fixes them: body, region, then each group top to bottom.
    """

    if not body_is_solid:
        raise FemDeclarationError(
            "A FEM region must be one closed solid body of air. Model the cavity "
            "as a solid (for example with Combine > Cut from a box) and select it."
        )
    region_name = str(name or "").strip()
    if not region_name:
        raise FemDeclarationError("Give the FEM region a name.")
    size = _positive(mesh_size_mm, "The region mesh size")
    medium_record = _medium(medium or {})

    used_regions = {str(item) for item in other_region_ids}
    if region_id is None or region_id in used_regions:
        region_id = _unique(identifier(region_name, fallback="region"), used_regions)

    group_ids: set[str] = set()
    names: set[str] = set()
    seen_faces: set[Any] = set()
    planned: list[GroupDeclaration] = []
    for position, raw in enumerate(groups, start=1):
        group_name = str(raw.get("name") or "").strip()
        label = group_name or f"Surface Group {position}"
        if not group_name:
            raise FemDeclarationError(f"{label} needs a name.")
        if group_name.casefold() in names:
            raise FemDeclarationError(f"Surface group name {group_name!r} is used twice.")
        names.add(group_name.casefold())
        kind = resolve_kind(raw.get("kind"))
        group_size = _positive(raw.get("mesh_size_mm"), f"The mesh size of {label!r}")
        role = raw.get("drive_role")
        if kind == "moving":
            role = str(role or "").strip().upper()
            drive_channel_for_role(role)
        else:
            role = None
        faces = list(raw.get("faces") or ())
        if not faces:
            raise FemDeclarationError(f"Select at least one face for {label!r}.")
        keys: list[Any] = []
        areas: list[float] = []
        for face in faces:
            if not face.get("body_matches", False):
                raise FemDeclarationError(
                    f"{label!r} contains a face of another body; select faces of the "
                    "FEM region's body only."
                )
            key = face["key"]
            if key in seen_faces:
                raise FemDeclarationError("A face can belong to only one surface group.")
            seen_faces.add(key)
            keys.append(key)
            areas.append(
                _positive(face.get("area_cm2"), f"The face area of {label!r}")
                * CM_TO_MM * CM_TO_MM
            )
        planned.append(
            GroupDeclaration(
                group_id=_unique(identifier(group_name, fallback="group"), group_ids),
                name=group_name,
                kind=kind,
                mesh_size_mm=group_size,
                drive_role=role,
                face_keys=tuple(keys),
                declared_area_mm2=math.fsum(areas),
                face_ids=tuple(uuid.uuid4().hex for _ in keys),
            )
        )
    return RegionDeclaration(
        region_id=region_id,
        name=region_name,
        mesh_size_mm=size,
        medium=medium_record,
        groups=tuple(planned),
    )


def _medium(raw: Mapping[str, Any]) -> dict[str, float]:
    speed = _positive(raw.get("sound_speed_m_per_s", DEFAULT_SOUND_SPEED_M_PER_S),
                      "The speed of sound")
    density = _positive(raw.get("density_kg_per_m3", DEFAULT_DENSITY_KG_PER_M3),
                        "The air density")
    try:
        loss = float(raw.get("bulk_loss_factor", DEFAULT_BULK_LOSS_FACTOR))
    except (TypeError, ValueError):
        raise FemDeclarationError("The bulk loss factor must be a number.") from None
    if not math.isfinite(loss) or not 0.0 <= loss <= 1.0:
        raise FemDeclarationError("The bulk loss factor must be between 0 and 1.")
    return {
        "sound_speed_m_per_s": speed,
        "density_kg_per_m3": density,
        "bulk_loss_factor": loss,
    }


# ----------------------------------------------------------------- reading back


def read_region_attribute(text: object) -> dict[str, Any] | None:
    """Parse a body attribute; ``None`` for anything that is not a v1 region."""

    if not isinstance(text, str) or not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value.get("schema") != ATTRIBUTE_SCHEMA:
        return None
    if not isinstance(value.get("region_id"), str) or not isinstance(value.get("groups"), list):
        return None
    try:
        if (identifier(value["region_id"], fallback="") != value["region_id"]
                or not value["region_id"] or not isinstance(value["name"], str)
                or not value["name"].strip() or isinstance(value["mesh_size_mm"], bool)):
            return None
        _positive(value["mesh_size_mm"], "Region size")
        medium = value["medium"]
        medium_keys = {"sound_speed_m_per_s", "density_kg_per_m3", "bulk_loss_factor"}
        if not isinstance(medium, dict) or set(medium) != medium_keys or any(isinstance(item, bool) for item in medium.values()):
            return None
        _medium(medium)
        ids = set()
        for group in value["groups"]:
            if (not isinstance(group["group_id"], str) or not group["group_id"]
                    or identifier(group["group_id"], fallback="") != group["group_id"]
                    or group["group_id"] in ids or isinstance(group["mesh_size_mm"], bool)):
                return None
            ids.add(group["group_id"])
            if not isinstance(group["name"], str) or not group["name"].strip():
                return None
            if group["kind"] not in SURFACE_KINDS:
                return None
            _positive(group["mesh_size_mm"], "Group size")
            if group["kind"] == "moving":
                drive_channel_for_role(group["drive_role"])
    except (KeyError, TypeError, AttributeError, FemDeclarationError):
        return None
    return value


def read_face_attribute(text: object) -> tuple[str, str] | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value.get("schema") != ATTRIBUTE_SCHEMA:
        return None
    region_id, group_id = value.get("region_id"), value.get("group_id")
    if not isinstance(region_id, str) or not isinstance(group_id, str):
        return None
    return region_id, group_id


def face_descriptor(
    *,
    area_cm2: float,
    centroid_cm: Sequence[float],
    normal: Sequence[float] | None,
    bbox_min_cm: Sequence[float],
    bbox_max_cm: Sequence[float],
    to_export_frame: Any = None,
) -> dict[str, Any]:
    """A face as WG matches it, in millimetres of the export frame.

    ``to_export_frame`` maps a point (and, with ``vector=True``, a direction)
    from the body's frame to the export frame; ``None`` means they coincide.
    A transformed bounding box is the box of its eight transformed corners.
    """

    def point(value: Sequence[float]) -> list[float]:
        mapped = to_export_frame(value) if to_export_frame else value
        return [float(item) * CM_TO_MM for item in mapped]

    corners = [
        point((x, y, z))
        for x in (bbox_min_cm[0], bbox_max_cm[0])
        for y in (bbox_min_cm[1], bbox_max_cm[1])
        for z in (bbox_min_cm[2], bbox_max_cm[2])
    ]
    unit = None
    if normal is not None:
        mapped = to_export_frame(normal, vector=True) if to_export_frame else normal
        length = math.sqrt(sum(float(item) ** 2 for item in mapped))
        if length > 0:
            unit = [float(item) / length for item in mapped]
    return {
        "area_mm2": float(area_cm2) * CM_TO_MM * CM_TO_MM,
        "centroid_mm": point(centroid_cm),
        "normal": unit,
        "bbox_mm": [
            [min(corner[axis] for corner in corners) for axis in range(3)],
            [max(corner[axis] for corner in corners) for axis in range(3)],
        ],
    }


def fem_member_name(region_id: str) -> str:
    return f"fem-regions/{region_id}.step"


@dataclass(frozen=True)
class RegionExport:
    """One declared body found by the scope walk, ready for the manifest."""

    object_id: str
    declaration: Mapping[str, Any]
    faces_by_group: Mapping[str, Sequence[Mapping[str, Any]]]
    face_ids_by_group: Mapping[str, Sequence[str | None]]


def validate_group_areas(export: RegionExport) -> None:
    """Refuse changed membership first, then missing baselines or area drift."""

    declaration = export.declaration
    group_ids = {group["group_id"] for group in declaration.get("groups") or ()}
    if set(export.faces_by_group) - group_ids:
        raise FemDeclarationError(
            f"FEM region {declaration['name']!r} face membership changed; re-declare this region before sending."
        )
    for group in declaration.get("groups") or ():
        group_id = str(group["group_id"])
        label = (
            f"Surface group {group.get('name', group_id)!r} of FEM region "
            f"{declaration.get('name', declaration['region_id'])!r}"
        )
        faces = export.faces_by_group.get(group_id) or ()
        if not faces:
            raise FemDeclarationError(
                f"{label} has no faces left. Reselect its faces and re-declare "
                "this region with Declare FEM Region (Experimental)…."
            )
        count = group.get("declared_face_count")
        ids = group.get("declared_face_ids")
        current_ids = export.face_ids_by_group.get(group_id) or ()
        if (type(count) is not int or not isinstance(ids, list) or len(ids) != count
                or any(not isinstance(key, str) or not key for key in ids)
                or len(set(ids)) != count):
            raise FemDeclarationError(
                f"{label} has no valid stored face membership; re-declare this region "
                "with Declare FEM Region (Experimental)… before sending."
            )
        if len(faces) != count or sorted(key if isinstance(key, str) else "" for key in current_ids) != sorted(ids):
            raise FemDeclarationError(
                f"{label} face membership changed. Reselect its faces and re-declare this region "
                "with Declare FEM Region (Experimental)… before sending."
            )
        try:
            declared = _positive(group.get("declared_area_mm2"), "The declared group area")
        except FemDeclarationError:
            raise FemDeclarationError(
                f"{label} has no valid stored total face area; re-declare this region "
                "with Declare FEM Region (Experimental)… before sending."
            ) from None
        current = math.fsum(
            _positive(face.get("area_mm2"), f"The face area of {label}")
            for face in faces
        )
        drift = abs(current - declared)
        limit = GROUP_AREA_TOLERANCE * declared
        # Treat only floating-point roundoff at the 1 % boundary as equal.
        if drift > limit and not math.isclose(drift, limit, rel_tol=1e-12):
            raise FemDeclarationError(
                f"{label} total face area changed by {100 * drift / declared:g} % "
                f"({declared:g} to {current:g} mm²), more than 1 %. Reselect its "
                "faces and re-declare this region with Declare FEM Region (Experimental)…."
            )


def validate_region_ids(regions: Sequence[tuple[str, str]]) -> None:
    """Refuse copied declarations, naming both bodies before geometry checks."""

    used: dict[str, str] = {}
    for region_id, body_name in regions:
        if region_id in used:
            raise FemDeclarationError(
                f"Two bodies declare FEM region {region_id!r}: {used[region_id]!r} "
                f"and {body_name!r}. A copied body keeps its declaration; delete "
                "the leftover copy or redeclare one of them with "
                "Declare FEM Region (Experimental)…."
            )
        used[region_id] = body_name


def plan_fem_regions(exports: Sequence[RegionExport]) -> list[dict[str, Any]]:
    """Build ``scope.fem_regions`` from the declared bodies in scope.

    A group whose membership changed or total face area drifted by more than
    1 % refuses the export rather than silently becoming a wall: a lost diaphragm
    would solve to silence and a lost interface would seal the chamber.
    """

    validate_region_ids([
        (str(export.declaration["region_id"]), export.object_id) for export in exports
    ])
    records: list[dict[str, Any]] = []
    for export in sorted(exports, key=lambda item: str(item.declaration.get("region_id"))):
        declaration = export.declaration
        region_id = str(declaration["region_id"])
        validate_group_areas(export)
        groups: list[dict[str, Any]] = []
        for group in declaration.get("groups") or ():
            group_id = str(group["group_id"])
            faces = list(export.faces_by_group.get(group_id) or ())
            record: dict[str, Any] = {
                "group_id": group_id,
                "name": str(group["name"]),
                "kind": str(group["kind"]),
                "mesh_size_mm": float(group["mesh_size_mm"]),
                "faces": sorted(faces, key=lambda face: json.dumps(face, sort_keys=True)),
            }
            if record["kind"] == "moving":
                record["drive_channel_id"] = drive_channel_for_role(str(group.get("drive_role")))
            groups.append(record)
        records.append(
            {
                "region_id": region_id,
                "name": str(declaration.get("name") or region_id),
                "object_id": export.object_id,
                "file": fem_member_name(region_id),
                "n_bodies_expected": 1,
                "mesh_size_mm": float(declaration["mesh_size_mm"]),
                "medium": _medium(declaration.get("medium") or {}),
                "surface_groups": groups,
            }
        )
    return records


def preflight_lines(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """What the Send dialog says about FEM regions."""

    lines: list[str] = []
    for record in records:
        groups = record.get("surface_groups") or ()
        kinds = {}
        for group in groups:
            kinds[group["kind"]] = kinds.get(group["kind"], 0) + 1
        detail = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
        lines.append(
            f"FEM region: {record['name']} ({record['mesh_size_mm']:g} mm"
            + (f"; {detail}" if detail else "; walls only")
            + ")"
        )
    if records and not any(
        group.get("kind") == "interface"
        for record in records
        for group in record.get("surface_groups") or ()
    ):
        lines.append(
            "No interface group: the FEM air is closed, so it couples to the exterior "
            "only through shared diaphragms."
        )
    return lines
