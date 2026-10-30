"""Character-level byte mapping and escaping for w:t content."""

from __future__ import annotations

import re

from docagent.errors import PlanError, TemplateError

_NAMED = {b"amp": "&", b"lt": "<", b"gt": ">", b"quot": '"', b"apos": "'"}
_INVALID_XML = re.compile("[^\t\n\r -퟿-�\U00010000-\U0010ffff]")


def decode_with_offsets(data: bytes, start: int, end: int) -> tuple[str, list[tuple[int, int]]]:
    """Decode element content and return each character's byte span in `data`."""
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    i = start
    while i < end:
        b = data[i]
        if b == 0x26:  # '&'
            j = data.index(b";", i, end)
            ref = data[i + 1 : j]
            if ref.startswith(b"#x"):
                ch = chr(int(ref[2:], 16))
            elif ref.startswith(b"#"):
                ch = chr(int(ref[1:]))
            elif ref in _NAMED:
                ch = _NAMED[ref]
            else:
                raise TemplateError(f"unknown entity &{ref.decode(errors='replace')}; at byte {i}")
            chars.append(ch)
            spans.append((i, j + 1))
            i = j + 1
        elif b == 0x3C:  # '<'
            raise TemplateError(f"markup inside text content at byte {i} (CDATA or comment)")
        elif b == 0x0D and i + 1 < end and data[i + 1] == 0x0A:
            chars.append("\n")
            spans.append((i, i + 2))
            i += 2
        else:
            n = 1 if b < 0x80 else 2 if b < 0xE0 else 3 if b < 0xF0 else 4
            chars.append(data[i : i + n].decode("utf-8"))
            spans.append((i, i + n))
            i += n
    return "".join(chars), spans


def check_text(text: str, where: str) -> None:
    """Refuse characters that cannot appear in XML 1.0 or that need run structure."""
    bad = _INVALID_XML.search(text)
    if bad:
        raise PlanError(f"{where}: character U+{ord(bad.group()):04X} is not allowed in a document")
    for ch, label in (("\n", "line break"), ("\r", "carriage return"), ("\t", "tab")):
        if ch in text:
            raise PlanError(f"{where}: {label} inside a text value; split it into separate paragraphs")
    if any(ch in text for ch in "☐☑☒"):
        raise PlanError(f"{where}: checkbox glyphs cannot appear in inserted text; they would read as new options")


def escape(text: str) -> bytes:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").encode("utf-8")
