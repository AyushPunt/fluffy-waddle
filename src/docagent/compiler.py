"""Region compiler: resolves manifest anchors to byte spans, approves manifests, proposes regions."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from docagent.errors import ManifestError, TemplateError
from docagent.manifest import (
    Anchor,
    Approval,
    Column,
    Manifest,
    Mutation,
    Region,
    RegionKind,
    ResolvedRef,
    TemplateRef,
)
from docagent.ooxml.package import Package, sha256
from docagent.ooxml.wordml import Checkbox, ParaText, PartIndex, Styles, norm, row_cells
from docagent.ooxml.xmlindex import Node

TEXT_PART = re.compile(r"^word/(document|header\d*|footer\d*)\.xml$")
# Run properties that would make inherited text hidden.
_HIDDEN_RPR = {"w:vanish", "w:webHidden", "w:specVanish"}
_PLACEHOLDER_STYLE_RPR = {"w:i", "w:color", "w:highlight", "w:shd", "w:rStyle"}
_PARAGRAPH_CHILDREN = {"w:pPr", "w:r", "w:proofErr"}
_RUN_CHILDREN = {"w:rPr", "w:t", "w:lastRenderedPageBreak"}
_TABLE_CHILDREN = {"w:tblPr", "w:tblGrid", "w:tr"}
_ROW_CHILDREN = {"w:trPr", "w:tblPrEx", "w:tc"}


class TemplateDoc:
    def __init__(self, raw: bytes, file_name: str = "template.docx"):
        self.raw = raw
        self.file_name = file_name
        self.pkg = Package.from_bytes(raw)
        if not self.pkg.has("word/document.xml"):
            raise TemplateError("package has no word/document.xml; is this a .docx?")
        self.styles = Styles(self.pkg.part("word/styles.xml") if self.pkg.has("word/styles.xml") else None)
        self._parts: dict[str, PartIndex] = {}

    def text_parts(self) -> list[str]:
        others = sorted(n for n in self.pkg.names if TEXT_PART.match(n) and n != "word/document.xml")
        return ["word/document.xml", *others]

    def part(self, name: str) -> PartIndex:
        if not TEXT_PART.match(name) or not self.pkg.has(name):
            raise TemplateError(f"anchor part {name!r} is not a document, header or footer part of this template")
        if name not in self._parts:
            self._parts[name] = PartIndex(name, self.pkg.part(name), self.styles)
        return self._parts[name]

    def template_ref(self) -> TemplateRef:
        return TemplateRef(file_name=self.file_name, sha256=sha256(self.raw), parts=self.pkg.fingerprint())


# --- resolved targets --------------------------------------------------------------------------


@dataclass(eq=False)
class WtEdit:
    t: Node
    char_start: int  # byte span of the matched characters inside this w:t
    char_end: int
    preserved: bool  # w:t already carries xml:space="preserve"


@dataclass(eq=False)
class InlineHit:
    para: Node
    wts: list[WtEdit]
    run_rpr: Node | None  # rPr of the run that holds the first matched character


@dataclass(eq=False)
class ParagraphHit:
    para: Node
    ppr: bytes
    ppr_without_sect: bytes
    first_rpr: Node | None


@dataclass(eq=False)
class CellProto:
    tcpr: bytes
    ppr: bytes
    rpr: Node | None
    merged_continue: bool


@dataclass(eq=False)
class RowsHit:
    table: Node
    prototype: Node
    trpr: bytes
    tblprex: bytes
    cells: list[CellProto]


@dataclass(eq=False)
class ChoiceHit:
    boxes: dict[str, Checkbox] = field(default_factory=dict)  # single_box: option -> box
    pairs: dict[str, tuple[Checkbox, Checkbox]] = field(default_factory=dict)  # yes_no: item -> (yes, no)


@dataclass(eq=False)
class Resolved:
    region: Region
    part: PartIndex
    spans: list[tuple[int, int]]
    roles: list[str]
    paths: list[str]
    context: bytes
    extent: list[tuple[int, int]]
    inline: list[InlineHit] = field(default_factory=list)
    paragraph: ParagraphHit | None = None
    rows: RowsHit | None = None
    choice: ChoiceHit | None = None

    def ref(self) -> ResolvedRef:
        return ResolvedRef(
            part=self.part.name,
            paths=self.paths,
            spans=[(s, e) for s, e in self.spans],
            context_sha256=sha256(self.context),
        )


def resolve(doc: TemplateDoc, region: Region) -> Resolved:
    pi = doc.part(region.anchor.part)
    try:
        resolver = {
            Mutation.INLINE_TEXT: _resolve_inline,
            Mutation.PARAGRAPHS: _resolve_paragraph,
            Mutation.ROWS: _resolve_rows,
            Mutation.CHECKBOX_STATE: _resolve_checkboxes,
            Mutation.NONE: _resolve_human,
        }[region.mutation]
        res = resolver(pi, region)
    except TemplateError as exc:
        raise TemplateError(f"region {region.id}: {exc}") from None
    order = sorted(range(len(res.spans)), key=lambda i: res.spans[i])
    res.spans = [res.spans[i] for i in order]
    res.roles = [res.roles[i] for i in order]
    return res


def _find_text(pi: PartIndex, a: Anchor) -> list[tuple[ParaText, int]]:
    lo, hi = pi.scope(a.section)
    hits = []
    for p in pi.paragraphs:
        if not lo <= p.start < hi:
            continue
        pt = pi.text(p)
        i = 0
        while (k := pt.text.find(a.text, i)) >= 0:
            hits.append((pt, k))
            i = k + len(a.text)
    where = f" in section {a.section!r}" if a.section else ""
    if not hits:
        raise TemplateError(f"placeholder {a.text!r} not found{where}")
    if a.occurrence is None:
        if len(hits) != 1:
            raise TemplateError(
                f"placeholder {a.text!r} occurs {len(hits)} times{where}; set anchor.occurrence or anchor.section"
            )
        return hits
    if a.occurrence == "all":
        return hits
    if not 1 <= a.occurrence <= len(hits):
        raise TemplateError(f"occurrence {a.occurrence} requested but {a.text!r} occurs {len(hits)} times{where}")
    return [hits[a.occurrence - 1]]


def _check_inheritable(rpr: Node | None, what: str) -> None:
    if rpr is not None:
        hidden = next((c.name for c in rpr.children if c.name in _HIDDEN_RPR), None)
        if hidden:
            raise TemplateError(f"{what} is formatted as hidden text ({hidden}); new text would be invisible")


def _inline_hit(pi: PartIndex, pt: ParaText, k: int, n: int) -> InlineHit:
    refs = pt.refs[k : k + n]
    if any(r is None for r in refs):
        raise TemplateError(
            "placeholder crosses a tab, line break, checkbox or field result; it must be plain run text"
        )
    groups: list[list] = []
    for ref in refs:
        if groups and groups[-1][0] is ref.t:
            groups[-1][2] = ref.end
        else:
            groups.append([ref.t, ref.start, ref.end])
    wts = []
    for t, cs, ce in groups:
        if t.parent is None or t.parent.name != "w:r":
            raise TemplateError(f"w:t at byte {t.start} is not inside a run")
        wts.append(WtEdit(t=t, char_start=cs, char_end=ce, preserved=t.attrs.get("xml:space") == "preserve"))
    run_rpr = wts[0].t.parent.child("w:rPr")
    _check_inheritable(run_rpr, "the placeholder run")
    return InlineHit(para=pt.para, wts=wts, run_rpr=run_rpr)


def _resolve_inline(pi: PartIndex, region: Region) -> Resolved:
    a = region.anchor
    if region.kind == RegionKind.SINGLE_CHOICE and not a.options:
        raise TemplateError("an inline single choice needs anchor.options (the template's option set)")
    hits = [_inline_hit(pi, pt, k, len(a.text)) for pt, k in _find_text(pi, a)]
    spans, roles, paths, context = [], [], [], b""
    for hit in hits:
        for wt in hit.wts:
            spans += [(wt.t.start, wt.t.start_end), (wt.char_start, wt.char_end)]
            roles += ["wt_tag", "wt_chars"]
        if hit.para.path() not in paths:
            paths.append(hit.para.path())
            context += pi.data[hit.para.start : hit.para.end]
    extent = [h.para.span for h in hits]
    return Resolved(region, pi, spans, roles, paths, context, extent, inline=hits)


def _resolve_paragraph(pi: PartIndex, region: Region) -> Resolved:
    a = region.anchor
    if a.occurrence == "all":
        raise TemplateError("a paragraph region must resolve to exactly one paragraph")
    pt, _k = _find_text(pi, a)[0]
    p = pt.para
    if norm(pt.text) != norm(a.text):
        raise TemplateError(
            f"paragraph mutation needs {a.text!r} to be the paragraph's only text; use inline_text instead"
        )
    for c in p.children:
        if c.name not in _PARAGRAPH_CHILDREN:
            raise TemplateError(f"placeholder paragraph contains <{c.name}>, which replacing it would destroy")
        if c.name == "w:r":
            bad = next((x.name for x in c.children if x.name not in _RUN_CHILDREN), None)
            if bad:
                raise TemplateError(f"placeholder run contains <{bad}>, which replacing it would destroy")
    ppr = p.child("w:pPr")
    ppr_bytes = pi.data[ppr.start : ppr.end] if ppr else b""
    without_sect = ppr_bytes
    sect = ppr.child("w:sectPr") if ppr else None
    if sect is not None:
        without_sect = pi.data[ppr.start : sect.start] + pi.data[sect.end : ppr.end]
    first_run = next((r for r in p.children_named("w:r") if r.child("w:t") is not None), None)
    first_rpr = first_run.child("w:rPr") if first_run else None
    if region.run_props == "inherit":
        _check_inheritable(first_rpr, "the placeholder run")
    hit = ParagraphHit(p, ppr_bytes, without_sect, first_rpr)
    return Resolved(
        region, pi, [p.span], ["paragraph"], [p.path()], pi.data[p.start : p.end], [p.span], paragraph=hit
    )


def _find_table(pi: PartIndex, a: Anchor) -> Node:
    lo, hi = pi.scope(a.section)
    want = [norm(h) for h in a.table_header]
    found = []
    for t in pi.tables:
        rows = pi.rows(t)
        if lo <= t.start < hi and rows and [pi.cell_text(c) for c in row_cells(rows[0])] == want:
            found.append(t)
    if len(found) != 1:
        where = f" in section {a.section!r}" if a.section else ""
        raise TemplateError(f"{len(found)} tables{where} have header row {a.table_header!r}; expected exactly one")
    return found[0]


def _resolve_rows(pi: PartIndex, region: Region) -> Resolved:
    a = region.anchor
    t = _find_table(pi, a)
    bad = next((c.name for c in t.children if c.name not in _TABLE_CHILDREN), None)
    if bad:
        raise TemplateError(f"table contains <{bad}> between rows; only plain rows are supported")
    rows = pi.rows(t)
    if len(rows) < 2:
        raise TemplateError("table needs a header row and a prototype row")
    try:
        proto = rows[region.prototype_row]
    except IndexError:
        raise TemplateError(f"prototype_row {region.prototype_row} is out of range") from None
    if proto is rows[0]:
        raise TemplateError("the header row cannot be the prototype row")
    trpr = proto.child("w:trPr")
    if trpr is not None and trpr.child("w:tblHeader") is not None:
        raise TemplateError("the prototype row is a repeating header row")
    bad = next((c.name for c in proto.children if c.name not in _ROW_CHILDREN), None)
    if bad:
        raise TemplateError(f"prototype row contains <{bad}>; only plain cells are supported")
    cells = []
    for tc in row_cells(proto):
        if any(tc.iter("w:tbl")):
            raise TemplateError("prototype row contains a nested table")
        tcpr = tc.child("w:tcPr")
        merge = tcpr.child("w:vMerge") if tcpr else None
        p = tc.child("w:p")
        ppr = p.child("w:pPr") if p else None
        run = next((r for r in p.children_named("w:r")), None) if p else None
        rpr = run.child("w:rPr") if run else None
        _check_inheritable(rpr, "a prototype cell run")
        cells.append(
            CellProto(
                tcpr=pi.data[tcpr.start : tcpr.end] if tcpr else b"",
                ppr=pi.data[ppr.start : ppr.end] if ppr else b"",
                rpr=rpr,
                merged_continue=merge is not None and merge.attrs.get("w:val", "continue") != "restart",
            )
        )
    if region.columns is not None and len(region.columns) != len(cells):
        raise TemplateError(f"manifest lists {len(region.columns)} columns but the prototype row has {len(cells)} cells")
    prex = proto.child("w:tblPrEx")
    hit = RowsHit(
        table=t,
        prototype=proto,
        trpr=pi.data[trpr.start : trpr.end] if trpr else b"",
        tblprex=pi.data[prex.start : prex.end] if prex else b"",
        cells=cells,
    )
    if region.row_mode == "replace_prototype":
        spans, roles = [proto.span], ["row"]
    else:
        spans, roles = [(rows[-1].end, rows[-1].end)], ["row_insert"]
    return Resolved(region, pi, spans, roles, [t.path()], pi.data[t.start : t.end], [t.span], rows=hit)


def _item_label(pi: PartIndex, box: Checkbox) -> str:
    if box.row_label:
        return box.row_label
    return pi.text(box.para).boxes[0].label_before


def _resolve_checkboxes(pi: PartIndex, region: Region) -> Resolved:
    a = region.anchor
    lo, hi = pi.scope(a.section)
    boxes = [b for b in pi.checkboxes if lo <= b.para.start < hi]
    hit = ChoiceHit()
    if a.layout == "single_box":
        if not a.options:
            raise TemplateError("checkbox regions need anchor.options")
        for option in a.options:
            matches = [b for b in boxes if b.label(a.label_side) == norm(option)]
            if len(matches) != 1:
                raise TemplateError(f"option {option!r} matches {len(matches)} checkboxes; expected exactly one")
            hit.boxes[option] = matches[0]
        used = list(hit.boxes.values())
    else:
        if not a.items:
            raise TemplateError("yes/no checklists need anchor.items")
        for item in a.items:
            row = [b for b in boxes if _item_label(pi, b) == norm(item)]
            yes = [b for b in row if b.label_after == norm(a.yes_label)]
            no = [b for b in row if b.label_after == norm(a.no_label)]
            if len(yes) != 1 or len(no) != 1:
                raise TemplateError(
                    f"item {item!r} has {len(yes)} {a.yes_label!r} and {len(no)} {a.no_label!r} boxes; expected one each"
                )
            hit.pairs[item] = (yes[0], no[0])
        used = [b for pair in hit.pairs.values() for b in pair]
    for b in used:
        if b.unsupported:
            raise TemplateError(f"checkbox near {b.label(a.label_side)!r} is unsupported: {b.unsupported}")
    spans = [s for b in used for s in b.spans]
    roles = [r for b in used for r in b.roles]
    paras = sorted({id(b.para): b.para for b in used}.values(), key=lambda p: p.start)
    context = b"".join(pi.data[p.start : p.end] for p in paras)
    return Resolved(region, pi, spans, roles, [p.path() for p in paras], context, [p.span for p in paras], choice=hit)


def _resolve_human(pi: PartIndex, region: Region) -> Resolved:
    a = region.anchor
    if a.table_header:
        t = _find_table(pi, a)
        return Resolved(region, pi, [], [], [t.path()], pi.data[t.start : t.end], [t.span])
    hits = _find_text(pi, a)
    paras = [pt.para for pt, _ in hits]
    context = b"".join(pi.data[p.start : p.end] for p in paras)
    return Resolved(region, pi, [], [], [p.path() for p in paras], context, [p.span for p in paras])


def check_overlaps(resolved: list[Resolved]) -> None:
    """No two regions may claim the same bytes. A w:t start tag may be shared: every
    inline region writes the same xml:space="preserve" tag to it."""
    by_part: dict[str, list[tuple[int, int, str, str]]] = defaultdict(list)
    for r in resolved:
        for (s, e), role in zip(r.spans, r.roles):
            by_part[r.part.name].append((s, e, role, r.region.id))
    for items in by_part.values():
        items.sort()
        for (s1, e1, role1, id1), (s2, e2, role2, id2) in zip(items, items[1:]):
            if (s1, e1) == (s2, e2) and role1 == role2 == "wt_tag":
                continue
            if s2 < e1 or s1 == s2:
                raise ManifestError(f"regions {id1} and {id2} overlap at bytes {s2}..{e1}")
    for r in resolved:
        if r.region.kind != RegionKind.HUMAN_ONLY:
            continue
        for hs, he in r.extent:
            for s, e, _role, rid in by_part[r.part.name]:
                if s < he and e > hs:
                    raise ManifestError(f"region {rid} would change human-only region {r.region.id}")


# --- approval ----------------------------------------------------------------------------------


def _check_template(manifest: Manifest, doc: TemplateDoc) -> None:
    ref = doc.template_ref()
    if ref.sha256 != manifest.template.sha256 or ref.parts != manifest.template.parts:
        raise ManifestError(
            "template does not match the manifest fingerprint; a revised template needs its manifest re-approved"
        )


def approve(manifest: Manifest, raw: bytes, approved_by: str, now: datetime | None = None) -> Manifest:
    """Resolve every region against the template and pin the result with the approver's name."""
    if not approved_by.strip():
        raise ManifestError("approval needs the approver's name")
    doc = TemplateDoc(raw, manifest.template.file_name)
    _check_template(manifest, doc)
    problems = [f"{r.id}: {p}" for r in manifest.regions for p in r.completeness_problems()]
    if problems:
        raise ManifestError("manifest is incomplete:\n  " + "\n  ".join(problems))
    resolved = [resolve(doc, r) for r in manifest.regions]
    check_overlaps(resolved)
    approved = manifest.model_copy(deep=True)
    for region, res in zip(approved.regions, resolved):
        region.resolved = res.ref()
    approved.approval = None
    approved.approval = Approval(
        approved_by=approved_by,
        approved_at=(now or datetime.now(UTC)).isoformat(timespec="seconds"),
        manifest_sha256=approved.content_sha256(),
    )
    return approved


