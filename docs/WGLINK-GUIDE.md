# Waveguide Generator add-in for Fusion: user guide

The Waveguide Generator add-in connects Autodesk Fusion to the Waveguide
Generator app (WG). It does two things:

- it sends a Fusion model to WG to be set up and solved, including models
  drawn from scratch in Fusion;
- it inserts a WG waveguide into Fusion as native geometry, and updates it
  when the design changes in WG.

This guide is for using the add-in. WG's side of the workflow, including driver
setup, solvers and results, is in WG's CAD Link guide and user manual (in the
`waveguide-generator` repository, `docs/CAD-LINK-GUIDE.md` and
`docs/USER-MANUAL.md`). Developer material lives here:

- [Implementation and development guide](../fusion-addins/WGLink/README.md):
  compatibility, delivery, file layout, the head-less Audit and Relink tools,
  and installing from a checkout;
- [Naming a link](WGLINK-LINK-NAMING.md), including the head-less
  `wglink_core.set_link_name` rename;
- [Validating a change without a release](WGLINK-DEV-LOOP.md);
- [Request status contract](REQUEST-STATUS.md) and
  [mesh settings contract](MESH-SIZING-V1.md);
- [Head-less workflow status](HEADLESS.md).

## 1. Install

WG's installer for macOS and Windows installs or updates the add-in and
connects it to WG's own Python runtime, so nothing else is needed. If the add-in
is missing, close Fusion and use **Install add-in** on WG's **CAD Link** panel.

1. Restart Fusion after installing WG.
2. Open **Utilities → Scripts and Add-Ins → Add-Ins**, select **WGLink**, run
   it, and tick **Run on Startup**. Fusion's own record of this checkbox wins
   over the add-in's setting, so a copy once started by hand stays manual until
   you tick it.
3. The commands appear in the **Waveguide Generator** panel under
   **Utilities**. The add-in list keeps the name `WGLink` so updates and
   existing documents keep working.
4. In WG, choose the **WGLink folder** under **Settings → CAD Link**. The
   add-in reads the same setting, so you never choose a folder in Fusion.

Install from one place only. A second copy is kept as a standby, but it makes
upgrades and support harder; remove the extra registration and restart Fusion.

## 2. The panel

| Command | What it does |
|---|---|
| **Sources & Mesh…** | Set up sources and mesh, then Solve. |
| **Solve in WG** | Send and solve now. |
| **Show Panel** | Show the Waveguide Generator panel: Sources & Mesh, Solve, the solve settings, and the progress of each run. |
| **Advanced** | A menu of the less frequent commands below. |
| Send to WG | Open this model in WG to inspect or set up. Does not solve. |
| Declare Body… | Mark a body as **Exterior shell** (solve its outer surface) or **Exclude** (leave it out); **Clear declaration** goes back to automatic. |
| Declare FEM Region… | Mark a solid body of air as a FEM region (section 7). |
| Insert | Insert a WG design as a managed link. Usually not needed: WG's **Send to CAD** inserts it for you. You can give the link a **Link name** of your own. |
| Update | Rebuild a managed link in place from the newest export of its design. |
| Detach | Remove the WG link data without changing the geometry. Fusion asks first, because the only way back is a fresh Insert. |

## 3. Sources & Mesh…

Open **Waveguide Generator ▸ Sources & Mesh…** to tell WG which faces make
sound and how fine to mesh the model.

1. **Drivers.** One fixed row for each role: **HF**, **MF**, **LF** and **Port
   or passive opening**. Click a row's **Pick faces**, then click faces in the
   model. One face picker fills whichever row you chose. A face picked for a
   second row moves there, and the dialog says so. **✕** clears a row. Leave
   unused rows empty. Drivers that come from a linked WG design are read-only.
2. **Walls.** The triangle size for every surface that is not a driver and not
   in a size zone.
3. **Size zones.** Optional parts that are finer or coarser than the walls.
   Click **+ Fine size zone (15 mm)** or **+ Coarse size zone (40 mm)**, name
   it, change its size if you like, and pick its faces or whole bodies.
   Removing a zone gives its faces back to the wall size.
