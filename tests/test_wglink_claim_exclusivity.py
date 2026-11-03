"""Two real consumers claim and read one request before authorizing a mutation.

Runs on every host, including native Windows where plain rename is not exclusive.
"""

import json
import subprocess
import sys
import time
import types
from pathlib import Path

from test_wglink_activation_boundary import _boundary
from test_wglink_addin_lifecycle import _per_request_folders, _per_request_handoff


def test_windows_claim_uses_exclusive_handle_rename_before_reading(monkeypatch, tmp_path):
    m = _boundary(monkeypatch, tmp_path, "WGLink_windows_claim", coordination=False).module
    ipc, bundles = _per_request_folders(monkeypatch, m, tmp_path / "files")
    _per_request_handoff(ipc, bundles, instance_id=None)
    h = m.wglink_handoff
    pending = h.next_pending_handoff(ipc, bundle_root=bundles)
    calls = []
    def handle_rename(source, target, wait_seconds):
        calls.append((source, target))
        source.rename(target)
    monkeypatch.setattr(m.wglink_protocol, "sys", types.SimpleNamespace(platform="win32"))
    monkeypatch.setattr(m.wglink_protocol, "_take_by_rename_windows", handle_rename)
    claim = h.claim_request(pending)
    assert claim is not None
    assert calls == [(pending.marker_path, claim.marker_path)]
    assert json.loads(claim.marker_path.read_text())["requestId"] == pending.request_id
    assert h.acknowledge_handoff(claim)


def test_two_process_claims_authorize_exactly_one_mutation(tmp_path):
    rounds = 40
    addin = Path(__file__).resolve().parents[1] / "fusion-addins/WGLink"
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    for index in range(rounds):
        ipc = tmp_path / str(index)
        ipc.mkdir()
        _per_request_handoff(ipc, bundles, instance_id=None)
    script = """
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import wglink_handoff as h
root, label, rounds = Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
mutations = []
for index in range(rounds):
    ipc = root / str(index)
    pending = h.next_pending_handoff(ipc, bundle_root=root / 'bundles')
    assert pending is not None
    (ipc / f'ready-{label}').touch()
    deadline = time.monotonic() + 5
    while not (ipc / 'go').exists():
        assert time.monotonic() < deadline
        time.sleep(.001)
    claim = h.claim_request(pending)
    if claim is not None:
        # claim_request's post-rename JSON read has succeeded. This is the
        # actual execution boundary, not a count of successful rename calls.
        mutations.append(index)
        h.acknowledge_handoff(claim)
print(json.dumps(mutations))
"""
    workers = [subprocess.Popen(
        [sys.executable, "-c", script, str(addin), str(tmp_path), label, str(rounds)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) for label in ("a", "b")]
    try:
        for index in range(rounds):
            ipc = tmp_path / str(index)
            deadline = time.monotonic() + 5
            while not all((ipc / f"ready-{label}").exists() for label in ("a", "b")):
                assert all(worker.poll() is None for worker in workers)
                assert time.monotonic() < deadline
                time.sleep(.001)
            (ipc / "go").touch()
        mutations = []
        for worker in workers:
            out, err = worker.communicate(timeout=10)
            assert worker.returncode == 0, err
            mutations.extend(json.loads(out))
        assert sorted(mutations) == list(range(rounds))
        assert not list(tmp_path.glob("*/.fusion-handoffs/*.json"))
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.terminate()
                worker.communicate(timeout=5)
