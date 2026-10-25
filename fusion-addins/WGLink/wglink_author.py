"""Author WG source roles and body declarations, and preview a return.

Deliberately free of ``adsk``, exactly like :mod:`wglink_inbox`: every Fusion
API call has to happen on Fusion's main thread inside a command handler, so
WGLink.py owns the dialogs, the live entities and every write, while this
module owns the decisions -- which appearance name a role means, which of the
selected faces actually change, and what the Send dialog says before anything
is exported.  That split is what makes the authoring commands testable without
Fusion.

The two conventions this module encodes are not invented here:

* a source is a face whose *appearance name* is exactly ``LF``, ``MF``, ``HF``
  or ``PASSIVE_CARDIOID`` (``wglink_send._face_role`` upper-cases and matches
  it against ``RECOGNISED_SOURCE_ROLES``, which also accepts the retired
  ``PORT_EXIT`` spelling on faces painted before the rename), and
* an unlinked ("Fusion-first") return is solved in the assembly frame itself:
  radiation along +Z, throat at z = 0, model centred on x = 0 and y = 0.  The
  solver hard-codes that identity frame; see ``assembly_frame_is_solver_frame``
  in the generator's ``server/solver/metal.py``.  Nothing here changes it, and
  the pre-flight no longer comments on how the model sits relative to it.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import math
import re
from typing import Any, Callable, Mapping, Sequence

if __package__:
    from . import wglink_protocol
else:
    import wglink_protocol


class AuthorError(Exception):
    """A refusal the user can act on. WGLink.py shows it like a WgLinkError.

    Deliberately not ``wglink_core.WgLinkError``: that module imports ``adsk``,
    and this one has to stay importable -- and testable -- without Fusion.
    """


# ---------------------------------------------------------------- source roles

SOURCE_ROLES = wglink_protocol.SOURCE_ROLES
DEFAULT_SOURCE_ROLE = "HF"
# What a person reads for each role id; the ids themselves never change.
SOURCE_ROLE_LABELS = {
    "HF": "HF – high-frequency driver",
    "MF": "MF – mid-frequency driver",
    "LF": "LF – low-frequency driver",
    "PASSIVE_CARDIOID": "Port or passive opening",
}


def source_role_label(role: object) -> str:
    """The display label of a role id (legacy spellings included)."""

    canonical = wglink_protocol.LEGACY_SOURCE_ROLE_ALIASES.get(str(role), str(role))
    return SOURCE_ROLE_LABELS.get(canonical, str(role))
CLEAR_SOURCE_LABEL = "Clear WG source"

# ``PORT_EXIT`` was the original name for the passive-cardioid port role.
# Choosing or typing it still has to work -- and still has to land on the new
# canonical role -- so existing habits and any saved dropdown selection do not
# start erroring.
LEGACY_SOURCE_ROLE_ALIASES = wglink_protocol.LEGACY_SOURCE_ROLE_ALIASES

def source_ids(role: str, used: set[str]) -> tuple[str, str]:
    """Allocate the source and drive ids shared by exterior and FEM authoring."""

    base = re.sub(r"[^a-z0-9]+", "-", role.strip().lower()).strip("-") or "unnamed"
    suffix = ""
    index = 2
    while f"source-{base}{suffix}" in used:
        suffix = f"-{index}"
        index += 1
    source_id = f"source-{base}{suffix}"
    used.add(source_id)
    return source_id, f"drive-{base}{suffix}"


# Every appearance name that counts as an already-painted source: the current
# roles plus every retired spelling. Recognition (a face someone already
# painted) has to accept this wider set; painting a *new* role only ever
# offers ``SOURCE_ROLES``. Keeping the two separate is what lets a face
# painted ``PORT_EXIT`` years ago keep reporting ``PORT_EXIT`` -- recognised,
# never silently renamed.
RECOGNISED_SOURCE_ROLES = wglink_protocol.RECOGNISED_SOURCE_ROLES


def resolve_source_choice(choice: object) -> str | None:
    """Turn a dropdown item into a role, or ``None`` for "clear"."""

    value = str(choice if choice is not None else "").strip()
    if not value:
        raise AuthorError(
            f"Choose a WG source role, or {CLEAR_SOURCE_LABEL!r}."
        )
    if value.casefold() == CLEAR_SOURCE_LABEL.casefold():
        return None
    canonical = value.upper().replace(" ", "_").replace("-", "_")
    canonical = LEGACY_SOURCE_ROLE_ALIASES.get(canonical, canonical)
    if canonical not in SOURCE_ROLES:
        raise AuthorError(
            f"{value!r} is not a WG source role. Choose one of "
            f"{', '.join(SOURCE_ROLES)}, or {CLEAR_SOURCE_LABEL!r}."
        )
    return canonical


def role_appearance_name(role: object) -> str:
    """The exact Fusion appearance name a role has to be authored as.

    The appearance name *is* the convention: the export reads
    ``face.appearance.name``, upper-cases it, and accepts it only when it is
    one of the four roles.  A near miss like "HF drive" is silently not a
    source, which is the failure this command exists to prevent.
    """

    value = resolve_source_choice(role)
    if value is None:
        raise AuthorError(f"{CLEAR_SOURCE_LABEL!r} has no appearance name.")
    return value


def _canonical_role(value: object) -> str | None:
    """The role a face already carries, or ``None`` for any other appearance.

    Deliberately returns the *literal* painted name, never an alias-resolved
    one: a face already painted ``PORT_EXIT`` is recognised as carrying a
    role, but this never rewrites it to ``PASSIVE_CARDIOID`` on its own --
    that only happens when the user explicitly repaints it.
    """

    if not isinstance(value, str):
        return None
    canonical = value.strip().upper()
    return canonical if canonical in RECOGNISED_SOURCE_ROLES else None


@dataclass(frozen=True)
class SourcePlan:
    """Which of the selected faces the command paints, clears, and leaves."""

    role: str | None
    appearance_name: str | None
    paint: tuple[int, ...]
    clear: tuple[int, ...]
    unchanged: tuple[int, ...]
    summary: str


def _indices(entries: Sequence[Mapping[str, Any]], noun: str) -> list[int]:
    result = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise AuthorError(f"Could not read the selected {noun}.")
        try:
            result.append(int(entry.get("index", position)))
        except (TypeError, ValueError) as exc:
            raise AuthorError(f"Could not read the selected {noun}.") from exc
    return result


def _total_area_mm2(
    entries: Sequence[Mapping[str, Any]], indices: set[int], positions: list[int]
) -> float | None:
    total = 0.0
    for position, entry in enumerate(entries):
        if positions[position] not in indices:
            continue
        value = entry.get("area_mm2")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        if not math.isfinite(float(value)):
            return None
        total += float(value)
    return total


def _faces_phrase(count: int) -> str:
    return "1 face" if count == 1 else f"{count} faces"


def _was(count: int) -> str:
    return "was" if count == 1 else "were"


def plan_source_assignment(
    faces: Sequence[Mapping[str, Any]], choice: object
) -> SourcePlan:
    """Decide what happens to each selected face.

    Painting replaces whatever role a face already carried, so switching a face
    from MF to HF is one action rather than clear-then-paint.  Clearing is
    deliberately narrower: only a face that actually carries one of the four
    roles is stripped, so a Clear that swept up an ordinary painted face never
    destroys the user's own material assignment.
    """

    role = resolve_source_choice(choice)
    entries = list(faces)
    if not entries:
        raise AuthorError(
            "Select at least one face. Faces are marked by painting them with a "
            "WG source appearance, so the command needs to know which ones."
        )
    positions = _indices(entries, "face")
    paint: list[int] = []
    clear: list[int] = []
    unchanged: list[int] = []
    for position, entry in enumerate(entries):
        index = positions[position]
        current = _canonical_role(entry.get("current_role"))
        if role is None:
            (clear if current is not None else unchanged).append(index)
        elif current == role:
            unchanged.append(index)
        else:
            paint.append(index)

    if role is None:
        summary = f"Cleared the WG source role from {_faces_phrase(len(clear))}."
        if not clear:
            summary = (
                "No selected face carried a WG source role, so nothing changed."
            )
        elif unchanged:
            summary += (
                f" {_faces_phrase(len(unchanged))} carried no WG role and kept "
                f"{'its' if len(unchanged) == 1 else 'their'} own appearance."
            )
    else:
        carrying = set(paint) | set(unchanged)
        area = _total_area_mm2(entries, carrying, positions)
        measured = f" ({area:.1f} mm² in total)" if area is not None else ""
        summary = (
            f"{_faces_phrase(len(carrying))} now drive the {role} source"
            f"{measured}."
        )
        if paint and unchanged:
            summary += f" {_faces_phrase(len(paint))} {_was(len(paint))} repainted."
        elif not paint:
            summary = (
                f"Every selected face already carried {role}"
                f"{measured}; nothing changed."
            )
    return SourcePlan(
        role=role,
        appearance_name=role_appearance_name(role) if role is not None else None,
        paint=tuple(paint),
        clear=tuple(clear),
        unchanged=tuple(unchanged),
        summary=summary,
    )


def group_appearance_name(group_id: str) -> str:
    if not isinstance(group_id, str) or not group_id.strip() or group_id != group_id.strip():
        raise AuthorError("A surface group needs a nonempty, trimmed id.")
    return f"WGMESH_{group_id}"


def plan_surface_group_assignment(
    faces: Sequence[Mapping[str, Any]], group_id: str | None
) -> SourcePlan:
    """Groups cannot replace a source or another group's membership."""
    name = group_appearance_name(group_id) if group_id is not None else None
    if not faces:
        raise AuthorError("Select at least one face or body for the surface group.")
    positions = _indices(faces, "face")
    paint, clear, unchanged = [], [], []
    for index, entry in zip(positions, faces):
        current = entry.get("appearance_name") or ""
        if name is None:
            (clear if current.startswith("WGMESH_") else unchanged).append(index)
        elif current == name:
            unchanged.append(index)
        elif _canonical_role(current) or entry.get("is_source"):
            raise AuthorError("A face cannot be in both a source and a surface group.")
        elif current.startswith("WGMESH_"):
            raise AuthorError("A face cannot be in two surface groups. Clear its group first.")
        else:
            paint.append(index)
    return SourcePlan(None, name, tuple(paint), tuple(clear), tuple(unchanged),
                      f"{'Saved' if name else 'Cleared'} the surface group on {_faces_phrase(len(faces))}.")


