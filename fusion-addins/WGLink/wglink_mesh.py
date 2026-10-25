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
    from . import wglink_mesh_sizing as sizing
    from . import wglink_mesh_history as history
else:
    import wglink_protocol as protocol
    import wglink_mesh_sizing as sizing
    import wglink_mesh_history as history


SETTINGS_ATTRIBUTE = "mesh_settings"
SETTINGS_SCHEMA = 1


class MeshSettingsError(ValueError):
    """Invalid document settings; never silently fall back to defaults."""


def normalize_source(source: Mapping[str, Any]) -> dict[str, Any]:
    """Complete a CAD authoring record before any mesh consumer reads it.

    Existing paint (including PORT_EXIT) and linked-throat selectors are
    authoritative. New dialog rows have only a role and a provisional id;
    deriving their selectors here also supports older partial CAD records.
    This does not invent source identities or relax the return protocol.
    """
    # CAD records can contain live Fusion entities, which cannot be deep-copied.
    result = dict(source)
    if "selectors" not in result:
        role = result.get("role")
        if role not in (*protocol.MESH_DEFAULTS["source_size_mm"], *protocol.LEGACY_SOURCE_ROLE_ALIASES):
            raise MeshSettingsError("Source needs a recognised role. Open Sources & Mesh… and choose its role.")
        result["selectors"] = {"appearance_labels": [role]}
        if result.get("instance_id"):
            result["selectors"]["linked_throat"] = {"instance_id": result["instance_id"]}
    if not isinstance(result["selectors"], Mapping):
        raise MeshSettingsError("Source selectors must be a record. Open Sources & Mesh… and review the source.")
    result["selectors"] = deepcopy(dict(result["selectors"]))
    return result


def validate_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Detach a schema-1 record using the single protocol mesh validator."""
    if not isinstance(settings, Mapping) or type(settings.get("schema")) is not int or settings["schema"] != SETTINGS_SCHEMA:
        raise MeshSettingsError("Mesh settings need schema 1. Open Sources & Mesh… and save again.")
    mesh = deepcopy(dict(settings))
    del mesh["schema"]
    # A group id completely determines its CAD paint selector. Keep explicit
    # selectors strict, but complete partial saved definitions in one reader.
    groups = mesh.get("surface_groups")
    for group in groups if isinstance(groups, list) else ():
        if isinstance(group, dict) and "selectors" not in group and group.get("group_id"):
            group["selectors"] = {"appearance_labels": [f"WGMESH_{group['group_id']}"]}
    sizes = mesh.get("source_size_mm")
    if not isinstance(sizes, Mapping) or any(not isinstance(key, str) or not key.strip() for key in sizes):
        raise MeshSettingsError("Mesh settings need sizes by source id.")
    sources = [{"id": key, "suggested_resolution_mm": size, "selectors": {}}
               for key, size in sizes.items()]
    _validate(mesh, sources)
    return {"schema": SETTINGS_SCHEMA, **mesh}


def _validate(mesh: Mapping[str, Any], sources: Sequence[Mapping[str, Any]]) -> None:
    try:
        protocol.validate_mesh({"mesh": mesh, "sources": [normalize_source(source) for source in sources]},
                               [protocol.MESH_SIZING_FEATURE])
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
    sources = [normalize_source(source) for source in sources]
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


#: The most reduced domain WG could plausibly solve (a quarter), which the
#: ceiling question uses: asked only when even that is over the limit.
LEAST_SOLVED_FRACTION = 0.25


def least_solved_estimate(
    total_area_mm2: float, sources: Sequence[Mapping[str, Any]], mesh: Mapping[str, Any],
    *, declared_reduced: bool = False,
) -> dict[str, Any]:
    """The estimate for the least WG could solve: a quarter, or all of it with symmetry off.

    Send/Solve preflight checks this lower end; Sources & Mesh shows the range.
    """
    fraction = 1.0 if mesh.get("symmetry") == "off" or declared_reduced else LEAST_SOLVED_FRACTION
    return estimate_triangles(total_area_mm2, sources, mesh, solved_fraction=fraction)


def estimate_triangles(
    total_area_mm2: float,
    sources: Sequence[Mapping[str, Any]],
    mesh: Mapping[str, Any],
    *,
    solved_fraction: float = 1.0,
    source_perimeters_mm: Mapping[str, float] | None = None,
    curvature_regions: Sequence[Mapping[str, float]] | None = None,
    surface_deviation_mm: float = protocol.MESH_DEFAULTS["surface_deviation_mm"],
) -> dict[str, Any]:
    """Approximate ``2.3 * sum(A / h²)``, including linear grading rings.

    Areas describe the geometry as drawn (already reduced models stay reduced).
    ``solved_fraction`` is an advisory symmetry fraction supplied by the caller,
    never a CAD symmetry verdict. Without one the estimate is for all drawn
    geometry. The result also includes ``full``, ``half`` and ``quarter`` counts.
    ``off`` always uses all of it. Rings approximate a flat source
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
            h = min(wall, sizing.graded_size_mm(
                (inner + outer) / 2, size_min_mm=size, size_max_mm=ceiling, dist_max_mm=distance))
            rings.append((ring_area, h))
    ring_area = sum(area for area, _ in rings)
    scale = min(1.0, remaining / ring_area) if ring_area else 0.0
    regions.extend(source_regions)
    regions.extend((area * scale, size) for area, size in rings)
    regions.append((remaining - ring_area * scale, wall))
    full = sizing.estimate_triangle_count(sizing.Region(area, size) for area, size in regions)
    fraction = 1.0 if mesh["symmetry"] == "off" else solved_fraction
    return {"full": full, "half": round(full * .5), "quarter": round(full * .25),
            "triangles": round(full * fraction), "drawn_triangles": full,
            "approximate": True, "curvature_aware": curvature_regions is not None}


