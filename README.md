# WGLink

WGLink is the maintained Fusion add-in for sending Fusion geometry to Waveguide Generator (WG), solving it there, and inserting or updating native WG geometry in Fusion. The add-in lives in `fusion-addins/WGLink/`. WG's platform installers package WGLink for macOS and Windows.

Send, Solve, Insert/Update handoffs, acknowledgements and status exchange use files in WG's IPC folder. Send imports a snapshot; Solve also requests a solve. Each explicit command has its own request id. Exports are no longer watched: the user presses Send, or explicitly requests Insert/Update. A new export alone never prompts Fusion.

File delivery is split into `wglink_inbox.py` (capabilities, requests and acknowledgements), `wglink_handoff.py` (Insert/Update and return requests) and `wglink_status.py` (Fusion status). Insert, Update, WG handoffs and return requests require capability schema 1 and Fusion delivery 3. Send and Solve additionally require WG request inbox 4. Unsupported schemas or delivery versions ask for "Update Waveguide Generator"; an unavailable inbox asks you to start WG and check its CAD Link folder. See the implementation guide for the WG packaged-pair references and R-2 re-pin changes.

The former WG Metal pipeline is maintained separately as a frozen fallback. It is not part of this repository or WGLink's updater.

Send and Solve can start an installed WG backend through its advertised launcher when needed, falling back to the WG that installed this add-in (`wglink_install.json`) when no valid launcher advertisement is available, including before WG's first start. Concurrent commands share startup; export and submission resume through Fusion's main-thread custom event after fresh capability and version checks. Startup errors show WG's reason before any export or request is written. With `WG2_DATA_DIR`, Fusion must inherit the same override, and a fresh installation still needs a root chosen in WG before Send can export. See the implementation guide's packaged-pair references for the launcher and backend-owner contracts.

## Docs

- [WGLink user guide](docs/WGLINK-GUIDE.md)
- [WGLink implementation and development guide](fusion-addins/WGLink/README.md)
- [Naming a WGLink link](docs/WGLINK-LINK-NAMING.md)
- [Validating a WGLink change without a release](docs/WGLINK-DEV-LOOP.md)
- [Headless workflow status](docs/HEADLESS.md)

WGLink's Send and Solve dialogs use an automatic model domain; there is no manual Model dropdown. With a current WG, explicit exports also record surviving Fusion origin-plane cut history and the Y-up/Z-up modelling orientation. Send/Solve also require WG to advertise those manifest capabilities before a return is exported.

## FEM regions

With WG's `femRegions >= 1` capability, **Declare FEM Region…** marks bounded
air bodies and their surface groups. Rigid groups can carry Miki absorbing
lining (default thickness 30 mm, flow resistivity 5000 Pa·s/m²); moving groups
can set a relative level from −120 to 0 dB inclusive (default 0). WG converts
it to the motion weight `10 ** (relative_level_db / 20)` and rejects a converted
weight that is zero or nonfinite. WG maps `lining` to BEAT
`wall_impedance {model: "miki", thickness_m: thickness_mm/1000, flow_resistivity_pa_s_per_m2: unchanged}`.
Several moving groups sharing one drive role mean **one physical driver**:
its shared diaphragm's front/rear surfaces or dome/surround. WG refuses
ideal-source channels with FEM diaphragms in more than one region. An
electrodynamic channel alone does not prove a shared diaphragm. Two separate
physical drivers must use separate drive channels (roles), then WG's **Combine**. See the
[protocol rules](fusion-addins/WGLink/README.md#fem-regions-fem-regions-v1).

## Install

The WG platform installer installs the packaged add-in and connects it to WG's runtime. For development from this checkout:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/install_wglink_addin.py --symlink
```

Restart Fusion, then enable WGLink under Utilities > Add-Ins. A symlinked development install uses this repository's active environment for Update resampling. It does not depend on a sibling or legacy checkout.

## Dependencies

`requirements.txt` installs WGLink's development and test dependencies into the active environment. The packaged add-in uses WG's pinned runtime.

## Tests

```bash
.venv/bin/python -m pytest tests -v
```

## License

AGPL-3.0-or-later
