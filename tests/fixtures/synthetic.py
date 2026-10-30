"""Synthetic golden set with traps and an answer key, for local stress testing without customer data.

Every case is one fictional GxP system with approved documents (URS, risk assessment,
validation summary, OQ summary) and traps the pipeline must not fall for:

  * a chunk from a different system stating tempting facts (user count, backup checks)
  * a draft risk assessment that states the overall risk level
  * a superseded validation summary with a different GAMP category and risk level
  * de-identification surrogates such as [SITE_3]
  * ratings that exist only in the trap documents

`auto_label` grades exported claims against the answer key. Structured regions are graded
exactly. Prose is graded by a conservative heuristic (any trap token, or any number that
is not in the approved evidence, is an error), so prose numbers are a lower bound on what
SMEs would accept, never an upper bound.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from docagent.eval import GoldenCase, GoldenSet, Label
from docagent.kb import Chunk, KnowledgeBase
from docagent.plan import SystemRecord
from docagent.verify import normalise

SYSTEMS = [
    ("AssayHub LIMS", "QC laboratory", "sample login, result entry and result review"),
    ("BatchMaster MES", "manufacturing floor", "electronic batch records and dispensing"),
    ("ColdChain Monitor", "warehouse", "temperature monitoring of storage units"),
    ("DocVault eDMS", "quality unit", "controlled document lifecycle and training records"),
    ("DeviationTrack QMS", "quality unit", "deviation, CAPA and change control records"),
    ("StabilityStudio", "stability laboratory", "stability study scheduling and pull dates"),
    ("LabelPrint Pro", "packaging line", "label artwork approval and print release"),
    ("EnviroLog EMS", "cleanroom operations", "environmental monitoring results"),
    ("CalTrack CMMS", "engineering", "calibration and maintenance records"),
    ("TrialSupply IRT", "clinical supply", "randomisation and drug supply allocation"),
    ("ReleaseGate ERP", "supply chain", "material status and batch release transactions"),
    ("SpecLink CDS", "analytical laboratory", "chromatography acquisition and processing"),
    ("CleanValid CIP", "utilities", "clean-in-place cycle records"),
    ("TrainingHub LMS", "human resources", "GxP training assignments and completions"),
    ("AuditTrail Analyzer", "data integrity office", "periodic audit trail review"),
]

REQUIREMENTS = [
    ("The system shall record a secure, time-stamped audit trail of every creation, modification and deletion "
     "of a GxP record.", "Direct", "High"),
    ("Record approval shall use an electronic signature showing the signer's printed name, the date and time "
     "of signing and the meaning of the signature.", "Direct", "High"),
    ("The system shall back up its database every day.", "Indirect", "Medium"),
    ("User access shall be role based, and privileged roles shall be limited to named administrators.",
     "Direct", "Medium"),
    ("The system shall enforce unique user identities and shall not allow shared accounts.", "Direct", "High"),
    ("Reports used for batch disposition shall be generated from the validated database view only.",
     "Direct", "High"),
    ("The system shall retain records for the retention period defined in the records retention schedule.",
     "Direct", "Medium"),
    ("Date and time stamps shall be taken from a synchronised network time source.", "Indirect", "Low"),
    ("The system shall alert the process owner when a scheduled task is overdue.", "Indirect", "Low"),
    ("Configuration changes shall be made only through the change control process.", "Indirect", "Medium"),
]

RISKS = ["Low", "Medium", "High"]


@dataclass
class Truth:
    requirements: list[tuple[str, str | None, str | None]]  # text, impact or None (gap), risk or None
    gxp: dict[str, str]  # item -> yes/no/unknown
    controls: dict[str, str]
    overall_risk: str | None
    approved_text: str  # concatenated approved evidence for the target
    poison: set[str] = field(default_factory=set)  # tokens that only appear in trap chunks


@dataclass
class SynthCase:
    case: GoldenCase
    truth: Truth


def _chunk(cid, system_id, doc_type, title, version, section, text, status="approved") -> Chunk:
    return Chunk(chunk_id=cid, system_id=system_id, doc_type=doc_type, doc_title=title, doc_version=version,
                 status=status, section=section, text=text, deidentified=True)


def make_case(i: int, rng: random.Random) -> SynthCase:
    name, dept, purpose = SYSTEMS[i % len(SYSTEMS)]
    sid = f"SYS-{1000 + i:04d}"
    version = f"{rng.randint(2, 7)}.{rng.randint(0, 9)}.{rng.randint(0, 9)}"
    old_version = f"{int(version[0]) - 1}.{rng.randint(0, 9)}"
    other_name, _, other_purpose = SYSTEMS[(i + 7) % len(SYSTEMS)]
    other_sid = f"SYS-{2000 + i:04d}"
    site = f"[SITE_{rng.randint(1, 99)}]"
    org = f"[ORG_{rng.randint(1, 99)}]"
    person = f"[PERSON_{rng.randint(1, 99)}]"
    chunks: list[Chunk] = []
    approved: list[str] = []

    def add(cid, doc_type, title, ver, section, text, status="approved"):
        chunks.append(_chunk(f"{cid}-{i}", sid, doc_type, title, ver, section, text, status))
        if status == "approved":
            approved.append(text)

    urs = f"User Requirements Specification, {name} v{version[0]}"
    add("urs-purpose", "URS", urs, f"{version[0]}.0", "1 Purpose",
        f"{name} manages {purpose} for the {dept} at {site}. It replaces paper records for that process.")
    users = rng.choice([
        f"The system is used by {dept} staff, who enter data, and by reviewers, who review and approve it. "
        f"{org} IT administers the application servers.",
        f"Operators of the {dept} record data in the system and supervisors approve it. {org} IT hosts the servers.",
    ])
    add("urs-users", "URS", urs, f"{version[0]}.0", "2 Users", users)

    reqs = rng.sample(REQUIREMENTS, rng.randint(3, 5))
    truth_reqs = []
    rated = set(rng.sample(range(len(reqs)), max(1, len(reqs) - 2)))  # some ratings exist only in trap docs
    for n, (text, impact, risk) in enumerate(reqs, 1):
        add(f"urs-req{n}", "URS", urs, f"{version[0]}.0", "4 Requirements", f"URS-{n:02d}: {text}")
    ra = f"Computer System Risk Assessment, {name} v{old_version}"
    ra_lines, draft_lines, old_lines = [], [], []
    for n, (text, impact, risk) in enumerate(reqs):
        short = text.split(" shall ")[0].replace("The system", "System").strip()
        if n in rated:
            ra_lines.append(f"URS-{n + 1:02d} ({short}): GxP impact {impact}, risk {risk}.")
            truth_reqs.append((text, impact, risk))
        else:
            other_risk = rng.choice([r for r in RISKS if r != risk])
            draft_lines.append(f"URS-{n + 1:02d} ({short}): GxP impact {impact}, risk {other_risk}.")
            truth_reqs.append((text, None, None))
    add("ra-reqs", "Risk Assessment", ra, old_version, "3 Requirements",
        " ".join(ra_lines) + f" Assessment approved by {person}, QA.")

    part11 = rng.random() < 0.8
    cat = rng.choice([3, 4, 4, 5])
    alcoa_stated = rng.random() < 0.5
    old_cat = rng.choice([c for c in (3, 4, 5) if c != cat])
    overall = rng.choice(RISKS + [None])
    vsr = f"Validation Summary Report, {name} v{old_version}"
    scope = (
        (f"The system creates and maintains electronic records and electronic signatures, so 21 CFR Part 11 and "
         f"EU GMP Annex 11 apply. " if part11 else
         f"The system holds GxP records that are subject to EU GMP Annex 11; 21 CFR Part 11 does not apply because "
         f"no product is supplied to the US market. ")
        + f"{name} is classified as GAMP 5 Category {cat}."
        + (" Data integrity controls follow the ALCOA+ principles." if alcoa_stated else "")
        + (f" The overall risk level of the system is {overall}." if overall else "")
    )
    add("vsr-scope", "Validation Summary Report", vsr, old_version, "2 Regulatory scope", scope)
    add("vsr-old", "Validation Summary Report", f"Validation Summary Report, {name} v1.0", "1.0",
        "2 Regulatory scope",
        f"{name} is classified as GAMP 5 Category {old_cat}. The overall risk level of the system is "
        f"{rng.choice([r for r in RISKS if r != overall])}.", status="superseded")

    audit_on = rng.random() < 0.85
    esig = rng.random() < 0.7
    backups = rng.choice(["yes", "no", "unknown"])
    oq = f"Operational Qualification Summary, {name} {version}"
    results = (
        (f"Test OQ-07 verified that the audit trail is enabled and records the old value, new value, user and time "
         f"stamp of each change. " if audit_on else
         "Test OQ-07 found the audit trail disabled in the production configuration; a deviation was raised. ")
        + ("Electronic signatures are used for record approval and were tested in OQ-09." if esig else
           "Electronic signatures are not used; approvals are recorded on paper.")
    )
    add("oq-results", "OQ Summary", oq, version, "5 Results", results)
    backup_text = {
        "yes": "Backup and restore were verified in OQ-12; a restore of the previous day's backup completed successfully.",
        "no": "Backup restore verification has not been performed for this release. It is planned before go-live.",
        "unknown": "Open items: none recorded for this release.",
    }[backups]
    add("oq-open", "OQ Summary", oq, version, "6 Open items", backup_text)

    # traps
    user_count = rng.randint(12, 240)
    chunks.append(_chunk(f"other-{i}", other_sid, "Validation Summary Report",
                         f"Validation Summary Report, {other_name}", "2.3", "2 System description",
                         f"{other_name} manages {other_purpose} and is used by {user_count} named users at {site}. "
                         f"Backups of {other_name} are verified monthly by {org} IT."))
    draft_overall = overall or rng.choice(RISKS)
    add("ra-draft", "Risk Assessment", f"Computer System Risk Assessment, {name} {version} (DRAFT)",
        f"{version} draft A", "6 Overall Risk",
        f"The overall risk level of {name} {version} is {draft_overall}. " + " ".join(draft_lines), status="draft")

    record = SystemRecord(record_id=sid, record_version="1",
                          system={"name": name, "id": sid, "version": version, "document_version": "1.0"})
    truth = Truth(
        requirements=truth_reqs,
        gxp={"21 CFR Part 11": "yes" if part11 else "no", "EU GMP Annex 11": "yes",
             "GAMP 5 Category 4": "yes" if cat == 4 else "no",
             "Data integrity (ALCOA+)": "yes" if alcoa_stated else "unknown"},
        controls={"Audit trail enabled": "yes" if audit_on else "no",
                  "Electronic signatures used": "yes" if esig else "no",
                  "Backups verified": backups},
        overall_risk=overall,
        approved_text=normalise(" ".join(approved)),
        poison={str(user_count), "monthly", other_name.lower(), f"category {old_cat}"},
    )
    case = GoldenCase(case_id=f"synth-{i:02d}-{sid}", split="tune" if i % 3 else "test",
                      record=record, kb=KnowledgeBase(chunks=chunks))
    return SynthCase(case, truth)


def build(n: int = 15, seed: int = 20261029) -> list[SynthCase]:
    rng = random.Random(seed)
    return [make_case(i, rng) for i in range(n)]


def golden_set(cases: list[SynthCase]) -> GoldenSet:
    return GoldenSet(name="synthetic-stress", cases=[c.case for c in cases])


# --- answer key -------------------------------------------------------------------------------------

_NUM = re.compile(r"\d+(?:\.\d+)*")


def _req_index(truth: Truth, text: str) -> int | None:
    t = normalise(re.sub(r"^URS-\d+:\s*", "", text.strip()))
    for k, (req, _i, _r) in enumerate(truth.requirements):
        if normalise(req) == t:
            return k
    return None


def auto_label(labels: list[Label], truths: dict[str, Truth]) -> list[Label]:
    by_row: dict[tuple[str, str], str] = {}
    for x in labels:
        if x.region == "requirements_rows" and x.ref.endswith(".requirement"):
            by_row[(x.case_id, x.ref.split(".")[0])] = x.text
    out = []
    for x in labels:
        truth = truths[x.case_id]
        ok: bool
        note = ""
        if x.kind == "multi_select":
            key = truth.gxp if x.region == "gxp_applicability" else truth.controls
            ok = key.get(x.text) == x.decision
            note = f"truth={key.get(x.text)}"
        elif x.kind == "single_choice":
            ok = truth.overall_risk is not None and x.text == truth.overall_risk
            note = f"truth={truth.overall_risk}"
        elif x.kind == "table_rows":
            row = x.ref.split(".")[0]
            col = x.ref.split(".")[1]
            idx = _req_index(truth, by_row.get((x.case_id, row), "") if col != "requirement" else x.text)
            if idx is None:
                ok, note = False, "requirement text not in the URS"
            elif col == "requirement":
                ok = True
            else:
                expected = truth.requirements[idx][1 if col == "gxp_impact" else 2]
                ok = expected is not None and x.text == expected
                note = f"truth={expected or 'gap'}"
        else:  # prose: heuristic lower bound
            low = normalise(x.text)
            bad = [p for p in truth.poison if p in low]
            bad += [n for n in _NUM.findall(x.text) if normalise(n) not in truth.approved_text]
            ok = not bad
            note = "heuristic" + (f"; unsupported: {bad}" if bad else "")
        out.append(x.model_copy(update={"label": ok, "note": note}))
    return out