def apply_face_appearance_plan(
    faces: Sequence[object], plan: SourcePlan, named_appearance: Callable[[str], object],
    *, write_appearance: Callable[[object, object], None] | None = None,
) -> None:
    """One paint/clear writer for sources and mesh groups, on the main thread.

    The optional writer preserves/restores group overrides at the Fusion boundary.
    By default, clearing a face override with None restores the body's appearance.
    ``named_appearance`` is the existing Fusion library/copy resolver.
    """
    appearance = named_appearance(plan.appearance_name) if plan.paint else None
    for indices, value, action in ((plan.paint, appearance, "paint"), (plan.clear, None, "clear")):
        for index in indices:
            try:
                if write_appearance is None:
                    faces[index].appearance = value
                else:
                    write_appearance(faces[index], value)
            except Exception as exc:
                # Fusion's own text (assetInst, journal checks) goes to the log
                # through the exception chain, not into the message box.
                raise AuthorError(f"Could not {action} the managed appearance on a selected face.") from exc


# ------------------------------------------------------------- body declaration

BODY_DECLARATIONS = ("exterior-shell", "exclude")
CLEAR_DECLARATION_LABEL = "Clear declaration"
DECLARATION_CHOICES = (
    ("Exterior shell — solve this body's outer surface", "exterior-shell"),
    ("Exclude — leave this body out of the return", "exclude"),
    (CLEAR_DECLARATION_LABEL, None),
)

