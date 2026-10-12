# WGLink for Fusion

WGLink inserts a Waveguide Generator `.wglink` bundle as native, managed
Fusion history. It supports the two solid WG export modes:

- `enclosure`: the realized enclosure block with one all-edge treatment, minus
  the waveguide cavity;
- `freestanding`: the fitted-spline surface, throat patch, stitch, and outward
  parameter-driven wall thickness that form the freestanding waveguide solid.

The add-in is licensed under the repository's AGPL-3.0 license. Its manifest
author is `m3gnus`.

## What WGLink manages

Each Assembly insertion owns one movable wrapper occurrence. The wrapper holds
the WG parameters, datum planes and axis, stable throat and mouth interface
sketches, ring sketches, native features, final body, source-role face, and the
attributes/entity tokens that identify them. Move and joint the wrapper, not an
individual managed body.

The enclosure starts as a rectangular prism. Its four longitudinal edges and
both four-edge end perimeters are passed together to one parametric treatment:
`edge_type=1` makes one fillet and `edge_type=2` makes one chamfer, with the
distance/radius still driven by the link's `enc_edge` expression. Giving Fusion
all twelve original edges at once lets it construct the three-way corner
mitres; no feature runs over faces made by an earlier chamfer.

Every browser object created by Insert has a deliberate user-facing name.
Bodies use `WGLink enclosure`, `WGLink freestanding waveguide`, and `WGLink
waveguide cut tool`; the freestanding construction bodies are named `WGLink
waveguide surface`, `WGLink throat patch body`, and `WGLink stitched waveguide
body` while they exist. These names are presentation only. WGLink continues to
resolve ownership and topology exclusively through attributes and entity
tokens, never names.

Fusion Part Design documents reject a second component. Insert therefore has
an explicit root-component fallback, enabled by default. Its report warns that
a root link cannot be moved or jointed as a unit. Start with an Assembly when
that behavior matters, or disable `allow_root_fallback` in a head-less call to
make Insert refuse instead.

WGLink pushes manifest parameters whose role is `interface`. Enclosure links
also own `<parameter_prefix>mouth_overshoot` (5 mm by default), which drives the
join extrude that carries the cavity through the baffle. Existing parameters
are updated by assigning their expression; they are never deleted and
recreated. Informational parameters remain JSON metadata, and unrelated
`wg_*` parameters are left alone.

The supported reference layer is:

- `WGI_THROAT_SKETCH` and `WGI_MOUTH_SKETCH`;
- WG datum planes and `WG_AXIS`;
- the managed enclosure or waveguide body, with the documented update limits
  below.

Send reads each link's `WG_THROAT_PLANE` and `WG_AXIS` by ownership, not by
name. Insert stamps every datum with the link that owns it and records its
entity token; Send resolves that token and requires it to name the stamped
datum. A renamed datum therefore still sends, and two links in one component —
the root-fallback links of a Part Design document — each send their own
throat frame. A recorded datum that has since been deleted sends no throat
contract for its link, never a neighbour's. Where the two records disagree, or
two datums claim one link, Send refuses and names the link. A link with no
such record is read by datum name only while it is the only link in its
component; beside another link it is refused. Re-insert it from WG to record
its datums.

Send also refuses a link whose wrapper placement is mirrored or not rigid. The
return contract's only chirality, `original`, is true of a rotation plus a
translation alone (determinant +1 within 1e-6, WG's rigid-placement
tolerance), so Send measures each placement and names the link instead of
labelling it `original`.

## Commands

The panel promotes the three everyday commands, **Set WG Source…**, **Solve in
WG** and **Send to WG**. **Manage WG Link…** contains **Declare Body…**,
**Insert**, **Update**, and **Detach**.
Insert and Update are ordinarily automatic — WG's *Send to CAD* publishes a
handoff the add-in applies on its own — so the dropdown is maintenance,
authoring-remedy, and recovery UI rather than the normal workflow. Audit and
Relink remain full head-less APIs but are not panel commands.

- **Set WG Source…** marks the selected faces as the `LF`, `MF`, `HF` or
  `PASSIVE_CARDIOID` drive source by creating (or reusing) an appearance named
  exactly after the role and painting it on. (`PASSIVE_CARDIOID` was named
  `PORT_EXIT` before this rename; a face already painted `PORT_EXIT` is still
  recognised as a source and keeps exporting under that name — only a newly
  painted face gets the new name.) That appearance name *is* the convention
  the export reads, and it is the one thing a model built from scratch in Fusion
  cannot be sent without; before this command it had to be authored by renaming
  a Fusion appearance by hand. The same dialog clears a role, which strips only
  faces that actually carry one of the four roles — a face painted with your own
  material is left alone.

  The command also stamps each selected face with the source's identity (a
  `source_identity` attribute on the native face: the id, the role, a per-face
  nonce and the number of faces the source was marked on). When WG advertises
  `"sourceIdentity": 1` in `wg-capabilities.json`, Send, Solve, a return WG
  asks for, the pre-flight and the heartbeat all declare `source-identity-v1`,
  and each `sources[].id` becomes that stable identity (`wgs-` and 20 base32
  characters) instead of the role-derived `source-hf`. A linked throat's identity
  is derived from its link's `instance_id` and needs no stamp. A WG that does not
  advertise the capability gets the legacy manifest, byte for byte, stamps or
  not. Fusion entity tokens and stamps never enter the manifest.

  Export refuses a painted role, never guessing, when a face carries no stamp,
  the faces carry two identities, a face was split or copied, or the source no
  longer adds up to the faces it was marked on. What re-running
  **Set WG Source…** does depends on the source:

  - **The source no longer resolves** — a member face's paint was removed or
    changed by hand, a face was removed, split or copied, or two identities
    share the role. Selecting every face that should drive the role and running
    the command gives it a **new** identity, removes the stale stamps from every
    other face, and WG asks for that source's setup again.
  - **The source still resolves** — the selected faces are added to it and it
    **keeps** its identity; this is also the remedy for faces painted by hand or
    before identities existed.
  - **Part of the source is outside what is being sent** (another component, or
    a body the selection leaves out) — the refusal says so. Include those faces
    in the export, or select them and **Clear** their WG source; running the
    command again on the faces in scope changes nothing.

  Clear removes the stamp from every selected face, including one whose paint
  was already removed by hand. A source whose faces were all deleted is simply
  absent from the next return. The command's stamp writes succeed together or
  are rolled back together (a read-only referenced component refuses a write);
  the appearance it already painted or cleared is kept, and the error says so —
  Send then names the remedy, or undo the command. A face whose stamp Fusion
  would not restore is reported, never called restored.
