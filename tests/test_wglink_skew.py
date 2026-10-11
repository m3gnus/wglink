"""Capability refusal precedes every command's export or model mutation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_wglink_transfer import _inbox, _outbox, _run, _transfer


@pytest.mark.parametrize("operation", ["send", "solve", "insert", "update"])
@pytest.mark.parametrize("channel,value", [
    ("solveCommandDelivery", 3),
    ("fusionRequestDelivery", 2),
    ("solveCommandDelivery", True),
    ("fusionRequestDelivery", "3"),
])
def test_incompatible_wg_refuses_before_export_picker_or_mutation(
    monkeypatch, tmp_path: Path, operation: str, channel: str, value: object
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_skew_{operation}_{channel}_{value}")
    module = fixture.module
    path = fixture.ipc / "wg-capabilities.json"
    advertised = json.loads(path.read_text())
    advertised[channel] = value
    advertised["version"] = "999.0.0"  # A product version cannot authorize delivery.
    path.write_text(json.dumps(advertised))
    calls = []
    for target, name in (
        (module.wglink_send, "send"),
        (module.wglink_core, "insert"),
        (module.wglink_core, "update"),
        (module, "_choose_bundle"),
        (module, "_send_options"),
    ):
        monkeypatch.setattr(target, name, lambda *_a, **_k: calls.append(1))

    _run(module, operation)

    assert calls == []
    assert _inbox(fixture.ipc) == [] and _outbox(fixture.ipc) == []
    assert fixture.timers == []
    assert fixture.ui.progress_dialogs == []
    title, text = fixture.ui.messages[-1]
    assert title == "WGLink refused" and "Update Waveguide Generator" in text


@pytest.mark.parametrize("payload", [
    {}, {"schemaVersion": 2, "solveCommandDelivery": 4, "fusionRequestDelivery": 3},
    {"schemaVersion": 1.0, "solveCommandDelivery": 4, "fusionRequestDelivery": 3},
    {"schemaVersion": True, "solveCommandDelivery": 4, "fusionRequestDelivery": 3},
    {"schemaVersion": 1, "solveCommandDelivery": 4},
    {"schemaVersion": 1, "fusionRequestDelivery": 3},
    {"schemaVersion": 1, "solveCommandDelivery": 4.0, "fusionRequestDelivery": 3},
])
def test_missing_or_invalid_capabilities_do_not_establish_compatibility(
    monkeypatch, tmp_path: Path, payload: dict
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_invalid_capabilities")
    (fixture.ipc / "wg-capabilities.json").write_text(json.dumps(payload))
    assert fixture.module.wglink_inbox.wg_speaks_delivery_version(fixture.ipc) is False
    _run(fixture.module, "solve")
    assert _inbox(fixture.ipc) == []
    assert "Update Waveguide Generator" in fixture.ui.messages[-1][1]


def test_product_version_and_retired_live_capability_do_not_block_file_delivery(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_capable_old_label", live=True)
    path = fixture.ipc / "wg-capabilities.json"
    payload = json.loads(path.read_text())
    payload.update(version="0.0.1", unrelatedCapability={"future": True})
    path.write_text(json.dumps(payload))
    _run(fixture.module, "send")
    assert [request["kind"] for request in _inbox(fixture.ipc)] == ["receive_snapshot"]


@pytest.mark.parametrize("operation", ["insert", "update"])
def test_incompatible_handoff_is_consumed_before_creating_or_mutating_a_document(
    monkeypatch, tmp_path: Path, operation: str
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, f"WGLink_handoff_skew_{operation}", advertised=3)
    module = fixture.module
    bundle = tmp_path / "workspace" / "wglink" / "speaker.wglink"
    bundle.mkdir()
    folder = fixture.ipc / ".fusion-handoffs"
    folder.mkdir()
    path = folder / "handoff-skew.json"
    path.write_text(json.dumps({
        "schemaVersion": 3, "target": "fusion360", "requestId": "handoff-skew",
        "operationId": "handoff-skew", "deliverySequence": 1,
        "bundlePath": str(bundle), "bundleId": "wgb", "exportId": "wge",
        "expectedDocumentId": "fusion:doc-a" if operation == "update" else "",
        "expectedInstanceId": "instance-a" if operation == "update" else "",
        "expectedReturnStateHash": "sha256:state" if operation == "update" else "",
    }))
    calls = []
    monkeypatch.setattr(module, "_active_document_id", lambda: "fusion:doc-a")
    for target, name in ((module, "_ensure_design_ready"), (module.wglink_core, "insert"), (module.wglink_core, "update")):
        monkeypatch.setattr(target, name, lambda *_a, **_k: calls.append(1))

    assert module._apply_pending_handoff({"document_id": "fusion:doc-a", "links": []}) == module.HANDLED
    assert calls == []
    assert not path.exists() and list(folder.iterdir()) == []
    assert "Update Waveguide Generator" in fixture.ui.messages[-1][1]
    assert module._request_trace["outcome"] == "refused"


def test_tick_never_reads_export_manifests_or_offers_a_newer_export(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_exports_unobserved")
    module = fixture.module
    bundle = tmp_path / "unrequested.wglink"
    bundle.mkdir()
    (bundle / "wglink.json").write_text('{"export":{"id":"wge_new","sequence":2}}')
    snapshot = {
        "document_name": "Horn", "document_id": "fusion:doc-a", "diagnostics": {},
        "links": [{"instance_id": "instance-a", "bundle_path": str(bundle), "export_id": "wge_old"}],
        "links_inspected": True,
    }
    monkeypatch.setattr(module, "_fusion_snapshot", lambda: snapshot)
    monkeypatch.setattr(module, "_settle_claims", lambda _snapshot: None)
    monkeypatch.setattr(module, "_service_geometry_refresh", lambda: None)
    original = Path.read_text
    reads = []

    def read(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    changed = []
    monkeypatch.setattr(module.wglink_core, "update", lambda *_a, **_k: changed.append(1))
    for _ in range(3):
        module._on_watch_tick()

    assert changed == [] and fixture.ui.messages == []
    assert bundle / "wglink.json" not in reads
    assert not hasattr(module, "_watcher")
    assert not hasattr(module, "_apply_announced_updates")


def test_file_request_errors_name_the_current_service(monkeypatch, tmp_path: Path) -> None:
    fixture = _transfer(monkeypatch, tmp_path, "WGLink_file_request_error")
    module = fixture.module
    reported = []
    monkeypatch.setattr(module, "_on_watch_tick", lambda: (_ for _ in ()).throw(RuntimeError("request read failed")))
    monkeypatch.setattr(module, "_report_error", lambda action, title, exc: reported.append((action, title, str(exc))))

    module.WatchEventHandler().notify(None)

    assert reported == [("Servicing WG file requests", "WGLink file request error", "request read failed")]
