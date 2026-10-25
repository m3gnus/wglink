# Fusion wording sweep inventory

The before column abbreviates variable model and diagnostic detail. This inventory covers user-visible changes only; internal names are retained.

## Command descriptions

| Before (variable detail abbreviated) | After |
|---|---|
| Set sources, mesh sizes and surface groups in this document. | Set up sources and mesh, then Send or Solve |
| Open this model in Waveguide Generator to inspect or set up. Does not solve. | Open this model in WG to inspect or set up. Does not solve. |
| Send this model to Waveguide Generator and solve it now. | Send and solve now. |
| Leave it out of the return / insert from a .wglink bundle / current bundle | Leave it out of the model sent to WG / WG model file / current WG model |

## Pickers and link labels

| Before (variable detail abbreviated) | After |
|---|---|
| Browse for a bundle folder… / Waveguide Generator bundles (*.wglink) / Bundle source | Browse for a WG model folder… / Waveguide Generator models (*.wglink) / Model source |
| Select a .wglink bundle to insert | Select a WG model to insert |
| Solver anchor instance; raw instance ids in choices | Reference link; Link 1, Link 2, … (the selected identity is unchanged) |
| <user label> · <instance id> | <user label> · Link 1, Link 2, … |
| parameters keep the bundle’s namespace | parameters keep the WG design’s name |

## Preflight

| Before (variable detail abbreviated) | After |
|---|---|
| WG links in scope: N (<instance ids>) | WG links in scope: N |
| WG links in scope: none — this exports as an unlinked (Fusion-first) return. | WG links in scope: none — this model was created in Fusion. |
| Source: PASSIVE_CARDIOID …, from link <instance id> | Source: Passive radiator …, from WG |

## Authoring

| Before (variable detail abbreviated) | After |
|---|---|
| painted <role> carry no WG source identity; unsaved document has no stable lineage | marked <display role> have no saved driver setup; document has not been saved yet |
| Adopt … PASSIVE_CARDIOID source? / role in source summaries and unknown-role choices | Adopt … Passive radiator source? / display role in summaries and choices |
| Exclude — leave this body out of the return / WG return entirely | Exclude — leave this body out of the model sent to WG / model sent to WG entirely |
| surface body with no declaration refuses the export | surface body with no declaration cannot be sent |
| Select the bodies to classify for the WG return. | Select the bodies to classify for the model sent to WG. |
| A surface group needs a nonempty, trimmed id / select … for the surface group | A size zone needs a name / select … for the size zone |
| both a source and a surface group / two surface groups / saved or cleared the surface group | both a source and a size zone / two size zones / saved or cleared the size zone |
| Could not load the saved FEM region and surface groups. … <raw exception> | The saved FEM region could not be read. Reselect the air body before saving. |

## Export progress

| Before (variable detail abbreviated) | After |
|---|---|
| Exporting STEP and validating the return bundle… | Exporting STEP and checking this model… |
| Return written. Asking Waveguide Generator to solve… | Model saved. Asking Waveguide Generator to solve… |
| Return written. Handing it to Waveguide Generator… | Model saved. Opening it in Waveguide Generator… |

## Manual success reports

| Before (variable detail abbreviated) | After |
|---|---|
| Inserted / Detached Waveguide Generator instance <id> | Inserted / Detached the Waveguide Generator model |
| Technical warnings including raw failure details | Some model checks could not be completed. The details have been recorded. (original warning in log) |

## Startup and legacy delivery

| Before (variable detail abbreviated) | After |
|---|---|
| N request(s)… picked up… older add-in… waits in the WGLink folder… | N model(s) from an earlier Fusion session are still waiting for Waveguide Generator. Open Waveguide Generator and check Settings → CAD Link, then try again. |
| Return from Send/Solve… operation <id>… reason code… malformed… within 7 days | Waveguide Generator could not receive an earlier model. Send or Solve again from Fusion. (whole notice in log) |
| Nothing was dropped… same request ids… | Your models are kept. Restart Fusion once the WGLink folder is writable to try again. |
| Could not hand earlier requests over at start-up: <raw exception> | Could not hand earlier requests to Waveguide Generator when Fusion started. (raw exception in log) |
| Their returns are still in the WGLink folder. | Their models are kept in the WGLink folder. |

## Shared protocol

| Before (variable detail abbreviated) | After |
|---|---|
| Port or passive opening | Passive radiator |
| Change the mesh resolution in Sources & Mesh…, or check the model size. | In Fusion, open Sources & Mesh… and raise Walls or add a coarser size zone, or check the model size. |

## Unexpected failures

