"""Keep Fusion display copy separate from persistent registration and link ids."""

from __future__ import annotations

import ast
from collections import Counter
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADDIN = ROOT / "fusion-addins" / "WGLink"


def _assignments(filename):
    values = {}
    for node in ast.parse((ADDIN / filename).read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass
    return values


def test_panel_commands_and_manifest_use_the_display_name():
    shell = _assignments("WGLink.py")
    assert shell["PANEL_NAME"] == "Waveguide Generator"
    assert _assignments("wglink_return.py")["MANAGE_DROPDOWN_NAME"] == "Manage Link…"
    commands = shell["COMMANDS"]
    assert commands["source"][1] == "Sources & Mesh…"
    assert commands["detach"][2] == (
        "Remove Waveguide Generator link data without changing bodies or features."
    )
    for _command_id, name, description in commands.values():
        assert "WGLink" not in name + description
    manifest = json.loads((ADDIN / "WGLink.manifest").read_text(encoding="utf-8"))
    assert "Waveguide Generator" in manifest["description"][""]
    assert "WGLink" not in str(manifest["description"])
    assert "the waveguide generator add-in" in manifest["description"][""].lower()
    assert "audit" not in manifest["description"][""]
    assert "relink" not in manifest["description"][""]
    assert manifest["id"] == "0cdbb593-fc6c-4615-96b5-1f05a5e18d3a"
    assert manifest["version"] == "0.3.2"


def test_registration_command_and_attribute_ids_stay_compatible():
    shell = _assignments("WGLink.py")
    assert shell["PANEL_ID"] == "hornlab_wglink_panel"
    assert {key: value[0] for key, value in shell["COMMANDS"].items()} == {
        "source": "hornlab_wglink_set_source",
        "declare": "hornlab_wglink_declare_body",
        "fem": "hornlab_wglink_declare_fem_region",
        "solve": "hornlab_wglink_solve",
        "insert": "hornlab_wglink_insert",
        "update": "hornlab_wglink_update",
        "send": "hornlab_wglink_send",
        "detach": "hornlab_wglink_detach",
        "requests": "hornlab_wglink_requests",
    }
    core = _assignments("wglink_core.py")
    assert core["ATTRIBUTE_GROUP"] == "WGLink"
    assert core["PACKAGED_RUNTIME_FILE"] == "wglink_runtime.json"
    source = (ADDIN / "WGLink.py").read_text(encoding="utf-8")
    for literal in (
        'Path.home() / ".hornlab" / "WGLink" / "settings.json"',
        '"hornlab_wglink_manage"',
        'f"hornlab_wglink_file_requests_{_watch_session_id}"',
        'f"hornlab_wglink_owner_candidate_{_watch_session_id}"',
        'f"hornlab_wglink_followup_{_watch_session_id}"',
        '"_hornlab_wglink_ipc_owner_runtime_v1"',
    ):
        assert literal in source


def test_dialogs_notifications_and_error_literals_have_no_old_display_name():
    # Scan all executable string literals, including f-string portions. This
    # covers dialog/file-picker titles, message boxes, logs, errors and labels.
    # WGLink names the exchange folder and the Scripts and Add-Ins entry.
    # Apart from those contexts, only these internal strings may retain it.
    # Counts also prevent a new bare "WGLink" dialog title from hiding behind an allowed value.
    expected = Counter({
        ("WGLink.py", "WGLink"): 1,  # settings directory
        ("WGLink.py", "WGLinkPickupCheck"): 1,
        ("WGLink.py", "WGLinkOwnerCandidate"): 1,
        ("WGLink.py", "WGLinkFileRequests"): 1,
        ("wglink_dev_tests.py", "WGLinkDevTests"): 1,  # dev-only runner thread
        ("wglink_core.py", "WGLink"): 1,  # persisted attribute group
        ("wglink_launcher.py", "WGLinkOwnerProbe"): 1,
        ("wglink_launcher.py", "WGLinkBackendAdmission"): 1,
        ("wglink_send.py", "WGLink.manifest"): 1,
        ("wglink_send.py", "hornlab-fusion-addin/WGLink"): 1,
    })
    found = Counter()
    for path in ADDIN.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
                if isinstance(body[0].value, ast.Constant):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:
                continue
            assert "Manage WG Link" not in node.value, (path.name, node.lineno)
            for retired_folder_name in (
                "CAD Link folder", "CAD Link workspace",
                "Waveguide Generator workspace", "Waveguide Generator bundle folder",
            ):
                assert retired_folder_name not in node.value, (path.name, node.lineno)
            internal_value = node.value.replace("WGLink folder", "").replace(
                "WGLink in Scripts and Add-Ins", ""
            )
            if "WGLink" in internal_value:
                found[path.name, node.value] += 1
    assert found == expected


def test_addin_operations_are_distinct_from_the_wg_app():
    literals = {}
    for filename in ("WGLink.py", "wglink_core.py"):
        tree = ast.parse((ADDIN / filename).read_text(encoding="utf-8"))
        literals[filename] = [
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
    shell_copy = "\n".join(literals["WGLink.py"])
    for phrase in (
        "Starting the Waveguide Generator add-in",
        "Stopping the Waveguide Generator add-in",
        "Promoting the Waveguide Generator add-in's standby registration",
        "another registration of the Waveguide Generator add-in",
        "The Waveguide Generator add-in finished updating the linked model.",
        "Stop and Run WGLink in Scripts and Add-Ins",
    ):
        assert phrase in shell_copy
    assert "Starting Waveguide Generator" not in literals["WGLink.py"]
    assert "Stopping Waveguide Generator" not in literals["WGLink.py"]
    # Starting the external WG app remains a separate, correctly named action.
    assert "Starting Waveguide Generator…" in literals["WGLink.py"]
    assert (
        "Updating the linked model with the Waveguide Generator add-in caused "
        "feature-health regressions: "
    ) in literals["wglink_core.py"]
