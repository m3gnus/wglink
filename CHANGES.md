# Changes

## Unreleased

- WGLink no longer carries its own triangle limits. It shows and applies only the limits Waveguide Generator
  advertises; when WG advertises none (an older version), the estimate states no limit, has no limit colour
  and Solve asks no mesh-ceiling question.

## 0.3.3 — 2026-10-25

- **Sources & Mesh…** now has fixed driver rows for HF, MF, LF and **Port
  or passive opening**, wall size and named size zones. One face picker fills
  the selected row; picking a face for another row moves it there. Colours
  update live while picking. **Save** stores the settings in the Fusion
  document without sending anything; **Cancel** restores the previous settings
  and colours. Drivers from a linked WG design remain read-only.
- A progress panel follows each **Send to WG** and **Solve in WG**, from
  export through preparation and solving, including the frequency count.
  **Requests…** reopens the panel and its history across documents and Fusion
  restarts. Finished solves offer **Show results in Waveguide Generator**;
  requests that need an answer offer the relevant action in WG.
- Pressing Send or Solve again for an unchanged model while its request is
  under way asks whether to send it again or show its progress. A changed
  model, or **Send again**, sends at once. Restarting Fusion resumes following
  requests without sending them again, and damaged history can be recovered.
- Send, Solve and Sources & Mesh… use preflight checks shared with WG.
  Problems that must be fixed are separated from warnings and questions.
  Open bodies ask for confirmation only when they were not already declared;
  a Solve whose smallest possible mesh exceeds WG's limit offers a choice to
  continue or open Sources & Mesh….
- The triangle estimate follows WG's meshing rules and shows a range while
  symmetry is uncertain, from a quarter model to the whole model. It accounts
  for recorded cuts and measured symmetry, and shows the count WG last used
  after a solve.
- Dialogs and messages use calm, plain language and say what to do next.
  Send and Solve no longer finish with a message box; technical diagnostics
  stay in the log and the progress panel's folded **Diagnostics** section.
- Saving, restoring and clearing source, mesh and FEM settings now use one
  checked operation. Unreadable saved settings block Save, and a failed Save
  restores settings and colours or explains when restoration needs attention.
- The user guide has been rewritten around the current panel, with steps for
  drivers, size zones, Send and Solve, request history, linked waveguides,
  Fusion models, FEM regions and installation.
- **Compatibility:** Send and Solve always use the newer request format and
  require WG to advertise `solveCommandDelivery >= 4`; Solve no longer falls
  back to the older format. Update WG first if needed. Sends and Solves left
  in `.wglink-outbox` by an earlier session now stay queued until a compatible
  WG starts; queued Sends are no longer removed, and queued Solves are no
  longer converted to the old format.

## 0.3.2 — 2026-10-24

- The Fusion toolbar, commands, dialogs and messages now call the add-in
  **Waveguide Generator**. Instructions consistently distinguish the add-in
  from its WGLink folder. Fusion's Scripts and Add-Ins registration stays
  `WGLink`; existing installations, links and saved documents keep working.
- **Sources & Mesh…** replaces **Set WG Source…**. Set source roles and sizes,
  wall size, grading distance and automatic or full-model symmetry in one
  dialog. Sources from a linked WG design remain read-only.
- Add named surface groups from faces or bodies, with **Fine 15 mm**,
  **Coarse 40 mm** or custom sizes. Group membership is painted in the model.
  Removing a face from a group restores its original appearance override or
  inherited body appearance. Group selections affect only the selected
  component instance; unsupported selections are refused with an explanation.
- Source and mesh settings are saved in the Fusion document. Reopening the
  dialog restores values and face selections; Send and Solve use the saved
  settings, or defaults for documents without them. Invalid saved settings,
  overlapping selections and empty or unrecognized painted groups are refused
  rather than silently discarded.
- Send, Solve and returns requested by WG now carry complete mesh settings
  through `mesh-sizing-v1`: wall and per-source sizes, grading distance,
  symmetry and named surface groups. An older WG without mesh-sizing support
  is refused before export, with instructions to update and restart it.
- The dialog shows a live approximate triangle count for the part WG will
  solve, with an amber indication at 18,000 triangles and a warning above
  22,000. Automatic symmetry also shows the count if WG finds no symmetry;
  curvature can raise the actual count. A source coarser than its walls is
  also a warning. These warnings do not block Save.