_DECLARATION_HELP = {
    "exterior-shell": (
        "Declares the selected surface body as the exterior WG solves. A visible "
        "surface body with no declaration refuses the export, because WG cannot "
        "tell a horn shell from modelling scaffolding."
    ),
    "exclude": (
        "Leaves the selected bodies out of the WG return entirely: sketch "
        "scaffolding, fixtures, and anything that is not part of the acoustic "
        "exterior."
    ),
    None: (
        "Removes the declaration and puts the selected bodies back under WG's "
        "automatic classification."
    ),
}


# Historical declared domains remain readable in pre-flight data, but the
# Send/Solve UI no longer authors one: WG chooses the domain automatically.
DOMAIN_PLANES = ("x0", "y0")
AXIS_FOR_DOMAIN_PLANE = {"x0": 0, "y0": 1}
DOMAIN_PLANE_LABEL = {"x0": "x = 0", "y0": "y = 0"}


def domain_phrase(planes: Sequence[str]) -> str:
    """How a declared domain reads in one line of dialog copy."""

    values = {str(plane) for plane in planes}
    canonical = tuple(plane for plane in DOMAIN_PLANES if plane in values)
    if not canonical:
        return "full model"
    labels = " and ".join(DOMAIN_PLANE_LABEL[plane] for plane in canonical)
    noun = "half" if len(canonical) == 1 else "quarter"
    return f"{noun} model, already cut on {labels}"