| Before (variable detail abbreviated) | After |
|---|---|
| <action> hit an unexpected error: <exception> / full traceback in Text Commands | Waveguide Generator could not <action> <document>. This has been recorded. |

## Box titles

| Before (variable detail abbreviated) | After |
|---|---|
| Waveguide Generator refused / automatic … refused / … error | Waveguide Generator |

## Model checks and remedies

Every row below retains its original diagnostic detail for headless callers and the log. The check, its timing, and its outcome remain unchanged.

| Category | Before (examples) | After |
|---|---|---|
| `mirrored_placement` | Placed mirrored… determinant… return contract… chirality… Send refuses it | This model contains a mirrored WG link. Insert a fresh copy from WG and move or rotate it into position. |
| `scaled_placement` | Not a rigid placement… rotation is not orthonormal… determinant… Send refuses it | This model contains a scaled or skewed WG link. Remove the scale or skew and send again. |
| `body_inclusion` | The return inventory and Fusion’s STEP export disagree… | Fusion would send different bodies from those selected. Hide unwanted bodies themselves in the browser, then send again. |
| `unclassified_visible_surface` | return export refused: visible surface body … is not classified… | A visible surface body has no purpose set. If it is a helper, hide the body itself in the browser; otherwise select it and use Manage Link… → Declare Body… → Exterior shell. |
| `scope_invalid` | return export refused: … / no included B-rep bodies | This model has no usable exterior in the selected scope. Review the visible bodies and Assembly scope, then send again. |
| `scope_selection` | Cannot export a selected … / placed away from the assembly origin… | This selection cannot be sent on its own. Leave Assembly scope empty to send the whole assembly. |
| `anchor_choice` | choose options['anchor_instance_id'] / Anchor instance … not one of … | This model needs a reference WG link. Choose a Reference link in the Send or Solve dialog. |
| `copied_link` | Instance … belongs to several managed bodies… options['entity_token']… | This model contains copies of the same WG link. Remove the unwanted copy or insert a fresh link from WG. |
| `link_placement` | Instance … has no readable occurrence transform / ambiguous wrapper… | The position of a WG link could not be read. Check its component and placement in Fusion, then send again. |
| `link_datums` | Recorded datum ownership disagrees / no record of which datum is its own… | A WG link's reference geometry could not be identified. Insert a fresh copy of the link from WG. |
| `source_setup` | Required throat selector… resolved to … faces / stored expected area… | A driver could not be identified on this model. Open Sources & Mesh… and select its faces again. |
| `source_changed` | Faces painted … carry different source identities / outside what is being sent… | A driver's faces have changed or are outside this selection. Review its faces in Sources & Mesh…, then save. |
| `source_identity_unreadable` | Fusion could not read a saved WG source identity in this design | The saved driver setup could not be read. Open Sources & Mesh… and select its faces again. |
| `source_name` | Source identity … longer than … UTF-8 bytes / …-byte mesh name… | A saved driver name cannot be used. Open Sources & Mesh… and save the driver setup again. |
| `no_drive` | The existing shared no-driver rule (already clean); previously raised as an untyped failure | This model has no driver yet. In Sources & Mesh…, pick the faces of a driver (HF, MF, LF or a port), or declare a FEM region with a moving surface. |
| `mesh_settings` | Could not read the saved mesh settings / schema and selector validation details… | The saved mesh settings could not be used. Open Sources & Mesh… and save them again. |
| `size_zone` | Surface group … has no faces / painted surface group has no settings… | A size zone's faces could not be used. Review the size zones in Sources & Mesh…, then save. |
| `fem_setup` | FEM region attribute … unsupported schema / region geometry validation details… | A FEM region could not be used. Review it in Declare FEM Region… before sending. |
| `fem_visibility` | assembly.step would carry FEM air volume bodies as exterior geometry… | A FEM air volume would also be sent as exterior geometry. Hide its occurrence in the browser before sending. |
| `domain_setup` | Reduced domain… negative side… tolerance… retained wedge… | The model does not match the chosen half or quarter. Check the cut or send the full model. |
| `export_body_count` | STEP body count gate refused the export: inventory expects … | Fusion exported a different number of bodies from those selected. Check body visibility in the browser, then send again. |
| `export_folder` | Could not create/publish return bundle… / options['overwrite']… | This model could not be saved for WG. Check that the WGLink folder is writable, then send again. |
| `export_busy` | Another Waveguide Generator export is already publishing … | This model is already being sent. Wait for it to finish, then try again. |
| `mesh_outdated` | Existing clean mesh-version remedy, previously raised as an untyped failure | This Waveguide Generator is too old for Fusion-side mesh settings. Update and restart Waveguide Generator, then Send again. |
| `fem_unavailable` | This Waveguide Generator does not offer FEM regions (update it, or turn FEM regions on)… | This Waveguide Generator does not offer FEM regions. Update it or turn FEM regions on, then try again. |
| `already_linked` | Already linked… so WG names the exact instance and the model state it expects… | This WG design is already linked in the active Fusion document. Select the link in WG’s CAD Link panel and send the update from there. |
| `expired` | The insert has expired… (with delivery details) | The model is ready, but the time to insert it has expired. Send the model from WG again. |
| `wg_outdated` | Does not advertise delivery capabilities… inbox 4… schema 1 / missing feature names… | This Waveguide Generator needs updating to work with Fusion. Update Waveguide Generator; it installs the matching Fusion add-in. |
| `wg_not_collecting` | Not collecting requests… has not said that it reads them… check its WGLink folder… | Waveguide Generator is not ready to receive this model. Open Waveguide Generator and check Settings → CAD Link, then try again. |
| `startup` | Launcher JSON / native exit code / main-thread event / installed-entry detail… | Waveguide Generator could not start. Start Waveguide Generator manually, then try again. |
| `document_changed` | The active Fusion document changed while WG was starting… (title: refused) | The active Fusion document changed while WG was starting. Send or Solve again from the intended document. |
| `model_changed` | Active document changed / changed after WG displayed its status / exact baseline details… | This model changed after WG checked it. Refresh CAD Link in WG, then try again. |
| `workspace` | No selected WGLink folder / moved folder path and underlying detail… | This model has no usable WGLink folder. Choose the folder in WG under Settings → CAD Link, then try again. |
| `delivery_write` | The return bundle is in the WGLink folder… could not write the request… raw OS detail… | This model was saved, but Waveguide Generator has not received it. Check that the WGLink folder is writable, then send again. |
| `delivery` | Earlier model could not be delivered; detailed transport reason | Waveguide Generator could not receive this model. Send or Solve again from Fusion. |
| `recovery` | Update interrupted — recovery required… partial change… | The model's previous update was interrupted. Undo the interrupted update or insert a fresh copy from WG. |

