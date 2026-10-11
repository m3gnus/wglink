from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_readme_local_links_exist():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)]+)\)", text):
        if "://" not in target:
            assert (ROOT / target.split("#")[0]).exists(), target


def test_command_followup_source_spacing_and_docstring_width():
    text = (ROOT / "fusion-addins/WGLink/WGLink.py").read_text(encoding="utf-8")
    before = text.split("def _after_command()", 1)[0]
    assert before.endswith("\n\n\n")
    docstring = before.split("def _command_finished()", 1)[1].split('"""')[1]
    assert all(len(line) <= 88 for line in docstring.splitlines())


def test_wglink_install_docs_reference_current_installer():
    for rel_path in ("README.md", "docs/WGLINK-GUIDE.md", "fusion-addins/WGLink/README.md"):
        text = (ROOT / rel_path).read_text(encoding="utf-8")
        assert "scripts/install_wglink_addin.py" in text
        assert "scripts/install_fusion_wg_metal_addin.py" not in text


def test_pipeline_guides_do_not_offer_removed_commands():
    for rel_path in ("docs/HEADLESS.md", "docs/WGMETAL-PIPELINE-GUIDE.md"):
        text = (ROOT / rel_path).read_text(encoding="utf-8")
        assert "python scripts/" not in text
        assert "WGLink" in text