def estimate_display(estimate: Mapping[str, Any], *, symmetry: str = "auto",
                     declared_reduced: bool = False, limits: Mapping[str, Any] | None = None,
                     learned: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Words, colour and ceiling for the same counts every caller uses.

    A learned fraction applies only to the same document, geometry and sizes.
    Counts always describe the drawn geometry, including an already cut model.
    """
    limit = protocol.solved_triangle_limit(limits)
    fraction = history.matching_fraction(learned)
    single = symmetry == "off" or declared_reduced or fraction is not None
    full = estimate["full"]
    count = full if symmetry == "off" else round(full * (fraction or 1.0))
    if single:
        text = f"About {count:,} triangles for the part {protocol.APP_NAME} solves."
    else:
        text = (f"About {estimate['quarter']:,} – {full:,} triangles for the part "
                f"{protocol.APP_NAME} solves ({protocol.APP_NAME} decides whether it can use symmetry).")
    least = full if symmetry == "off" or declared_reduced else estimate["quarter"]
    ceiling = protocol.ceiling_finding(least, limits=limits,
        symmetry="off" if symmetry == "off" or declared_reduced else "auto", in_sources_and_mesh=True)
    colour = "green" if (count if single else full) <= limit else "amber"
    if ceiling is None and not single and full > limit:
        text += (f" This may exceed the limit if {protocol.APP_NAME} cannot use symmetry; "
                 f"{protocol.APP_NAME} will ask before solving.")
    warning = (limits or {}).get("fullDomainWarning", protocol.DEFAULT_TRIANGLE_WARNING)
    if full > warning and full <= limit:
        text += " A mesh this size may take longer to solve."
    text += f" Limit {limit:,}."
    previous = history.learned_text(learned)
    if previous:
        text += " " + previous
    return {"text": text, "colour": colour, "ceiling": ceiling}


def drawn_fraction(domain: Mapping[str, Any] | None, cuts: list, included: list) -> float:
    """Declared reduction only: a cut must cover every included body.

    This describes the geometry as drawn, never a prediction of WG symmetry.
    """
    domain = domain or {}
    if domain.get('kind') in ('half', 'quarter'):
        return .5 if domain['kind'] == 'half' else .25
    ids = {row['object_id'] for row in included}
    planes = {plane for plane in ('x0', 'y0') if ids and ids <=
              {cut.get('body_object_id') for cut in cuts if cut.get('plane') == plane}}
    return .5 ** len(planes)
