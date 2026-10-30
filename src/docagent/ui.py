"""Browser UI for the template engine, built on Streamlit.

    uv run streamlit run src/docagent/ui.py

The page walks the same four stages as the CLI: approve a template, provide the
inputs, draft a plan with the model, fill and verify. It calls the package API
directly; nothing here bypasses the manifest approval or the immutability gate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import streamlit as st
from pydantic import ValidationError

from docagent.author import author
from docagent.cli import load_dotenv
from docagent.compiler import TemplateDoc, approve, propose
from docagent.errors import DocAgentError
from docagent.gate import run_gate
from docagent.kb import KnowledgeBase
from docagent.llm import DEFAULT_MODEL, AnthropicLLM
from docagent.manifest import Manifest
from docagent.ooxml.wordml import norm
from docagent.plan import BoilerplateLibrary, FillPlan, SystemRecord
from docagent.writer import fill

ROOT = Path(__file__).resolve().parents[2]
MODELS = [DEFAULT_MODEL, "claude-sonnet-5-5", "claude-haiku-5-5"]

# --- session state ----------------------------------------------------------------------------

KEYS: dict[str, Any] = {
    "template_bytes": None,
    "template_name": "",
    "manifest_text": "",
    "record_text": "",
    "library_text": "",
    "kb_text": "",
    "plan_text": "",
    "audit": None,
    "fill_result": None,
    "gate_result": None,
    "flash_error": "",
}


def _init() -> None:
    for k, v in KEYS.items():
        st.session_state.setdefault(k, v)


def _parse(model: type, text: str, what: str):
    """Validate JSON text against a pydantic model; return (object, error-or-None)."""
    if not text.strip():
        return None, f"{what} is empty"
    try:
        return model.model_validate_json(text), None
    except ValidationError as exc:
        return None, f"{what} is not valid: {exc.errors()[0]['msg']} at {'.'.join(str(x) for x in exc.errors()[0]['loc'])}"
    except ValueError as exc:
        return None, f"{what} is not valid JSON: {exc}"


def _manifest() -> tuple[Manifest | None, str | None]:
    return _parse(Manifest, st.session_state.manifest_text, "manifest")


def _approved_manifest() -> Manifest | None:
    m, _ = _manifest()
    if m is None or m.approval is None:
        return None
    return m


def _do_approve(by: str) -> None:
    m, err = _manifest()
    if m is None:
        st.session_state.flash_error = err or "no manifest"
        return
    try:
        approved = approve(m, st.session_state.template_bytes, by)
    except DocAgentError as exc:
        st.session_state.flash_error = str(exc)
        return
    st.session_state.manifest_text = approved.dump()
    st.session_state.plan_text = ""
    st.session_state.fill_result = None
    st.session_state.flash_error = ""


def _demo_available() -> bool:
    return (ROOT / "tests" / "fixtures" / "csra.py").exists()


def _load_demo() -> None:
    sys.path.insert(0, str(ROOT))
    from tests.fixtures import review
    from tests.fixtures.csra import build_template
    from tests.fixtures.kb import build_kb

    raw = build_template()
    st.session_state.template_bytes = raw
    st.session_state.template_name = "csra_template.docx"
    st.session_state.manifest_text = review.reviewed_manifest(raw).dump()
    st.session_state.record_text = review.record().model_dump_json(indent=2)
    st.session_state.library_text = review.library().model_dump_json(indent=2)
    st.session_state.kb_text = build_kb().model_dump_json(indent=2)
    st.session_state.plan_text = ""
    st.session_state.audit = None
    st.session_state.fill_result = None
    st.session_state.gate_result = None


def _inspect_rows(raw: bytes, name: str) -> tuple[list[dict], list[dict]]:
    doc = TemplateDoc(raw, name)
    paras, tables = [], []
    for part in doc.text_parts():
        pi = doc.part(part)
        for p in pi.paragraphs:
            pt = pi.text(p)
            text = norm(pt.text)
            if not text and not pt.boxes:
                continue
            level = next((lvl for h, lvl, _ in pi.headings if h is p), None)
            paras.append({"part": part, "style": f"H{level}" if level else "", "text": text[:120],
                          "checkboxes": len(pt.boxes)})
        for i, t in enumerate(pi.tables, 1):
            rows = pi.rows(t)
            header = [pi.cell_text(c) for c in rows[0].children_named("w:tc")] if rows else []
            tables.append({"part": part, "table": i, "rows": len(rows), "header": " | ".join(header)})
    return paras, tables


def _json_editor(key: str, label: str, height: int = 320) -> None:
    """The text area is bound to the session-state key, so programmatic updates must happen
    before it renders (earlier in the script, or in a button callback)."""
    st.text_area(label, key=key, height=height)


def _upload_json(key: str, label: str) -> None:
    up = st.file_uploader(label, type=["json"], key=f"up_{key}")
    if up is not None and st.button(f"Use uploaded {label.lower()}", key=f"btn_{key}"):
        st.session_state[key] = up.getvalue().decode("utf-8")
        st.rerun()


# --- sidebar ----------------------------------------------------------------------------------

def _sidebar() -> None:
    st.sidebar.title("docagent")
    st.sidebar.caption("AI authoring agent for regulated documents")
    if _demo_available() and st.sidebar.button("Load demo (CSRA template)", width="stretch"):
        _load_demo()
        st.rerun()
    if st.sidebar.button("Clear workspace", width="stretch"):
        for k, v in KEYS.items():
            st.session_state[k] = v
        st.rerun()

    st.sidebar.divider()
    st.sidebar.subheader("Status")
    m, _ = _manifest()
    rec, _ = _parse(SystemRecord, st.session_state.record_text, "record")
    kb, _ = _parse(KnowledgeBase, st.session_state.kb_text, "kb")
    lib, _ = _parse(BoilerplateLibrary, st.session_state.library_text, "library")
    plan, _ = _parse(FillPlan, st.session_state.plan_text, "plan")
    rows = [
        ("Template", st.session_state.template_bytes is not None, st.session_state.template_name),
        ("Manifest", m is not None, f"{len(m.regions)} regions" if m else ""),
        ("Approved", m is not None and m.approval is not None, m.approval.approved_by if m and m.approval else ""),
        ("System record", rec is not None, rec.record_id if rec else ""),
        ("Boilerplate library", lib is not None, f"{len(lib.entries)} entries" if lib else "optional"),
        ("Knowledge base", kb is not None, f"{len(kb.chunks)} chunks" if kb else "needed for drafting"),
        ("Fill plan", plan is not None, f"{len(plan.regions)} regions" if plan else "optional"),
        ("Filled output", st.session_state.fill_result is not None,
         "gate passed" if st.session_state.fill_result else ""),
    ]
    for name, ok, note in rows:
        st.sidebar.markdown(f"{'✅' if ok else '◻️'} **{name}**  <span style='color:gray'>{note}</span>",
                            unsafe_allow_html=True)


# --- tabs -------------------------------------------------------------------------------------

def tab_template() -> None:
    st.subheader("1 · Template")
    st.write("Upload the approved Word template. The engine never re-serialises it; "
             "it only splices into regions you approve in the next step.")
    up = st.file_uploader("Template (.docx)", type=["docx"])
    if up is not None and st.button("Use this template"):
        st.session_state.template_bytes = up.getvalue()
        st.session_state.template_name = up.name
        st.session_state.manifest_text = ""
        st.session_state.plan_text = ""
        st.session_state.fill_result = None
        st.rerun()

    raw = st.session_state.template_bytes
    if raw is None:
        st.info("No template loaded. Upload one, or load the demo from the sidebar.")
        return
    st.success(f"Loaded **{st.session_state.template_name}** ({len(raw):,} bytes)")
    st.download_button("Download template", raw, st.session_state.template_name,
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    try:
        paras, tables = _inspect_rows(raw, st.session_state.template_name)
    except DocAgentError as exc:
        st.error(f"The template cannot be used: {exc}")
        return
    st.markdown("**What the compiler sees**")
    c1, c2 = st.columns([3, 2])
    with c1:
        st.dataframe(paras, width="stretch", height=360)
    with c2:
        st.dataframe(tables, width="stretch", height=360)


def tab_regions() -> None:
    st.subheader("2 · Regions and approval")
    raw = st.session_state.template_bytes
    if raw is None:
        st.info("Load a template first.")
        return

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Propose regions from the template"):
            try:
                st.session_state.manifest_text = propose(TemplateDoc(raw, st.session_state.template_name)).dump()
                st.session_state.plan_text = ""
                st.session_state.fill_result = None
                st.rerun()
            except DocAgentError as exc:
                st.error(str(exc))
    with c2:
        _upload_json("manifest_text", "Manifest JSON")

    m, err = _manifest()
    if st.session_state.manifest_text and err:
        st.error(err)
    if m is not None:
        if m.warnings:
            for w in m.warnings:
                st.warning(w)
        rows = [{
            "id": r.id, "kind": r.kind.value, "mutation": r.mutation.value,
            "anchor": r.anchor.text or (" | ".join(r.anchor.table_header) if r.anchor.table_header else "") or "",
            "section": r.anchor.section or "", "source": r.source or "", "library_key": r.library_key or "",
            "needs_review": "; ".join(r.needs_review),
        } for r in m.regions]
        st.dataframe(rows, width="stretch")
        pending = sum(1 for r in m.regions if r.needs_review)
        if m.approval:
            st.success(f"Approved by **{m.approval.approved_by}** at {m.approval.approved_at}. "
                       f"Manifest SHA-256 `{m.approval.manifest_sha256[:16]}…`. Editing the JSON below "
                       "invalidates the approval; approve again after any change.")
        elif pending:
            st.warning(f"{pending} region(s) still need review. Set the kind, source or library key in the "
                       "JSON and clear each `needs_review` list, then approve.")

    with st.expander("Edit manifest JSON", expanded=m is None and bool(st.session_state.manifest_text)):
        _json_editor("manifest_text", "manifest.json", 420)

    st.markdown("**Approve**")
    c1, c2 = st.columns([3, 1])
    by = c1.text_input("Approved by (name and role)", placeholder="Template Owner (QA)")
    c2.button("Approve manifest", disabled=m is None or not by.strip(), on_click=_do_approve, args=(by.strip(),))
    if st.session_state.flash_error:
        st.error(st.session_state.flash_error)
    if m is not None:
        st.download_button("Download manifest.json", st.session_state.manifest_text, "manifest.json",
                           "application/json")


def tab_inputs() -> None:
    st.subheader("3 · Inputs")
    st.write("Facts come from the system record, boilerplate from the approved library, and evidence "
             "for the model from the knowledge base. None of these pass through the model unmasked.")
    t1, t2, t3 = st.tabs(["System record", "Boilerplate library", "Knowledge base"])
    with t1:
        _upload_json("record_text", "System record JSON")
        _json_editor("record_text", "record.json")
        rec, err = _parse(SystemRecord, st.session_state.record_text, "record")
        if st.session_state.record_text:
            if err:
                st.error(err)
            else:
                st.success(f"Record **{rec.record_id}** v{rec.record_version}, {len(rec.system)} fields")
    with t2:
        _upload_json("library_text", "Library JSON")
        _json_editor("library_text", "library.json")
        lib, err = _parse(BoilerplateLibrary, st.session_state.library_text, "library")
        if st.session_state.library_text:
            if err:
                st.error(err)
            else:
                st.success(f"{len(lib.entries)} approved entries")
    with t3:
        _upload_json("kb_text", "Knowledge base JSON")
        _json_editor("kb_text", "kb.json")
        kb, err = _parse(KnowledgeBase, st.session_state.kb_text, "kb")
        if st.session_state.kb_text:
            if err:
                st.error(err)
            else:
                rec, _ = _parse(SystemRecord, st.session_state.record_text, "record")
                usable = len(kb.for_system(rec.record_id)) if rec else 0
                st.success(f"{len(kb.chunks)} chunks; {usable} are approved evidence for the current record"
                           + ("" if rec else " (load a record to scope)"))


def tab_author() -> None:
    st.subheader("4 · Draft with the model")
    m = _approved_manifest()
    rec, rerr = _parse(SystemRecord, st.session_state.record_text, "record")
    kb, kerr = _parse(KnowledgeBase, st.session_state.kb_text, "kb")
    missing = [x for x, ok in [("an approved manifest", m), ("a system record", rec), ("a knowledge base", kb)] if not ok]
    if missing:
        st.info("Drafting needs " + ", ".join(missing) + ".")
    c1, c2, c3 = st.columns([2, 1, 1])
    model = c1.selectbox("Model", MODELS, index=0)
    samples = c2.number_input("Samples per single choice", 1, 7, 3)
    load_dotenv(ROOT / ".env")
    if c3.button("Draft plan", disabled=bool(missing), type="primary"):
        try:
            with st.spinner("Drafting, checking and judging every claim…"):
                result = author(m, rec, kb, AnthropicLLM(model=model), samples=int(samples))
            st.session_state.plan_text = result.plan.model_dump_json(indent=2)
            st.session_state.audit = result.audit
            st.session_state.fill_result = None
            st.rerun()
        except DocAgentError as exc:
            st.error(str(exc))

    audit = st.session_state.audit
    if audit:
        s = audit["summary"]
        a, b, c = st.columns(3)
        a.metric("Claims drafted", s["claims"])
        b.metric("Verified", s["accepted"])
        c.metric("Gaps declared by the model", s["declared_gaps"])
        rows = [{
            "region": r["region"], "kind": r["kind"],
            "verified": f"{sum(1 for c in r['claims'] if c['accepted'])}/{len(r['claims'])}",
            "calls": len(r["calls"]), "note": r["note"] or "",
        } for r in audit["regions"]]
        st.dataframe(rows, width="stretch")
        st.download_button("Download plan.audit.json", json.dumps(audit, indent=2), "plan.audit.json",
                           "application/json")

    st.markdown("**Fill plan** (drafted by the model, or written by hand)")
    _upload_json("plan_text", "Plan JSON")
    _json_editor("plan_text", "plan.json")
    plan, err = _parse(FillPlan, st.session_state.plan_text, "plan")
    if st.session_state.plan_text:
        if err:
            st.error(err)
        elif m and plan.manifest_sha256 != m.approval.manifest_sha256:
            st.error("This plan was made for a different manifest version; the writer will refuse it.")
        else:
            st.success(f"Plan covers {len(plan.regions)} region(s)")
            st.download_button("Download plan.json", st.session_state.plan_text, "plan.json", "application/json")


def tab_fill() -> None:
    st.subheader("5 · Fill and verify")
    raw = st.session_state.template_bytes
    m = _approved_manifest()
    rec, _ = _parse(SystemRecord, st.session_state.record_text, "record")
    plan, perr = _parse(FillPlan, st.session_state.plan_text, "plan")
    lib, lerr = _parse(BoilerplateLibrary, st.session_state.library_text, "library")
    missing = [x for x, ok in [("a template", raw), ("an approved manifest", m), ("a system record", rec)] if not ok]
    if missing:
        st.info("Filling needs " + ", ".join(missing) + ". A plan and a library are optional: without them "
                "every authored region becomes a gap or a blocking item.")
    if st.session_state.plan_text and perr:
        st.warning(perr)
    if st.session_state.library_text and lerr:
        st.warning(lerr)

    if st.button("Fill document", disabled=bool(missing), type="primary"):
        try:
            with st.spinner("Splicing regions and running the immutability gate…"):
                result = fill(raw, m, rec, plan, lib)
            st.session_state.fill_result = result
            st.rerun()
        except DocAgentError as exc:
            st.session_state.fill_result = None
            st.error(str(exc))

    res = st.session_state.fill_result
    if res is None:
        return
    r = res.report
    st.success("Immutability gate passed. Every byte outside the approved regions is identical to the template.")
    a, b, c, d = st.columns(4)
    a.metric("Regions", len(r["regions"]))
    b.metric("Gaps", len(r["gaps"]))
    c.metric("Blocking items", len(r["blocking"]))
    d.metric("Awaiting human", len(r["awaiting_human"]))
    out_name = Path(st.session_state.template_name).stem + "_filled.docx"
    c1, c2 = st.columns(2)
    c1.download_button("Download " + out_name, res.output, out_name,
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document", type="primary")
    c2.download_button("Download report.json", json.dumps(r, indent=2, ensure_ascii=False),
                       out_name.replace(".docx", ".report.json"), "application/json")

    st.markdown("**Regions**")
    st.dataframe([{"id": x["id"], "kind": x["kind"], "status": x["status"],
                   "gaps": ", ".join(f"GAP-{n}" for n in x["gaps"]), "blocking": "; ".join(x["blocking"])}
                  for x in r["regions"]], width="stretch")
    if r["gaps"]:
        st.markdown("**Gap markers** (SME input required)")
        st.dataframe(r["gaps"], width="stretch")
    if r["blocking"]:
        st.markdown("**Blocking items** (sign-off is blocked until decided)")
        st.dataframe(r["blocking"], width="stretch")
    with st.expander("Full report"):
        st.json(r)


def tab_gate() -> None:
    st.subheader("6 · Gate any output")
    st.write("Re-run the immutability gate on a document produced earlier, against the loaded template and "
             "approved manifest. This is what a reviewer runs before sign-off.")
    raw = st.session_state.template_bytes
    m = _approved_manifest()
    if raw is None or m is None:
        st.info("Load the template and an approved manifest first.")
        return
    up = st.file_uploader("Output (.docx)", type=["docx"], key="gate_docx")
    rep = st.file_uploader("Its fill report (optional, supplies the witness)", type=["json"], key="gate_report")
    if up is not None and st.button("Run gate"):
        witness = None
        if rep is not None:
            try:
                witness = json.loads(rep.getvalue()).get("witness")
            except ValueError:
                st.warning("The report is not valid JSON; running without a witness.")
        try:
            st.session_state.gate_result = run_gate(raw, up.getvalue(), m, witness).to_dict()
        except DocAgentError as exc:
            st.session_state.gate_result = {"passed": False, "failures": [str(exc)], "checks": {}}
    g = st.session_state.gate_result
    if g:
        if g["passed"]:
            st.success("Gate passed")
        else:
            st.error("Gate failed")
            for f in g["failures"]:
                st.write("- " + f)
        with st.expander("Checks"):
            st.json(g["checks"])


# --- page -------------------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="docagent", page_icon="📄", layout="wide")
    _init()
    _sidebar()
    st.title("Regulated document authoring")
    tabs = st.tabs(["1 Template", "2 Regions", "3 Inputs", "4 Draft", "5 Fill", "6 Gate"])
    with tabs[0]:
        tab_template()
    with tabs[1]:
        tab_regions()
    with tabs[2]:
        tab_inputs()
    with tabs[3]:
        tab_author()
    with tabs[4]:
        tab_fill()
    with tabs[5]:
        tab_gate()


main()
