#!/usr/bin/env python3
"""Run a scripted check inside the running Fusion add-in, from a terminal.

Works only against a dev-synced WGLink (``scripts/dev_sync_wglink.py``): that
install watches ``<ipc>/.dev-tests/requests`` and writes
``<ipc>/.dev-tests/results/<id>.json`` (plus screenshots in
``results/<id>/``). Fusion must be running with a design open.

    python scripts/fusion_dev_test.py --scenario noop
    python scripts/fusion_dev_test.py --scenario sources-mesh-baseline --wait 120

Exit status: 0 passed, 1 failed or error, 2 no result in time.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import uuid

ADDIN = Path(__file__).resolve().parents[1] / "fusion-addins" / "WGLink"
sys.path.insert(0, str(ADDIN))

import wglink_dev_tests  # noqa: E402
import wglink_workspace  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", required=True, help="noop, reload or sources-mesh-baseline")
    parser.add_argument("--wait", type=float, default=120.0, help="seconds to wait for the result")
    parser.add_argument("--ipc", type=Path, help="WGLink IPC folder (default: WG's)")
    parser.add_argument("--modules", nargs="*", default=[],
                        help="with --scenario reload: extra helper modules to reload first")
    args = parser.parse_args()

    ipc = args.ipc or wglink_workspace.ipc_folder()
    if ipc is None:
        print("No WGLink IPC folder; has WG or the add-in run on this machine?", file=sys.stderr)
        return 2
    requests, results = wglink_dev_tests.folders(ipc)
    request_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    wglink_dev_tests.write_json(requests / f"{request_id}.json", {"id": request_id, "scenario": args.scenario,
                                                                "modules": args.modules})
    print(f"requested {args.scenario} as {request_id} in {requests}", flush=True)

    result_path = results / f"{request_id}.json"
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        if result_path.is_file():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            print(json.dumps(result, indent=2, sort_keys=True))
            print(f"\n{result.get('status')}: {result_path}")
            return 0 if result.get("status") == "passed" else 1
        time.sleep(0.5)
    still_queued = (requests / f"{request_id}.json").exists()
    print(f"No result after {args.wait:g} s. "
          + ("The request was never picked up: is Fusion running a dev-synced WGLink "
             "(dev_sync, then Stop/Run the add-in)?" if still_queued else
             f"The scenario started but has not finished; look for {result_path} later."),
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