def declaration_choices() -> tuple[str, ...]:
    """The dropdown items of the Declare Body command, in display order."""

    return tuple(label for label, _value in DECLARATION_CHOICES)


def resolve_declaration_choice(choice: object) -> str | None:
    """Turn a dropdown item -- or a raw declaration -- into a stored value."""

    value = str(choice if choice is not None else "").strip()
    if not value:
        raise AuthorError(
            f"Choose a body declaration, or {CLEAR_DECLARATION_LABEL!r}."
        )
    for label, declaration in DECLARATION_CHOICES:
        if value.casefold() == label.casefold():
            return declaration
    folded = value.casefold()
    if folded in {CLEAR_DECLARATION_LABEL.casefold(), "clear", "none"}:
        return None
    if folded in BODY_DECLARATIONS:
        return folded
    raise AuthorError(
        f"{value!r} is not a body declaration. Choose one of "
        f"{', '.join(BODY_DECLARATIONS)}, or {CLEAR_DECLARATION_LABEL!r}."
    )


def declaration_help_text(choice: object) -> str:
    return _DECLARATION_HELP[resolve_declaration_choice(choice)]


@dataclass(frozen=True)
class DeclarationPlan:
    """Which of the selected bodies get an attribute written or removed."""

    declaration: str | None
    write: tuple[int, ...]
    clear: tuple[int, ...]
    unchanged: tuple[int, ...]
    summary: str


def _bodies_phrase(count: int) -> str:
    return "1 body" if count == 1 else f"{count} bodies"


