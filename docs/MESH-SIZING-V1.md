# Mesh snapshot contract

`fusion-addins/WGLink/wglink_protocol.py` owns `mesh-sizing-v1`, its defaults,
presets and validation. WG copies this file **byte-for-byte** to
`server/cadlink/wglink_protocol.py`; do not add a second mesh validator or table.
The writer and WG ingress profiles both call `validate_mesh`.

`wgreturn_version` remains **1.1**. `required_features` contains
`mesh-sizing-v1` exactly when the top-level `mesh` object exists. Every new
Send/Solve writes the complete object, including defaults and empty lists:

```json
{
  "wall_size_mm": 25,
  "grading_distance_mm": 100,
  "symmetry": "auto",
  "source_size_mm": {"source-hf": 6},
  "surface_groups": [{
    "group_id": "back",
    "name": "Back",
    "size_mm": 40,
    "selectors": {"appearance_labels": ["WGMESH_back"]},
    "observed": {
      "face_count": 1,
      "total_area_mm2": 143500,
      "per_face_area_mm2": [143500]
    }
  }],
  "settings_origin": "user"
}
```

All six fields are required. Sizes and the grading **distance** are finite
numbers greater than zero (booleans are invalid). `source_size_mm` names every
`sources[].id` exactly once, with no extra ids. Each source must still write
`suggested_resolution_mm`, equal to its entry in the map. Source sizes can be
coarser than walls; walls are independent. `symmetry` is `auto` or `off`;
`settings_origin` is `defaults` or `user`.

Group ids are nonempty, trimmed and unique. Group names are nonempty.
The only group selector is exactly `appearance_labels: ["WGMESH_<group_id>"]`.
Labels cannot repeat or collide with source appearance or shell labels. Observations
reuse the source evidence checks: positive integer face count, positive finite
areas, and exactly one per-face area per face. `observed.bodies` is optional
for groups; when present it is a list of nonempty body names. Sources retain
their existing required `bodies` field. STEP resolution, overlapping resolved
faces and area drift are checked by WG, not guessed by this schema validator.
Resolved source and group faces must not overlap, even when their sizes match.
WG refuses source/group and group/group overlaps, including a painted group on
a geometrically selected linked throat. Source and group targets replace the
wall target only on their own disjoint faces.

`mesh.symmetry` is a CAD preparation policy, independent of
`assembly.domain`. `off` maps to `preparation.symmetry_mode = "full"`;
`auto` maps to `"auto"`. It does not rewrite the domain declaration or expand
geometry already drawn as a quarter. Other CAD-authored `symmetry` verdict
keys remain forbidden.

## Producer and W2a

`MESH_DEFAULTS` holds walls 25, grading distance 100, symmetry auto, source
roles HF 6 / MF 15 / LF 25 / PASSIVE_CARDIOID 25 mm. `MESH_PRESETS` holds
Fine 15 / Coarse 40 mm. `PORT_EXIT` uses the existing role alias.
`default_mesh(sources)` returns detached defaults and preserves source
suggestions already supplied. `build_return_manifest(mesh=...)` attaches the
required feature and validates/detaches the snapshot; omitting `mesh` keeps
legacy builder behavior. Send uses the root component's `WGLink/mesh_settings`
JSON attribute (schema 1), or defaults when it is absent, through the same
builder. The stored record is the complete mesh object plus `schema: 1`.
Saving sets `settings_origin` to `user`. Sizes are keyed by source id; new ids
get their role defaults and ids outside the export stay stored but are not
emitted. Invalid or unsupported stored data refuses rather than resetting it.

Group membership lives on face paint `WGMESH_<group_id>`, including faces
expanded from body selections. Groups share the source appearance writer;
clearing an override restores the body's appearance. Every Send remeasures
group observations from the included faces. Empty or unknown painted groups
refuse. The document settings and current membership enter the return-state
fingerprint, so changing mesh settings invalidates a pending snapshot.

`wglink_mesh.estimate_triangles` is advisory: `2.3 * sum(A / h²)` plus linear
grading rings around sources, bounded by remaining wall area. Curved wall
patches can supply measured areas and sampled principal radii; their effective
size also respects WG's chord-deviation ceiling (`sqrt(8 * radius * deviation)`,
0.15 mm deviation). Without curvature evidence the area estimate is a lower
bound on curved models. WG's actual count is authoritative. A caller may
supply a symmetry fraction for the geometry as drawn; it is an estimate,
not a CAD symmetry verdict. Symmetry Off uses the whole drawn geometry.
The dialog belongs to W2b.

