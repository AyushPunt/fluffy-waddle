"""Headless run of the Streamlit UI through streamlit.testing; skipped when the ui group is not installed."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from docagent.manifest import Manifest  # noqa: E402
from tests.fixtures import review  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "src" / "docagent" / "ui.py"


def _buttons(at: AppTest) -> dict[str, object]:
    return {b.label: b for b in at.button}


def _fill_without_model(at: AppTest) -> None:
    """Untick "Draft with the model" so the test never calls the API, then fill."""
    box = next(c for c in at.checkbox if c.label == "Draft with the model")
    box.uncheck().run()
    _buttons(at)["Fill document"].click().run()


@pytest.fixture
def app() -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    assert not at.exception
    return at


def test_demo_approve_fill_without_plan(app: AppTest) -> None:
    app.sidebar.button[0].click().run()  # Load demo
    assert not app.exception
    ss = app.session_state
    assert ss["template_name"] == "csra_template.docx"
    assert ss["kb_text"] and ss["record_text"] and ss["library_text"]
    assert next(c for c in app.checkbox if c.label == "Draft with the model").value is True

    inputs = {t.label: t for t in app.text_input}
    inputs["Approved by (name and role)"].set_value("Template Owner (QA)").run()
    _buttons(app)["Approve manifest"].click().run()
    assert not app.exception
    m = Manifest.model_validate_json(ss["manifest_text"])
    assert m.approval is not None and m.approval.approved_by == "Template Owner (QA)"

    _fill_without_model(app)
    assert not app.exception and not list(app.error)
    report = ss["fill_result"].report
    status = {r["id"]: r["status"] for r in report["regions"]}
    assert status["system_name"] == "filled"  # fact from the record
    assert status["purpose"] == "filled"  # boilerplate from the library
    assert status["system_description"] == "gap"  # authored region, no plan
    assert status["overall_risk"] == "blocked"  # undecided choice blocks sign-off
    assert report["awaiting_human"] == ["approvals"]


def test_fill_with_hand_written_plan(app: AppTest) -> None:
    app.sidebar.button[0].click().run()
    inputs = {t.label: t for t in app.text_input}
    inputs["Approved by (name and role)"].set_value("Template Owner (QA)").run()
    _buttons(app)["Approve manifest"].click().run()
    ss = app.session_state
    approved = Manifest.model_validate_json(ss["manifest_text"])
    ss["plan_text"] = review.plan(approved).model_dump_json(indent=2)
    app.run()
    assert not app.exception

    _fill_without_model(app)
    assert not app.exception and not list(app.error)
    report = ss["fill_result"].report
    status = {r["id"]: r["status"] for r in report["regions"]}
    assert status["requirements_rows"] == "gap"  # rows written; one cell is a declared {{GAP}}
    assert status["overall_risk"] == "filled"
    assert status["gxp_applicability"] == "blocked"  # the demo plan leaves one item unknown on purpose
    assert len(report["blocking"]) == 2  # same as examples/demo.py through the CLI


def test_plan_for_another_manifest_is_flagged(app: AppTest) -> None:
    app.sidebar.button[0].click().run()
    inputs = {t.label: t for t in app.text_input}
    inputs["Approved by (name and role)"].set_value("Template Owner (QA)").run()
    _buttons(app)["Approve manifest"].click().run()
    ss = app.session_state
    ss["plan_text"] = '{"plan_version": 1, "manifest_sha256": "' + "0" * 64 + '", "regions": {}}'
    app.run()
    assert any("different manifest version" in e.value for e in app.error)


def test_clear_workspace_resets_state(app: AppTest) -> None:
    app.sidebar.button[0].click().run()
    assert app.session_state["template_bytes"] is not None
    app.sidebar.button[1].click().run()  # Clear workspace
    assert not app.exception
    assert app.session_state["template_bytes"] is None
    assert app.session_state["manifest_text"] == ""