- **Solve in WG** writes the same validated `.wgreturn` bundle as Send to WG,
  then asks Waveguide Generator to prepare that exact bundle and start the
  solve, so WG is already solving when you switch to it. The request is a
  one-shot marker carrying its own command id, deliberately separate from
  `wgreturn.json`: WG re-reads returns whenever it re-lists the workspace, so
  intent recorded inside the immutable geometry evidence would be re-observed
  and re-solved. WG records each command id as spent, refuses a bundle whose
  manifest hash changed after the request, and stops at its own ingestion
  gates — unacknowledged blocking findings never solve automatically. There is
  no remembered "solve next time" preference: an expensive solve should not be
  a side effect of a later plain send.
- **Insert** offers the bundles already sitting in Waveguide Generator's
  workspace, validates the chosen one before Fusion mutation, and builds the
  full WG viewport model. **Send to CAD** in Waveguide Generator also publishes
  a one-shot handoff beside the completed bundle: WGLink inserts a new link into
  the active Fusion design automatically, including after a cold start. If that
  bundle is already linked, WG must send an explicit Update handoff for its
  exact target; an Insert refuses the duplicate. Exports are no longer watched. For assemblies
  containing repeated placements of one design, WG includes the chosen
  `expectedInstanceId` beside the expected document id. WGLink updates only the
  uniquely matching managed instance. A missing selection for repeated links,
  a stale id, a duplicated id, or an instance target without its document target
  leaves the handoff unacknowledged and changes no geometry. Legacy and current
  single-instance handoffs still resolve automatically.
- **Update** reads the stored bundle path, resamples the new grid outside
  Fusion, validates the existing sketch topology, rolls the timeline back, and
  moves fit points in place. On macOS and Linux the resampler runs in its own
  session, and its timeout kills that whole process group. On Windows it is
  put into a kill-on-close job object just after it starts, and a timeout
  ends the job: the whole tree when the assignment succeeded. There is a short
  window before the assignment in which a process it started would escape,
  and where the job cannot be created or assigned the timeout kills only the
  resampler itself, as before. Either way the wait for its output is bounded,
  and WGLink never signals a process it did not start. Before its first mutation it also verifies that
  the tagged throat face remains in the component-local link frame. It creates
  and deletes no document features. If the stored path is missing, Update
  searches WG's current workspace for the same design id, selects its highest
  export sequence, records that path using the same identity guard as Relink,
  and continues. A manually moved bundle outside the workspace can still be
  selected through the head-less `wglink_core.relink` API.
- The head-less **`audit` API** reports bundle/link state, pushed-parameter
  drift, source tag state, feature health, the measured link-frame offset, and
  evidence that the managed body is unmodified, modified, missing, or unknown.
  The add-in continuously publishes the same document/link inventory to WG in
  `.fusion-status.json`, so Audit has no panel command. Each live link also
  publishes the exact body entity tokens, assembly-transform hash, source ids,
  and drive-channel ids that its next validated return would use. Those fields
  are optional: if Fusion cannot resolve a stable entity token, strict
  transform, or complete source contract, WGLink omits that identity instead
  of inventing a name or defaulting a transform.
- **Send to WG** observes the root or one occurrence subtree without changing
  the document, applies the explicit return-scope policy, and writes an atomic,
  checksummed `.wgreturn` bundle into WG's own workspace. The destination is
  not a dialog choice: WG only ingests from `<workspace>/wgreturn`, so a
  return written anywhere else is invisible to it. Names are collision-safe
  rather than overwriting, because a return WG has not ingested yet is not in
  content-addressed storage. A body can carry the `WGLink` attribute
  `return_declaration=exterior-shell` or `return_declaration=exclude` when its
  surface/exclusion intent cannot be inferred safely; **Declare Body…** writes
  and clears it.
- **Declare Body…** classifies the selected bodies as `exterior-shell` or
  `exclude`, or clears the declaration. A single visible, undeclared body is
  taken as the exterior shell whether Fusion reports it as a solid or surface.
  With multiple visible bodies, undeclared surfaces refuse the export and this
  command is the in-product remedy. Hidden and explicitly excluded bodies are intentionally left out
  and recorded as information, not as degraded export evidence.
- The head-less **`relink` API** records a manually moved or renamed bundle
  path outside the current WG workspace. The design id must match unless the
  caller explicitly forces the operation.
- **Detach** removes `WGLink` attributes only. Bodies, sketches, features, and
  appearances remain in the document. The panel command requires confirmation:
  identity removal is permanent, and inserting a fresh WG copy is the only way
  to obtain a managed link again.

## FEM regions (`fem-regions-v1`)

**Declare FEM Region…** appears under Manage only when
`wg-capabilities.json` advertises `femRegions` at add-in startup.
WG may have this feature turned off. Restart the add-in after enabling it; there is no polling
or toolbar refresh. Opening the command and executing it both re-check the
capability and refuse if it has been withdrawn. Without the capability, no
FEM command or icon is registered, and a design without declared regions keeps
the existing manifest and bundle bytes.

