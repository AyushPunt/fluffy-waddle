"""Grounded drafting, phase 3 first slice: evidence -> model draft -> claim checks -> fill plan.

For each model-authored region the drafter gets only the target system's chunks,
with system-record values masked as {{system.*}} tokens. It must return atomic
claims, each with a chunk ID and a verbatim quote. Every claim then has to pass
the deterministic checks in verify.py and a separate judge call. A claim that
fails any check becomes a {{GAP: ...}} token, a checklist item becomes unknown,
and a single choice needs three samples that agree. The output is an ordinary
fill plan for the writer, plus an audit record of IDs and hashes only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from docagent.errors import ManifestError
from docagent.kb import KnowledgeBase, mask_record_values
from docagent.llm import LLM, LLMResult
from docagent.manifest import Manifest, Region, RegionKind
from docagent.plan import FillPlan, SystemRecord
from docagent.verify import ClaimCheck, check_claim

DRAFT_RULES = """You draft one region of a regulated GxP validation deliverable, a Computer System Risk Assessment, \
for the target system {{system.id}}.

Rules:
- Use only the evidence chunks provided. They all describe the target system. Each chunk names its source \
document and that document's version; the target system is at version {{system.version}}. Where an older \
document is the only source, the claim must stay true for the current version as the quote states it, and the \
quote must not depend on the older version.
- Each claim is one short, atomic statement supported by a single chunk. Give that chunk's chunk_id and a quote \
copied character for character from it, long enough to support the claim on its own.
- Text such as {{system.name}} is a placeholder for a value from the system record. Copy placeholders exactly and \
never write the value itself.
- Do not add numbers, dates, names, versions or qualifiers that the quote does not contain, and do not generalise.
- Bracketed surrogates such as [SITE_1], [ORG_1] or [PERSON_2] stand for names removed during \
de-identification. Never copy them into a claim: leave that detail out, or return a gap if the region needs it.
- When the region needs something the evidence does not state, return a gap item whose topic names what is \
missing. A gap is always better than a guess.
- Write plain, formal English suitable for a validation document."""

JUDGE_RULES = """You check claims for a regulated GxP document. For each pair, decide whether the quote on its own \
fully supports every part of the claim. Answer entailed=false if the claim adds anything the quote does not state: \
a number, name, date, scope, frequency, certainty, or a different system or version. Each quote comes with the version of the document it was \
taken from and the system's current version; a claim that states something as current although the quote comes \
from an older version and is version-specific (a test result, a count, a configuration value) is not entailed. \
Placeholders such as {{system.name}} stand for the same value in claim and quote. Judge only the text given, \
without outside knowledge."""

_ITEM = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["claim", "gap"]},
        "topic": {"type": "string"},
        "text": {"type": "string"},
        "chunk_id": {"type": "string"},
        "quote": {"type": "string"},
    },
    "required": ["kind", "topic", "text", "chunk_id", "quote"],
    "additionalProperties": False,
}
_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "entailed": {"type": "boolean"}},
                "required": ["id", "entailed"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}
_ALLOWED_TOKEN = re.compile(r"\{\{\s*system(?:\.[a-z0-9_]+)+\s*\}\}")
_BAD_TEXT = re.compile(r"[\n\r\t☐☑☒\[\]]")
_SURROGATE = re.compile(r"\[[A-Z]+_\d+\]")
_PLANNED = (RegionKind.PROSE, RegionKind.TABLE_ROWS, RegionKind.SINGLE_CHOICE, RegionKind.MULTI_SELECT)


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _clean_topic(topic: str, fallback: str) -> str:
    topic = " ".join(re.sub(r"[\[\]{}]", " ", topic).split())[:150]
    return topic or fallback


def _usable_text(text: str) -> bool:
    """Claim text may hold plain words and {{system.*}} tokens only."""
    if not text.strip() or _BAD_TEXT.search(text):
        return False
    rest = _ALLOWED_TOKEN.sub("", text)
    return "{{" not in rest and "}}" not in rest


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class _Draft:
    ref: str
    topic: str
    text: str  # what goes into the document
    statement: str  # what the judge verifies
    chunk_id: str
    quote: str
    decision: str = ""  # checklist answer this draft supports
    check: ClaimCheck | None = None

    @property
    def accepted(self) -> bool:
        return self.check is not None and self.check.accepted


@dataclass
class AuthorResult:
    plan: FillPlan
    audit: dict[str, Any]
    drafts: list[tuple[str, _Draft]] = field(default_factory=list)  # for golden-set labelling, never persisted by the audit


