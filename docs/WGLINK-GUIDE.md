# WGLink User Guide

WGLink connects Fusion 360 to the Waveguide Generator app (WG). It inserts a
WG design as native, managed Fusion history, keeps it updated as the design
changes in WG, and sends Fusion geometry back to WG to be solved — including
models drawn from scratch in Fusion. This guide is for using the add-in; the
architecture and update limits are documented in
[`fusion-addins/WGLink/README.md`](../fusion-addins/WGLink/README.md), and the
WG side of the workflow in WG's own user guide.

## 1. Install

The ordinary Waveguide Generator platform installer installs or updates WGLink
on macOS and Windows. It also connects Update to WG's existing pinned Python
environment, so the Fusion add-in needs neither this source checkout nor a
second virtual environment. Restart Fusion after installing WG and confirm
**Run on Startup** is ticked for WGLink under **Utilities → Scripts and
Add-Ins**.

For add-in development, install from this repository's checkout instead:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/install_wglink_addin.py --symlink
```

Use the symlink install for development. Update resamples spline profiles by invoking this
repository's `.venv` and `scripts/wglink_resample.py`; Fusion's embedded Python
does not have the scientific stack. Those packages come from the add-in
checkout's own active environment — the installer never probes sibling
checkouts for them.

Fusion's own record of the Run on Startup toggle
overrides the add-in manifest, so a copy once started by hand stays manual
until the box is ticked. Install from exactly one location: a second copy
loads a second module instance. WGLink elects one active copy and keeps the
others as recovery standbys, but duplicate installations still make upgrades
and support needlessly ambiguous.

One-time setup on the WG side: choose a **WGLink folder** in WG under
**Settings → CAD Link**. The add-in reads the same setting, so Insert and
Send need no folder dialogs afterwards.

Exports are no longer watched. Press **Send to WG** to send Fusion geometry;
use **Update** or an explicit WG **Send to CAD** Update handoff to apply a new
WG export. Creating an export alone never raises a newer-export prompt.

Internally, `wglink_inbox.py` handles WG requests and acknowledgements,
`wglink_handoff.py` reads Insert/Update and return requests, and
`wglink_status.py` publishes Fusion status. `WGLink.py` owns the panel and
main-thread command execution.

### Compatibility

Compatibility comes from WG's advertised capabilities, not its product version
string. Insert, Update, WG handoffs and return requests require capability
schema 1 and integer `fusionRequestDelivery >= 3`. They continue to work when
WG's CAD Link request consumer is off. Send and Solve additionally require
integer `solveCommandDelivery >= 4`. An unsupported schema or delivery version
asks you to **Update Waveguide Generator** before export or model mutation.
A missing advertisement or disabled Send/Solve consumer asks you to start WG
and check its CAD Link folder.

Return requests publish a `.wgreturn` bundle for WG to read directly; they do
not write WG's Send/Solve inbox. Like Send and Solve, they still require WG's
`automaticDomain` and `documentUp` manifest capabilities before export.
`sourceIdentity` remains optional.

The file-channel split deliberately changes these compatibility behaviors;
include them in the R-2 re-pin and release notes:

- Solve no longer falls back to schema 3. New Send and Solve requests always
  use schema 4 and require `solveCommandDelivery >= 4`.
- Unanswered items from earlier sessions in `.wglink-outbox` stay queued for
  an older or unavailable WG until a later startup with both delivery channels
  compatible. Previously, with inbox 3, queued Solves were converted to schema
  3 and queued Sends were removed with a not-delivered notice. Answered items
  retain their existing handling.

## 2. The panel

Three everyday commands sit directly on the WGLink panel. **Manage WG Link…**
contains only **Declare Body…**, **Insert**, **Update**, and **Detach**.

| Command | What it does |
|---|---|
| **Set WG Source…** | Mark the selected faces as the `LF`, `MF`, `HF`, or `PASSIVE_CARDIOID` drive source (applies an appearance with that exact name and stamps the source's identity; Clear removes both). A face already painted `PORT_EXIT`, the role's old name, is still recognised. |
| **Solve in WG** | Start WG's backend when needed, export the assembly and ask WG to prepare and solve it. |
| **Send to WG** | Export the assembly as a validated `.wgreturn` bundle without asking for a solve. |
| Declare Body… | Classify a body for the return: `exterior-shell` includes an open surface body, `exclude` leaves a body out, Clear restores automatic scoping. |
| Insert | Insert a WG `.wglink` bundle as a managed link. Optionally give the link a **Link name** of your own; leave it empty to use WG's design name. Ordinarily unneeded: WG's Send to CAD offers the insert automatically. |
| Update | Rebuild a managed link in place from its design's latest locator, even if its stored bundle still exists. An invalid locator refuses. Without a locator, use the stored bundle; if it moved, find the newest export of the same design in WG's current workspace. |
| Detach | Permanently remove WGLink identity without changing geometry. Fusion asks for confirmation because the only way back is to insert a fresh copy from WG. |

Audit, Relink and renaming a link remain available through the headless
`wglink_core.audit`, `wglink_core.relink` and `wglink_core.set_link_name`
APIs. Audit's document/link data is also published
continuously to WG in `.fusion-status.json`, so it does not need a panel
command. Newer heartbeats add optional `linkName`, `bodyObjectIds`,
`transformHash`, `sourceIds`, and `driveChannelIds` members to each managed
link. They come from
the same strict observation used by Send to WG; an unavailable or fallback
identity is omitted, while older WG clients can continue ignoring the added
members under heartbeat schema 1.

**Send to WG** and **Solve in WG** publish the complete return bundle before
writing one request to `ipc/wglink/.wg-solve-requests/<operationId>.json`.
Send imports and displays the snapshot; Solve also asks WG to prepare and solve.
A request keeps the same id across a delivery retry, so WG can recover it as
one operation. Pressing Send or Solve again creates a new request.

With a current installed WG, **Solve in WG** and **Send to WG** start its
backend when needed through the launcher WG last advertised. Startup runs in
the background; Fusion remains responsive. Presses during startup share one
startup attempt, then each command exports and submits on Fusion's main thread.
An already collecting backend receives the command without another launch.
The backend uses the same WG data folder, saved settings and history; opening
WG later shows the same jobs and results. A configured non-default
`WG2_DATA_DIR` is passed explicitly to the launcher.

Startup is bounded to at most 75 seconds of waiting. If startup fails, WGLink
shows the reason and asks you to start WG manually; it exports and writes no
request. If you switch documents while WG is starting, run the command again
from the intended document. Older WG installations and development checkouts
that advertise no supported launcher still need WG started manually.
Insert, Update and WG-initiated handoffs do not start the backend.

The add-in exchanges requests, acknowledgements, Insert/Update handoffs and
status with WG through files. With acknowledgement support, WG's durable
acceptance is shown after it takes the request; until then, the message says
it was written to the inbox. If WG leaves it waiting for a minute, WGLink says
so once. A refused acknowledgement includes WG's reason; fix it and send again.

Earlier queued returns are migrated to request files once at startup. If an
older WG cannot accept a queued Send, WGLink reports it as not delivered and
asks you to update WG and send again. A failed migration keeps the request
under its original id for the next startup.

## 3. The linked round trip

1. In WG, **Send to CAD** writes the bundle and raises Fusion; the add-in
   offers the Insert (first time) or Update (afterwards) — one click.
   If the Fusion document contains multiple managed placements of the same WG
   design, choose the instance in WG first. The handoff names that exact
   instance; WGLink refuses missing, stale, or ambiguous placement identity
   rather than updating whichever copy happens to appear first.
2. Edit in Fusion: move and joint the **wrapper occurrence**, not the managed
   bodies inside it. WG parameters appear as `wg_<name>_*` user parameters.
3. **Solve in WG** sends the geometry back and starts the solve. WG switches
   itself to CAD mode and shows progress; if the ingestion reports blocking
   findings, WG parks the request and shows what it is waiting on.

Renaming the WG design is safe: the parameter namespace stays fixed to its
lineage. Each new export has its own immutable bundle folder; existing legacy
folders remain readable.

### Immutable exports and manual Update

WG publishes one `<export-id>.wglink/` directory per export in the existing
`wglink` exchange folder. Its contents keep the existing bundle format.
`<design-id>.latest.json` selects the current export. Manual Update reads that
locator in the current WG exchange folder (then beside the stored bundle)
before accepting the stored path, validates its exact keys, schema,
positive sequence, relative basename, manifest IDs and checksums, and refuses
symlinks. A present but invalid locator never falls back to an older export.
No locator means the legacy stored-path and workspace lookup still applies.

An explicit WG handoff always opens its own `bundlePath`, never latest. Missing
paths or bundle/export ID mismatches refuse before creating or changing a
Fusion document. The optional `bundleLayout: "immutable-v1"` is additive to the
existing handoff fields. Document attribute names, schema and parameter slugs
stay the same; `bundle_path` records the immutable export actually applied.

The add-in advertises `immutableBundles: 1` in its existing Fusion heartbeat.
An older WG ignores that extra field and its stable-folder exports still work.
Older add-ins still accept automatic Sends to immutable paths; their manual
Update stays on the applied export until the add-in is updated through WG's
add-in update action and Fusion is restarted. There is no mutable mirror.

### The three names a link has

A linked document carries three separate names, and they are allowed to differ.
Nothing is wrong when they do.

| Name | Where it comes from | What it controls |
|---|---|---|
| **Fusion document name** | You, in Fusion (`waveguide v1`) | The Fusion document and its `.f3d`. WGLink publishes it to WG, which titles the CAD project with it. |
| **WG design name** | WG, from the design you exported (`260308Tritonia-M`) | Nothing, on its own. It is a label WG stamps into the bundle, and it follows a rename in WG. |
| **Link name** | You, optionally, in the Insert dialog | The link's label in WGLink's menus, in the timeline group, and in what WG shows for the placement. Nothing else. |

The `wg_<name>_*` user parameters are a **fourth** thing, and no name above
moves them. That namespace is minted from the design's name the first time it
is exported, and then frozen to the design's lineage for good. Fusion datums,
enclosure expressions and your own features all reference it by name, and
Fusion cannot retarget an expression to
a different parameter. So a design first exported as `260308Tritonia-M` keeps
`wg_260308tritonia_m_*` even after it is renamed in WG, and even in a document
you called something else. New bundle folders use the export ID; old stable
folder paths remain readable. Keeping the namespace preserves every existing
linked document through design renames.

Rename a link at any time — it changes a label and nothing a rebuild or a
return reads:

```python
wglink_core.set_link_name(app, "Left waveguide")            # one link
wglink_core.set_link_name(app, "Left waveguide", {"instance_id": "..."})
wglink_core.set_link_name(app, "")                          # back to WG's name
```

The wrapper component (`WGLink_<name>_1`) is an ordinary Fusion component and
you can rename it in the browser yourself; WGLink finds its links by stored
identity, never by a component or timeline name.

## 4. Starting from a model drawn in Fusion

A from-scratch model — no WG design behind it — is a legal return. Three
requirements:

1. **A drive source.** Mark the throat or diaphragm face with
   **Set WG Source…**. Hand-painting an appearance named exactly `LF`, `MF`,
   `HF`, or `PASSIVE_CARDIOID` onto the face does the same thing.
2. **Exterior bodies.** One visible, undeclared solid or surface body is taken
   as the exterior shell. With multiple visible bodies, classify open surfaces
   with **Declare Body…** (`exterior-shell` or `exclude`); otherwise the export
   refuses them as unclassified. Hidden bodies are intentionally left out.
3. **The solver frame.** With no link to anchor the model, WG assumes it
   radiates along **+Z**, throat at the **origin**, centred on x = 0 and
   y = 0 so mirror symmetry can be found. The Send/Solve dialogs show a
   pre-flight summary — scope, sources with areas, and bold warnings when the
   frame looks wrong. Fix placement before sending.

In WG the return arrives marked `unlinked`; acknowledge that one finding, set
mesh sizing and drive channels in the CAD Link panel, and solve.

### Source identity

A Waveguide Generator that reads source identities keeps a painted source's
settings from one return to the next by the identity **Set WG Source…** stamped
on its faces. Once WG reads them, Send and Solve refuse a painted role, and the
message names it, when:

- faces were painted by hand or before identities existed — select them and run
  **Set WG Source…** with that role; faces added to a source that still resolves
  keep its identity;
- a face's paint was removed or changed by hand, a face was removed, split or
  copied, or the role carries two identities — select **every** face that should
  drive the role and run **Set WG Source…** again. That gives the source a new
  identity, and WG asks for its setup once;
- some of the source's faces are outside what you are sending — include them,
  or select them and **Clear** their WG source. Clear also takes the identity off
  a face whose paint you already removed by hand.

A linked waveguide's throat needs nothing: its identity follows the link.

**Upgrade note.** The first time WG advertises source identities, every
document's status token changes once, because source ids are part of it. WG
therefore shows models it imported earlier as changed; send them again. Painted
sources marked before this release are refused until you re-run
**Set WG Source…** on them.

## 5. Automatic model domain

There is no Model domain dropdown. Send and Solve show **Automatic (WG
decides)** in pre-flight, and WG chooses the smallest safe domain from the solve
mesh.

For a model already cut on an origin plane, WGLink reads the Fusion timeline
during that explicit Send or Solve. A live, unsuppressed **Split Body** or cut
extrude whose current result has a planar cut face on the YZ, XZ or XY origin
plane is recorded with its feature name and the side the exported body kept.
The extrude sketch plane alone does not establish the cut plane. A Split Body
tool on an origin plane is also recorded; a construction plane counts only when
it is coincident with an origin plane (for example an offset of zero). If the
cut face cannot be read at the current timeline marker, its advisory descriptor
is omitted. Pre-flight shows evidence such as **Cut: Split Body 3 at x = 0**.

WG revalidates that evidence against the solve mesh. Supported positive-side,
open cuts can be mirrored; negative-side, capped, leaking or otherwise unsafe
cuts are refused with a remedy. With no surviving cut evidence, a pre-cut shape
is solved as shown rather than guessed to be symmetric. Full models keep WG's
validated automatic cutting.

The automatic contract is negotiated. A capable WG receives
`assembly.domain = {"kind":"automatic"}` under `domain-automatic-v1`, plus any
recorded `assembly.cut_provenance`. This WGLink version refuses Send, Solve and
WG-requested returns against an older WG, because silently omitting those fields
would change the model-domain meaning and discard the cut evidence. Update and
restart WG before exporting.

Sources need no adjustment. A half model's drive face is already half its full
area, which is exactly what WG's own cutter would have produced, and the solver
scales the mirrored domain the same way either way.

## 6. Assembly scope

**Leave Assembly scope empty** to return the root component, or select exactly
one occurrence.

A selected occurrence is exported as its **component**, in that component's own
frame. Fusion's STEP export takes a Component and writes it in its own
coordinates; it offers no way to export an occurrence in its assembly
placement. So the bundle states which frame it is in
(`coordinate_system.export_frame`), and every coordinate in it — the bounding
box, each instance placement, and cut plane — is read from
the native bodies, which Fusion defines as the bodies "outside the context of
an assembly". A moved or jointed wrapper occurrence therefore returns
correctly, which is the ordinary WGLink round trip: Insert places the wrapper,
you move and joint it, and Solve in WG returns it.

When WG advertises support, the return also records Fusion's current general
Y-up or Z-up modelling orientation as `coordinate_system.document_up`. This is
captured at export, so changing the preference later does not reinterpret an
older return.

The one shape this cannot do is a **placed occurrence that itself contains
sub-assemblies**. Its children's bodies are native to their own components, and
getting from there to the exported frame means composing the placement chain —
arithmetic WGLink will not do unverified. That case is refused, and there are
three real ways forward: leave Assembly scope empty and send the whole root
assembly, select one of the sub-assemblies on its own, or move the occurrence
back onto the assembly origin by editing or deleting the joint or Move feature
that placed it. (Fusion's **Ground** is not one of them: it freezes an
occurrence where it already is and never moves it back to the origin.)

## 7. FEM regions for coupled FEM/BEM solves

A WG that advertises `femRegions` can solve bounded air (a rear chamber, a
port, a phase plug, a compression-driver cavity) with tetrahedral FEM and
couple it to the BEM exterior, the way Boundary Lab does. Model each air
volume as **one closed solid body**, then run **Manage WG Link… > Declare FEM
Region…** on it:

1. Select the air body. A body declared before reloads its settings and faces.
2. Name the region, and set its mesh size and bulk loss factor (0 is lossless
   air; Boundary Lab's presets 0.002 to 0.05 add damping).
3. **Add group** for every face set that is not a plain wall, and give each a
   name, a kind, a local mesh size and its faces:
   - **Driver diaphragm (moving)** with a drive role (LF, MF or HF). It joins
     the same drive channel as a painted source of that role. **Relative level
     (dB)** sets its motion relative to the driver (default 0, from −120 to 0
     dB inclusive).
   - **Interface to exterior air** for each opening where the chamber meets
     the exterior: a port exit, a horn throat. WG makes the exterior mesh
     conform to it.
   - **Plane-wave termination** for an anechoic tube end.
   - **Rigid wall (local size)** for a wall that needs a finer mesh. Enable
     **Absorbing lining** for Miki lining, then set its thickness (default
     30 mm) and flow resistivity (default 5000 Pa·s/m²); both must be positive.

   Every face in no group is a rigid wall at the region size. OK stays
   disabled, with the reason shown, until the declaration is complete: one
   group per face, faces of the selected body only, unique names, and a drive
   role for each diaphragm.
4. **Hide the air body** before Send or Solve. Fusion exports by visibility, so
   a visible air body would also be sent as exterior geometry; WGLink refuses
   that by name and exports each region separately.

The declaration lives on the body and on each face as WGLink attributes, so it
follows the faces through later edits. A group whose faces all disappeared
(a face rebuilt by an edit) refuses the Send rather than silently becoming a
wall: reopen the dialog and reselect its faces. A diaphragm in a FEM region
drives the return on its own, so a compression driver needs no painted source.
Several moving groups sharing one drive role mean **one physical driver**:
front and rear surfaces of its shared diaphragm in two regions, or dome and
surround in one region. A role with several painted channels is ambiguous and
refused. WG refuses ideal-source channels with FEM diaphragms in more than one
region. An electrodynamic channel alone does not prove a shared diaphragm.
Two separate physical drivers must use separate drive channels (roles), then
WG's **Combine**.

Pre-flight lists each region as, for example, **FEM region: Rear chamber
(8 mm; 1 interface, 1 moving)**. The return carries `fem-regions/<id>.step`
per region and `scope.fem_regions` under the `fem-regions-v1` feature. Faces
travel as area, centroid, normal and bounding box in millimetres; WG matches
them to the meshed surfaces and keeps ownership of every mesh tag. Against a WG
without `femRegions`, a design that declares regions is refused, and one that
declares none sends exactly as before.
`femRegions >= 1` accepts v1 with optional rigid-group `lining` and
moving-group `relative_level_db`; WG converts the dB value to the linear motion
weight `10 ** (relative_level_db / 20)` and rejects a converted weight that is
zero or nonfinite. WG maps `lining` to BEAT
`wall_impedance {model: "miki", thickness_m: thickness_mm/1000, flow_resistivity_pa_s_per_m2: unchanged}`.
Changed lining or level invalidates the return-state hash.

FEM regions are sent in the root frame only: leave Assembly scope empty, or
select an occurrence placed at the assembly origin.

## 8. Troubleshooting

- **"Why is this link called something I never typed?"** — it is WG's name for
  the design, not a name for your Fusion document. See *The three names a link
  has* in section 3. Give the link your own **Link name** at Insert, or rename
  it afterwards with `wglink_core.set_link_name`. The `wg_<name>_*` parameters
  keep the bundle's namespace either way, on purpose.
- **"Waveguide Generator has no selected CAD Link workspace"** — choose the
  WGLink folder in WG under Settings → CAD Link, then send again.
- **"The CAD Link folder Waveguide Generator is set to no longer exists"** — the
  folder WG remembers was moved, renamed or deleted. The message names it.
  Choose the folder again in WG under Settings → CAD Link, then send again.
- **"WGLink cannot find the component that holds the WG waveguide …"** — Send
  could not find the component the waveguide was inserted into, so it cannot
  say where the waveguide sits, and it will not guess. If you deleted or
  replaced that component, undo it and send again. If it is still there, do
  not insert a second copy (a whole-assembly Send would carry both); report it
  with the message. What makes the component unfindable in that case is not
  yet known.
- **The panel is missing after a Fusion restart** — tick Run on Startup for
  WGLink (see Install); Fusion's toggle overrides the manifest.
- **"WGLink helper body … is still visible"** on Send — a freestanding
  insertion leaves a stitched surface shell behind, and Fusion's STEP export
  writes every visible body of the exported component, so that shell would
  reach the solver as a second radiating surface. Insert now hides it, but a
  document inserted by an earlier WGLink still has it visible. Hide **the body
  itself** in the browser — the refusal names it — and send again, or run
  Insert again. Hiding a folder that contains the body will not work: Autodesk
  exports a body that is invisible only because its group is hidden as if it
  were visible.
- **A surface sits on top of the waveguide in the browser, and I selected and
  simulated it by mistake** — a freestanding insertion leaves a stitched
  surface shell behind. It is zero-thickness and coincident with the final
  solid, so in the browser and in the canvas it is easy to pick instead of the
  body you meant, and a solve started from it is a solve of the wrong
  geometry. Insert hides it, and so does **Update** — run Update on the
  document and WGLink hides every leftover helper body it still shows,
  reporting them under `helpers`. Audit names them without touching anything,
  so you can see which document is affected before you change it. A document
  built before this change keeps the shell visible until one of those runs.
  Failing that, hide **the body itself** in the browser. Hiding a folder or
  group that contains it does **not** work: Autodesk exports a body that is
  invisible only because its group is hidden as if it were visible, so it
  would still reach the STEP file and the solver. Do not delete the shell —
  WGLink reads its managed bodies by role for tag repair and anchoring, and a
  deleted one cannot be restored without recreating the link.
- **"WGLink parameter namespace mismatch"** on Update — the document's link
  predates the stable-namespace fix and its bundle was renamed. The refusal
  names the recovery: Detach and delete the component, then Insert; the
  rebuild restores the managed bodies, datums and parameters, and only
  user-authored features on the old parameters need repointing.
- **"WGLink Insert creates parameters; it never takes over ones it did not
  create"** — the namespace this Insert would have used is already occupied by
  parameters WGLink does not own, and it could not allocate a free one either.
  Insert normally steps to the next free namespace on its own: Detach leaves a
  link's `wg_<name>_*` parameters and the geometry they drive behind, so a
  fresh Insert of that design takes `wg_<name>2_` rather than reassigning the
  detached model's expressions. The refusal names the colliding parameters;
  rename or delete them, or keep them and let the new link have its own
  namespace. Only Update rewrites parameters, and only for the namespace a
  link this document still records was minted with.
- **"The linked bundle could not be found in the current WG workspace"** —
  Update already searched the selected WG workspace by design identity. Put the
  bundle back under that workspace and run Update again. If it was deliberately
  moved elsewhere, re-point it with the headless `wglink_core.relink` API.
- **Nothing arrives in WG after Solve in WG** — current installed WG can start
  its backend automatically. A startup refusal shows the reason; start WG
  manually and try again. A request already written survives until WG takes it.
  Open the WG window to see progress and results.
- **WG shows an update as still running after Fusion restarted** — WGLink
  settles what it can against the open document, read-only, and never runs an
  interrupted update again. If the update was for a document you do not have
  open, WGLink keeps it rather than reporting it as never started; open that
  document and it is settled. An update that had started changing the model
  reports **Update interrupted — recovery required**: undo the partial change
  or repair the link, then send it from WG again.
- **Duplicate WGLink installations** — remove the extra registration and
  restart Fusion. Current copies elect one active panel and file request service and promote a
  surviving standby if the owner stops, but a mixed-version duplicate remains
  an unsupported installation to diagnose.