The command stores a bounded-air declaration for a
coupled FEM/BEM solve, following the FEM export dialog of the Fusion-to-MSH
add-in: one solid body, a region size and bulk loss, and any number of named
surface groups (moving diaphragm with a drive role, interface to the exterior,
plane-wave termination, rigid wall), each with a local size and its faces.
Rigid groups offer **Absorbing lining** with thickness (default 30 mm) and
flow resistivity (default 5000 Pa·s/m²). Moving groups offer **Relative level
(dB)**, default 0 dB, with values at or below zero.

Protocol rules: `femRegions >= 1` means accepts `fem-regions-v1` with these
optional surface-group keys:

- `lining` is allowed only on `rigid` groups and is an object with exactly
  `model: "miki"`, positive finite `thickness_mm`, and positive finite
  `flow_resistivity_pa_s_per_m2`.
- `relative_level_db` is allowed only on `moving` groups and is a finite number
  ≤ 0. WG converts it to the linear motion weight `10 ** (relative_level_db / 20)`.
  Omission means 0 dB (weight 1); unlined walls omit `lining`.

Existing declarations read with these defaults. Default dialog values add no
keys to the return, preserving existing FEM bytes. Lining and relative level
are included in the return-state hash, so changes invalidate an earlier return.

- `wglink_fem.py` (no `adsk`) owns validation, the attribute payloads, face
  descriptors and the `scope.fem_regions` records.
- The native body carries `WGLink/fem_region` (JSON: region id, name, size,
  medium, groups including `declared_area_mm2`) and each grouped face carries `WGLink/fem_surface_group`
  (JSON: region id, group id). Faces are compared by entity token or temp id,
  never by wrapper identity.
- Send (`wglink_send._fem_region_exports`) refuses a visible region body,
  a non-root frame, and a group whose faces are gone or whose total area differs
  from the stored declaration by more than 1 %. Missing or invalid area totals
  in older declarations require "re-declare this region" before Send. The
  attribute schema stays at 1; the area baseline is local authoring evidence
  and is not added to the shared protocol or return manifest.
  It exports each region by
  copying the body into a temporary root component, which is deleted again,
  and gates the member to one solid.
- Several moving groups may share one drive role: front and rear of one
  diaphragm in two regions, or dome and surround in one region.
  WG decides whether the channel is electrodynamic; **ideal-source channels
  with FEM diaphragms in more than one region are refused by WG**.
  A FEM diaphragm still refuses a role with more than one exterior source channel
  (for example, `drive-lf` and `drive-lf-2`); the refusal names the role and
  conflicting sources. Use a different role for each driver.
  One painted exterior source and FEM diaphragms on the same role are allowed
  and share one drive channel as one driver.
- No meshing happens in Fusion. Faces are described by geometry in the export
  frame and WG matches them to its own gmsh surfaces, so mesh tags stay WG's.
- `wglink_protocol.validate_fem_regions` is shared by both profiles; the
  feature is optional and gated on WG's `femRegions` capability.

A design with declarations and no advertised capability is refused with:
"This Waveguide Generator does not offer FEM regions (update it, or turn FEM
regions on). Clear the declarations to send without them."

## The workspace is WG's setting

The WGLink folder is chosen once, under **Settings → CAD Link** in Waveguide
Generator. It is separate from WG's run-output folder. WGLink reads it from
WG's own `cadlink_settings.json` — resolved the way WG resolves it,
`WG2_DATA_DIR` included — and lists the bundles in `<workspace>/wglink` in the
Insert dropdown, newest first, labelled by design name and export sequence.
Update also searches those bundles by design identity when a stored path no
longer exists. Nothing is ever written back to WG's settings file.

There is deliberately no second copy of the folder in Fusion. Storing one made the
first insert a two-place setup and let the two settings disagree, which inserts
a bundle WG is no longer writing to. When the workspace cannot be read — WG
never ran, the folder is on a disconnected drive, the bundle came from another
machine — the Insert dropdown falls back to the browse entries and manual picker
behaves as it always did.

## Delivery with Waveguide Generator

Commands and requests cross through WG's machine-local IPC folder,
`<WG data folder>/ipc/wglink`. The contract is WG's
`docs/architecture/CAD-OPERATIONS.md`; WGLink implements the add-in's half.

- **Capability gate before export or mutation.** Insert, Update, WG handoffs
  and return requests require a schema-1 `wg-capabilities.json` advertising
  integer `fusionRequestDelivery >= 3`. Send and Solve additionally require
  integer `solveCommandDelivery >= 4`. WG's Solve consumer can be off while
  Fusion-bound requests still run. Product version
  strings do not establish compatibility. A WG advertising older or invalid
  delivery values is refused with "Update Waveguide Generator" before export
  or mutation; missing inbox advertisement reports "not collecting requests".
  Fusion-bound requests and the heartbeat retain delivery version 3. Optional
  manifest requirements are checked separately (Send/Solve and return requests
  also need `automaticDomain` and `documentUp`; source identity is optional).
  Return requests publish a bundle WG reads directly, without writing WG's
  Send/Solve inbox. The add-in version stays 0.2.1.
- **Send and Solve: one request file each (the WG request inbox).** Both write
  `.wg-solve-requests/<operationId>.json` and nothing else, whatever the activation gate says — and differ only in `kind`
  (`receive_snapshot` or `prepare_and_solve`). Both write schema 4; a WG
  advertising only inbox 3 is refused for both commands. A WG that advertises
  nothing is "not collecting requests", and both refuse. The outcome is said at
  command time: "Written to WG's inbox (request …)" when WG advertises
  `solveAcknowledgement` (then WGLink looks for WG's
  `.wg-solve-acks/<requestId>.json` every few seconds and says "WG accepted the
  request", WG's own refusal reason, or after two minutes with the file gone and
  no acknowledgement "taken but not confirmed" -- never "solving"), "Sent to
  Waveguide Generator (request …)" for an older WG, or why WG was not
  asked. Pressing Send again is a new request. A second request never
  replaces one WG has not read yet. The file is staged under a hidden name,
  synced and renamed into place, so WG never sees part of one. The request is
  planned once: WG's capability decides its schema and exact contents before
  the first attempt, and every attempt writes that same request -- a retry
  never re-decides the schema. Any failed attempt is retried, up to three
  attempts in all with a short pause (0.45 s of pauses; the file operations
  themselves have no deadline). If none succeeds, the outcome depends on how
  they failed. When no attempt reached the rename, nothing was written and
  the command is refused ("not asked"). When a rename raised, the file may
  still have landed -- and WG may take it at once -- so WGLink reads the inbox
  once more: a file equal to the planned request, field for field and type for
  type, is "Sent"; anything else is **Unconfirmed** -- the short request id,
  "WG may already have it", and a pointer to WG's CAD Link panel before
  sending again. Unconfirmed never says "not asked", and it gets the pickup
  check like a sent request (contract Amendment A1). "Never replaces" rests on
  each request id being a fresh `uuid4`: a rename replaces whatever holds its
  name.
