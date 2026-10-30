"""Inputs to a fill run: system record, boilerplate library and the fill plan.

The fill plan carries only what a model may author (prose, table cells, choices).
Facts and boilerplate never pass through it: code copies them from the system
record and the approved library. Text may contain two tokens:

    {{system.version}}           late-bound fact, substituted from the system record
    {{GAP: what is missing}}     explicit gap; the writer numbers it [GAP-n: ... — SME input required]
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from docagent.errors import PlanError
from docagent.manifest import canonical_sha256
from docagent.ooxml.text import check_text

_Strict = ConfigDict(extra="forbid")


class SystemRecord(BaseModel):
    model_config = _Strict

    record_id: str
    record_version: str
    system: dict[str, Any]

    def lookup(self, path: str) -> str | None:
        node: Any = {"system": self.system}
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        if isinstance(node, bool) or not isinstance(node, (str, int, float)):
            return None
        value = str(node).strip()
        return value or None

    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class LibraryEntry(BaseModel):
    model_config = _Strict

    paragraphs: list[str] = Field(min_length=1)
    version: str
    approved_by: str
    sha256: str


class BoilerplateLibrary(BaseModel):
    model_config = _Strict

    entries: dict[str, LibraryEntry]

    def text(self, key: str, pinned_sha256: str) -> list[str]:
        entry = self.entries.get(key)
        if entry is None:
            raise PlanError(f"boilerplate {key!r} is not in the library")
        actual = entry_sha256(entry.paragraphs)
        if actual != entry.sha256:
            raise PlanError(f"boilerplate {key!r} text does not match its recorded hash")
        if actual != pinned_sha256:
            raise PlanError(f"boilerplate {key!r} is not the version pinned by the approved manifest")
        return entry.paragraphs


def entry_sha256(paragraphs: list[str]) -> str:
    return canonical_sha256(paragraphs)


class ProseValue(BaseModel):
    model_config = _Strict
    paragraphs: list[str]


class RowsValue(BaseModel):
    model_config = _Strict
    rows: list[dict[str, str]]


class ChoiceValue(BaseModel):
    model_config = _Strict
    choice: str | None = None


class MultiValue(BaseModel):
    model_config = _Strict
    items: dict[str, Literal["yes", "no", "unknown"]]


class FillPlan(BaseModel):
    model_config = _Strict

    plan_version: Literal[1] = 1
    manifest_sha256: str
    regions: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


# --- text with tokens --------------------------------------------------------------------------

_TOKEN = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_GAP = re.compile(r"^\s*GAP\s*:\s*(.+?)\s*$", re.DOTALL)
_REF = re.compile(r"^\s*([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)\s*$")
MAX_GAP_DESCRIPTION = 200


@dataclass
class Seg:
    text: str
    gap: bool = False


@dataclass
class Gaps:
    items: list[dict[str, Any]] = field(default_factory=list)

    def add(self, region_id: str, description: str) -> Seg:
        description = " ".join(description.split())
        if not description or len(description) > MAX_GAP_DESCRIPTION or any(c in description for c in "[]{}"):
            raise PlanError(f"{region_id}: gap description must be 1-{MAX_GAP_DESCRIPTION} chars without brackets")
        check_text(description, f"{region_id} gap description")
        n = len(self.items) + 1
        self.items.append({"n": n, "region": region_id, "description": description})
        return Seg(f"[GAP-{n}: {description} — SME input required]", gap=True)


def expand(text: str, record: SystemRecord, gaps: Gaps, region_id: str, allow_gaps: bool = True) -> list[Seg]:
    """Split text into literal and gap segments, binding {{system.*}} facts from the record."""
    where = f"region {region_id}"
    if "[GAP-" in text:
        raise PlanError(f"{where}: write gaps as {{{{GAP: ...}}}}; the writer numbers them")
    segs: list[Seg] = []
    pos = 0
    for m in _TOKEN.finditer(text):
        if m.start() > pos:
            segs.append(Seg(text[pos : m.start()]))
        body = m.group(1)
        if (g := _GAP.match(body)) is not None:
            if not allow_gaps:
                raise PlanError(f"{where}: approved boilerplate cannot contain gap tokens")
            segs.append(gaps.add(region_id, g.group(1)))
        elif (r := _REF.match(body)) is not None:
            value = record.lookup(r.group(1))
            segs.append(Seg(value) if value is not None else gaps.add(region_id, f"{r.group(1)} is not in the system record"))
        else:
            raise PlanError(f"{where}: unknown token {{{{{body}}}}}")
        pos = m.end()
    if pos < len(text):
        segs.append(Seg(text[pos:]))
    merged: list[Seg] = []
    for s in segs:
        if not s.gap and ("{{" in s.text or "}}" in s.text):
            raise PlanError(f"{where}: unbalanced token braces")
        if merged and not s.gap and not merged[-1].gap:
            merged[-1] = Seg(merged[-1].text + s.text)
        else:
            merged.append(s)
    for s in merged:
        check_text(s.text, where)
    return merged


def plain(text: str) -> list[Seg]:
    return [Seg(text)] if text else []