def load_approved(manifest: Manifest, raw: bytes) -> tuple[TemplateDoc, list[Resolved]]:
    """Refuse unless the manifest is approved, unchanged, and still matches the template exactly."""
    if manifest.approval is None:
        raise ManifestError("manifest is not approved")
    if manifest.approval.manifest_sha256 != manifest.content_sha256():
        raise ManifestError("manifest was edited after approval; it must be re-approved")
    doc = TemplateDoc(raw, manifest.template.file_name)
    _check_template(manifest, doc)
    resolved = [resolve(doc, r) for r in manifest.regions]
    for region, res in zip(manifest.regions, resolved):
        if region.resolved != res.ref():
            raise ManifestError(f"region {region.id}: anchor no longer matches the approved span; re-approve")
    check_overlaps(resolved)
    return doc, resolved


# --- proposal ----------------------------------------------------------------------------------

PLACEHOLDER = re.compile(r"\[[^\[\]\n]{2,160}\]|<[^<>\n]{2,160}>|«[^«»\n]{2,160}»|\{\{[^{}\n]{2,80}\}\}")
_SIGNATURE_WORDS = ("signature", "signed", "approved by", "approver")
_ID_HEADER = re.compile(r"\b(id|no\.?|number|#)\s*$", re.IGNORECASE)
_SCALE = {"low", "medium", "high", "very low", "very high", "minor", "major", "critical", "moderate", "severe"}
_YES_NO = {"yes", "no", "n/a", "na"}


