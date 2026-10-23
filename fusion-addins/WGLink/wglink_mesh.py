"""Document mesh settings and advisory sizing, without Fusion imports.

The stored schema is the complete protocol mesh plus ``schema: 1``. Group
observations are the last saved evidence; Send measures their paint again.
The protocol owns validation, defaults and presets.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from typing import Any, Mapping, Sequence

if __package__:
    from . import wglink_protocol as protocol
else:
    import wglink_protocol as protocol


SETTINGS_ATTRIBUTE = "mesh_settings"
SETTINGS_SCHEMA = 1


class MeshSettingsError(ValueError):
    """Invalid document settings; never silently fall back to defaults."""


def validate_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Detach a schema-1 record using the single protocol mesh validator."""
    if not isinstance(settings, Mapping) or type(settings.get("schema")) is not int or settings["schema"] != SETTINGS_SCHEMA:
        raise MeshSettingsError("Mesh settings need schema 1. Open Sources & Mesh… and save again.")
    mesh = deepcopy(dict(settings))
    del mesh["schema"]
    sizes = mesh.get("source_size_mm")
    if not isinstance(sizes, Mapping) or any(not isinstance(key, str) or not key.strip() for key in sizes):
        raise MeshSettingsError("Mesh settings need sizes by source id.")
    sources = [{"id": key, "suggested_resolution_mm": size, "selectors": {}}
               for key, size in sizes.items()]
    _validate(mesh, sources)
    return {"schema": SETTINGS_SCHEMA, **mesh}


def _validate(mesh: Mapping[str, Any], sources: Sequence[Mapping[str, Any]]) -> None:
    try:
        protocol.validate_mesh({"mesh": mesh, "sources": sources}, [protocol.MESH_SIZING_FEATURE])
    except protocol.ProtocolValidationError as exc:
        raise MeshSettingsError(f"Mesh settings: {exc}. Open Sources & Mesh… and save again.") from exc


def loads_settings(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise MeshSettingsError("Could not read mesh settings. Open Sources & Mesh… and save again.") from exc
    return validate_settings(value)


def dumps_settings(mesh: Mapping[str, Any]) -> str:
    """Save a user snapshot; presence of this attribute means user settings."""
    value = validate_settings({**mesh, "schema": SETTINGS_SCHEMA, "settings_origin": "user"})
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def snapshot(
    sources: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any] | None = None,
    *,
    surface_groups: Sequence[Mapping[str, Any]] = (),
    source_aliases: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Current sources only; new ids get defaults, saved ids keep their size.

    Callers provide groups measured from the current export scope. Stored
    observations are never emitted as if they described today's geometry.
    """
    mesh = protocol.default_mesh(sources)
    if settings is not None:
        stored = validate_settings(settings)
        for key in ("wall_size_mm", "grading_distance_mm", "symmetry"):
            mesh[key] = stored[key]
        for key in mesh["source_size_mm"]:
            for alias in (key, *(source_aliases or {}).get(key, ())):
                if alias in stored["source_size_mm"]:
                    mesh["source_size_mm"][key] = stored["source_size_mm"][alias]
                    break
        mesh["settings_origin"] = "user"
    mesh["surface_groups"] = deepcopy(list(surface_groups))
    effective_sources = [{**source, "suggested_resolution_mm": mesh["source_size_mm"][source["id"]]}
                         for source in sources]
    _validate(mesh, effective_sources)
    return mesh


def estimate_triangles(
    total_area_mm2: float,
    sources: Sequence[Mapping[str, Any]],
    mesh: Mapping[str, Any],
    *,
    solved_fraction: float = 1.0,
    source_perimeters_mm: Mapping[str, float] | None = None,
    curvature_regions: Sequence[Mapping[str, float]] | None = None,
    surface_deviation_mm: float = 0.15,
) -> dict[str, Any]:
    """Approximate ``2.3 * sum(A / h²)``, including linear grading rings.

    Areas describe the geometry as drawn (already reduced models stay reduced).
    ``solved_fraction`` is an advisory symmetry fraction supplied by the caller,
    never a CAD symmetry verdict. Without one the estimate is for all drawn
    geometry. ``off`` always uses all of it. Rings approximate a flat source
    neighbourhood, bounded by the remaining wall area; curves and overlapping
    neighbourhoods make WG's actual triangle count authoritative.

    Optional curved wall patches carry area_mm2 and radius_mm (the sampled
    smaller principal radius). Their effective size also respects WG's chord
    deviation ceiling, h = sqrt(8 * radius * deviation). They must be disjoint
    wall patches, excluding sources and groups. Without curvature evidence the
    area estimate is a lower bound on curved models. Fusion's face evaluator
    can supply these patches; this pure model never loads gmsh or Fusion.
    """
    _validate(mesh, sources)
    if not math.isfinite(total_area_mm2) or total_area_mm2 <= 0:
        raise MeshSettingsError("The estimate needs a positive surface area.")
    if not math.isfinite(solved_fraction) or not 0 < solved_fraction <= 1:
        raise MeshSettingsError("The estimate needs a symmetry fraction above 0 and at most 1.")
    if not math.isfinite(surface_deviation_mm) or surface_deviation_mm <= 0:
        raise MeshSettingsError("The estimate needs a positive surface deviation.")
    wall = mesh["wall_size_mm"]
    groups = mesh["surface_groups"]
    regions = [(group["observed"]["total_area_mm2"], group["size_mm"]) for group in groups]
    ceiling = max([wall, *(group["size_mm"] for group in groups)])
    source_regions = [(source["observed"]["total_area_mm2"],
                       min(wall, mesh["source_size_mm"][source["id"]])) for source in sources]
    remaining = max(0.0, total_area_mm2 - sum(area for area, _ in [*regions, *source_regions]))
    for patch in curvature_regions or ():
        area, radius = patch["area_mm2"], patch["radius_mm"]
        if not all(math.isfinite(value) and value > 0 for value in (area, radius)) or area > remaining:
            raise MeshSettingsError("Curved wall patches need positive areas and radii within the wall area.")
        regions.append((area, min(wall, math.sqrt(8 * radius * surface_deviation_mm))))
        remaining -= area
    # Midpoint annuli integrate the same linear Threshold as mesh_sizing.
    # Refined neighbourhoods replace wall area, rather than adding surface.
    rings = []
    distance = mesh["grading_distance_mm"]
    for source, (area, size) in zip(sources, source_regions):
        perimeter = (source_perimeters_mm or {}).get(source["id"], 2 * math.sqrt(math.pi * area))
        if not math.isfinite(perimeter) or perimeter <= 0:
            raise MeshSettingsError("The estimate needs positive source perimeters.")
        for index in range(32):
            inner, outer = distance * index / 32, distance * (index + 1) / 32
            ring_area = perimeter * (outer - inner) + math.pi * (outer**2 - inner**2)
            h = min(wall, size + (ceiling - size) * (inner + outer) / (2 * distance))
            rings.append((ring_area, h))
    ring_area = sum(area for area, _ in rings)
    scale = min(1.0, remaining / ring_area) if ring_area else 0.0
    regions.extend(source_regions)
    regions.extend((area * scale, size) for area, size in rings)
    regions.append((remaining - ring_area * scale, wall))
    full = round(2.3 * sum(area / size**2 for area, size in regions))
    fraction = 1.0 if mesh["symmetry"] == "off" else solved_fraction
    return {"triangles": round(full * fraction), "drawn_triangles": full,
            "approximate": True, "curvature_aware": curvature_regions is not None}