- **The pickup check.** A minute after a Send or Solve, a one-shot timer asks
  on the main thread whether the request file is still there, and only that;
  if it is, WGLink says once that WG is closed, older than the add-in, or not
  collecting requests. It is counted under the command that wrote the file.
  The timer does not survive a restart, so at start-up WGLink looks once at
  the inbox for its own request files WG has not taken -- reading nothing in
  Fusion, counted under `startup` -- and says once, with the same three causes,
  how many are waiting. Files already older than a minute are checked at once;
  if one is younger, the single check waits until it has had its minute. The
  scan is capped at 256 directory entries, so in an inbox holding more than
  that a waiting request can be missed, and a later start-up is not
  guaranteed to reach it either.
- **Items an earlier session queued** in `.wglink-outbox` are converted at
  start-up, against what WG advertises then:
  into request files when both delivery channels are compatible. With an
  incompatible WG or no inbox advertised, unanswered items stay queued until
  a later start-up with a compatible WG. An item whose
  write fails, or whose outbox file is held, is kept and reported once; the next
  start-up writes it again under the same id, which WG recovers as the same
  operation. A final answer stored by an earlier session is never resent; an
  outstanding refusal is shown before removing its item. A failure of the conversion as a whole is reported the same way
  and never stops start-up.
- **Two Fusion processes on one WG data folder.** Requests stay apart: each
  has a fresh `uuid4` id and its own file. Return bundle names are reserved
  with an exclusive create, so two processes never publish over each other.
  Windows answers that create with a permission error while another process
  holds the name or has just deleted it; WGLink retries that name for about a
  tenth of a second, then takes the next name, and after eight held names in a
  row refuses visibly. On macOS and Linux a permission error is the folder's
  and is refused at once.
  **Known limit:** `.fusion-status.json` is one file, and the last writer wins,
  so two Fusion processes overwrite each other's status for WG. Panel ownership
  is elected within each Fusion process.
- **WG's requests.** Return requests and handoffs arrive as
  `.fusion-return-requests/<id>.json` and `.fusion-handoffs/<id>.json`. WGLink
  takes them in `deliverySequence` order, claims each by renaming it to a
  hidden name, runs it at most once and deletes the claim, also when the
  request is refused.
- **Exact targets.** An update names the Fusion document, the exact instance
  and the model state WG measured. A handoff that names no instance is an
  insert, and it is refused if the active document already links that design:
  WGLink never picks "the one matching link" for WG. A return request names
  its document, instance and baseline too.
- **The target, immediately before mutating.** Update and Insert take a
  precondition and run it after their last read and before their first write:
  for an update, the live model state against WG's baseline; for an insert, the
  active document and "no link of this design yet". A document that moved in
  between is a conflict, never an overwrite.
- **Reconciliation, then interruption.** Update and Insert stamp WG's operation
  id beside the export identity, as their last write. A handoff whose operation
  id is already on a link is acknowledged without mutating, and before the
  baseline check, so a lost acknowledgement never reads as a conflict. Before
  its first write that changes the model, Update saves the user's exact timeline
  marker and journals `prepared`, `applying`, `applied`, then `verified`, with one
  stable `startedAt`. The marker is restored to its saved position on every exit;
  Update never moves it blindly to the end. Export identity and operation evidence
  are stamped only after verification, then the journal is cleared. An apply
  failure leaves `applying`; a verification failure leaves `applied` and reports
  **Update applied but not verified — recovery required**. That operation is never
  run again, and the heartbeat publishes `phase` and `startedAt` additively in
  `document.applyingOperation` so WG can say where recovery stopped.
- **Insert destination and expiry.** A new WG can bind an insert to the active
  document with `destination: {kind: "document", value: <document id>}`, or to a
  new document with `{kind: "new_document", value: <request id>}` when none is
  active. WGLink consumes and refuses a different destination, and consumes an
  insert more than 30 minutes after `requestedAt` as `expired`, without touching
  the document. A request with no `destination` keeps the version-3 behaviour for
  a pinned older WG.
- **Supersession.** An update that has not started yet is dropped when a newer
  one for the same document and instance arrives; only the newest runs. WG
  withdraws the older file itself; WGLink covers a file WG could not remove.
  The heartbeat's `diagnostics.recentOutcomes` names the dropped request with
  the outcome `superseded`.
- **Leftover claims.** The first tick with an active document settles every
  claim an interrupted session left behind, read-only, and lists each in
  `diagnostics.recentOutcomes`: evidence on a link means it
  applied, the applying mark means recovery is required, and neither means it
  never started. None is run again; each claim is removed.
  **A claim whose Fusion document is not the open one is kept, not settled.**
  All three answers above are reads of that exact document, so with another one
  open none of them can be established, and reporting "never started" would
  cancel an operation that may have applied or may need recovery. Such a claim
  waits — in this session or a later one — until its document is active, and
  WGLink logs that the target document must be opened.
  *Target document unavailable* and *operation never started* stay different
  things.
