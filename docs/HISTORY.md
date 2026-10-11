# History

WGLink was developed in the `m3gnus/hornlab-fusion-addin` repository, which began as the
WGMetalPipeline add-in. This repository was extracted from that repository's `main` at
`63ba92c012887c9b81da5273f25f418a29a826b5` (WGLink 0.2.1). That repository is now private;
the commit map below is the way to find an old commit's counterpart here.

The extraction rewrote history in three ways:

- WGMetalPipeline files that no current file descends from were removed from every commit.
  Commits that touched only those files were dropped (53 of 234).
- Author and committer identities were normalised to `m-a <m3gnus@users.noreply.github.com>`.
  GitHub's own committer identity on commits made in the GitHub interface was kept.
- A personal e-mail address quoted in an old document and a test fixture was replaced.

File contents at the tip are unchanged, apart from the two removed WGMetalPipeline files
`docs/WGMETAL-PIPELINE-GUIDE.md` and `scripts/probe_parameter_namespace_ownership.py`.

## Commit map

Oldest first. `old` is the commit in `hornlab-fusion-addin`, `new` the commit here; `-` means
the commit was dropped.

| old | new | subject |
|---|---|---|
| `50e81dd38c` | `307965ce31` | Initial import: WGMetalPipeline Fusion add-in extracted from HornLab |
| `96081c064d` | `96dc5de9e2` | Remove frequency role mesh sizing |
| `195fa57774` | - | Fix coupled-cardioid dialog forwarding and voltage-drive scaling |
| `542d5fc284` | - | Add per-driver LEM coupling plan; sync ROADMAP status |
| `9cada38b03` | - | Stage B: implement per-driver LEM solve path |
| `be145d056d` | - | Stage C: add Driver LEM dialog controls |
| `c2809de00d` | - | Stage D: document per-driver LEM coupling |
| `d53408fffe` | - | Track the per-driver LEM plan document |
| `4f53aa399e` | - | Fix review findings in per-driver LEM coupling |
| `e2a943ba8f` | `a997bdc8b2` | Add user guide |
| `efd05f4df9` | `849753a3f9` | Add fusion derived acoustic outputs |
| `194ce81ee6` | `21a4b29aa2` | Fix review findings: group delay, sibling precedence, migration, docs |
| `6e11de9c7c` | - | Route Fusion plots through HornLab themes |
| `51de322a58` | - | Add structured Fusion run outputs and reports |
| `15c98297a0` | `28d70aef78` | Document Fusion output layout and reports |
| `40d98c154e` | - | Fix Fusion output review findings |
| `75253973fb` | `2ab5d5cd95` | Pin published hornlab-sim and hornlab-plots in requirements |
| `144715882d` | `af4a10ddb5` | Bump sim and plots pins to the rename and themes commits |
| `838a8a56f0` | `df61107e6b` | Add named presets, headless batch guide, and A/B run comparison |
| `d14db6efc5` | `e7452cb58f` | Adopt canonical hornlab_plots.derived renderers; add impedance PNGs |
| `dd1f6be92f` | `0ecd27fb29` | Label electrical impedance plots |
| `2641e2162e` | `a5f0dd38b6` | Reorganize run folders, add LF/HF two-way crossover, display-flatten phase overlays |
| `1392999e78` | - | Use projected area for axial front-loaded driver coupling |
| `a696c6cbc0` | - | Expose a Source Motion (Axial/Normal) control in the Fusion dialog |
| `28bb2b7714` | `e3f0ad2f46` | Bump metal-bem pin to 0672622 (axial source BC) for coupled-axial default |
| `461442b8b4` | - | prepare_step_for_wg_metal: heal-on-failure fallback for Fusion sliver edges |
| `9cff4847df` | - | prepare_step_for_wg_metal: make the OCC-healing fallback correctness-safe |
| `a1b4a85db5` | `15052c3986` | Fix Fusion healing and symmetry fallback |
| `51f2047c27` | `4e3096930f` | Add hybrid FEM-BEM chamber pipeline |
| `c99238afec` | `65795f12b9` | Fix dependency and pipeline failure contracts |
| `7244688436` | `c55e0ce186` | Wire FEM chamber safety contracts into the pipeline |
| `ef9a6bdae0` | `7dc36db40f` | Document published dependencies and drop dead legacy discovery paths |
| `133dca5988` | `2e79c2aa95` | Use installed HornLab dependencies |
| `fc2bfcab87` | - | Prevent stale Fusion launch status |
| `b1228a51e8` | - | Protect Fusion settings from partial writes |
| `ffcce304cf` | - | Simplify driver database loading |
| `25a14bc764` | - | Speed up mesh edge frequency checks |
| `98c4a31cf0` | - | Fix mesh topology and component winding |
| `1e7207898c` | - | Deduplicate orientation diagnostic output logic |
| `676476fe91` | - | Simplify report artifact link rendering |
| `757456d298` | - | Vectorize beamwidth threshold scanning |
| `66d57f5f17` | - | Fix clamped pressure phase interpolation |
| `75ba232228` | - | Share crossover filter reconstruction |
| `29bf8fd706` | - | Simplify run comparison artifact loading |
| `258519a977` | - | Speed up solver output processing |
| `0a4a0458c8` | - | Keep launch metadata usable when the on-disk record is not |
| `173b1d62af` | - | Surface reciprocal passivity diagnostics |
| `d308931e5f` | - | Retire expanded mesh manifest alias |
| `831bdfe5b2` | - | Skip reduced source mesh export in pipeline |
| `3cf7d31f70` | - | Anchor reduced mesh orientation to source caps |
| `cb3bce421d` | - | Propagate incomplete pressure rows to beamwidth |
| `d466346f9f` | `e4d8d21cae` | W4b: retire WG mesh bridges (bianco-era) — per-source exports + launch handoff removed |
| `75792d7147` | - | Add --symmetry-planes auto-cut: OCC mirror test and half-space reduction |
| `55a872c6f1` | - | Add --require-sources gate and solve-cost preflight to the pipeline |
| `c56c1c2dc7` | - | Wire auto-cut through the launcher; derive polars from the verdict |
| `9d98aa02ce` | - | Add ab_gate.py: reusable A/B solve regression gate |
| `7627249d8a` | `aca2fdac5e` | Refuse an unconfirmed auto-cut; friendlier dialog label; auto-cut docs |
| `fee7d1639d` | - | Review fixes: snap band in STEP units, floor guard, grid checks |
| `2543df0145` | - | Review fixes: preflight pricing, beamwidth-plane refusals, verdict guard |
| `4e9674e31a` | `df90d2ec07` | Prepare consumes the mesher's neutral auto-cut and resample APIs |
| `e57ca1f3fd` | `36364d8538` | Name the pinned mesher revision when its API surface is missing |
| `8965daffc0` | `bb1b5c466f` | Ignore the local spike directory |
| `f510b085c5` | `1c5039c4c7` | Add the WGLink Fusion adapter: insert, update, audit, relink, detach |
| `528ad59ad4` | `697cce043b` | Pin the modules at their v1.0.0 tags |
| `a0f4988093` | `97c0564eb2` | Build the mouth overshoot as a face extrude, and fix the transform's units |
| `2f0e7c6376` | `7eef274721` | Mitre the box corners, guard the link frame, name every body |
| `ecdf46f5db` | `5a3f5384b2` | Let the wrapper move: Fusion grounds a design's first component |
| `744b5489bb` | `456ad2e3a2` | Take mesh sizing from the mesher instead of a local copy |
| `c4b38d56c8` | `334d2a4ff4` | Pin the mesher at 1.1.1 and version the two add-ins |
| `4a5d10aacf` | - | Call the mesher's STEP import instead of owning it |
| `baf90dad84` | - | Delete the PORT_EXIT_L/_R alias fallback; it never fired |
| `74073a43eb` | `aec67cdf2e` | Add the wgreturn policy layer (Phase 2, section 9 step 1) |
| `b3f6c9a212` | `dbb2d7aff5` | Add Send to WG: observe the assembly and publish the return bundle |
| `8cb4dc81fb` | `1b9b2e7cd4` | Follow the mesher's three-value healing fallback return |
| `ec5b9d9e9e` | `913159102f` | Adopt an existing WGLink panel instead of rebuilding it |
| `d668416330` | `2dae5e0794` | Give WGLink icons and let it start with Fusion |
| `3e70ebd3bc` | `7bc4af0f80` | Notice a newer export and offer to apply it |
| `0e663931b9` | `27027d2d9e` | Take the workspace from WG instead of asking for it again |
| `6435ae57e9` | `9b37d7507d` | Move the module pins to the 0.1.x releases |
| `adb9cdf8c0` | `85c3c0cad1` | Auto-insert Waveguide Generator handoffs |
| `50cce623c9` | `55b548d1a3` | Make WGLink transport stateful |
| `5f9e067d96` | `6b5edec875` | Add revision-safe two-way WGLink sync |
| `bd34ddb797` | `c4db921b24` | Survive stale duplicate WGLink registrations |
| `78a8b63d4c` | `a2a919cd93` | Record observed parameters in mm; stop manual sends clobbering returns |
| `f964dcd69e` | `f0fdb7121e` | Suppress refused return-request retries; harden request names and units |
| `00239bcaae` | `7b0ff36437` | Publish which managed parameters drifted, not just how many |
| `52a6647cc7` | `8761691b1d` | Stamp return bundles as wgreturn 1.1 |
| `f00f5d0606` | `3c0acdc88f` | Follow dedicated WGLink folder settings |
| `56614e6ad5` | `ddce1b2663` | Add Solve in WG and simplify the command dialogs |
| `4a832d2453` | `d14c083708` | Promote the two everyday WGLink commands |
| `82d0838f26` | `6614d40b63` | Give the Manage WG Link dropdown its own glyph |
| `286b0cf8bc` | `4ec3ccfbab` | Author WG sources and body declarations from the WGLink panel |
| `a9d3b566f0` | `7ef382ea6b` | Name the concrete recovery in the namespace-mismatch refusal |
| `031a70afd1` | `ac65301100` | Give WGLink its own user guide and scope the WG Metal Pipeline one |
| `a35ad1e68d` | `1fce1a3764` | Carry the Fusion document out with the return that came from it |
| `4204a289f5` | `e0b4559be3` | Let WG decide whether returns carry the CAD document |
| `bd22559125` | `33c69c0676` | Explain vertical-offset enclosure refusals |
| `6d2145d637` | `0ceb56d097` | Refuse visible bodies marked for exclusion |
| `16249aa235` | `bfd4a7e928` | Refresh Fusion export inventory on demand |
| `410195330f` | `6e2a2cc6a3` | Simplify the Manage dropdown: self-healing Update, confirmed Detach |
| `30b90672cc` | `86f8b10d1a` | Show progress while exporting CAD returns |
| `7a50c6420c` | `a9e54affa6` | Make CAD return publication crash-safe |
| `63d4cff3c0` | `73be8ece2b` | Share one Fusion snapshot per watch tick |
| `82e0b45f15` | - | Retire the superseded add-in planning documents |
| `4123ebe2c6` | `bdd180505c` | Support WG-managed WGLink runtimes |
| `5a58f6b701` | `a6cd6cf27e` | Make duplicate WGLink ownership recoverable |
| `b67076b468` | `333463741d` | Fail closed when WGLink runtime is missing |
| `a17458120b` | `d296063198` | Merge WGLink ownership and packaged runtime support |
| `3ee9272d49` | `e8b7751515` | Target exact Fusion instances from WG handoffs |
| `3575a0b5fb` | `175de3f607` | Publish exact Fusion heartbeat identities |
| `8b01ae03d5` | - | Derive DI and power response from the solved balloon when one exists |
| `6793ec7655` | `50c49054d8` | Rename WGLink PORT_EXIT source role to PASSIVE_CARDIOID |
| `540b1b4623` | `9ce262649b` | Update WGLink docs for the PASSIVE_CARDIOID role rename |
| `b2eb14506f` | `f2b07b3e6d` | Test the PASSIVE_CARDIOID role rename and legacy recognition |
| `fe7c5be22e` | - | Read driver databases from the shared HornLab folder |
| `dab1b7a7da` | `ccc6fab0d9` | Merge feature/passive-cardioid-role: rename PORT_EXIT to PASSIVE_CARDIOID with legacy recognition |
| `4e69284aee` | `ac998152cd` | Merge feature/sphere-power-metrics: DI and power response from the solved balloon |
| `c3b75d8030` | `aecf409ae3` | Merge fix/driver-db-shared-folder: read driver databases from the shared HornLab folder |
| `824cf885d1` | `013caef471` | Accept and validate the source-interface-v1 wglink feature |
| `e536844833` | `7834d991b2` | Stop WGLink helper bodies from refusing their own return export |
| `06ef5c337b` | `0e42ebb5df` | Bump hornlab-waveguide-mesher to e145f85: reduced-component orientation repair |
| `d0640c6be7` | `8c51751de1` | Export a Component to STEP, and let CAD declare a pre-cut domain |
| `15ff783a54` | `27ec3ad037` | Return a placed occurrence in its own component frame, and name the frame |
| `f0bf2fa219` | `d0dac5ad08` | Hold the export inventory and the helper verdict against a domain declaration |
| `7f9d91081d` | `23de9e2e35` | Measure a nested body where it sits, not in its own component |
| `0a65f7ee02` | `b318ae3eb6` | Expect a declared cut's share of a linked throat, not the whole disc |
| `dd885153b8` | `374a884856` | Let a helper repeat a source the exported body already carries |
| `214d46a617` | `df7d7702f1` | Compare a managed parameter by value, not by its spelling |
| `6f60399b96` | `43e100c3ea` | Let a link carry the user's own name, and say which names are frozen |
| `fe4c65be80` | `d839e313b5` | Let the heartbeat read the same resolved links Audit reads |
| `5660a2de69` | `9a875c3ae0` | Give every instance sharing a bundle its own export verdict |
| `f22aa7c5df` | `27b20c8158` | Count a namespace as taken when the document still holds it |
| `4ce23f962e` | `cd20fb2b29` | Let the export fake see the body the inventory forgot |
| `2db0c795ca` | `6d90a7ba24` | Count a hollow solid as the one body it is |
| `21fd59f166` | `519abb2f7e` | Hide the leftover shell at Insert, and name it at Send when it is too late |
| `1da0bb36ab` | `e475441ceb` | Drive the helper hide through _close_and_thicken and insert |
| `6ff3438be4` | `532a50058e` | Hide the leftover shell on documents that already have one |
| `63550a504b` | - | Keep the executable reproduction of the namespace-ownership defect |
| `f2e5fcc970` | `e177840d92` | Drop the maintainer's personal address from the published add-in |
| `07b94aac14` | `4f2c281852` | Bump hornlab-waveguide-mesher to c036c62: the healing ladder was inert here |
| `63d86014fd` | `92ab6b709f` | Stop a refused handoff from starving every later IPC tick |
| `0266aea826` | `ceeb59880e` | Bind resampled geometry to the export identity Update validated |
| `b0dc8b56e5` | `57fedb8dcc` | Judge linked geometry by the same export rules as local geometry |
| `63a7c08a0d` | `22f9e107f9` | Resolve one occurrence placement for the manifest and its fingerprint |
| `5cfcfd627f` | `b7f0bdebf2` | Let Detach clear the duplicate instance id it is advertised to clear |
| `58fd2ecb43` | - | Write the run report's links as URLs, not as host paths |
| `df52b1ec74` | `562536508f` | Let the zip name guard be tested on Windows too |
| `4a1ae94635` | - | Stop asserting that pow and log10 are bitwise inverses |
| `82f4969484` | - | Let this module import on Windows |
| `6d3e290219` | `e4ce8d7f10` | Assert the path, not the separator |
| `56009c0de1` | `754b04d908` | Give this repository a gate of its own |
| `1df1359a93` | `5b36ed5371` | Finish the Windows pass: four more literals, and one case that cannot run there |
| `f4e9800df7` | - | Put every expected_paths literal through Path |
| `7d67d8daa3` | `855c4fadfe` | Stop re-measuring a document the heartbeat has already measured, and make an add-in edit provable (#3) |
| `eeb82f681c` | `7f7e77139c` | Stop the suite when it is about to measure the wrong mesher |
| `bc3e930dea` | - | Take the mesher's triangle reader and signed volume, not copies |
| `d92cd2a2e9` | `63787b9081` | Pin this repository's line endings, because it is consumed byte-wise |
| `99fb0f320b` | `275352d894` | Measure the live model before a guarded update or return |
| `9f1b37b1b9` | `07fbafa204` | Refuse a Send that would export a FEM air body as exterior geometry |
| `6aaac838b5` | `a4ffa42c7e` | Negotiate versioned delivery with WG and read its per-request files |
| `556c971b9f` | `87a5820aaa` | Let CI be dispatched on an exact candidate |
| `c387d64a9b` | `8ce969beff` | Speak delivery version 3 only, with exact targets and interruption evidence |
| `1a13b54b5d` | `3cce9cf71d` | Bound a stale capability file: name an older WG, and an untaken solve |
| `49a8e74369` | `3651e6b3cd` | Mark only real changes, recheck inserts live, keep settled outcomes visible |
| `fd177fa9f9` | `31cfcb3e5a` | Bump hornlab-sim to 260155d and hornlab-plots to ff30caf: numerical fixes |
| `e1b72ee05a` | `17749e3fd2` | Send each link's own throat datums and a measured chirality |
| `18619b633b` | `b2b7aa7d5b` | Journal WGLink updates through verification |
| `55e22f2a58` | `f666df2342` | Declare source-identity-v1 returns when WG advertises it |
| `f482426131` | `8f46e87cd9` | Stamp and reassign source identities from Set WG Source |
| `04b2524b46` | `851bc91f81` | Report refused rollbacks and kept paint truthfully |
| `98b1cd9e9f` | `c0fac7e40f` | Register a live CAD Link session with WG beside the v3 files |
| `2a835c2217` | `24f4dd0f11` | Avoid late loopback writes after client timeouts |
| `68551dfc98` | `6a9361f1f9` | Deliver Send and Solve in WG through a durable outbox beside the v3 files |
| `4ad32f0bbb` | `0134efa195` | Tolerate delivered items disappearing from outbox test snapshots |
| `83011fdf56` | `5f9c7fa5d6` | Tolerate Windows outbox inspection races in tests |
| `1ec0cd3eab` | `4749318a6b` | Skip heartbeat geometry for unlinked Fusion documents |
| `3dd9771c6f` | `197b88ded4` | Make the periodic Fusion heartbeat read cached state only |
| `3d9788a642` | `bcdaa0268c` | Never report a settled outbox item removed while its file is still there |
| `bc5e352c82` | `03d9f9eaab` | Never let a held outbox file drop the live session |
| `a8a800d9a0` | `d66f249a4f` | Complete the WGLink live operation consumer |
| `599ef82d8f` | `c7d8d60bb4` | Say what waiting behind a command does and does not mean |
| `20f6cd34dd` | `4e63c9343d` | Offer to adopt paint that predates WG source identities |
| `2c79e83905` | `61528f065e` | Bump hornlab-waveguide-mesher to 7fef1f60: mirrored-parent reduced orientation |
| `3d46f638b2` | - | Orient automatic symmetry cuts from their mirrored parent |
| `6746588cef` | - | Warn when auto-cut reorients a model that was already cut |
| `20e0b650e6` | `af9fcd77a2` | Add per-thread activity counters for CAD Link inspection entry points |
| `60f1820cfa` | `b8749f5213` | Let the owner switch automatic CAD Link coordination off |
| `84c55b0b3f` | `70d0e9c35c` | Test that an unreadable document is never read as having no links |
| `d4d0b3089e` | `586a2af2c2` | Send and Solve through one WG request file, versioned by what WG reads |
| `71c25a2496` | `ebafef6812` | Address the independent review of the M1 add-in branch |
| `6d34fbd6f6` | `e3cc5b70c1` | Pin the gate-on watch tick's geometry-measurement throttle |
| `99ec1e244a` | `58eff4d908` | Register a dev-synced WGLink under the commit it is running |
| `089f96de6e` | `0432457760` | Harden the WGLink transfer path against write failures, restarts and orphaned resamplers |
| `0fc2ad867a` | `e21a0ce528` | Report an unknown request write as unconfirmed, and confirm only the complete request |
| `f6138e0ced` | `4966ed228a` | Plan a request once and confirm only that exact request, type for type |
| `1d6ccf58e0` | `801ead0ed1` | Treat a held return name on Windows as busy, not as a crash |
| `5796085544` | `2a30accabc` | Stop a deliberately hidden body blocking the solve, and say why an export is refused |
| `ffae08b281` | `3427501357` | Refuse an export whose hidden body carries a painted source |
| `7a20f9c6bd` | `d252436cda` | Record an explicitly excluded body as information, unless it carries a source |
| `a4bbf43e1e` | `b845166df9` | Bump hornlab-waveguide-mesher to 63bf8276: auto-cut parentage is checked against the parent surfaces |
| `1c2e9de861` | - | Retry auto-cut with symmetry-equivalent sides |
| `a772804b7c` | - | Cover auto-cut fallback orchestration |
| `6d30f7abd8` | `4392caef45` | Export automatic CAD domain evidence |
| `18d105ab14` | `2538a12512` | Remove obsolete CAD domain chooser code |
| `b83648fcc0` | `51fc6007c7` | Enforce automatic domain evidence safety |
| `40fbd28c3c` | `88d1368762` | Retire bundled WG Metal pipeline from Fusion add-in |
| `48bde2a00b` | `ed93e8479d` | Default WGLink automatic coordination to off |
| `0e94f0e9d3` | `677bfde552` | Clarify visible surface refusal remedies |
| `3fddeb3f06` | `efb34c0c86` | Use structural visible-surface refusal advice |
| `6b85a99fd3` | `7745408c1a` | Require readable Fusion visibility during scope walk |
| `deeea29588` | `223d957f36` | Propagate selected occurrence visibility through scope walk |
| `4c50e575da` | `43ad82f61a` | Read cut provenance without rolling Fusion timeline |
| `9bd442f9c7` | `dcbc50b698` | Attribute cut provenance to current result faces |
| `efe7868750` | `589f0c3e37` | Reap the containment test's own grandchild in teardown |
| `99488a00ed` | `a29f0d6f00` | Confirm the Solve handoff once, without a competing progress bar |
| `853a156d9f` | `d41ab2fc9d` | Record old-code WGLink endpoint oracle |
| `820c25c1d5` | `fb1b086271` | Add profiled CAD Link protocol module |
| `07f3002d76` | `a8e3355cd3` | Route WGLink callers through profiled protocol |
| `56824aa109` | `7f5c81ba4a` | Preserve Python 3.9 compatibility in WGLink protocol |
| `13e02bf81b` | `f55b5e85c0` | Remove the unlinked solver-frame preflight copy |
| `1dac00acb2` | `d02b76d6fc` | Keep painted source ids stable across unsaved closes |
| `ea0f529755` | `c4d93413d7` | Bump WGLink add-in version to 0.2.0 |
| `85f09dfd1f` | `a0a65437ae` | Keep an outbox item listed while a replace of it is in flight |
| `04d33eeddc` | `62955771be` | Prove the WG request is written only after the return bundle is whole |
| `b8d6a422c3` | `d51d2a9e5b` | Say a request was written to WG's inbox until WG's acknowledgement says otherwise |
| `18874913e5` | `abdf422848` | Test the acknowledgement reader against files WG's own writer produced |
| `7ec3c907e2` | `fa5d671b5a` | Version 0.2.1 |
| `6edb26c69e` | `b12ee46b0e` | Remove the WGLink live session; every command goes through files |
| `19544475ef` | `b6464c2658` | Settle requests an earlier version claimed over HTTP, from files |
| `090286c8e3` | `6b1fbbda18` | Never let an unreadable earlier claim journal stop WGLink from loading |
| `48321da164` | `b4ff0863a8` | Split the WGLink file channels into inbox, handoff and status; stop watching exports |
| `7050e8d12d` | `d10245c0fe` | Gate each WG channel on its own capability; tell users to update WG only when that helps |
| `62aaf0ea25` | `150afe4787` | Start WG through its installed launcher when Send or Solve finds it not running |
| `63ba92c012` | `63ebd10a3c` | Accept only WG's installed launcher, bound discovery, and stop joined continuations on shutdown |
