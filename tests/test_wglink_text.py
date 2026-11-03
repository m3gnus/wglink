"""Every failure category crosses the real shell boundary and Fusion FakeUI."""
from __future__ import annotations

import ast
from pathlib import Path
import sys

import pytest

from test_wglink_addin_lifecycle import _Definitions, _Panels, _UI, _load_instance

ADDIN = Path(__file__).resolve().parents[1] / "fusion-addins/WGLink"
sys.path.insert(0, str(ADDIN))
import wglink_text as wording  # noqa: E402


@pytest.mark.parametrize("code", [*wording.PROBLEMS, "authoring", "fem_validation", "preflight",
                                  "KeyError", "RuntimeError", "OSError"])
@pytest.mark.parametrize("boundary", ["_message", "_modal", "_report_error"])
def test_failure_matrix_keeps_detail_and_request_in_log(monkeypatch, code, boundary):
    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    shell = _load_instance(monkeypatch, "WGLink_wording_matrix", ui)
    text = shell.wglink_text
    monkeypatch.setattr(text, "context", lambda: ("Horn A", "request-abc"))
    detail = "manifest selector PASSIVE_CARDIOID determinant request id raw-detail"
    if code in text.PROBLEMS:
        exc = text.problem(code, detail)
    elif code in {"authoring", "fem_validation", "preflight"}:
        exc = text.UserFacing(code, "The selected faces could not be used.",
                              "Select their faces again.", detail=detail)
    else:
        exc = {"KeyError": KeyError, "RuntimeError": RuntimeError, "OSError": OSError}[code](detail)
    try:
        raise exc
    except Exception as caught:
        if boundary == "_report_error":
            shell._report_error("Send to WG", "Waveguide Generator error", caught)
        else:
            getattr(shell, boundary)(caught, "Waveguide Generator refused")
    (title, message), = ui.messages
    assert shell.wglink_protocol.user_text_violations(title + " " + message) == []
    assert detail not in message
    if isinstance(exc, text.UserFacing):
        assert message == " ".join(filter(None, (exc.text, exc.next_step)))
    else:
        assert message.startswith("Waveguide Generator could not ")
        assert "Horn A. This has been recorded." in message
    logged = "\n".join(ui.text_palette.written)
    assert detail in logged and "request-abc" in logged
    assert f"code={code}" in logged


def test_matrix_covers_every_explicit_user_facing_code():
    codes = set()
    for path in ADDIN.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name in {"UserFacing", "problem"} and node.args and isinstance(node.args[0], ast.Constant):
                codes.add(node.args[0].value)
    assert codes <= {*wording.PROBLEMS, "authoring", "fem_validation", "preflight"}


def test_plain_wglink_error_has_a_next_step_without_exposing_its_diagnostic(monkeypatch):
    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    shell = _load_instance(monkeypatch, "WGLink_wording_legacy_error", ui)
    monkeypatch.setattr(shell.wglink_text, "context", lambda: ("Horn A", "request-1"))
    exc = shell.wglink_core.WgLinkError("bundle manifest secret diagnostic")
    shell._message(exc, action="update")
    message = ui.messages[0][1]
    assert "could not update Horn A" in message
    assert "Review the model in Fusion before trying again" in message
    assert "secret diagnostic" not in message
    assert "secret diagnostic" in "\n".join(ui.text_palette.written)


def test_wrapped_safety_message_keeps_its_remedy_and_diagnostic(monkeypatch):
    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    shell = _load_instance(monkeypatch, "WGLink_wording_wrapped", ui)
    try:
        try:
            raise shell.wglink_text.problem("mirrored_placement", "determinant=-1")
        except Exception as exc:
            raise shell.wglink_core.WgLinkError("wrapper schema") from exc
    except Exception as exc:
        shell._message(exc, request_id="wrapped-request")
    assert "mirrored WG link" in ui.messages[0][1]
    assert "Insert a fresh copy" in ui.messages[0][1]
    assert "determinant=-1" in "\n".join(ui.text_palette.written)
    assert "wrapped-request" in "\n".join(ui.text_palette.written)


