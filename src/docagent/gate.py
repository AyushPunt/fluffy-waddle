"""Immutability gate: proves an output differs from its template only inside approved spans.

The gate trusts nothing from the writer. It re-resolves the approved manifest on
the template, then requires:
  * every part without a region to be the identical raw ZIP record;
  * every other part to equal the template byte for byte outside the masked spans;
  * each masked fragment to contain only the markup its mutation allows;
  * tables, grids, merged cells, checkbox count and labels, sections and fields to match.
The writer may pass a witness (fragment lengths) so matching needs no search. A
witness cannot widen the mask: it only says where fragments end.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from docagent.compiler import Resolved, load_approved
from docagent.errors import DocAgentError, PackageError
from docagent.manifest import Manifest
from docagent.ooxml.package import Package
from docagent.ooxml.wordml import CHECKED_GLYPHS, UNCHECKED_GLYPHS, PartIndex, row_shape
from docagent.render import PRESERVE_TAG, RPR_ORDER, VAL_ATTR

_TAG = re.compile(rb"<(/?)([A-Za-z_][\w.-]*(?::[\w.-]+)?)")
_FULL_TAG = re.compile(rb"<[A-Za-z_][\w.-]*:[\w.-]+(?:\s[^<>]*)?/?>")
_DENY = {
    "w:tbl", "w:tr", "w:tc", "w:sdt", "w:bookmarkStart", "w:bookmarkEnd", "w:fldChar", "w:instrText",
    "w:fldSimple", "w:drawing", "w:pict", "w:object", "w:hyperlink", "w:ins", "w:del", "w:moveFrom",
    "w:moveTo", "mc:AlternateContent", "w:commentRangeStart", "w:commentRangeEnd", "w:commentReference",
    "w:footnoteReference", "w:endnoteReference", "w:txbxContent", "w:altChunk", "w:subDoc",
    "w:customXml", "w:smartTag", "w:br", "w:cr", "w:sym", "w:permStart", "w:permEnd",
}
_INLINE_ALLOWED = {"w:t", "w:r", "w:rPr"} | {f"w:{x}" for x in RPR_ORDER}
_COUNTED = (
    "w:sectPr", "w:sdt", "w:bookmarkStart", "w:fldChar", "w:fldSimple", "w:drawing", "w:pict",
    "w:object", "w:hyperlink", "w:footnoteReference", "w:endnoteReference", "w:commentRangeStart",
)
_STATE_VALUES = {b"0", b"1", b"true", b"false"}
_VAL_VALUE = re.compile(rb"\s[A-Za-z0-9]+:val\s*=\s*[\"']([^\"']*)[\"']")


@dataclass
class GateResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "failures": self.failures, "checks": self.checks}


def merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int, list[int]]]:
    """Join spans that touch, so adjacent edits are matched as one fragment."""
    groups: list[tuple[int, int, list[int]]] = []
    for i, (s, e) in enumerate(spans):
        if groups and groups[-1][1] == s and groups[-1][0] != groups[-1][1] and s != e:
            gs, _ge, members = groups[-1]
            groups[-1] = (gs, e, members + [i])
        else:
            groups.append((s, e, [i]))
    return groups


def match_masked(
    orig: bytes, out: bytes, groups: list[tuple[int, int, list[int]]], lengths: list[int] | None
) -> list[bytes] | None:
    """Fragments of `out` filling the masked groups, or None if any unmasked byte differs."""
    bounds = [0] + [x for s, e, _ in groups for x in (s, e)] + [len(orig)]
    gaps = [orig[bounds[i] : bounds[i + 1]] for i in range(0, len(bounds), 2)]
    frags: list[bytes] = []
    if lengths is not None:
        if len(lengths) != len(groups):
            return None
        pos = 0
        for gap, n in zip(gaps, lengths):
            if out[pos : pos + len(gap)] != gap:
                return None
            pos += len(gap)
            frags.append(out[pos : pos + n])
            pos += n
        return frags if out[pos:] == gaps[-1] else None
    if not out.startswith(gaps[0]) or not out.endswith(gaps[-1]):
        return None
    pos, limit = len(gaps[0]), len(out) - len(gaps[-1])
    for gap in gaps[1:-1]:
        k = out.find(gap, pos, limit) if gap else pos
        if k < 0:
            return None
        frags.append(out[pos:k])
        pos = k + len(gap)
    if pos > limit:
        return None
    frags.append(out[pos:limit])
    return frags


def _tags(frag: bytes) -> list[str]:
    return [m.group(2).decode() for m in _TAG.finditer(frag)]


def _strip_val(tag: bytes) -> bytes:
    return re.sub(rb"\s*/?>$", b"", VAL_ATTR.sub(b"", tag))


def _check_fragment(orig: bytes, frag: bytes, roles: set[str], glyphs: set[str], where: str) -> list[str]:
    problems = []
    names = _tags(frag)
    if b"w14:paraId" in frag or b"w14:textId" in frag:
        problems.append(f"{where}: fragment copies a paragraph id")
    if roles <= {"wt_tag", "wt_chars"}:
        bad = sorted({n for n in names if n not in _INLINE_ALLOWED})
        if bad:
            problems.append(f"{where}: inline text fragment contains {bad}")
        if "wt_tag" in roles:
            tag = _FULL_TAG.match(orig).group()
            if not (frag.startswith(tag) or frag.startswith(PRESERVE_TAG)):
                problems.append(f"{where}: w:t start tag changed beyond xml:space")
            if roles == {"wt_tag"} and frag not in (tag, PRESERVE_TAG):
                problems.append(f"{where}: w:t start tag changed beyond xml:space")
    elif roles == {"cb_tag"}:
        if not _FULL_TAG.fullmatch(frag) or _strip_val(frag) != _strip_val(orig):
            problems.append(f"{where}: checkbox state tag changed beyond its value")
        elif (m := _VAL_VALUE.search(frag)) and m.group(1) not in _STATE_VALUES:
            problems.append(f"{where}: checkbox state has an invalid value {m.group(1)!r}")
    elif roles == {"cb_glyph"}:
        try:
            ch = frag.decode("utf-8")
        except UnicodeDecodeError:
            ch = ""
        if ch not in glyphs:
            problems.append(f"{where}: checkbox glyph replaced by {ch!r}")
    else:
        allowed_structure = {"w:tr", "w:tc"} if roles & {"row", "row_insert"} else set()
        bad = sorted({n for n in names if n in _DENY and n not in allowed_structure})
        if bad:
            problems.append(f"{where}: fragment adds structure {bad}")
        if frag.count(b"<w:sectPr") > orig.count(b"<w:sectPr"):
            problems.append(f"{where}: fragment adds a section break")
    return problems


def _structure(t: PartIndex, o: PartIndex) -> list[str]:
    problems = []
    name = t.name
    if len(t.tables) != len(o.tables):
        return [f"{name}: table count changed from {len(t.tables)} to {len(o.tables)}"]
    for i, (tt, ot) in enumerate(zip(t.tables, o.tables), 1):
        tg, og = tt.child("w:tblGrid"), ot.child("w:tblGrid")
        if (t.data[tg.start : tg.end] if tg else b"") != (o.data[og.start : og.end] if og else b""):
            problems.append(f"{name}: grid of table {i} changed")
        allowed = {row_shape(r) for r in t.rows(tt)}
        odd = [row_shape(r) for r in o.rows(ot) if row_shape(r) not in allowed]
        if odd:
            problems.append(f"{name}: table {i} has rows whose cell merges differ from the template")
    tb = [(b.kind, b.label_after, b.label_before) for b in t.checkboxes]
    ob = [(b.kind, b.label_after, b.label_before) for b in o.checkboxes]
    if tb != ob:
        problems.append(f"{name}: checkboxes or their option text changed ({len(tb)} -> {len(ob)})")
    for el in _COUNTED:
        a, b = sum(1 for _ in t.root.iter(el)), sum(1 for _ in o.root.iter(el))
        if a != b:
            problems.append(f"{name}: number of <{el}> changed from {a} to {b}")
    return problems


def _well_formed(data: bytes, name: str) -> list[str]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    try:
        etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        return [f"{name}: output is not well-formed XML ({exc})"]
    return []


def _glyphs_by_span(resolved: list[Resolved]) -> dict[tuple[str, int, int], set[str]]:
    out = {}
    for r in resolved:
        if r.choice is None:
            continue
        boxes = list(r.choice.boxes.values()) + [b for pair in r.choice.pairs.values() for b in pair]
        for b in boxes:
            for span, role in zip(b.spans, b.roles):
                if role == "cb_glyph":
                    out[(r.part.name, *span)] = (
                        {b.checked_glyph, b.unchecked_glyph} if b.kind == "sdt" else UNCHECKED_GLYPHS | CHECKED_GLYPHS
                    )
    return out


def run_gate(
    template: bytes, output: bytes, manifest: Manifest, witness: dict[str, list[int]] | None = None
) -> GateResult:
    try:
        doc, resolved = load_approved(manifest, template)
        out_pkg = Package.from_bytes(output)
    except DocAgentError as exc:
        return GateResult(False, [str(exc)])
    failures: list[str] = []
    tpl = doc.pkg
    if tpl.names != out_pkg.names:
        return GateResult(False, ["output has a different set or order of package parts"])

    masks: dict[str, list[tuple[tuple[int, int], str]]] = {}
    for r in resolved:
        masks.setdefault(r.part.name, []).extend(zip(r.spans, r.roles))
    glyphs = _glyphs_by_span(resolved)
    checked_parts = masked_spans = 0

    for entry in tpl.entries:
        name = entry.name
        out_entry = out_pkg.entry(name)
        try:
            out_data = out_entry.data()
        except PackageError as exc:
            failures.append(str(exc))
            continue
        checked_parts += 1
        if name not in masks or not masks[name]:
            same_central = entry.central[:42] + entry.central[46:] == out_entry.central[:42] + out_entry.central[46:]
            if entry.raw_record != out_entry.raw_record or not same_central:
                failures.append(f"{name}: changed although it holds no approved region")
            continue
        items = sorted(set(masks[name]))  # shared w:t tags appear once
        spans = [s for s, _ in items]
        roles = [r for _, r in items]
        groups = merge_spans(spans)
        masked_spans += len(spans)
        orig = entry.data()
        frags = match_masked(orig, out_data, groups, witness.get(name) if witness else None)
        if frags is None:
            failures.append(f"{name}: bytes outside the approved regions differ from the template")
            continue
        for (gs, ge, members), frag in zip(groups, frags):
            group_roles = {roles[i] for i in members}
            allowed = glyphs.get((name, gs, ge), set())
            failures += _check_fragment(orig[gs:ge], frag, group_roles, allowed, f"{name} bytes {gs}..{ge}")
        failures += _well_formed(out_data, name)
        try:
            failures += _structure(doc.part(name), PartIndex(name, out_data, doc.styles))
        except DocAgentError as exc:
            failures.append(f"{name}: output cannot be indexed ({exc})")

    return GateResult(
        passed=not failures,
        failures=failures,
        checks={
            "parts_checked": checked_parts,
            "parts_with_regions": sorted(n for n, m in masks.items() if m),
            "masked_spans": masked_spans,
            "witness_used": witness is not None,
        },
    )
