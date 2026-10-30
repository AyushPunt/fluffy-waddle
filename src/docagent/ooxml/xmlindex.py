"""Element index with exact byte offsets for an XML part.

python-docx and lxml re-serialise on save, which changes bytes the template
owner approved. We never serialise template XML. Instead we index each
element's byte span in the original part and splice new bytes into approved
spans only.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from xml.parsers import expat

from docagent.errors import TemplateError

NS = {
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main": "w",
    "http://schemas.microsoft.com/office/word/2010/wordml": "w14",
    "http://schemas.microsoft.com/office/word/2012/wordml": "w15",
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships": "r",
    "http://schemas.openxmlformats.org/markup-compatibility/2006": "mc",
    "http://www.w3.org/XML/1998/namespace": "xml",
    "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing": "wp",
    "http://schemas.openxmlformats.org/drawingml/2006/main": "a",
    "http://schemas.microsoft.com/office/word/2010/wordprocessingShape": "wps",
    "urn:schemas-microsoft-com:vml": "v",
    "urn:schemas-microsoft-com:office:office": "o",
}
STRICT_NS = "http://purl.oclc.org/ooxml/wordprocessingml/main"

# Elements whose character data we keep; everything else in WordprocessingML is markup.
_TEXT_ELEMENTS = {"w:t", "w:instrText", "w:delText"}
_TAG = re.compile(rb"<[^>\"']*(?:(?:\"[^\"]*\"|'[^']*')[^>\"']*)*>")
_ENCODING = re.compile(rb"^\s*<\?xml[^>]*encoding=[\"']([A-Za-z0-9._-]+)[\"']")


@dataclass(eq=False)
class Node:
    name: str
    attrs: dict[str, str]
    start: int  # offset of '<' of the start tag
    start_end: int  # offset just after the start tag's '>'
    end_start: int = -1  # offset of '</' (== start_end for an empty element)
    end: int = -1  # offset just after the end tag's '>'
    parent: Node | None = field(default=None, repr=False)
    children: list[Node] = field(default_factory=list, repr=False)
    text: str = ""

    @property
    def span(self) -> tuple[int, int]:
        return (self.start, self.end)

    @property
    def content_span(self) -> tuple[int, int]:
        return (self.start_end, self.end_start)

    @property
    def is_empty(self) -> bool:
        return self.start_end == self.end

    def iter(self, *names: str) -> Iterator[Node]:
        """Descendants in document order (excluding self), optionally filtered by name."""
        stack = list(reversed(self.children))
        while stack:
            node = stack.pop()
            if not names or node.name in names:
                yield node
            stack.extend(reversed(node.children))

    def child(self, name: str) -> Node | None:
        for c in self.children:
            if c.name == name:
                return c
        return None

    def children_named(self, name: str) -> list[Node]:
        return [c for c in self.children if c.name == name]

    def ancestor(self, *names: str) -> Node | None:
        node = self.parent
        while node is not None:
            if node.name in names:
                return node
            node = node.parent
        return None

    def path(self) -> str:
        parts = []
        node: Node | None = self
        while node is not None:
            if node.parent is None:
                parts.append(f"{node.name}[1]")
            else:
                same = [c for c in node.parent.children if c.name == node.name]
                parts.append(f"{node.name}[{same.index(node) + 1}]")
            node = node.parent
        return "/" + "/".join(reversed(parts))


def _qname(expat_name: str) -> str:
    uri, sep, local = expat_name.rpartition(" ")
    if not sep:
        return expat_name
    if uri == STRICT_NS:
        raise TemplateError("Strict OOXML documents are not supported; save as a Transitional .docx")
    prefix = NS.get(uri)
    return f"{prefix}:{local}" if prefix else f"{{{uri}}}{local}"


def index_xml(data: bytes, part_name: str = "<part>") -> Node:
    """Parse an XML part and return its root element with byte spans on every node."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise TemplateError(f"{part_name}: UTF-16 parts are not supported")
    m = _ENCODING.match(data.lstrip(b"\xef\xbb\xbf"))
    if m and m.group(1).lower() not in (b"utf-8", b"utf8"):
        raise TemplateError(f"{part_name}: only UTF-8 parts are supported")

    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    stack: list[Node] = []
    text_parts: dict[int, list[str]] = {}
    root: list[Node] = []

    def refuse_doctype(*_args):
        raise TemplateError(f"{part_name}: DOCTYPE declarations are not allowed")

    def start(name: str, attrs: dict[str, str]) -> None:
        i = parser.CurrentByteIndex
        m = _TAG.match(data, i)
        if m is None:
            raise TemplateError(f"{part_name}: cannot delimit start tag at byte {i}")
        node = Node(_qname(name), {_qname(k): v for k, v in attrs.items()}, i, m.end())
        if data[m.end() - 2 : m.end()] == b"/>":
            node.end_start = node.end = m.end()
        if stack:
            node.parent = stack[-1]
            stack[-1].children.append(node)
        else:
            root.append(node)
        stack.append(node)

    def end(_name: str) -> None:
        node = stack.pop()
        if node.end < 0:
            k = parser.CurrentByteIndex
            node.end_start = k
            node.end = data.index(b">", k) + 1
        parts = text_parts.pop(id(node), None)
        if parts:
            node.text = "".join(parts)

    def chars(text: str) -> None:
        if stack and stack[-1].name in _TEXT_ELEMENTS:
            text_parts.setdefault(id(stack[-1]), []).append(text)

    parser.StartDoctypeDeclHandler = refuse_doctype
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise TemplateError(f"{part_name}: not well-formed XML ({exc})") from exc
    return root[0]