4. **Advanced.** **Grading distance** (how far from each driver the mesh grows
   to the wall size), **Symmetry** (**Automatic** or **Off**; Off solves the
   whole model, which is slower) and **Reset to defaults**.
5. **Estimate.** About how many triangles WG will solve, against WG's limit.
   Until WG has seen the model it is a range from a quarter model to the whole
   model, because WG decides whether it can use symmetry. After a solve it
   also shows the count WG last used.
6. Click **Save**, or **Cancel** to change nothing. Saving stores everything in
   the Fusion document and sends nothing. Then use **Send to WG** or **Solve in
   WG**.

Defaults: HF 6 mm, MF 15 mm, LF and port 25 mm, walls 25 mm, grading distance
100 mm. Smaller sizes mean more triangles.

The dialog gives each driver a lasting identity, so WG keeps that driver's
setup from one send to the next. If you split, copy or delete a driver face,
or repaint one by hand, reopen Sources & Mesh…, pick every face of that driver
again and Save. WG then asks for that driver's setup once more.

## 4. Send and Solve

1. Click **Send to WG** or **Solve in WG**.
2. **Assembly scope**: leave it empty to send the whole design, or select one
   occurrence.
3. **Solver anchor instance** (only with several linked waveguides): the one
   whose throat and direction set the solve's origin and axis.
