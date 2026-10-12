# Changes

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
  and its CAD Link folder checked. Product version strings do not gate delivery.
- Commands, acknowledgements and status use files; the live HTTP session and
  export watcher are removed. A new export alone no longer prompts Fusion.
  Earlier HTTP claims are settled from file and document evidence without
  re-running mutations. Unanswered legacy `.wglink-outbox` requests stay queued
  for an older or unavailable WG until a startup with both delivery channels
  compatible; queued Solves no longer fall back to schema 3 and queued Sends
  are no longer discarded with a not-delivered notice.
- Designs without FEM regions retain the previous bundle bytes apart from
  `generator.adapter_version`, which now reports 0.3.0.
