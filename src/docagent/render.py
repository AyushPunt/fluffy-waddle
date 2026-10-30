"""Builders for the only XML the writer ever emits.

New markup is assembled from verbatim slices of the template (pPr, rPr, tcPr,
trPr) plus w:p, w:r, w:t, w:tc, w:tr wrappers and the gap highlight. Nothing
else is generated.
"""

from __future__ import annotations

import re

from docagent.compiler import CellProto, RowsHit
from docagent.ooxml.text import escape
from docagent.ooxml.xmlindex import Node
from docagent.plan import Seg

PRESERVE_TAG = b'<w:t xml:space="preserve">'
HIGHLIGHT = b'<w:highlight w:val="yellow"/>'
# CT_RPr child order; Word reports a damaged file when rPr children are out of order.
RPR_ORDER = [
    "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "outline",
    "shadow", "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden", "color", "spacing",
    "w", "kern", "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText",
    "vertAlign", "rtl", "cs", "em", "lang", "eastAsianLayout", "specVanish", "oMath",
]
_HIGHLIGHT_RANK = RPR_ORDER.index("highlight")
VAL_ATTR = re.compile(rb"(\s[A-Za-z0-9]+:val\s*=\s*)([\"'])[^\"']*\2")


def rpr_bytes(data: bytes, rpr: Node | None) -> bytes:
    return data[rpr.start : rpr.end] if rpr is not None else b""


def highlighted_rpr(data: bytes, rpr: Node | None) -> bytes:
    if rpr is None or rpr.is_empty:
        return b"<w:rPr>" + HIGHLIGHT + b"</w:rPr>"
    for c in rpr.children:
        if c.name == "w:highlight":
            return data[rpr.start : c.start] + HIGHLIGHT + data[c.end : rpr.end]
        local = c.name.removeprefix("w:")
        if local in RPR_ORDER and RPR_ORDER.index(local) > _HIGHLIGHT_RANK:
            return data[rpr.start : c.start] + HIGHLIGHT + data[c.start : rpr.end]
    return data[rpr.start : rpr.end_start] + HIGHLIGHT + data[rpr.end_start : rpr.end]


def runs(segs: list[Seg], data: bytes, rpr: Node | None) -> bytes:
    base, marked = rpr_bytes(data, rpr), highlighted_rpr(data, rpr)
    return b"".join(
        b"<w:r>" + (marked if s.gap else base) + PRESERVE_TAG + escape(s.text) + b"</w:t></w:r>" for s in segs
    )


def paragraph(ppr: bytes, body: bytes) -> bytes:
    return b"<w:p>" + ppr + body + b"</w:p>"


def inline_chars(segs: list[Seg], data: bytes, run_rpr: Node | None) -> bytes:
    """Bytes that replace placeholder characters inside a w:t.

    Plain text stays in the placeholder's own run. A gap closes that run, emits a
    highlighted run, and reopens a run with the same properties for any text after it.
    """
    base, marked = rpr_bytes(data, run_rpr), highlighted_rpr(data, run_rpr)
    out = b""
    for s in segs:
        if s.gap:
            out += (
                b"</w:t></w:r><w:r>" + marked + PRESERVE_TAG + escape(s.text) + b"</w:t></w:r>"
                b"<w:r>" + base + PRESERVE_TAG
            )
        else:
            out += escape(s.text)
    return out


def cell(proto: CellProto, data: bytes, paragraphs: list[list[Seg]]) -> bytes:
    body = b"".join(paragraph(proto.ppr, runs(segs, data, proto.rpr)) for segs in paragraphs or [[]])
    return b"<w:tc>" + proto.tcpr + body + b"</w:tc>"


def row(hit: RowsHit, cells: list[bytes]) -> bytes:
    return b"<w:tr>" + hit.tblprex + hit.trpr + b"".join(cells) + b"</w:tr>"


def set_state_tag(tag: bytes, checked: bool) -> bytes:
    """Rewrite the val attribute of w14:checked, w:checked or w:default, keeping everything else."""
    value = b"1" if checked else b"0"
    if VAL_ATTR.search(tag):
        return VAL_ATTR.sub(lambda m: m.group(1) + m.group(2) + value + m.group(2), tag, count=1)
    prefix = re.match(rb"<([A-Za-z0-9]+):", tag).group(1)
    return tag[:-2].rstrip() + b" " + prefix + b':val="' + value + b'"/>'