- **Correlation.** The heartbeat's `diagnostics.lastRequest` names the last
  round trip (`channel`, `correlationId`, `attemptId`, `delivery`, `outcome`),
  and each link publishes the `operationId` stamped on it.

### The heartbeat reads cached state only

The four-second heartbeat inspects no geometry. Measuring a link's state means
walking the root export scope and evaluating every included face and body on
Fusion's main thread, which on a dense linked document is a permanent load on
the application the user is modelling in. So the tick publishes identity —
stored attributes, which cost nothing — and, for the measured half, whatever a
previous measurement left in the cache.

Each link therefore carries two extra tokens beside its measured state, both
property reads and both additive under heartbeat schema 1:

- `geometryRevisionToken` — the revision the document is at **now**. It moves
  with the timeline count, each managed body's revision and visibility, the
  stored export id and edit version, and the source-identity stamping
  generation.
- `measuredRevisionToken` — the revision the published measurement was taken
  at, or `null` when there is no measurement to offer.

Equal tokens mean `documentSignatureHash`, `documentBodyCount`,
`sourceStateHash`, `bodyFingerprintHash` and `localBodyState` describe the
document as it stands. Anything else — unequal tokens, or a null
`geometryRevisionToken` meaning the revision could not be computed at all — means
they are an observation of an earlier revision and must not be read as current.
Read the hashes, not the tokens, for whether there is an observation to use: an
empty `documentSignatureHash` with `localBodyState: "unknown"` is "cannot tell",
which is what a restart or a switch to a document nothing has measured yet
publishes. It is never another document's measurement and never a claim of a
fresh one.

**An observation is never withdrawn for being old.** There is no age ceiling on
what the heartbeat publishes, because nothing renews a cache the tick may not
measure into: a ceiling would only decide how long a linked idle document took
to lose its baseline for good, and WG refuses to publish a return request or an
exact-target handoff without one. The tokens are what an age ceiling used to
approximate, and they are exact.

A measurement is asked for only when a linked document has no observation worth
publishing, or when a WGLink command the user completed may have moved one —
and by every guarded operation WG asks for, which measures inline. "Has no
observation" rather than "has just changed" is deliberate: a request can be
spent without answering anything (the document stopped being nameable, its
links went, the measurement threw), and a rule that asks once per change never
learns the answer never arrived. `GEOMETRY_REFRESH_RETRY_SECONDS` is the rate
at which such a document may be asked about again, so this is a rate rather
than a retry loop — and rather than a fixed allowance, which would give up on a
document that was merely still loading and leave it with no baseline for the
rest of the session. An observation stops the asking entirely. A document whose
observation is merely *stale* asks for nothing.

Each request names the document it is about, and is dropped without measuring
if that document is no longer active, cannot be named, or has no WGLink links.
At most one refresh is pending per add-in instance — a second request for the
same document is dropped, and one for a different document replaces it, so the
live question is never stuck behind a dead one. It is paid for on the next
tick, the only main thread the Fusion API allows.

A guarded update or return measures inline and refuses a document that has
moved or that WGLink cannot read, so a stale published token can cost a refusal
but never an overwrite; that refusal refreshes the cache, so the next heartbeat
carries the current state.

An unsaved document is identified by its root component's `entityToken`, not by
any Python-side identity: Fusion's bindings mint a fresh proxy per property
read, so neither `id(document)` nor holding the object says anything durable
about the document. A document that cannot be named at all is cached under
nothing and reports "cannot tell".

## File delivery modules

`WGLink.py` owns the Fusion UI, main-thread dispatch and optional file request
service. The former `wglink_watch.py` is split by responsibility:

| Module | Responsibility | Old range at `7ec3c90` |
|---|---|---|
| `wglink_inbox.py` | WG capabilities, Send/Solve inbox writer, acknowledgement reader, atomic request helpers | 133–706, with the relevant constants/helpers from the header |
| `wglink_handoff.py` | Insert/Update handoffs, return-request readers, ordering, claims and recovery | 708–1135 |
| `wglink_status.py` | `.fusion-status.json` payload and durable writer/removal | 1152–1385 |

The planned status start at line 1137 was the export `Announcement` record;
status actually starts at line 1152 in `7ec3c90`. At this slice's starting HEAD,
inbox definitions were at 133–700, handoff definitions at 702–1118, and status
at 1135–1375. A5-1's durable directory sync in status publication is retained. `Announcement`, `ExportWatcher`,
export identity scanning and newer-export prompts are deleted. The user presses
Send; publishing a new export alone never prompts or changes Fusion geometry.
Explicit Insert/Update commands and WG handoffs remain.

## WG packaged-pair references and R-2 re-pin

Installed backend admission lives in `wglink_launcher.py`. It reads the
additive `launcher.argv` / `launcher.ensureBackend` advertisement from WG's
`server/cadlink/fusion_delivery.py` and calls that entry with `--ensure-backend`.
The contract is in WG's `launchers/ensure_backend.py`,
`launchers/backend_start.py`, `docs/INSTALL.md` and
`docs/architecture/CAD-OPERATIONS.md` (Backend ownership / Stage 6).
Read-only collection detection mirrors `server/platform/backend_owner.py`:
`<data>/locks/server.pid` supplies pid/port, and `/health` must confirm the same
pid and filesystem data folder, alongside the existing inbox capabilities.
Capabilities are written at backend startup, so their file age alone cannot
identify a stopped backend. No owner lock is acquired by the add-in.
Only `WG2_DATA_DIR` choosing a non-default data folder adds `--data-dir`;
the CAD workspace never supplies a data-folder override. A single admission
worker joins concurrent commands and returns through the existing custom event.
The worker uses only file/process/HTTP operations and the thread-safe
`fireCustomEvent` bridge; Fusion geometry, dialogs and command continuation
stay on the main thread. Stop cancels the public launcher, whose contract owns
cleanup of any unconfirmed backend child. Successful backends stay detached.
Admission never retries another launcher or data folder.

### Cold start before WG's first launch

