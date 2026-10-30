from docagent.eval import Label, evaluate
from tests.fixtures.synthetic import auto_label, build, golden_set


def test_generator_is_deterministic_and_has_traps():
    a, b = build(5), build(5)
    assert golden_set(a).model_dump() == golden_set(b).model_dump()
    case = a[0]
    statuses = {c.status for c in case.case.kb.chunks}
    assert statuses == {"approved", "superseded", "draft"}
    assert any(c.system_id != case.case.record.record_id for c in case.case.kb.chunks)
    assert {c.split for c in golden_set(a).cases} == {"tune", "test"}


def _lab(case, region, kind, ref, text, decision="", accepted=True):
    return Label(case_id=case.case.case_id, split=case.case.split, region=region, kind=kind, ref=ref, text=text,
                 chunk_id="c", quote="q", doc_version="1", accepted=accepted, decision=decision)


def test_auto_label_grades_against_the_answer_key():
    case = build(1)[0]
    t = case.truth
    req_text, impact, risk = next(r for r in t.requirements if r[1] is not None)
    gap_req = next((r for r in t.requirements if r[1] is None), None)
    labels = [
        _lab(case, "requirements_rows", "table_rows", "r1.requirement", f"URS-01: {req_text}"),
        _lab(case, "requirements_rows", "table_rows", "r1.risk", risk),
        _lab(case, "requirements_rows", "table_rows", "r1.gxp_impact", "Low"),
        _lab(case, "gxp_applicability", "multi_select", "item:EU GMP Annex 11", "EU GMP Annex 11", decision="yes"),
        _lab(case, "data_integrity_controls", "multi_select", "item:Backups verified", "Backups verified",
             decision="yes" if t.controls["Backups verified"] != "yes" else "no"),
        _lab(case, "overall_risk", "single_choice", "choice", "High"),
        _lab(case, "system_description", "prose", "p1.1", f"It is used by {max(t.poison, key=len)} people."),
        _lab(case, "system_description", "prose", "p1.2", "{{system.name}} replaces paper records for that process."),
    ]
    if gap_req:
        labels.append(_lab(case, "requirements_rows", "table_rows", "r2.requirement", gap_req[0]))
        labels.append(_lab(case, "requirements_rows", "table_rows", "r2.risk", "Medium"))
    graded = {x.ref: x.label for x in auto_label(labels, {case.case.case_id: t})}
    assert graded["r1.requirement"] and graded["r1.risk"] and not graded["r1.gxp_impact"]
    assert graded["item:EU GMP Annex 11"] and not graded["item:Backups verified"]
    assert graded["choice"] == (t.overall_risk == "High")
    assert not graded["p1.1"] and graded["p1.2"]
    if gap_req:
        assert graded["r2.requirement"] and not graded["r2.risk"]
    ev = evaluate(auto_label(labels, {case.case.case_id: t}), case.case.split)
    assert ev.overall.unlabelled == 0 and ev.errors