- **Fix before saving** identifies invalid sizes, overlapping selections,
  empty groups and duplicate group names. Saved faces that cannot be selected
  are reported by row and count so they can be unhidden or reselected.
- Sources and groups can be removed from any editable row. Group paint missing
  from the saved settings can be recovered or cleared in the dialog.
  **Reset to defaults** resets sizes while preserving group names, membership
  and symmetry.
- Fix dialog construction and input handling in Fusion. A failed Save restores
  source and group paint, appearance inheritance and saved settings exactly.
  Sources & Mesh… and Declare FEM Region… refuse Save if their dialog could
  not be built completely, preventing partial settings from being applied.

## 0.3.1 — 2026-10-21

- Follow the design's latest immutable export on manual Update, even when the
  previously applied bundle still exists. Validate locator keys, schema,
  sequence, confined paths, manifest identities and bundle checksums; refuse
  invalid locators without fallback. Older layouts without locators keep the
  existing stored-path and discovery behavior.
- Bind automatic Insert/Update handoffs to their exact bundle and export IDs
  before creating or changing a Fusion document. Missing paths and mismatches
  refuse instead of discovering a replacement.
- Advertise `immutableBundles: 1` in the Fusion heartbeat. Bundle discovery
  offers each design's latest export alongside legacy stable-folder bundles.
  Document attributes and lineage parameter namespaces remain compatible.
- Internal: one shared bundle-identity serializer for the verifier and the
  resample subprocess (output unchanged); retired Audit/Relink toolbar artwork
  removed (the `audit()`/`relink()` APIs remain).

## 0.3.0 — 2026-10-12

- **Declare FEM Region…** appears under Manage when WG advertises `femRegions`.
  Declare a solid body of air with region size, bulk loss and named diaphragm,
  exterior-interface, plane-wave termination or rigid-wall surface groups.
  Send and Solve include declared regions through `fem-regions-v1` when WG
  offers them. If WG no longer offers FEM regions, the commands offer to clear
  the declarations and continue without them.
- Send refuses FEM groups that have lost any declared face, changed membership,
  or changed total area by more than 1 %. Older declarations without the area
  baseline must be re-declared. Visible region bodies and regions outside the
  root component are also refused before export.
- A failed temporary-region cleanup refuses Send and names the leftover
  component. A later Send refuses leftover region copies, hidden or visible,
  names both the original and copy, and asks for the copy to be deleted.
- FEM diaphragms share drive channels with painted exterior sources on the same
  drive role. Several moving groups may name one drive role (the front and rear
  of one diaphragm in two regions, or dome and surround in one region); WG
  decides whether that channel can share a diaphragm. A diaphragm whose role has
  several exterior source channels is still refused with the conflicting names.
- Rigid groups take an optional absorbing lining (Miki model: thickness and flow
  resistivity, defaulting to 30 mm and 5000 Pa·s/m²), and moving groups an
  optional relative drive level from -120 to 0 dB. Both are part of the declared
  group, so changing them changes the return.
- Declare FEM Region… no longer carries an Experimental label.
- Send and Solve start an installed WG backend through WG's advertised launcher
  when it is down, or, on a fresh install that has never run, through the
  launcher named by WG's install marker. Concurrent commands share startup;
  startup failures report WG's reason before exporting or writing a request.
- Each file channel checks its own capability: Insert, Update, handoffs and
  return requests require capability schema 1 and `fusionRequestDelivery >= 3`;
  Send and Solve additionally require `solveCommandDelivery >= 4`. An older WG
  receives an update instruction; an unavailable inbox asks for WG to be started
  and its WGLink folder checked. Product version strings do not gate delivery.
- Commands, acknowledgements and status use files; the live HTTP session and
  export watcher are removed. A new export alone no longer prompts Fusion.
  Earlier HTTP claims are settled from file and document evidence without
  re-running mutations. Unanswered legacy `.wglink-outbox` requests stay queued
  for an older or unavailable WG until a startup with both delivery channels
  compatible; queued Solves no longer fall back to schema 3 and queued Sends
  are no longer discarded with a not-delivered notice.
- Designs without FEM regions retain the previous bundle bytes apart from
  `generator.adapter_version`, which now reports 0.3.0.