@dataclass
class _RegionAudit:
    region: str
    kind: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)
    declared_gaps: int = 0
    note: str | None = None


class Author:
    def __init__(
        self,
        manifest: Manifest,
        record: SystemRecord,
        kb: KnowledgeBase,
        llm: LLM,
        judge: LLM | None = None,
        samples: int = 3,
    ):
        if manifest.approval is None or manifest.approval.manifest_sha256 != manifest.content_sha256():
            raise ManifestError("drafting needs an approved, unedited manifest")
        self.manifest = manifest
        self.record = record
        self.kb = kb
        self.llm = llm
        self.judge_llm = judge or llm
        self.samples = samples
        self.system_id = record.record_id
        self.drafts: list[tuple[str, _Draft]] = []  # kept in memory for eval labelling only
        self.evidence = kb.for_system(self.system_id)
        self.masked = {c.chunk_id: mask_record_values(c.text, record) for c in self.evidence}
        chunks = "\n".join(
            f'<chunk id="{c.chunk_id}" doc_type="{c.doc_type}" doc_version="{mask_record_values(c.doc_version, record)}" '
            f'title="{mask_record_values(c.doc_title, record)}" section="{mask_record_values(c.section, record)}">\n'
            f"{self.masked[c.chunk_id]}\n</chunk>"
            for c in self.evidence
        )
        self.system = [
            {"type": "text", "text": DRAFT_RULES},
            {"type": "text", "text": f"Evidence for {{{{system.id}}}}:\n{chunks}", "cache_control": {"type": "ephemeral"}},
        ]

    # --- public -------------------------------------------------------------------------------

    def run(self) -> AuthorResult:
        values: dict[str, dict[str, Any]] = {}
        audits: list[_RegionAudit] = []
        for region in self.manifest.regions:
            if region.kind not in _PLANNED:
                continue
            audit = _RegionAudit(region.id, region.kind.value)
            if not self.evidence:
                audit.note = "no evidence for the target system; region left for the writer to gap"
                audits.append(audit)
                continue
            value = {
                RegionKind.PROSE: self._prose,
                RegionKind.TABLE_ROWS: self._rows,
                RegionKind.MULTI_SELECT: self._multi,
                RegionKind.SINGLE_CHOICE: self._choice,
            }[region.kind](region, audit)
            if value is not None:
                values[region.id] = value
            audits.append(audit)
        plan = FillPlan(manifest_sha256=self.manifest.approval.manifest_sha256, regions=values)
        return AuthorResult(plan, self._audit(plan, audits), self.drafts)

    # --- model calls --------------------------------------------------------------------------

    def _call(self, audit: _RegionAudit, purpose: str, user: str, schema: dict, effort: str) -> LLMResult:
        result = self.llm.complete_json(self.system, user, schema, effort)
        audit.calls.append(result.meta(purpose))
        return result

    def _judge(self, audit: _RegionAudit, drafts: list[_Draft]) -> None:
        """Checks 1-3 in code, then one judge call for the drafts that passed them."""
        for d in drafts:
            d.check = check_claim(d.text, d.chunk_id, d.quote, self.system_id, self.kb, self.masked)
            d.check.text_allowed = _usable_text(d.text)
            if d.decision == "no":
                # A "no" is supported by evidence that states something else ("Category 3" for the item
                # "GAMP 5 Category 4"), so the item's own terms need not appear in the quote; the judge decides.
                d.check.facts_sourced = True
                d.check.unsourced = []
        pending = [d for d in drafts if d.check.deterministic_ok]
        if pending:
            pairs = "\n\n".join(
                f'<pair id="{d.ref}">\n<claim>{d.statement}</claim>\n'
                f'<quote doc_version="{self._version(d.chunk_id)}">{d.quote}</quote>\n</pair>'
                for d in pending
            )
            result = self.judge_llm.complete_json(
                [{"type": "text", "text": JUDGE_RULES}],
                f"The system is at version {{{{system.version}}}}. Check each pair.\n\n{pairs}",
                _JUDGE_SCHEMA,
                "medium",
            )
            audit.calls.append(result.meta("judge"))
            verdicts = {v["id"]: v["entailed"] for v in (result.data or {}).get("verdicts", [])}
            for d in pending:
                d.check.entailed = verdicts.get(d.ref) is True
        for d in drafts:
            audit.claims.append({"ref": d.ref, "quote_sha256": _sha(d.quote), **d.check.as_audit()})
            self.drafts.append((audit.region, d))

    def _version(self, chunk_id: str) -> str:
        chunk = self.kb.get(chunk_id)
        return mask_record_values(chunk.doc_version, self.record) if chunk else "unknown"

    # --- region kinds -------------------------------------------------------------------------

    def _label(self, region: Region) -> str:
        return region.hints.get("section") or region.id.replace("_", " ")

    def _render(self, d: _Draft) -> str:
        if d.accepted:
            return d.text.strip()
        why = "de-identified detail" if _SURROGATE.search(d.text) else "no verified evidence"
        return "{{GAP: " + _clean_topic(d.topic, "content") + f" ({why})}}}}"

    def _prose(self, region: Region, audit: _RegionAudit) -> dict | None:
        schema = _obj({"paragraphs": {"type": "array", "items": _obj({"items": {"type": "array", "items": _ITEM}})}})
        user = (
            f"Region: {region.id}\nSection: {self._label(region)}\n"
            f"Template instruction: {region.anchor.text}\n\n"
            "Draft this section as one to four short paragraphs. Each paragraph is a list of items. A claim item "
            "has kind=claim, a short topic, the sentence as text, chunk_id and quote. A gap item has kind=gap, the "
            "missing information as topic, and empty text, chunk_id and quote."
        )
        result = self._call(audit, "draft", user, schema, "high")
        if result.data is None:
            audit.note = "model refused; region left for the writer to gap"
            return None
        layout: list[list[_Draft | str]] = []
        drafts: list[_Draft] = []
        for i, para in enumerate(result.data.get("paragraphs", []), 1):
            row: list[_Draft | str] = []
            for j, item in enumerate(para.get("items", []), 1):
                if item["kind"] == "gap":
                    audit.declared_gaps += 1
                    row.append("{{GAP: " + _clean_topic(item["topic"], self._label(region)) + "}}")
                    continue
                d = _Draft(f"p{i}.{j}", item["topic"], item["text"], item["text"], item["chunk_id"], item["quote"])
                drafts.append(d)
                row.append(d)
            layout.append(row)
        self._judge(audit, drafts)
        paragraphs = [" ".join(x if isinstance(x, str) else self._render(x) for x in row) for row in layout if row]
        return {"paragraphs": paragraphs} if paragraphs else None

    def _rows(self, region: Region, audit: _RegionAudit) -> dict | None:
        headers = dict(zip([c.name for c in region.columns], region.anchor.table_header))
        planned = [c.name for c in region.columns if c.source == "plan"]
        schema = _obj({"rows": {"type": "array", "items": _obj({"cells": _obj({name: _ITEM for name in planned})})}})
        cols = "\n".join(f"- {name}: column '{headers[name]}'" for name in planned)
        user = (
            f"Region: {region.id}\nTable in section: {self._label(region)}\nColumns:\n{cols}\n\n"
            "Return one row per requirement stated in the evidence, in evidence order. Every cell is a claim item "
            "(short cell text, chunk_id, quote) or a gap item (missing information as topic, other fields empty). "
            "Row numbers are added by code; do not write them."
        )
        result = self._call(audit, "draft", user, schema, "high")
        if result.data is None:
            audit.note = "model refused; region left for the writer to gap"
            return None
        drafts: list[_Draft] = []
        rows: list[dict[str, _Draft | str]] = []
        first = planned[0]
        for i, row in enumerate(result.data.get("rows", []), 1):
            cells: dict[str, _Draft | str] = {}
            anchor_text = row["cells"][first]["text"] or row["cells"][first]["topic"]
            for name in planned:
                item = row["cells"][name]
                if item["kind"] == "gap":
                    audit.declared_gaps += 1
                    cells[name] = "{{GAP: " + _clean_topic(item["topic"], f"{headers[name]} for row {i}") + "}}"
                    continue
                statement = item["text"] if name == first else (
                    f"For the requirement '{anchor_text}', the {headers[name]} is: {item['text']}"
                )
                d = _Draft(f"r{i}.{name}", item["topic"] or f"{headers[name]} for row {i}", item["text"], statement,
                           item["chunk_id"], item["quote"])
                drafts.append(d)
                cells[name] = d
            rows.append(cells)
        self._judge(audit, drafts)
        out = [{k: v if isinstance(v, str) else self._render(v) for k, v in row.items()} for row in rows]
        return {"rows": out} if out else None

    def _multi(self, region: Region, audit: _RegionAudit) -> dict | None:
        a = region.anchor
        items = a.options if a.layout == "single_box" else a.items
        schema = _obj({"items": {"type": "array", "items": _obj({
            "item": {"type": "string", "enum": items},
            "decision": {"type": "string", "enum": ["yes", "no", "unknown"]},
            "chunk_id": {"type": "string"},
            "quote": {"type": "string"},
        })}})
        listing = "\n".join(f"- {x}" for x in items)
        user = (
            f"Region: {region.id}\nChecklist in section: {self._label(region)}\nItems:\n{listing}\n\n"
            "For every item decide whether it holds for the target system: yes, no, or unknown. A yes or no needs "
            "the chunk_id and a verbatim quote that states it. Use unknown, with empty chunk_id and quote, whenever "
            "the evidence does not say. Never infer an answer from silence."
        )
        result = self._call(audit, "draft", user, schema, "high")
        if result.data is None:
            audit.note = "model refused; every item stays unknown"
            return {"items": {}}
        drafts: list[_Draft] = []
        decisions: dict[str, str] = {}
        for entry in result.data.get("items", []):
            item, decision = entry["item"], entry["decision"]
            if item not in items or item in decisions:
                continue
            if decision == "unknown":
                decisions[item] = "unknown"
                continue
            verb = "holds" if decision == "yes" else "does not hold"
            d = _Draft(f"item:{item}", item, item, f"The following {verb} for the system: {item}",
                       entry["chunk_id"], entry["quote"], decision)
            drafts.append(d)
        self._judge(audit, drafts)
        for d in drafts:
            decisions[d.topic] = d.decision if d.accepted else "unknown"
        return {"items": {x: decisions.get(x, "unknown") for x in items}}

    def _choice(self, region: Region, audit: _RegionAudit) -> dict | None:
        options = region.anchor.options
        schema = _obj({
            "choice": {"type": "string", "enum": [*options, "unknown"]},
            "chunk_id": {"type": "string"},
            "quote": {"type": "string"},
        })
        user = (
            f"Region: {region.id}\nSingle choice in section: {self._label(region)}\n"
            f"Options from the template: {', '.join(options)}\n\n"
            "Choose the option the evidence states for the target system and give the chunk_id and a verbatim "
            "quote. Answer unknown, with empty chunk_id and quote, if the evidence does not determine it."
        )
        picks = []
        for n in range(1, self.samples + 1):
            result = self._call(audit, f"sample {n}", user, schema, "high")
            picks.append(result.data)
        choices = {p["choice"] if p else "unknown" for p in picks}
        if choices == {"unknown"}:
            audit.note = "every sample answered unknown: the evidence does not state it"
            return {"choice": None}
        if len(choices) != 1 or "unknown" in choices:
            audit.note = f"samples disagreed: {', '.join(sorted(choices))}"
            return {"choice": None}
        choice = choices.pop()
        first = picks[0]
        d = _Draft("choice", self._label(region), choice, f"The {self._label(region)} for the system is {choice}.",
                   first["chunk_id"], first["quote"])
        self._judge(audit, [d])
        if not d.accepted:
            audit.note = "samples agreed but the citation failed verification"
        return {"choice": choice if d.accepted else None}

    # --- audit --------------------------------------------------------------------------------

    def _audit(self, plan: FillPlan, regions: list[_RegionAudit]) -> dict[str, Any]:
        claims = [c for r in regions for c in r.claims]
        return {
            "audit_version": 1,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "system_id": self.system_id,
            "record_sha256": self.record.sha256(),
            "manifest_sha256": self.manifest.approval.manifest_sha256,
            "kb_sha256": self.kb.sha256(),
            "evidence_chunk_ids": [c.chunk_id for c in self.evidence],
            "excluded_chunk_ids": self.kb.excluded_for(self.system_id),
            "draft_model": self.llm.model,
            "judge_model": self.judge_llm.model,
            "plan_sha256": plan.sha256(),
            "summary": {
                "claims": len(claims),
                "accepted": sum(1 for c in claims if c["accepted"]),
                "declared_gaps": sum(r.declared_gaps for r in regions),
            },
            "regions": [r.__dict__ for r in regions],
        }


def author(
    manifest: Manifest, record: SystemRecord, kb: KnowledgeBase, llm: LLM, judge: LLM | None = None, samples: int = 3
) -> AuthorResult:
    return Author(manifest, record, kb, llm, judge, samples).run()