def plan_body_declaration(
    bodies: Sequence[Mapping[str, Any]], choice: object
) -> DeclarationPlan:
    """Decide which selected bodies need their declaration attribute touched."""

    declaration = resolve_declaration_choice(choice)
    entries = list(bodies)
    if not entries:
        raise AuthorError("Select at least one body to declare.")
    positions = _indices(entries, "body")
    write: list[int] = []
    clear: list[int] = []
    unchanged: list[int] = []
    for position, entry in enumerate(entries):
        index = positions[position]
        current = entry.get("current")
        current = str(current) if current in BODY_DECLARATIONS else None
        if current == declaration:
            unchanged.append(index)
        elif declaration is None:
            clear.append(index)
        else:
            write.append(index)

    if declaration is None:
        summary = (
            f"Cleared the WG declaration on {_bodies_phrase(len(clear))}."
            if clear
            else "No selected body carried a WG declaration, so nothing changed."
        )
    else:
        changed = len(write)
        summary = (
            f"Declared {_bodies_phrase(changed)} as {declaration}."
            if changed
            else f"Every selected body was already {declaration}; nothing changed."
        )
        if declaration == "exterior-shell" and write:
            solids = [
                entry
                for position, entry in enumerate(entries)
                if positions[position] in set(write)
                and str(entry.get("body_kind") or "") == "solid"
            ]
            if len(solids) == len(write):
                summary += (
                    " Solid bodies are already included automatically; the "
                    "declaration matters for surface bodies."
                )
    if unchanged and (write or clear):
        summary += (
            f" {_bodies_phrase(len(unchanged))} {_was(len(unchanged))} already correct."
        )
    return DeclarationPlan(
        declaration=declaration,
        write=tuple(write),
        clear=tuple(clear),
        unchanged=tuple(unchanged),
        summary=summary,
    )


# ----------------------------------------------------------------- pre-flight

#: What the preview says about a model with nothing to drive it (the shared rule's words).
NO_SOURCE_WARNING = wglink_protocol.finding_text(
    wglink_protocol.preflight_finding("no_drive", wglink_protocol.NO_DRIVE_MESSAGE)
)
LOG_HINT = (
    "The full traceback is in Fusion's Text Commands palette "
    "(View → Show Text Commands)."
)

_BODY_KIND_NOUNS = (
    ("solid", "solid", "solids"),
    ("surface", "surface body", "surface bodies"),
    ("mesh", "mesh body", "mesh bodies"),
)


@dataclass(frozen=True)
class Preflight:
    """What the Send and Solve dialogs state before anything is exported."""

    lines: tuple[str, ...]
    warnings: tuple[str, ...]

    def text(self) -> str:
        blocks = list(self.lines)
        if self.warnings:
            if blocks:
                blocks.append("")
            blocks.extend(f"⚠ {warning}" for warning in self.warnings)
        return "\n".join(blocks)

    def html(self) -> str:
        rows = [html.escape(line) for line in self.lines]
        if self.warnings:
            if rows:
                rows.append("")
            rows.extend(
                f"<b>⚠ {html.escape(warning)}</b>" for warning in self.warnings
            )
        return "<br>".join(rows)