## Deliberately retained

- Schema versions, role ids, `surface_groups`, selectors, canonical JSON, validators and stable diagnostic codes are internal contracts. Their original diagnostic text remains available in logs; it is no longer displayed as an exception body.
- FEM “surface groups” describe boundary conditions, not mesh size zones. Those labels remain.
- The Requests progress panel already uses clean shared sentences. Its HTML, layout, polling and history are unchanged.
- Old “Return ready”, “Scope: clean”, success-summary paths and ids, and a Sources & Mesh save box were absent in this base. No replacement box was added. Send/Solve success still uses the palette; Sources & Mesh still saves silently.
- User document, link, body and zone names remain user data. The guard exempts explicitly supplied names from vocabulary checks.
- User guides are untouched. The ongoing rules are in [WORDING.md](WORDING.md).

## Duplication check

Uses the existing `wglink_protocol.user_text_violations`, shared preflight/status rules, driver-label table, Fusion FakeUI, transactional writers and launcher. Fusion messages and diagnostic handling share `wglink_text.user_message`; scope classification wording shares `scope_problem`, and command failure verbs share `action_verb`. No second jargon guard, protocol implementation or UI surface was added.

## Verification

Measured from base `2d06ea9` on the wording-sweep branch with CPython 3.13
and the dependencies in `requirements.txt`, including the pinned mesher
`63bf827607252f608640eef7ecba508c13f2a186`. Every test command was unpiped,
redirected to its own log, and gated with `&& git diff --check`.

| Focused run | Passed |
|---|---:|
| Wording, Send, authoring | 294 |
| FEM dialog, dispatch receipt, wording | 189 |
| Other model/source/mesh checks | 364 |
| Add-in lifecycle | 153 |
| Protocol profiles, preflight, status, file channels, delivery, visible names | 428 |
| Launcher, acknowledgement, palette, history, Python 3.9 syntax | 264 |
| Transfer, transfer failures, legacy recovery, endpoint oracle | 225 |

These cover **1,790 distinct cases**. The wording file's 127 cases occur in
two runs; its failure-category matrix accounts for 123 of those cases (every
category plus three internal exception types, through three UI boundaries).
The remaining four check coverage, chained diagnostics, the vocabulary list,
and successful reports containing user names and technical warnings.

The matrix asserts clean box text and titles, diagnostic detail and request
identity in the log. The structural profiles and endpoint oracle retain their
original fixtures and canonical-byte assertions. No full suite, numerical
solve, or live Fusion smoke check was run. Independent review found a success
report/name-exemption defect; its correction and shared action wording were
reviewed again with no remaining blocking finding.