4. Read **Pre-flight**. It lists the bodies, drivers, any cut and FEM regions
   that will be sent, and sorts problems three ways:
   - **Fix first.** **Send** or **Solve** stays disabled and the text says what
     to change. Examples: no driver and no moving FEM surface ("This model has
     no driver yet."), a driver or size zone without faces, a face in two rows,
     a size zone without a name or with a repeated name, an unclassified
     surface body.
   - **Asked when you click OK.** A body that is not a closed solid asks
     whether to send anyway. A Solve whose smallest possible mesh is still
     over WG's limit asks: **Yes** solves anyway, **No** opens Sources & Mesh…,
     **Cancel** goes back. If you solve anyway here, WG does not ask again.
   - **Noted.** For example, a size zone whose faces are all outside the
     selected occurrence is left out of that send.
5. Click **Send** or **Solve**.

If WG is not running, the add-in starts it in the background where it can and
then sends; Fusion stays responsive. If WG cannot be started, the add-in says
why and sends nothing; start WG yourself and try again.

### The progress panel

The **Waveguide Generator** panel docks on the right whenever you click Send
or Solve, and opens by itself when WG needs you. Close it whenever you like;
**Show Panel** opens it again. It shows each request:

- "<model> is open in Waveguide Generator." after a Send;
- "Preparing the simulation…", "Waiting for the solver to start.", then
  "Solving · 12 of 36 frequencies";
- "Solved (run 4)." with **Show results in Waveguide Generator**;
- "Waveguide Generator needs you." with WG's question and **Open Waveguide
  Generator**. When the mesh is too large it also offers **Open Sources &
  Mesh…**. You answer in WG's pop-up;
- "Waveguide Generator could not prepare <model>. This has been recorded for
  the developers." when WG hit a problem of its own.

If WG has not picked a model up after a while, the panel says so and offers
**Send again**. Its top row has **Sources & Mesh…** and **Solve in WG**, so
the everyday commands are one click away while you watch a run.

**Solve settings** in the panel show the start and end frequency, the number of
frequencies and their spacing this model's last solve used. Change any of them
and the next **Solve in WG** uses your values; Waveguide Generator keeps them as
this model's settings, so you see the same values in WG. A blank field keeps
WG's value. **Keep WG's settings** drops your changes before they are sent.
Driver choices and the rest of the settings stay in WG. The list shows
this document's requests; **Clear finished** removes finished entries. It keeps the last 50 requests
from the last 30 days, also across a Fusion restart; a restart never sends
anything again. Technical detail is under the folded **Diagnostics**.

## 5. A WG waveguide in Fusion

1. In WG, **Send to CAD** writes the design and raises Fusion; the add-in
   inserts it the first time and offers **Update** afterwards. If the document
   holds several placements of the same design, choose the placement in WG
   first; the add-in never guesses which copy you mean.
2. Move and joint the **wrapper component** the add-in created, not the bodies
   inside it. WG's parameters appear as `wg_<name>_*` user parameters.
3. Build your own features from the inserted sketches, planes and axes where
   you can. Check them after each **Update**.
4. **Solve in WG** sends the finished model back and solves it.

### Immutable exports and manual Update

Each export from WG is kept as its own folder and never changed afterwards.
**Update** always applies the newest export of the link's design, even if the
one applied before still exists. If WG's record of the newest export is
damaged, Update stops rather than falling back to an older export. A **Send to
CAD** from WG applies exactly the export it names. An add-in older than this
behaviour keeps applying the export it has until you update it from WG
(**Install add-in**) and restart Fusion. The details are in the
[implementation guide](../fusion-addins/WGLink/README.md).

A link has three names, and they may differ: the Fusion document name (WG uses
it for your runs), WG's design name (a label), and an optional **Link name**
you give at Insert (the label in the add-in's menus and the timeline). The
`wg_<name>_*` parameters keep the name the design had when it was first
exported, even after you rename it, so your own expressions keep working.

## 6. A model drawn in Fusion

A model with no WG design behind it works the same way. It needs:

1. **Drivers**, picked in Sources & Mesh….
2. **The bodies to solve.** One visible solid or surface body is taken as the
   outside of the speaker. With several visible bodies, use **Declare Body…**
   to mark surface bodies **Exterior shell** or **Exclude**. Hidden bodies are
   left out.
3. **A sensible placement.** WG works out which way the model radiates.
   Putting the throat at the origin, radiating along +Z, and centring the
   model on x = 0 and y = 0 lets WG find mirror symmetry. Pre-flight warns
   when the placement looks wrong.

### Models already cut in half or quarter

There is no model-domain choice: WG picks the smallest safe domain itself. If
you cut the model in Fusion on an origin plane with **Split Body** or a cut
extrude, the add-in records the cut and Pre-flight shows it, for example
**Cut: Split Body 3 at x = 0**. WG checks it against the mesh. A clean cut on
the YZ or XZ plane can be mirrored. A capped, leaking or unsafe cut cannot,
and WG says what to change. With no recorded cut, a cut shape is solved as it
is. Driver faces need no adjustment: a half model's driver is already half its
area.

### Assembly scope

A selected occurrence is sent in its component's own frame, read from Fusion's
native bodies, so a moved or jointed wrapper still lands correctly. The add-in
cannot send a placed occurrence that itself contains sub-assemblies. Instead,
leave Assembly scope empty, select one of the sub-assemblies, or move the
occurrence back to the assembly origin by editing the joint or Move feature
that placed it. (Fusion's **Ground** does not move it back.)

## 7. FEM regions

FEM regions solve bounded air (a rear chamber, a port, a phase plug, a
compression-driver cavity) and couple it to the outside field. Model each air
volume as **one closed solid body**, then run **Advanced → Declare FEM
Region…** on it:

1. Select the air body. A body declared before reloads its settings.
2. Name the region and set its mesh size and loss factor (0 is lossless air;
   0.002 to 0.05 adds damping).
3. Add a group for every face set that is not a plain wall, with a name, a
   kind, a local mesh size and its faces:
   - **Driver diaphragm (moving)** with a role (LF, MF or HF). It joins the
     driver of that role. **Relative level (dB)** sets its motion relative to
     the driver (0 down to −120 dB).
   - **Interface to exterior air** where the chamber meets the outside, such
     as a port exit or horn throat.
   - **Plane-wave termination** for an anechoic tube end.
   - **Rigid wall (local size)** for a wall that needs a finer mesh. Tick
     **Absorbing lining** for a lined wall and set its thickness (default
     30 mm) and flow resistivity (default 5000 Pa·s/m²).

   Faces in no group are rigid walls. OK stays disabled, with the reason, until
   every face is in at most one group, names are unique and each diaphragm has
   a role.
4. **Hide the air body** before Send or Solve. Fusion sends visible bodies, so
   a visible air body would also be sent as a wall.

Several moving groups on one role are **one physical driver**, for example the
front and rear of one diaphragm in two regions, or a dome and surround in one.
Two separate drivers need separate roles. If a group's faces disappear after an
edit, Send stops until you reopen the dialog and pick them again. FEM regions
are sent in the root frame only: leave Assembly scope empty, or select an
occurrence placed at the assembly origin.

## 8. Troubleshooting

- **The panel is missing after a restart.** Tick **Run on Startup** for
  `WGLink` in Scripts and Add-Ins (section 1).
- **"Waveguide Generator has no selected WGLink folder"**, or the folder no
  longer exists. Choose the folder again in WG under **Settings → CAD Link**,
  then send again.
- **Nothing arrives in WG.** Open WG and check it is running and current. The
  progress panel says whether WG picked the model up; use **Send again** only
  after checking WG, because each Send is a new request.
- **Send and Solve stay disabled.** Read Pre-flight; it names what to change.
- **"…helper body … is … still visible"** on Send. An older insert left a
  helper surface visible, and Fusion sends every visible body. Run **Update**
  (it hides leftover helper bodies), or hide **the body itself** in the
  browser. Hiding the folder that holds it does not work, and do not delete
  it: the add-in needs it to repair the link.
- **I selected and solved a thin surface by mistake.** That is the same helper
  surface, lying exactly on the solid. Run **Update** to hide it.
- **The link has a name I never typed.** It is WG's design name. Give your own
  **Link name** at Insert, or rename it later with the head-less
  `wglink_core.set_link_name` ([Naming a link](WGLINK-LINK-NAMING.md)). The
  parameters keep their name on purpose.
- **Update says the parameter names do not match.** The link predates the
  stable parameter names. Detach, delete the component and Insert again; only
  your own features that use the old parameters need repointing.
- **Insert will not take over existing parameters.** The add-in never adopts
  parameters it did not create. Insert normally picks the next free name
  (`wg_<name>2_`); if it cannot, rename or delete the parameters it names.
- **Update cannot find the design.** Put the WG export back under WG's WGLink
  folder and run Update again.
- **WG shows an update as still running after Fusion restarted.** The add-in
  settles it against the open document without running it again. Open the
  document it was for. If it reports **Update interrupted — recovery
  required**, undo the partial change and send it from WG again.
- **The add-in cannot find the component that holds the WG waveguide.** It
  will not guess where the waveguide sits. If you deleted or replaced that
  component, undo it and send again. If it is still there, do not insert a
  second copy; report the problem with the message.

For the full detail of an error, open **View → Show Text Commands** in Fusion.

## 9. Upgrading or installing by hand

Most people never need this section: WG's installer installs and updates the
add-in for you.

**Installing from a checkout.** Developers install the add-in from this
repository instead:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/install_wglink_addin.py --symlink
```

Then restart Fusion and tick **Run on Startup** as in section 1. The
[implementation guide](../fusion-addins/WGLink/README.md) has the details.

### Compatibility

The add-in checks what WG says it supports, not WG's version number. If WG is
too old, the add-in asks you to **Update Waveguide Generator** before it
exports anything. This release changes two things for people upgrading from an
older add-in; they belong in the R-2 re-pin and the release notes:

- Solve no longer falls back to schema 3. Send and Solve always use the newer
  request format and need a WG that advertises `solveCommandDelivery >= 4`.
  With an older WG, update WG first.
- Sends and Solves left over from an earlier session in `.wglink-outbox`
  stay queued until WG starts with a compatible version. Before, queued Solves were
  converted to the old format and queued Sends were removed with a "not
  delivered" notice.