def test_guard_covers_all_spec_terms_and_keeps_user_document_names():
    for word in ("return", ".wgreturn", "UUID", "Text Commands", "main-thread", "event",
                 "options['anchor_instance_id']", "bytes", "determinant", "orthonormal", "chirality"):
        assert wording.wglink_protocol.user_text_violations(word), word
    assert wording.user_message(RuntimeError("schema"), document="Gate horn", logger=lambda _: None) == (
        "Waveguide Generator could not use Gate horn. This has been recorded.")


def test_success_report_preserves_user_names_and_logs_technical_warnings(monkeypatch):
    ui = _UI(_Panels(), _Definitions(reserve_ids=False))
    shell = _load_instance(monkeypatch, "WGLink_wording_success", ui)
    report = {"design_name": "Return Horn", "link_name": "Gate speaker", "wrapper": "occurrence",
              "warnings": ["Timeline marker restoration failed: selector"]}
    shell._message(shell._summary("insert", report), names=shell.wglink_text.report_names(report))
    title, message = ui.messages[0]
    assert message.startswith("Inserted the Waveguide Generator model.")
    assert "Return Horn" in message and "Gate speaker" in message
    assert shell.wglink_protocol.user_text_violations(
        title + message, names=shell.wglink_text.report_names(report)) == []
    assert "Timeline marker restoration failed: selector" in "\n".join(ui.text_palette.written)
    assert "could not use" not in message


def _body(name, *, visible, painted=(), **flags):
    return {"kind": "body", "object_id": name, "name": name, "path": f"Horn:1/{name}", "body_kind": "solid",
            "visible": visible, "suppressed": False, "external_reference": "none", "declaration": None,
            "component": "Horn", "source_face_roles": painted, **flags}


def test_a_scope_refusal_names_each_body_and_its_fix(monkeypatch):
    # A WG body split by hand: the linked original and two painted pieces are
    # hidden, one piece is shown.
    from wglink_return import WgReturnError, plan_export_scope

    plan = plan_export_scope("root", [
        _body("WG enclosure", visible=False, wglink_managed=True, wglink_role="enclosure",
              contains_solver_anchor=True, contains_required_source=True),
        _body("WG enclosure (1)", visible=False, painted=("HF",)),
        _body("WG enclosure (2)", visible=True, painted=("MF", "HF")),
    ])
    with pytest.raises(WgReturnError) as exc:
        plan.manifest_scope()
    monkeypatch.setattr(wording, "log", lambda _text: None)

    message = wording.scope_message(exc.value)

    assert message == (
        "WG cannot use this model as it is. "
        "“WG enclosure (1)” is hidden but has HF driver faces: show it, or clear those faces with Sources & Mesh… "
        "“WG enclosure” is hidden, but it carries the WG link and holds driver faces: show it in the browser."
    )


def test_a_scope_refusal_without_wording_keeps_the_general_remedy(monkeypatch):
    monkeypatch.setattr(wording, "log", lambda _text: None)
    from wglink_return import WgReturnError

    worded = {"name": "Piece", "reason_code": "hidden_painted", "source_roles": ["HF"], "reason": "r"}
    unworded = {"name": "Odd", "reason": "cannot be classified"}

    assert wording.scope_message(WgReturnError("x", reasons=[unworded])) == " ".join(wording.PROBLEMS["scope_invalid"])
    assert wording.scope_message(WgReturnError("x", reasons=[worded, unworded])).endswith(
        wording.PROBLEMS["scope_invalid"][1])


def test_a_body_name_with_a_guarded_word_still_reaches_the_user(monkeypatch):
    monkeypatch.setattr(wording, "log", lambda _text: None)
    from wglink_return import WgReturnError

    reason = {"name": "Gate manifest", "reason_code": "hidden_painted", "source_roles": ["HF"], "reason": "r"}

    assert "“Gate manifest”" in wording.scope_message(WgReturnError("x", reasons=[reason]))
