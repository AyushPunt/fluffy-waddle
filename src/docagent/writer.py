"""Byte-splicing writer: fills approved regions and refuses any output the gate rejects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from docagent import render
from docagent.compiler import InlineHit, Resolved, load_approved
from docagent.errors import GateError, PlanError
from docagent.gate import merge_spans, run_gate
from docagent.manifest import Manifest, Mutation, RegionKind
from docagent.ooxml.package import sha256
from docagent.ooxml.text import check_text
from docagent.ooxml.wordml import Checkbox
from docagent.plan import (
    BoilerplateLibrary,
    ChoiceValue,
    FillPlan,
    Gaps,
    MultiValue,
    ProseValue,
    RowsValue,
    Seg,
    SystemRecord,
    expand,
    plain,
)

# Region kinds whose content may come from a fill plan (that is, from a model).
_PLANNED = {RegionKind.PROSE, RegionKind.TABLE_ROWS, RegionKind.SINGLE_CHOICE, RegionKind.MULTI_SELECT}
_VALUE_MODELS = {
    RegionKind.PROSE: ProseValue,
    RegionKind.TABLE_ROWS: RowsValue,
    RegionKind.SINGLE_CHOICE: ChoiceValue,
    RegionKind.MULTI_SELECT: MultiValue,
}

Span = tuple[int, int]


@dataclass
class FillResult:
    output: bytes
    report: dict[str, Any]


@dataclass
class _RegionOutcome:
    edits: dict[Span, bytes] = field(default_factory=dict)
    gaps_before: int = 0
    blocking: list[str] = field(default_factory=list)
    status: str = "filled"


class _Filler:
    def __init__(self, record: SystemRecord, plan: FillPlan | None, library: BoilerplateLibrary | None):
        self.record = record
        self.plan = plan
        self.library = library
        self.gaps = Gaps()
        self.library_used: dict[str, str] = {}

    def value(self, res: Resolved):
        r = res.region
        raw = self.plan.regions.get(r.id) if self.plan else None
        if raw is None:
            return None
        try:
            return _VALUE_MODELS[r.kind].model_validate(raw)
        except ValidationError as exc:
            raise PlanError(f"region {r.id}: plan value does not match the {r.kind} schema: {exc}") from None

    def label(self, res: Resolved) -> str:
        return res.region.hints.get("section") or res.region.id.replace("_", " ")

    # --- per mutation ------------------------------------------------------------------------

    def fill(self, res: Resolved) -> _RegionOutcome:
        r = res.region
        out = _RegionOutcome()
        if r.kind == RegionKind.HUMAN_ONLY:
            out.status = "awaiting_human"
            return out
        if r.mutation == Mutation.INLINE_TEXT:
            segs = self._inline_segments(res, out)
            for hit in res.inline:
                out.edits.update(_inline_edits(res.part.data, hit, segs))
        elif r.mutation == Mutation.PARAGRAPHS:
            out.edits[res.spans[0]] = self._paragraphs(res)
        elif r.mutation == Mutation.ROWS:
            out.edits[res.spans[0]] = self._rows(res)
        elif r.mutation == Mutation.CHECKBOX_STATE:
            self._checkboxes(res, out)
        return out

    def _inline_segments(self, res: Resolved, out: _RegionOutcome) -> list[Seg]:
        r = res.region
        if r.kind == RegionKind.FACT:
            value = self.record.lookup(r.source)
            if value is None:
                return [self.gaps.add(r.id, f"{r.source} is not in the system record")]
            return expand_literal(value, r.id)
        if r.kind == RegionKind.BOILERPLATE:
            paragraphs = self._library(res)
            if len(paragraphs) != 1:
                raise PlanError(f"region {r.id}: inline boilerplate must be a single paragraph")
            return expand(paragraphs[0], self.record, self.gaps, r.id, allow_gaps=False)
        if r.kind == RegionKind.SINGLE_CHOICE:
            value = self.value(res)
            if value is None or value.choice is None:
                return [self.gaps.add(r.id, f"choose one of: {', '.join(r.anchor.options)}")]
            if value.choice not in r.anchor.options:
                raise PlanError(f"region {r.id}: {value.choice!r} is not in the template's option set")
            return plain(value.choice)
        value = self.value(res)  # inline prose
        if value is None:
            return [self.gaps.add(r.id, f"{self.label(res)} has not been drafted")]
        if len(value.paragraphs) != 1:
            raise PlanError(f"region {r.id}: inline prose takes exactly one paragraph")
        return self._text(value.paragraphs[0], r.id)

    def _text(self, text: str, region_id: str) -> list[Seg]:
        if not text.strip():
            raise PlanError(f"region {region_id}: empty text; use a {{{{GAP: ...}}}} token instead")
        return expand(text, self.record, self.gaps, region_id)

    def _library(self, res: Resolved) -> list[str]:
        r = res.region
        if self.library is None:
            raise PlanError(f"region {r.id}: a boilerplate library is required")
        paragraphs = self.library.text(r.library_key, r.library_sha256)
        self.library_used[r.library_key] = r.library_sha256
        return paragraphs

    def _paragraphs(self, res: Resolved) -> bytes:
        r, hit, data = res.region, res.paragraph, res.part.data
        if r.kind == RegionKind.BOILERPLATE:
            paras = [expand(t, self.record, self.gaps, r.id, allow_gaps=False) for t in self._library(res)]
        else:
            value = self.value(res)
            if value is None or not value.paragraphs:
                paras = [[self.gaps.add(r.id, f"{self.label(res)} has not been drafted")]]
            else:
                paras = [self._text(t, r.id) for t in value.paragraphs]
        rpr = hit.first_rpr if r.run_props == "inherit" else None
        last = len(paras) - 1
        return b"".join(
            render.paragraph(hit.ppr if i == last else hit.ppr_without_sect, render.runs(segs, data, rpr))
            for i, segs in enumerate(paras)
        )

    def _rows(self, res: Resolved) -> bytes:
        r, hit, data = res.region, res.rows, res.part.data
        planned = [c.name for c in r.columns if c.source == "plan"]
        value = self.value(res)
        if value is None or not value.rows:
            first = planned[0] if planned else None
            gap = self.gaps.add(r.id, f"no rows drafted for {self.label(res)}")
            cells = [
                render.cell(proto, data, [[gap]] if col.name == first else [])
                for col, proto in zip(r.columns, hit.cells)
            ]
            return render.row(hit, cells)
        out = b""
        for i, row in enumerate(value.rows):
            if set(row) != set(planned):
                raise PlanError(f"region {r.id}: row {i + 1} must have exactly the columns {planned}")
            cells = []
            for col, proto in zip(r.columns, hit.cells):
                text = col.format.format(n=col.start + i) if col.source == "row_number" else row[col.name]
                if proto.merged_continue:
                    if text:
                        raise PlanError(f"region {r.id}: column {col.name} continues a vertical merge; leave it empty")
                    cells.append(render.cell(proto, data, []))
                    continue
                lines = text.split("\n") if text else []
                if col.source == "row_number":
                    paragraphs = [expand_literal(line, r.id) for line in lines]
                else:
                    paragraphs = [self._text(line, r.id) for line in lines]
                cells.append(render.cell(proto, data, paragraphs))
            out += render.row(hit, cells)
        return out

    def _checkboxes(self, res: Resolved, out: _RegionOutcome) -> None:
        r, choice = res.region, res.choice
        states: list[tuple[Checkbox, bool | None]] = []
        if r.kind == RegionKind.SINGLE_CHOICE:
            value = self.value(res)
            picked = value.choice if value else None
            if picked is not None and picked not in choice.boxes:
                raise PlanError(f"region {r.id}: {picked!r} is not in the template's option set")
            if picked is None:
                out.blocking.append("no option chosen")
            states = [(box, None if picked is None else opt == picked) for opt, box in choice.boxes.items()]
        else:
            value = self.value(res)
            decisions = value.items if value else {}
            known = choice.boxes.keys() if r.anchor.layout == "single_box" else choice.pairs.keys()
            stray = sorted(set(decisions) - set(known))
            if stray:
                raise PlanError(f"region {r.id}: items not in the template: {stray}")
            for item in known:
                decision = decisions.get(item, "unknown")
                if decision == "unknown":
                    out.blocking.append(f"{item}: unknown")
                if r.anchor.layout == "single_box":
                    states.append((choice.boxes[item], None if decision == "unknown" else decision == "yes"))
                else:
                    yes, no = choice.pairs[item]
                    tick = None if decision == "unknown" else decision == "yes"
                    states += [(yes, tick), (no, None if tick is None else not tick)]
        data = res.part.data
        for box, state in states:
            for span, role in zip(box.spans, box.roles):
                original = data[span[0] : span[1]]
                if state is None:
                    out.edits[span] = original
                elif role == "cb_tag":
                    out.edits[span] = render.set_state_tag(original, state)
                else:
                    glyph = (box.checked_glyph if state else box.unchecked_glyph) if box.kind == "sdt" else (
                        "☒" if state else "☐"
                    )
                    out.edits[span] = glyph.encode("utf-8")


def expand_literal(value: str, region_id: str) -> list[Seg]:
    """System-record and computed values are copied literally; braces are not tokens here."""
    check_text(value, f"region {region_id}")
    return plain(value)


def _inline_edits(data: bytes, hit: InlineHit, segs: list[Seg]) -> dict[Span, bytes]:
    """New text goes where the placeholder's first character was; the rest of the
    placeholder is removed from the following runs, which keep their properties.
    Every touched w:t gets xml:space="preserve" so Word keeps spaces at its edges."""
    edits: dict[Span, bytes] = {}
    for i, wt in enumerate(hit.wts):
        tag = data[wt.t.start : wt.t.start_end]
        edits[(wt.t.start, wt.t.start_end)] = tag if wt.preserved else render.PRESERVE_TAG
        edits[(wt.char_start, wt.char_end)] = render.inline_chars(segs, data, hit.run_rpr) if i == 0 else b""
    return edits


def _part_order(resolved: list[Resolved]) -> list[Resolved]:
    def key(r: Resolved):
        part_rank = 0 if r.part.name == "word/document.xml" else 1
        anchor = (r.spans or r.extent or [(0, 0)])[0][0]
        return (part_rank, r.part.name, anchor)

    return sorted(resolved, key=key)


def fill(
    template: bytes,
    manifest: Manifest,
    record: SystemRecord,
    plan: FillPlan | None = None,
    library: BoilerplateLibrary | None = None,
) -> FillResult:
    doc, resolved = load_approved(manifest, template)
    if plan is not None:
        if plan.manifest_sha256 != manifest.approval.manifest_sha256:
            raise PlanError("fill plan was made for a different manifest version")
        for rid in plan.regions:
            try:
                kind = manifest.region(rid).kind
            except KeyError:
                raise PlanError(f"fill plan names unknown region {rid!r}") from None
            if kind not in _PLANNED:
                raise PlanError(f"region {rid} is {kind}; code fills it, so the plan may not")

    filler = _Filler(record, plan, library)
    per_part: dict[str, dict[Span, bytes]] = {}
    regions_report = []
    blocking = []
    for res in _part_order(resolved):
        before = len(filler.gaps.items)
        outcome = filler.fill(res)
        edits = per_part.setdefault(res.part.name, {})
        missing = set(res.spans) - set(outcome.edits)
        if missing:
            raise AssertionError(f"writer left masked spans of {res.region.id} without an edit")
        edits.update(outcome.edits)
        gap_ids = [g["n"] for g in filler.gaps.items[before:]]
        status = outcome.status
        if outcome.blocking:
            status = "blocked"
        elif gap_ids:
            status = "gap"
        regions_report.append(
            {"id": res.region.id, "kind": res.region.kind.value, "status": status, "gaps": gap_ids,
             "blocking": outcome.blocking}
        )
        blocking += [{"region": res.region.id, "item": b} for b in outcome.blocking]

    replacements: dict[str, bytes] = {}
    witness: dict[str, list[int]] = {}
    for part_name, edits in per_part.items():
        if not edits:
            continue
        data = doc.part(part_name).data
        spans = sorted(edits)
        out, pos = [], 0
        for s, e in spans:
            out += [data[pos:s], edits[(s, e)]]
            pos = e
        out.append(data[pos:])
        replacements[part_name] = b"".join(out)
        witness[part_name] = [sum(len(edits[spans[i]]) for i in members) for _s, _e, members in merge_spans(spans)]

    output = doc.pkg.rebuild(replacements)
    gate = run_gate(template, output, manifest, witness)
    if not gate.passed:
        raise GateError("immutability gate rejected the output:\n  " + "\n  ".join(gate.failures))

    report = {
        "report_version": 1,
        "status": "draft_for_review",
        "template": {"file_name": manifest.template.file_name, "sha256": manifest.template.sha256},
        "manifest": {"sha256": manifest.approval.manifest_sha256, "approved_by": manifest.approval.approved_by},
        "system_record": {
            "record_id": record.record_id,
            "record_version": record.record_version,
            "sha256": record.sha256(),
        },
        "plan_sha256": plan.sha256() if plan else None,
        "boilerplate": filler.library_used,
        "output_sha256": sha256(output),
        "regions": regions_report,
        "gaps": filler.gaps.items,
        "blocking": blocking,
        "awaiting_human": [r["id"] for r in regions_report if r["status"] == "awaiting_human"],
        "gate": gate.to_dict(),
        "witness": witness,
    }
    return FillResult(output, report)