A valid capabilities launcher takes precedence. If that advertisement is missing
or invalid, including when the IPC folder does not exist yet, WGLink falls back
to the WG that installed this add-in: `wglink_install.json` already records the
bundle's `waveguideGeneratorRoot`. Its unchanged marker schema supplies the
Windows or macOS installed entry. WG creates the data and IPC folders at startup;
WGLink only reads their paths. The fresh capabilities and delivery versions are
checked before any export or request is written.

Windows requires both the installer-owned `ensure-backend-v1` marker and the
matching signature in the executable. Developer syncs, checkout installs,
hand-copied add-ins and moved or missing bundles require a manual WG start when
no valid capabilities launcher is available. Start a moved bundle manually once
to refresh its advertisement. An invalid advertised launcher still reports its
validation error if the install marker cannot provide a valid fallback.

With `WG2_DATA_DIR`, Fusion must inherit the same override; a per-launch WG
`--data-dir` choice is not discoverable by Fusion. A fresh override installation
still needs a root chosen in WG before the resumed Send can export; otherwise
the existing no-workspace message appears. `folders.json` changes the run and CAD
exchange folders, not IPC discovery in the application data folder.

The WG `dev` files `server/tests/test_wglink_packaged_pair.py`,
`server/tests/test_wglink_shared_protocol.py` and
`scripts/build_wglink_package.py` reference these add-in paths/symbols:

| Add-in source path | Referenced symbols or contract |
|---|---|
| `fusion-addins/WGLink/WGLink.py` | `_fingerprint_hash` extracted by the shared-protocol test; package builder requires the file |
| `fusion-addins/WGLink/WGLink.manifest` | `version` must match WG's `integrations/wglink/source.json` `addinVersion` (still 0.2.1) |
| `fusion-addins/WGLink/wglink_core.py` | literal `PACKAGED_RUNTIME_FILE = "wglink_runtime.json"` |
| `fusion-addins/WGLink/wglink_return.py` | `build_return_manifest`, `dumps_return_manifest`; standalone loader and its failure/foreign-module checks |
| `fusion-addins/WGLink/wglink_watch.py` | packaged-pair test: `CAPABILITIES_FILENAME`, `write_solve_request`; shared-protocol test: `write_wg_request` |
| `fusion-addins/WGLink/wglink_protocol.py` | standalone loader dependency and byte-for-byte vendored provenance; protocol contracts listed below |
| `tests/fixtures/wgreturn-endpoint-oracle/` | shared `PROVENANCE.json`, `cases.json`, `goldens.json`, `wg_ingress_cases.json` bytes |
| `scripts/wglink_resample.py` | required source-preserving package member |
| `LICENSE` | required source-preserving package member |
| `fusion-addins/WGLink/**` | every regular file is included recursively, excluding bytecode/cache; new modules need no builder enumeration change |

The shared-protocol tests reference the preserved vendored protocol symbols
`canonical_json`, `UTF8_STRICT` and the `_wg_`-prefixed profile objects:
`SUPPORTED_MAJOR`, `SUPPORTED_VERSION`, `SUPPORTED_FEATURES`, `EXPORT_FRAMES`,
`DOCUMENT_UP_FEATURE`, `DOCUMENT_UP_AXES`, `DOMAIN_PLANES`,
`DOMAIN_KIND_FOR_PLANES`, `REDUCED_DOMAIN_FEATURE`, `DOMAIN_AUTOMATIC_FEATURE`,
`DOMAIN_AUTOMATIC`, `CUT_FEATURE_KINDS`, `CUT_TOOL_KINDS`, `CUT_ORIGIN_PLANES`,
`CUT_KEPT_SIDES`, `SOURCE_IDENTITY_FEATURE`, `GMSH_PHYSICAL_NAME_MAX_BYTES`,
`SOURCE_IDENTITY_MAX_BYTES`, `WORST_CASE_SOURCE_TAG`, `REQUIRED_BASE_FEATURES`,
`FORBIDDEN_VERDICT_KEYS`, `_WINDOWS_DRIVE`, plus `_mapping`, `_list`, `_string`,
`_integer`, `_required`, `_portable_member_name`, `_fail` and `validate_manifest`.
The literal comparison also scans all `_wg_` functions and uppercase constants.
These protocol bytes and fixtures are unchanged by this split.

**R-2:** in both WG test files, change the `_from_package(...,
"wglink_watch.py")` load to `"wglink_inbox.py"`. The three inbox symbols above
keep their names and signatures. The existing loader already supplies
`wglink_protocol`, the only dependency of the standalone inbox, so it needs no
other change. `write_solve_request` still writes schema 3 for the packaged-pair
oracle, but now requires advertised inbox 4 and Fusion delivery 3; WG `dev`'s
`capabilities()` already advertises these. The production Send/Solve planner
always writes schema 4. No compatibility re-export is retained.

When re-pinning, update WG's `integrations/wglink/source.json` commit and
`server/cadlink/wglink_protocol.source.json` provenance commit together as
appropriate; the protocol SHA stays the same. WG's `server/tests/fixtures/cadlink-endpoint-oracle/PROVENANCE.json`
`addin_commit` must match the package pin (its test asserts this), so
regenerate/update that provenance. The packaged-pair test also pins its
`wg_commit`; keep that WG endpoint-oracle baseline consistent when regenerating.
If the add-in shared-fixture provenance
changes, re-copy it to WG's `server/tests/fixtures/wglink-shared-protocol/` and
update that directory's `MANIFEST.sha256` consistently. Rebuild the package and run both WG suites against the new pin.
The builder itself needs no code change, and `addinVersion` remains 0.2.1.

## Automatic coordination, and switching it on

WGLink runs two engines that act without a user command: the four-second
watch tick and owner-candidate promotion. Together they resolve every managed link
on a timer, publish status every tick, apply WG's handoffs and return requests,
from explicit requests. Exports are no longer watched and no newer-export
prompt is offered. That is **automatic coordination**, and it is off by
default.

