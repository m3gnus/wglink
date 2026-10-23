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
Source faces take precedence over groups, then walls; reject faces in two groups.

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
legacy builder behavior. `send` currently supplies `default_mesh(sources)`.
W2a replaces that call with the document settings snapshot, using these same
constants and builder. Persistence, group painting and the dialog belong to W2.

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
can reject `mesh.symmetry` first, because its verdict-key scan precedes feature
negotiation. Both prevent old WG from silently consuming a new snapshot.

The release/pin owner handles the WGLink 0.4.0 bump, package pin, protocol
provenance, endpoint oracle updates and paired-package qualification; W1 does
not change release numbers or WG's vendored files.