WG must advertise capability schema 1 with integer `meshSizing >= 1`
(booleans, floats and strings do not count). Manual Send/Solve, resumed backend
startup and WG-requested returns check it before exporting. No legacy fallback
is permitted. Refusal: “This Waveguide Generator is too old for Fusion-side
mesh settings. Update and restart Waveguide Generator, then Send again.”

## G1 integration

Copy the protocol and update `server/cadlink/wglink_protocol.source.json`
(commit/path/SHA-256), then advertise `meshSizing: 1`. Copy the shared fixtures
under `tests/fixtures/mesh-sizing-v1` alongside the existing
`server/tests/fixtures/wglink-shared-protocol` oracle, updating WG's fixture
inventory and tests to cover the new directory. Keep the old endpoint oracle
as the legacy verdict/byte baseline.

One WG `snapshot_mesh(manifest)` maps `wall_size_mm` to `rigid_size_mm`,
`grading_distance_mm` to the stored `transition_mm` field, copies source sizes
and groups, and sets preparation symmetry. For feature-bearing snapshots,
apply it on every new solve, including WG-started solves, overriding project
mesh settings. Record the result in the solve request. Groups use the existing
STEP source selector resolver and G2's surface groups, retaining rigid tags.
Verify the defaults table against `shared/solve-defaults.json` plus role sizes.

Without the feature, use the legacy path: preserve person-saved setups and
retained run requests/artifacts. No validation or load operation injects defaults
into an old return. Keep optional group fields absent from legacy setup content
so revision hashes and rebuilds stay identical. M0 owns its separate
`wg_defaults` setup policy.

The frozen pre-W1 validator rejects a feature-only probe with
`unknown required feature(s): mesh-sizing-v1`. With a complete new block it
rejects `$.mesh.symmetry: CAD-authored verdict keys are forbidden`, because
its verdict-key scan precedes feature negotiation. Both prevent old WG from
silently consuming a new snapshot. If a new return is imported directly into
an old WG and shows either diagnostic, update and restart Waveguide Generator,
then Send again from Fusion. Do not strip mesh fields or required features.

The release/pin owner handles the WGLink 0.3.2 bump, package pin, protocol
provenance, endpoint oracle updates and paired-package qualification; W1 does
not change release numbers or WG's vendored files.

## Shared estimates and WG evidence

The mesher owns sizing arithmetic. `wglink_mesh_sizing.py` is byte-for-byte
`hornlab_mesher/mesh_sizing.py` at WG's mesher pin; its `.source.json` records
the repository, revision, source path and SHA-256. Refresh both with WG's
`scripts/vendor_wglink_mesh_sizing.py <mesher checkout> <WGLink checkout>`.
Both repositories test parity. The adapter supplies `Region`s (sources, zones,
curvature patches and rings evaluated with `graded_size_mm`) to
`estimate_triangle_count`, and returns `full`, `half`, `quarter` drawn counts.
`MESH_DEFAULTS.surface_deviation_mm` owns the imported chord-deviation default
for both WG and the adapter; it is not an extra serialized mesh setting.

WG advertises `meshLimits: {solvedTriangles, fullDomainWarning}` from its builder
constants. Missing/invalid fields fall back to 22,000/18,000. The dialog shows
a quarter-to-full range before WG supplies evidence; full counts at/below the
limit are green, possible/definite excess is amber. The ceiling question is
asked only if even the quarter is over. Off or a declared cut uses the entire
drawn geometry once, with no extra reduction.

On submission, `wglink_mesh_history` keeps a document identity, mesh-settings
hash, body revisions/visibility, placements, parameters, selected scope and
source/zone face membership with the estimate and request's manifest digest.
On an explicit Sources & Mesh command it reads those requests' status files
(`solved` or `needsYou(mesh_too_large)`) and learns their actual mesh. Only a
matching document, settings and geometry uses that fraction and displays
“Last WG mesh: …”; otherwise only the previous domain is described. No
heartbeat surveys geometry or polls status for this feature. History is bounded
to 50 records/30 days. Estimate/actual pairs are cached under `meshCalibration`
in add-in diagnostics. A historical record cannot determine today's symmetry.