The owner can switch it on in WGLink's own settings file,
`~/.hornlab/WGLink/settings.json` (Fusion does not inherit a shell, so this is
not an environment variable):

```json
{"automatic_coordination": true}
```

The value is read once when the add-in starts; restart Fusion or the add-in to
change it. A missing or invalid value leaves coordination off; only a JSON
`true` enables it. An invalid value is reported as `invalid`. With it off:

- neither engine starts or registers;
- WG's handoffs and return requests are not consumed. Insert and Update remain as commands;
- status is published once at start-up, after each command, and removed at
  shutdown, never on a clock. WG therefore sees Fusion's status age out between
  commands;
- the geometry refresh a command asks for is paid for by that command's own
  follow-up, a one-shot main-thread event raised when the command returns;
- interrupted claims are settled once at start-up, read-only and never re-run.
  One whose document is not open, or whose links cannot be read, is kept and
  settled after a later command once it can be;
- a follow-up delivered while another WGLink command holds the main thread
  waits, and that command's completion runs it.

The heartbeat's `diagnostics.activation` states the setting, and
`diagnostics.activity` carries the execution counters: every link resolution,
geometry measurement, source inventory, document signature, status
publication, mutation and export, by the cause that reached it
(`command:<name>`, `startup`, `claim-settlement`, `shutdown`, `tick`,
`promotion`, or `unattributed`). `betweenCommands` is
everything no command asked for; start-up, claim settlement and shutdown are
bounded and excluded, and a standby's timer-started `promotion` is not.

With coordination off the owner's lease has no clock, because no thread renews
it: it is held until that registration is stopped. A second registration in
the same Fusion (a managed and a dev-synced copy, say) therefore does not take
over by itself; it stays idle until it is itself stopped and run again, or
Fusion restarts.

A document whose WG links cannot be read is "not inspected", never "no links":
nothing is reconciled against it, no insert or update is decided from it, and
no status claiming it has no links is published.

## Duplicate-registration ownership and recovery

Fusion may load several registered WGLink paths into one Python process. Each
entry point now loads **all** of its executable `wglink_*` helpers beneath a
registration-unique package name; no registration borrows a helper module that
another checkout put in Python's ordinary import cache.

One deliberately shared, data-only broker elects the registration allowed to
own WGLink's machine-local IPC in that Fusion process. The model is a renewable
lease with two phases:

1. A registration atomically claims an absent or expired lease as
   `constructing`. It replaces any stale panel, creates every command/control,
   registers its private watch event, and only then commits the lease as
   `active`. Any exception rolls back the entire panel/watch transaction and
   releases the claim.
2. Only the active lease owner publishes `.fusion-status.json`, consumes
   handoff/return-request markers, or owns the command panel. Its background
   worker renews the lease without reading Fusion objects; all Fusion API work
   remains in the custom-event handler on Fusion's main thread.
3. Other registrations register only a private candidate event. They publish no
   session id, because they cannot service session-scoped requests. An orderly
   owner removes its panel/status before releasing. If its worker disappears,
   the lease expires after three watch intervals; exactly one candidate claims
   it, transactionally rebuilds the panel and file request service, and becomes the IPC owner.
   A delayed stop from the former owner sees that its lease is gone and cannot
   delete its successor's UI or event.

The broker is process-local by design: two Fusion processes have independent
sessions, while duplicate registrations inside either process must expose one
serviceable session. Fake-backed lifecycle tests cover rollback, two- and
three-registration promotion, orderly owner-first stop, expired-owner takeover,
and realistic occurrence-proxy attribute behavior. The same cases still need
verification in a live Fusion process before this recovery path is considered
field-validated.

### Source identity: live-Fusion verification still owed

Fake-backed tests cover the resolution, refusal, reassignment, rollback and
capability gating above. These depend on Fusion behaviour a fake cannot prove,
and need a live check on macOS and Windows before the feature is field-validated:

- A face split, copied, pasted or patterned carries its original's attribute
  onto every result (the split/copy refusal relies on it).
- Stamps survive an upstream timeline edit and recompute. A lost stamp on a
  face that kept its paint reads as "carries no identity" — a false refusal.
- Painting a face through an occurrence proxy, and whether the native face then
  reads that paint; decision 4 (one face placed twice is one source) assumes the
  proxies of one native face share its stamp.
- `Design.findAttributes` reaches faces in externally referenced components, and
  a read-only referenced component refuses the write so the rollback runs.
- Fusion `==` identifies an attribute's `parent` with the same face reached from
  a body's `faces` (the fakes use object identity or the token fallback).
- Undo of Set WG Source… restores the stamps. Undo moves nothing the heartbeat's
  change key watches, so the published token can stay stale for up to the 60 s
  age ceiling, then reads "cannot tell" until the next measurement (at most the
  120 s duty-cycle wait).
- The plan's exit rows: a source face removed, split or made ambiguous triggers
  reassignment, not a silent remap.

## The Send and Solve pre-flight

Both export dialogs state what is about to happen before OK: bodies included,
WG links in scope, and each source with its role and measured area. A model
with no drivable source is named there rather than at the dead end after OK.

A return with no WG links in scope is legal — a model built from scratch in
Fusion is exported as an *unlinked* return — but it carries no throat frame, so
WG solves it in the assembly frame exactly as modelled: radiation along +Z,
throat at z = 0, bounding box centred on x = 0 and y = 0. Nothing enforces that
convention on either side, and a mis-framed model simply yields wrong
directivity, so the pre-flight measures the model against it and says which of
the three assumptions the model breaks. It never refuses.

Insert remembers the last bundle folder. When a document has several managed
links, Update and Detach show a **Managed link** dropdown listing them by design
name; a single-link document is not asked. Head-less Audit and Relink callers
choose with `options['instance_id']`. Send instead exposes an anchor choice only
when its selected scope contains several linked instances.

## Update atomicity and recovery

