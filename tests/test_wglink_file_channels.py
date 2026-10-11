"""Explicit handoff claims, inbox writes and Fusion status files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fusion-addins" / "WGLink"))
import wglink_inbox  # noqa: E402
import wglink_handoff  # noqa: E402
import wglink_status  # noqa: E402


def _bundle(root: Path, name: str, export_id: str, sequence: int = 1) -> Path:
    bundle = root / f"{name}.wglink"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "wglink.json").write_text(
        json.dumps({"export": {"id": export_id, "sequence": sequence}}),
        encoding="utf-8",
    )
    return bundle


def _handoff(
    root: Path, bundle: Path, export_id: str = "wge_2", request_id: str = "req-1"
) -> Path:
    """One handoff file as WG writes it (delivery version 3)."""

    marker = root / f"{request_id}.json"
    marker.write_text(
        json.dumps({
            "schemaVersion": 3,
            "target": "fusion360",
            "requestId": request_id,
            "operationId": request_id,
            "deliverySequence": 1,
            "bundlePath": str(bundle),
            "bundleId": "wgb_2",
            "exportId": export_id,
            "sequence": 2,
            "designId": "wgd_a",
            "expectedDocumentId": "fusion:doc-a",
            "expectedInstanceId": "instance-b",
            "expectedReturnStateHash": "sha256:return-state",
        }),
        encoding="utf-8",
    )
    return marker


def test_a_scoped_pending_handoff_is_read_and_acknowledged(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "horn", "wge_2", sequence=2)
    marker = _handoff(tmp_path, bundle)

    handoff = wglink_handoff.read_pending_handoff(marker)

    assert handoff is not None
    assert handoff.request_id == handoff.operation_id == "req-1"
    assert handoff.bundle_path == str(bundle)
    assert handoff.export_id == "wge_2"
    assert handoff.sequence == "2"
    assert handoff.design_id == "wgd_a"
    assert handoff.expected_document_id == "fusion:doc-a"
    assert handoff.expected_instance_id == "instance-b"
    assert handoff.expected_return_state_hash == "sha256:return-state"
    claimed = wglink_handoff.claim_request(handoff)
    assert claimed is not None and not marker.exists()
    assert wglink_handoff.acknowledge_handoff(claimed) is True
    assert not claimed.marker_path.exists()


def test_acknowledging_a_handoff_deletes_only_its_own_claim(tmp_path: Path) -> None:
    """A file written at the same name after the claim is a new request."""

    bundle = _bundle(tmp_path, "horn", "wge_2")
    marker = _handoff(tmp_path, bundle)
    handoff = wglink_handoff.read_pending_handoff(marker)
    assert handoff is not None
    claimed = wglink_handoff.claim_request(handoff)
    assert claimed is not None

    _handoff(tmp_path, bundle, export_id="wge_3")

    assert wglink_handoff.acknowledge_handoff(claimed) is True
    assert json.loads(marker.read_text())["exportId"] == "wge_3"
    # Acknowledging the request itself -- not a claim -- deletes nothing.
    assert wglink_handoff.acknowledge_handoff(handoff) is False
    assert marker.exists()


def test_fusion_status_publishes_document_config_and_parameters_atomically(
    tmp_path: Path,
) -> None:
    marker = wglink_status.write_fusion_status(
        tmp_path,
        session_id="session-a",
        document_name="Tritonia V",
        adapter_version="1.2.3",
        workspace_root=tmp_path / "exchange",
        links=[{
            "instance_id": "instance-a",
            "design_id": "wgd_a",
            "design_hash": "sha256:config",
            "formula": "r-osse",
            "config_present": "true",
            "parameter_count": "14",
            "parameter_drift_count": "99",
            "drifted_parameters": ["wg_mouth_thickness", "wg_length"],
            "local_body_state": "modified",
            "body_fingerprint_hash": "sha256:body",
            "document_signature_hash": "sha256:return-state",
            "document_body_count": "3",
            "source_state_hash": "sha256:sources",
            "geometry_revision_token": "sha256:revision-now",
            "measured_revision_token": "sha256:revision-measured",
            "body_object_ids": ["body-b", "body-a", "body-a"],
            "transform_hash": "sha256:transform",
            "source_ids": ["source-hf"],
            "drive_channel_ids": ["drive-hf"],
            "export_id": "wge_a",
        }],
        updated_at=datetime(2026, 8, 12, 15, 30, tzinfo=timezone.utc),
    )

    payload = json.loads(marker.read_text())
    assert payload["adapterVersion"] == "1.2.3"
    assert payload["workspaceRoot"] == str((tmp_path / "exchange").resolve())
    assert payload["document"]["name"] == "Tritonia V"
    assert payload["document"]["links"][0] == {
        "bundlePath": None,
        "configPresent": True,
        "designHash": "sha256:config",
        "designId": "wgd_a",
        "designName": None,
        "editVersion": None,
        "exportId": "wge_a",
        "exportSequence": None,
        "formula": "r-osse",
        "instanceId": "instance-a",
        "linkName": None,
        "lineageId": None,
        "parameterCount": 14,
        "parameterDriftCount": 2,
        "driftedParameters": ["wg_length", "wg_mouth_thickness"],
        "localBodyState": "modified",
        "bodyFingerprintHash": "sha256:body",
        "documentSignatureHash": "sha256:return-state",
        "documentBodyCount": 3,
        "sourceStateHash": "sha256:sources",
        # A cache-only heartbeat says which revision the document is at and
        # which revision its published measurement came from, so a consumer
        # can tell a current observation from an older one for free.
        "geometryRevisionToken": "sha256:revision-now",
        "measuredRevisionToken": "sha256:revision-measured",
        "bodyObjectIds": ["body-a", "body-b"],
        "transformHash": "sha256:transform",
        "sourceIds": ["source-hf"],
        "driveChannelIds": ["drive-hf"],
    }
    link = payload["document"]["links"][0]
    assert link["parameterDriftCount"] == len(link["driftedParameters"])
    assert list(tmp_path.glob(f"{wglink_status.FUSION_STATUS_FILENAME}.*")) == []
    assert wglink_status.remove_fusion_status(tmp_path, session_id="other") is False
    assert wglink_status.remove_fusion_status(tmp_path, session_id="session-a") is True
    assert not marker.exists()


def test_fusion_status_publishes_no_parameter_drift_as_an_empty_list(
    tmp_path: Path,
) -> None:
    marker = wglink_status.write_fusion_status(
        tmp_path,
        session_id="session-a",
        document_name="Tritonia V",
        links=[{
            "instance_id": "instance-a",
            "parameter_drift_count": "2",
            "drifted_parameters": [],
        }],
    )

    link = json.loads(marker.read_text())["document"]["links"][0]
    assert link["driftedParameters"] == []
    assert link["parameterDriftCount"] == 0
    assert link["parameterDriftCount"] == len(link["driftedParameters"])


def test_fusion_status_omits_unproven_or_malformed_optional_identities(
    tmp_path: Path,
) -> None:
    marker = wglink_status.write_fusion_status(
        tmp_path,
        session_id="session-a",
        document_name="Tritonia V",
        links=[{
            "instance_id": "instance-a",
            "body_object_ids": [],
            "transform_hash": "",
            "source_ids": ["source-hf", ""],
            "drive_channel_ids": "drive-hf",
        }],
    )

    link = json.loads(marker.read_text())["document"]["links"][0]
    assert "bodyObjectIds" not in link
    assert "transformHash" not in link
    assert "sourceIds" not in link
    assert "driveChannelIds" not in link


def test_return_request_is_targeted_to_one_addin_session_and_acknowledged(tmp_path: Path) -> None:
    marker = tmp_path / "request-a.json"
    marker.write_text(json.dumps({
        "schemaVersion": 3,
        "target": "fusion360",
        "requestId": "request-a",
        "operationId": "request-a",
        "deliverySequence": 1,
        "sessionId": "session-a",
        "designId": "wgd_a",
        "documentId": "fusion:doc-a",
        "instanceId": "instance-a",
        "expectedReturnStateHash": "sha256:return-state",
    }))

    assert wglink_handoff.read_return_request(marker, session_id="other") is None
    request = wglink_handoff.read_return_request(marker, session_id="session-a")
    assert request is not None
    assert request.design_id == "wgd_a"
    assert request.document_id == "fusion:doc-a"
    assert request.instance_id == "instance-a"
    assert request.expected_return_state_hash == "sha256:return-state"
    claimed = wglink_handoff.claim_request(request)
    assert claimed is not None
    assert wglink_handoff.acknowledge_return_request(claimed) is True
    assert not marker.exists() and not claimed.marker_path.exists()


def test_handoff_refuses_an_out_of_workspace_bundle(tmp_path: Path) -> None:
    root = tmp_path / "workspace" / "wglink"
    root.mkdir(parents=True)
    outside = _bundle(tmp_path, "outside", "wge_2")
    marker = _handoff(root, outside)

    assert wglink_handoff.read_pending_handoff(marker) is None


def test_machine_local_handoff_accepts_only_the_selected_workspace_bundle_root(
    tmp_path: Path,
) -> None:
    bundles = tmp_path / "workspace" / "wglink"
    bundles.mkdir(parents=True)
    bundle = _bundle(bundles, "horn", "wge_2")
    ipc = tmp_path / "data" / "ipc" / "wglink"
    ipc.mkdir(parents=True)
    marker = _handoff(ipc, bundle)

    handoff = wglink_handoff.read_pending_handoff(marker, bundle_root=bundles)

    assert handoff is not None
    claimed = wglink_handoff.claim_request(handoff)
    assert claimed is not None
    assert wglink_handoff.acknowledge_handoff(claimed) is True


@pytest.mark.parametrize(
    "payload",
    [
        {},
        # What a WG before delivery version 3 wrote.
        {"schemaVersion": 1, "target": "fusion360", "bundlePath": "x", "bundleId": "b",
         "exportId": "e"},
        {"schemaVersion": 2, "target": "fusion360", "requestId": "r", "operationId": "r",
         "deliverySequence": 1},
        {"schemaVersion": 3, "target": "other", "requestId": "r", "operationId": "r",
         "deliverySequence": 1},
        {"schemaVersion": 3, "target": "fusion360", "requestId": "r", "operationId": "r",
         "deliverySequence": 1, "bundlePath": ""},
    ],
)
def test_an_invalid_pending_handoff_is_silent(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    marker = tmp_path / "r.json"
    marker.write_text(json.dumps(payload))
    assert wglink_handoff.read_pending_handoff(marker) is None


def _advertise_current_delivery(ipc: Path) -> None:
    ipc.mkdir(parents=True, exist_ok=True)
    (ipc / wglink_inbox.CAPABILITIES_FILENAME).write_text(json.dumps({
        "schemaVersion": 1, "solveCommandDelivery": 4, "fusionRequestDelivery": 3,
    }))


def test_a_solve_request_names_the_bundle_and_pins_its_manifest(tmp_path) -> None:
    """The intent is a separate marker, not a field in the geometry manifest.

    WG re-reads returns whenever it re-lists the workspace, so an intent flag
    inside the evidence would be re-observed and re-solved; a command id can be
    spent exactly once. The manifest hash lets WG refuse a bundle that changed
    between the publish and this write.
    """

    workspace = tmp_path / "workspace"
    bundle = workspace / "wgreturn" / "speaker.wgreturn"
    bundle.mkdir(parents=True)
    (bundle / "wgreturn.json").write_bytes(b'{"document": {}}')
    _advertise_current_delivery(tmp_path / "ipc")

    marker = wglink_inbox.write_solve_request(
        tmp_path / "ipc",
        command_id="cmd-1",
        return_id="wgr_1",
        bundle_path=bundle,
        workspace_root=workspace,
    )

    payload = json.loads(marker.read_text())
    assert marker.parent.name == wglink_inbox.SOLVE_REQUESTS_DIRECTORY
    assert marker.name == "cmd-1.json"
    assert payload["target"] == "waveguide-generator"
    assert payload["commandId"] == "cmd-1"
    # Workspace-relative, so WG resolves it inside its own selected folder.
    assert payload["bundlePath"] == "wgreturn/speaker.wgreturn"
    assert payload["manifestSha256"] == (
        "sha256:" + hashlib.sha256(b'{"document": {}}').hexdigest()
    )


def test_a_solve_request_refuses_a_bundle_outside_the_workspace(tmp_path) -> None:
    outside = tmp_path / "elsewhere" / "speaker.wgreturn"
    outside.mkdir(parents=True)
    (outside / "wgreturn.json").write_bytes(b"{}")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _advertise_current_delivery(tmp_path / "ipc")

    with pytest.raises(OSError):
        wglink_inbox.write_solve_request(
            tmp_path / "ipc",
            command_id="cmd-1",
            return_id="wgr_1",
            bundle_path=outside,
            workspace_root=workspace,
        )


def test_fusion_status_publishes_the_link_label_beside_wgs_design_name(
    tmp_path: Path,
) -> None:
    marker = wglink_status.write_fusion_status(
        tmp_path,
        session_id="session-a",
        document_name="waveguide v1",
        links=[{
            "instance_id": "instance-a",
            "design_name": "260308Tritonia-M",
            "link_name": "Left waveguide",
        }],
    )

    payload = json.loads(marker.read_text())
    link = payload["document"]["links"][0]
    # Three names, all published, none derived from another: the Fusion
    # document, WG's design, and the user's own label for this placement.
    assert payload["document"]["name"] == "waveguide v1"
    assert link["designName"] == "260308Tritonia-M"
    assert link["linkName"] == "Left waveguide"


def test_a_link_with_no_label_publishes_a_null_rather_than_a_guess(
    tmp_path: Path,
) -> None:
    marker = wglink_status.write_fusion_status(
        tmp_path,
        session_id="session-a",
        document_name="waveguide v1",
        links=[{"instance_id": "instance-a", "design_name": "asro68"}],
    )

    link = json.loads(marker.read_text())["document"]["links"][0]
    # Never fill the label in from the design name here: a client that shows
    # both would then show one name twice and call it two.
    assert link["linkName"] is None
    assert link["designName"] == "asro68"


def test_fusion_status_publishes_tick_diagnostics_when_they_are_supplied(
    tmp_path: Path,
) -> None:
    """The heartbeat is the only place a closed Fusion can be measured from.

    Nothing else reports what one watch tick cost on the user's own document,
    and that tick runs on Fusion's main thread every four seconds -- so its
    wall clock is what a "Fusion feels slow" report is actually about.
    """

    marker = wglink_status.write_fusion_status(
        tmp_path,
        session_id="session-a",
        document_name="waveguide v1",
        links=[],
        diagnostics={
            "watchIntervalSeconds": 4.0,
            "lastTickMs": {"return_state_ms": 812.4, "snapshot_ms": 900.1},
            "source": {"sourceCommit": "abc1234+dirty"},
        },
    )
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["diagnostics"]["lastTickMs"]["return_state_ms"] == 812.4
    assert payload["diagnostics"]["source"]["sourceCommit"] == "abc1234+dirty"
    assert payload["schemaVersion"] == 1


def test_fusion_status_omits_diagnostics_when_there_are_none(tmp_path: Path) -> None:
    marker = wglink_status.write_fusion_status(
        tmp_path, session_id="session-a", document_name="waveguide v1", links=[]
    )
    assert "diagnostics" not in json.loads(marker.read_text(encoding="utf-8"))