def _body_count_phrase(included: list[Mapping[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for item in included:
        kind = str(item.get("body_kind") or "solid")
        counts[kind] = counts.get(kind, 0) + 1
    phrases = []
    for kind, singular, plural in _BODY_KIND_NOUNS:
        count = counts.pop(kind, 0)
        if count:
            phrases.append(f"{count} {singular if count == 1 else plural}")
    for kind in sorted(counts):
        phrases.append(f"{counts[kind]} {kind}")
    return ", ".join(phrases) if phrases else "none"


def preflight_summary(
    scope: Mapping[str, Any], findings: Sequence[Mapping[str, Any]] | None = None
) -> Preflight:
    """Compose the read-only Send/Solve summary from plain gathered numbers.

    ``scope`` is what ``wglink_send.preflight_scope`` gathers on Fusion's main
    thread: counts, ids, source roles and areas, and two bounding boxes. No
    Fusion object ever reaches this function. ``findings`` are the shared
    preflight rules' verdict on it (``wglink_protocol.preflight_findings``);
    each is shown as a warning, blockers first.
    """

    if not isinstance(scope, Mapping):
        raise AuthorError("Pre-flight needs the gathered scope report.")
    if findings is None:
        findings = wglink_protocol.preflight_findings(scope)
    warnings = tuple(wglink_protocol.finding_text(finding) for finding in findings)
    if any(finding.get("code") in {"scope_unclassified_surface", "scope_invalid"} for finding in findings):
        return Preflight(lines=(), warnings=warnings)
    selection = str(scope.get("selection") or "root")
    included = [
        item for item in (scope.get("included") or []) if isinstance(item, Mapping)
    ]
    instance_ids = [str(value) for value in (scope.get("instance_ids") or [])]
    sources = [
        item for item in (scope.get("sources") or []) if isinstance(item, Mapping)
    ]
    domain = scope.get("domain")
    automatic_domain = (
        isinstance(domain, Mapping) and domain.get("kind") == "automatic"
    )
    domain_planes = (
        tuple(str(plane) for plane in (domain.get("cut_planes") or []))
        if isinstance(domain, Mapping)
        else ()
    )

    lines = [
        "Scope: root assembly" if selection == "root" else f"Scope: {selection}",
        f"Bodies included: {_body_count_phrase(included)}",
    ]
    if instance_ids:
        lines.append(
            f"WG links in scope: {len(instance_ids)} ({', '.join(instance_ids)})"
        )
    else:
        lines.append(
            "WG links in scope: none — this exports as an unlinked "
            "(Fusion-first) return."
        )
    if sources:
        for source in sources:
            role = str(source.get("role") or "?")
            faces = source.get("face_count")
            faces = int(faces) if isinstance(faces, int) else 0
            area = source.get("area_mm2")
            measured = (
                f"{float(area):.1f} mm²"
                if isinstance(area, (int, float)) and not isinstance(area, bool)
                else "area unread"
            )
            instance_id = str(source.get("instance_id") or "").strip()
            suffix = f", from link {instance_id}" if instance_id else ""
            lines.append(
                f"Source: {role} — {measured} over {_faces_phrase(faces)}{suffix}"
            )
    else:
        lines.append("Sources: none")
    if domain_planes:
        lines.append(f"Domain: {domain_phrase(domain_planes)} ✓")
    elif automatic_domain:
        lines.append("Domain: Automatic (WG decides)")
    for raw_cut in scope.get("cut_provenance") or ():
        if not isinstance(raw_cut, Mapping):
            continue
        feature = raw_cut.get("feature")
        name = str(feature.get("name") or "").strip() if isinstance(feature, Mapping) else ""
        plane = {"x0": "x = 0", "y0": "y = 0", "z0": "z = 0"}.get(
            str(raw_cut.get("plane") or "")
        )
        if name and plane:
            lines.append(f"Cut: {name} at {plane}")
    lines.extend(str(line) for line in (scope.get("fem_regions") or ()))
    return Preflight(lines=tuple(lines), warnings=warnings)


# ------------------------------------------------------- user-facing failures


def _detail(exc: BaseException | None) -> str:
    if exc is None:
        return "no details were reported"
    text = " ".join(str(exc).split())
    if not text:
        text = exc.__class__.__name__
    return text if len(text) <= 200 else text[:197] + "…"


def failure_message(action: str, exc: BaseException | None) -> str:
    """One human line for a message box; the traceback goes to the log.

    A raw ``traceback.format_exc()`` in a modal states the add-in's internals
    and nothing the user can act on, and it hides the one sentence that would
    have identified the failure.
    """

    return f"{action} hit an unexpected error: {_detail(exc)}\n\n{LOG_HINT}"


def preflight_unavailable(exc: BaseException | None, *, refusal: bool = False) -> str:
    """Text for the pre-flight box when the model could not be surveyed.

    ``refusal``: ``exc`` is one the add-in words for the user (WG not running
    or too old, no design), which says what to do. Any other failure is the
    add-in's own: the caller logs it, and the user reads one calm line.
    """

    if refusal and exc is not None:
        return _detail(exc)
    return wglink_protocol.PREFLIGHT_UNAVAILABLE_MESSAGE