def _slug(text: str, words: int = 6) -> str:
    parts = re.findall(r"[a-z0-9]+", text.lower())
    while parts and parts[0].isdigit():  # drop heading numbers such as "3 Requirements"
        parts.pop(0)
    parts = parts[:words]
    slug = "_".join(parts)[:48].strip("_")
    return slug if slug and slug[0].isalpha() else f"r_{slug}" if slug else "region"


def _looks_like_placeholder_style(rpr: Node | None) -> bool:
    return rpr is not None and any(c.name in _PLACEHOLDER_STYLE_RPR for c in rpr.children)


def propose(doc: TemplateDoc) -> Manifest:
    """Draft a manifest from signals already in the template. A template owner must review it."""
    regions: list[Region] = []
    warnings: list[str] = []
    ids: set[str] = set()

    def new_id(base: str, part: str) -> str:
        stem = part.rsplit("/", 1)[-1].removesuffix(".xml")
        base = base if part == "word/document.xml" else f"{base}_{stem}"
        rid, n = base, 2
        while rid in ids:
            rid, n = f"{base}_{n}", n + 1
        ids.add(rid)
        return rid

    for part in doc.text_parts():
        pi = doc.part(part)
        claimed: list[tuple[int, int]] = []

        def is_claimed(node: Node, claimed: list[tuple[int, int]] = claimed) -> bool:
            return any(s <= node.start < e for s, e in claimed)

        for t in pi.tables:
            rows = pi.rows(t)
            if t.ancestor("w:tbl") is not None or not rows:
                continue
            header = [pi.cell_text(c) for c in row_cells(rows[0])]
            section = pi.section_of(t)
            if any(w in h.lower() for h in header for w in _SIGNATURE_WORDS):
                regions.append(
                    Region(
                        id=new_id("approvals", part),
                        kind=RegionKind.HUMAN_ONLY,
                        mutation=Mutation.NONE,
                        anchor=Anchor(part=part, section=section, table_header=header),
                    )
                )
                claimed.append(t.span)
                continue
            if len(rows) < 2:
                continue
            last = rows[-1]
            texts = [pi.cell_text(c) for c in row_cells(last)]
            has_box = any(b.para.ancestor("w:tr") is last for b in pi.checkboxes)
            if not has_box and all(x == "" or PLACEHOLDER.fullmatch(x) for x in texts) and len(texts) == len(header):
                columns = [
                    Column(name=_slug(h, 3) or f"col{i + 1}", source="row_number" if _ID_HEADER.search(h) else "plan")
                    for i, h in enumerate(header)
                ]
                regions.append(
                    Region(
                        id=new_id(_slug(section or "table", 4) + "_rows", part),
                        kind=RegionKind.TABLE_ROWS,
                        mutation=Mutation.ROWS,
                        anchor=Anchor(part=part, section=section, table_header=header),
                        columns=columns,
                        row_mode="replace_prototype",
                        hints={"section": section},
                        needs_review=["columns"],
                    )
                )
                claimed.append(last.span)

        groups: dict[tuple[str | None, int], list[Checkbox]] = defaultdict(list)
        for b in pi.checkboxes:
            if not is_claimed(b.para):
                tbl = b.para.ancestor("w:tbl")
                groups[(pi.section_of(b.para), tbl.start if tbl else -1)].append(b)
        for (section, _), boxes in groups.items():
            bad = [b for b in boxes if b.unsupported]
            for b in bad:
                warnings.append(f"{part}: unsupported checkbox in section {section!r}: {b.unsupported}")
            boxes = [b for b in boxes if not b.unsupported]
            labels = [b.label("after") for b in boxes]
            if not boxes:
                continue
            if any(not label for label in labels):
                warnings.append(f"{part}: checkbox without a label in section {section!r}; define this group by hand")
                continue
            if len(set(labels)) < len(labels):
                if {label.lower() for label in labels} <= _YES_NO:
                    items = list(dict.fromkeys(_item_label(pi, b) for b in boxes))
                    yes = next(lab for lab in labels if lab.lower() == "yes")
                    no = next((lab for lab in labels if lab.lower() == "no"), "No")
                    regions.append(
                        Region(
                            id=new_id(_slug(section or "checklist", 4), part),
                            kind=RegionKind.MULTI_SELECT,
                            mutation=Mutation.CHECKBOX_STATE,
                            anchor=Anchor(
                                part=part, section=section, items=items, layout="yes_no", yes_label=yes, no_label=no
                            ),
                            hints={"section": section},
                        )
                    )
                else:
                    warnings.append(f"{part}: checkbox labels repeat in section {section!r}; define this group by hand")
                continue
            single = all(label.lower() in _SCALE for label in labels)
            regions.append(
                Region(
                    id=new_id(_slug(section or "choice", 4), part),
                    kind=RegionKind.SINGLE_CHOICE if single else RegionKind.MULTI_SELECT,
                    mutation=Mutation.CHECKBOX_STATE,
                    anchor=Anchor(part=part, section=section, options=labels),
                    hints={"section": section},
                    needs_review=["kind"],
                )
            )

        facts: dict[str, list[ParaText]] = defaultdict(list)
        prose_seen: dict[tuple[str | None, str], int] = defaultdict(int)
        for p in pi.paragraphs:
            if is_claimed(p):
                continue
            pt = pi.text(p)
            for m in PLACEHOLDER.finditer(pt.text):
                if any(r is None for r in pt.refs[m.start() : m.end()]):
                    warnings.append(f"{part}: placeholder {m.group()!r} crosses a tab, field or checkbox; skipped")
                    continue
                text = m.group()
                section = pi.section_of(p)
                whole = norm(pt.text) == norm(text)
                if whole and p.ancestor("w:tbl") is None and len(text.split()) > 3:
                    prose_seen[(section, text)] += 1
                    first_run = next((r for r in p.children_named("w:r") if r.child("w:t")), None)
                    rpr = first_run.child("w:rPr") if first_run else None
                    regions.append(
                        Region(
                            id=new_id(_slug(text), part),
                            kind=RegionKind.PROSE,
                            mutation=Mutation.PARAGRAPHS,
                            anchor=Anchor(part=part, section=section, text=text),
                            run_props="paragraph" if _looks_like_placeholder_style(rpr) else "inherit",
                            hints={"section": section},
                        )
                    )
                else:
                    facts[text].append(pt)
        for (section, text), count in prose_seen.items():
            if count > 1:
                warnings.append(f"{part}: {text!r} occurs {count} times in section {section!r}; set anchor.occurrence")
        for text, hits in facts.items():
            slug = _slug(text, 4)
            regions.append(
                Region(
                    id=new_id(slug, part),
                    kind=RegionKind.FACT,
                    mutation=Mutation.INLINE_TEXT,
                    anchor=Anchor(part=part, text=text, occurrence="all" if len(hits) > 1 else None),
                    source=f"system.{slug}",
                    needs_review=["source"],
                )
            )

        sdt_boxes = sum(1 for _ in pi.root.iter("w14:checkbox"))
        found = sum(1 for b in pi.checkboxes if b.kind == "sdt")
        if sdt_boxes != found:
            warnings.append(f"{part}: {sdt_boxes - found} checkbox content controls wrap whole paragraphs; unsupported")
        other_sdts = sum(1 for s in pi.root.iter("w:sdt") if not any(s.iter("w14:checkbox")))
        if other_sdts:
            warnings.append(f"{part}: {other_sdts} content controls found; they are not yet used as anchors")

    return Manifest(template=doc.template_ref(), regions=regions, warnings=warnings)