Fusion offers no transaction covering parameter edits and sketch fit-point
moves. WGLink validates identity, build mode, bundle content, resampler output,
ring counts, points per ring, interface sketches, and rollback availability
before the first mutation. It also compares the tagged throat face centre with
`(0, vertical_offset)` and its plane with the stored throat z. If a body Move
has carried the body away from its own datums, Update reports the measured
x/y/plane-z offset and refuses. Undo the body Move, or use **Detach** if the
geometry is now genuinely user-owned. Head-less callers can pass `force=True`
when proceeding is intentional. It then performs one rolled-back pass and
restores the timeline marker for a single recompute. A progress JSON file is
written after every ring.

The frame guard is deliberately component-local. Moving the whole wrapper
component moves its body and managed datums together, is a legitimate assembly
placement, and still passes. The guard only detects a body moved relative to
those datums. The head-less Audit API never refuses: it reports the offset and
marks the local body state `modified` when the invariant fails.

That is the strongest honest boundary Fusion exposes; it is not a database
transaction. If a rebuild fails after mutation begins, use **Undo** to recover
the document. Do not continue modelling on a partially failed rebuild.

## What the head-less Audit API can and cannot prove

Audit is evidence, not a freshness authority. CAD cannot author WG freshness.
It can observe a missing or changed body, parameter drift, the geometric source
tag, and unhealthy timeline features. Parameter drift is measured numerically,
not textually: Fusion does not re-emit a parsed expression bit-for-bit, so two
spellings of one length within `EXPRESSION_REL_TOLERANCE` (1e-9 relative,
1e-9 mm absolute) are the same value. That band is far below the finest change
a user can enter, and far above the float round-trip it exists to absorb. Fusion has no face-to-feature reverse
index, so Audit cannot enumerate every direct B-rep reference and cannot detect
a reference that silently rebound to the wrong face. A green Audit therefore
does not prove semantic reference correctness. Direct face/edge references are
unsupported where a stable interface sketch or datum can be used instead.

The source-role appearance is reasserted geometrically after every Update.
This removes appearances that spread to derived faces during recompute and
repaints the one planar throat disc at the expected realized area.

## Referencing managed artifacts from your own component

The managed datums, sketches and body live inside the wrapper component, so a
sketch or feature in the root cannot reference them directly — Fusion answers
`planarEntity is not in the assembly context of this component`. Reach them
through the occurrence, which is ordinary Fusion practice:

```python
plane = occurrence.component.constructionPlanes.itemByName("WG_BAFFLE_PLANE")
host = plane.createForAssemblyContext(occurrence)   # now usable from the root
```

The same applies to faces of the managed body. In the UI this is automatic —
clicking the datum inside the occurrence picks the proxy for you.

Fusion grounds the first component created in a design to its parent. On Insert,
WGLink clears that flag from its wrapper so the component can be placed and
jointed. For a document built before this change, right-click the wrapper in the
browser and choose **Unground From Parent**, or re-run Insert.

## Measured behaviour, Fusion 2704.1.53

Tritonia-V enclosure and asro68 freestanding, throwaway documents:

| | measured |
|---|---|
| enclosure volume vs the mesher's own solid | **−0.0047 %** (53,558,269 mm³), one all-edge treatment |
| enclosure face inventory | 3 planar-z, 8 at 45°, **8 at 54.7°** (three-way mitres), 8 vertical, 1 cavity |
| enclosure bounding box | exact on all six faces, before and after |
| cavity surface vs the mesher's grid | mean **0.00016 mm**, max **0.0012 mm** |
| update | 2572 fit points moved, **0 features regressed**, including a user feature built on a managed datum |
| source tag | one face, 506.70 mm², 0 strays, before and after |
| freestanding solid vs the mesher's | −0.75 % (the mesher exports an opened throat; the native build has a driver plate) |

The pre-change deviation maximum was confined to the last three stations at
the mouth. It persisted as section density increased because the former axial
duplicate mouth ring forced the fitted loft's end tangent. The loft now ends at
the real mouth ring and a separate face extrude provides the punch-through;
Fusion-side deviation and volume measurements for that change are pending.

The one-feature enclosure measurement above is from the owner's hand-corrected
document. The implementation enforces a twelve-edge input before creating the
single treatment, but the add-in-built corner face inventory and resulting
volume still require the Fusion verification fixture.

**Not verified:** §5.6's moved-wrapper case. Setting `occurrence.transform2`
from a script reads the new matrix straight back, but after
`design.snapshots.add()` the occurrence is at identity again and nothing has
moved — reproduced on a plain component with no WGLink involvement, so it is
Fusion's scripted-move behaviour rather than the add-in's. Dragging the wrapper
in the UI and pressing Update is therefore still an unchecked path.

## Install

Waveguide Generator's macOS and Windows platform installers install a packaged
copy and point it at WG's own pinned scientific runtime. That is the end-user
path: it needs no hornlab-fusion-addin checkout and no second virtualenv.

For development, run from this repository checkout:

```sh
.venv/bin/python scripts/install_wglink_addin.py --symlink
```

The symlinked install is recommended because Update must invoke the repository
`.venv` and `scripts/wglink_resample.py`; Fusion's embedded Python does not have
the scientific interpolation stack. A copied install can still work when
`HORNLAB_FUSION_ADDIN_REPO` points to the checkout, or when a head-less caller
passes `repo_root` and `python_path` options.

Restart Fusion and use the WGLink toolbar panel under **Utilities**. The
manifest sets `runOnStartup` to `true`: start-up only registers a panel and its
command definitions, and every piece of geometry work happens when a command is
executed, so there is nothing heavy to defer. Leaving it `false` meant the panel
was gone after each Fusion restart until it was started by hand.

Icons live in `resources/<operation>/{16x16,32x32,64x64}.png` and are generated
by `scripts/make_wglink_icons.py`. A checkout without them still works; Fusion
falls back to unadorned buttons.
