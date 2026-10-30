"""WordprocessingML semantics on top of the byte-span index.

Provides paragraph text with a per-character map back to w:t bytes (so anchors
match across split runs), heading-based sections, tables, and the three
checkbox forms Word produces: w14 content controls, legacy form fields and
☐/☒ glyphs typed as text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from docagent.errors import TemplateError
from docagent.ooxml.text import decode_with_offsets
from docagent.ooxml.xmlindex import Node, index_xml

W_MAIN_DECL = b'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
MARK = "￼"  # stands in for a checkbox inside paragraph text
UNCHECKED_GLYPHS = {"☐"}
CHECKED_GLYPHS = {"☑", "☒"}

_REVISIONS = {
    "w:ins", "w:del", "w:moveFrom", "w:moveTo", "w:rPrChange", "w:pPrChange",
    "w:sectPrChange", "w:tblPrChange", "w:trPrChange", "w:tcPrChange", "w:tblGridChange",
}
# Subtrees that never contribute visible paragraph text.
_SKIP = {
    "w:pPr", "w:rPr", "w:sdtPr", "w:sdtEndPr", "w:delText", "w:instrText",
    "mc:Fallback", "w:txbxContent", "w:p", "w:tbl", "w:fldData",
}
_TRUE = {"1", "true", "on"}


def norm(text: str) -> str:
    """Collapse whitespace and drop checkbox marks, for comparing labels and anchors."""
    return " ".join(text.replace(MARK, " ").split())


@dataclass(eq=False)
class Checkbox:
    kind: str  # "sdt" | "legacy" | "glyph"
    para: Node
    checked: bool
    spans: list[tuple[int, int]] = field(default_factory=list)  # bytes that carry the state
    roles: list[str] = field(default_factory=list)  # "cb_tag" | "cb_glyph", aligned with spans
    checked_glyph: str = "☒"
    unchecked_glyph: str = "☐"
    unsupported: str | None = None
    pos: int = 0
    label_after: str = ""
    label_before: str = ""
    cell_label: str = ""  # neighbouring cell text when the box sits alone in its cell
    row_label: str = ""  # first cell text when the box sits in a later cell of a table row

    def label(self, side: str) -> str:
        own = self.label_after if side == "after" else self.label_before
        return own or self.cell_label


@dataclass(eq=False)
class CharRef:
    t: Node  # the w:t element holding the character
    start: int
    end: int


@dataclass(eq=False)
class ParaText:
    para: Node
    text: str
    refs: list[CharRef | None]  # None marks characters that must not be edited
    boxes: list[Checkbox]


class Styles:
    def __init__(self, data: bytes | None):
        self._styles: dict[str, tuple[str, str | None, int | None]] = {}
        if not data:
            return
        root = index_xml(data, "word/styles.xml")
        for s in root.children_named("w:style"):
            sid = s.attrs.get("w:styleId")
            if not sid:
                continue
            name = s.child("w:name")
            based = s.child("w:basedOn")
            ppr = s.child("w:pPr")
            outline = ppr.child("w:outlineLvl") if ppr else None
            self._styles[sid] = (
                (name.attrs.get("w:val", "") if name else "").lower(),
                based.attrs.get("w:val") if based else None,
                int(outline.attrs["w:val"]) if outline and outline.attrs.get("w:val", "").isdigit() else None,
            )

    def heading_level(self, style_id: str | None, _depth: int = 0) -> int | None:
        if style_id is None or style_id not in self._styles or _depth > 20:
            return None
        name, based, outline = self._styles[style_id]
        if name.startswith("heading ") and name[8:].strip().isdigit():
            return int(name[8:])
        if outline is not None and outline < 9:
            return outline + 1
        return self.heading_level(based, _depth + 1)


class _Walker:
    """Builds ParaText for paragraphs in document order (field state spans paragraphs)."""

    def __init__(self, data: bytes):
        self.data = data
        self.fields: list[str] = []

    def paragraph(self, p: Node) -> ParaText:
        self.p = p
        self.chars: list[str] = []
        self.refs: list[CharRef | None] = []
        self.boxes: list[Checkbox] = []
        self._walk(p, editable=True)
        text = "".join(self.chars)
        positions = [b.pos for b in self.boxes]
        for i, b in enumerate(self.boxes):
            nxt = positions[i + 1] if i + 1 < len(positions) else len(text)
            prv = positions[i - 1] + 1 if i > 0 else 0
            b.label_after = norm(text[b.pos + 1 : nxt])
            b.label_before = norm(text[prv : b.pos])
        return ParaText(p, text, self.refs, self.boxes)

    def _emit(self, ch: str, ref: CharRef | None) -> None:
        self.chars.append(ch)
        self.refs.append(ref)

    def _box(self, box: Checkbox) -> None:
        box.pos = len(self.chars)
        self.boxes.append(box)
        self._emit(MARK, None)

    def _walk(self, node: Node, editable: bool) -> None:
        for c in node.children:
            n = c.name
            if n in _SKIP:
                continue
            if n == "w:sdt":
                box = self._sdt_checkbox(c)
                if box is not None:
                    self._box(box)
                elif (content := c.child("w:sdtContent")) is not None:
                    self._walk(content, editable)
            elif n == "w:fldChar":
                kind = c.attrs.get("w:fldCharType")
                if kind == "begin":
                    self.fields.append("code")
                    box = self._legacy_checkbox(c)
                    if box is not None:
                        self._box(box)
                elif kind == "separate" and self.fields:
                    self.fields[-1] = "result"
                elif kind == "end" and self.fields:
                    self.fields.pop()
            elif n == "w:fldSimple":
                self._walk(c, editable=False)
            elif n == "w:t":
                if not (self.fields and self.fields[-1] == "code"):
                    self._text(c, editable and not self.fields)
            elif n == "w:tab":
                self._emit("\t", None)
            elif n in ("w:br", "w:cr"):
                self._emit("\n", None)
            elif n == "w:noBreakHyphen":
                self._emit("-", None)
            elif n == "w:sym":
                self._emit("�", None)
            else:
                self._walk(c, editable)

    def _text(self, t: Node, editable: bool) -> None:
        text, spans = decode_with_offsets(self.data, *t.content_span)
        if text != t.text:
            raise TemplateError(f"text decoding mismatch in w:t at byte {t.start}")
        for ch, (s, e) in zip(text, spans):
            if editable and (ch in UNCHECKED_GLYPHS or ch in CHECKED_GLYPHS):
                self._box(Checkbox("glyph", self.p, ch in CHECKED_GLYPHS, [(s, e)], ["cb_glyph"]))
            else:
                self._emit(ch, CharRef(t, s, e) if editable else None)

    def _sdt_checkbox(self, sdt: Node) -> Checkbox | None:
        pr = sdt.child("w:sdtPr")
        spec = pr.child("w14:checkbox") if pr else None
        if spec is None:
            return None
        cs, us = spec.child("w14:checkedState"), spec.child("w14:uncheckedState")
        box = Checkbox(
            "sdt",
            self.p,
            False,
            checked_glyph=chr(int(cs.attrs.get("w14:val", "2612"), 16)) if cs else "☒",
            unchecked_glyph=chr(int(us.attrs.get("w14:val", "2610"), 16)) if us else "☐",
        )
        state = spec.child("w14:checked")
        content = sdt.child("w:sdtContent")
        ts = list(content.iter("w:t")) if content else []
        if state is None:
            box.unsupported = "content control has no w14:checked element"
            return box
        box.checked = state.attrs.get("w14:val", "1") in _TRUE
        if cs and us and cs.attrs.get("w14:font") != us.attrs.get("w14:font"):
            box.unsupported = "checked and unchecked glyphs use different fonts"
            return box
        if len(ts) != 1 or (content and any(content.iter("w:sym"))):
            box.unsupported = "checkbox glyph is not a single w:t character"
            return box
        glyph, spans = decode_with_offsets(self.data, *ts[0].content_span)
        if len(glyph) != 1:
            box.unsupported = "checkbox glyph is not a single character"
            return box
        box.spans = [state.span, spans[0]]
        box.roles = ["cb_tag", "cb_glyph"]
        return box

    def _legacy_checkbox(self, fld: Node) -> Checkbox | None:
        ff = fld.child("w:ffData")
        spec = ff.child("w:checkBox") if ff else None
        if spec is None:
            return None
        checked, default = spec.child("w:checked"), spec.child("w:default")
        state = checked or default
        if state is None:
            return Checkbox("legacy", self.p, False, unsupported="form field has no w:default or w:checked")
        value = state.attrs.get("w:val", "1" if state is checked else "0")
        return Checkbox("legacy", self.p, value in _TRUE, [state.span], ["cb_tag"])


def row_cells(row: Node) -> list[Node]:
    return row.children_named("w:tc")


def row_shape(row: Node) -> tuple:
    shape = []
    for tc in row_cells(row):
        pr = tc.child("w:tcPr")
        span = pr.child("w:gridSpan") if pr else None
        merge = pr.child("w:vMerge") if pr else None
        shape.append(
            (
                span.attrs.get("w:val", "1") if span else "1",
                merge.attrs.get("w:val", "continue") if merge else None,
            )
        )
    return tuple(shape)


class PartIndex:
    """One indexed WordprocessingML part (document, header or footer)."""

    def __init__(self, name: str, data: bytes, styles: Styles):
        self.name = name
        self.data = data
        self.styles = styles
        self.root = index_xml(data, name)
        if W_MAIN_DECL not in data[self.root.start : self.root.start_end]:
            raise TemplateError(f"{name}: the WordprocessingML namespace must be bound to prefix 'w'")
        revision = next(self.root.iter(*_REVISIONS), None)
        if revision is not None:
            raise TemplateError(
                f"{name}: tracked change <{revision.name}> at {revision.path()}; "
                "accept or reject all changes before the template is approved"
            )
        self.paragraphs = [p for p in self.root.iter("w:p") if p.ancestor("mc:Fallback") is None]
        walker = _Walker(data)
        self._texts = {id(p): walker.paragraph(p) for p in self.paragraphs}
        self.checkboxes = [b for p in self.paragraphs for b in self._texts[id(p)].boxes]
        self.tables = [t for t in self.root.iter("w:tbl") if t.ancestor("mc:Fallback") is None]
        self.headings: list[tuple[Node, int, str]] = []
        for p in self.paragraphs:
            level = self._heading_level(p)
            if level is not None and p.ancestor("w:tbl", "w:txbxContent") is None:
                self.headings.append((p, level, norm(self._texts[id(p)].text)))
        self._label_cells()

    def text(self, p: Node) -> ParaText:
        return self._texts[id(p)]

    def cell_text(self, tc: Node) -> str:
        return norm(" ".join(self._texts[id(p)].text for p in tc.children_named("w:p")))

    def rows(self, tbl: Node) -> list[Node]:
        return tbl.children_named("w:tr")

    def section_of(self, node: Node) -> str | None:
        current = None
        for p, _level, text in self.headings:
            if p.start > node.start:
                break
            current = text
        return current

    def scope(self, section: str | None) -> tuple[int, int]:
        """Byte range covered by the section that starts at the given heading text."""
        if section is None:
            return (0, len(self.data))
        wanted = norm(section)
        hits = [i for i, (_p, _l, text) in enumerate(self.headings) if text == wanted]
        if len(hits) != 1:
            found = "no heading" if not hits else f"{len(hits)} headings"
            raise TemplateError(f"{self.name}: {found} with text {section!r}")
        p, level, _ = self.headings[hits[0]]
        end = next((h.start for h, lvl, _ in self.headings[hits[0] + 1 :] if lvl <= level), len(self.data))
        return (p.end, end)

    def _heading_level(self, p: Node) -> int | None:
        ppr = p.child("w:pPr")
        if ppr is None:
            return None
        outline = ppr.child("w:outlineLvl")
        if outline is not None and outline.attrs.get("w:val", "").isdigit():
            level = int(outline.attrs["w:val"])
            return level + 1 if level < 9 else None
        style = ppr.child("w:pStyle")
        return self.styles.heading_level(style.attrs.get("w:val") if style else None)

    def _label_cells(self) -> None:
        for box in self.checkboxes:
            tc = box.para.ancestor("w:tc")
            if tc is None or tc.parent is None:
                continue
            cells = row_cells(tc.parent)
            idx = cells.index(tc)
            if idx > 0:
                box.row_label = self.cell_text(cells[0])
            if not box.label_after and not box.label_before and len(self.cell_text(tc)) == 0:
                after = [self.cell_text(c) for c in cells[idx + 1 :]]
                before = [self.cell_text(c) for c in reversed(cells[:idx])]
                box.cell_label = next((t for t in after + before if t), "")
