"""Region manifest: the approved contract between a template version and the writer.

A manifest names every region that may change, how it may change, and (once
approved) the exact byte spans that the immutability gate will mask. Nothing
outside those spans can change.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RegionKind(StrEnum):
    BOILERPLATE = "boilerplate"
    HUMAN_ONLY = "human_only"
    FACT = "fact"
    PROSE = "prose"
    TABLE_ROWS = "table_rows"
    SINGLE_CHOICE = "single_choice"
    MULTI_SELECT = "multi_select"


class Mutation(StrEnum):
    NONE = "none"
    INLINE_TEXT = "inline_text"  # replace placeholder characters inside existing runs
    PARAGRAPHS = "paragraphs"  # replace a placeholder-only paragraph with authored paragraphs
    ROWS = "rows"  # clone a prototype row
    CHECKBOX_STATE = "checkbox_state"  # flip existing checkboxes


ALLOWED_MUTATIONS: dict[RegionKind, set[Mutation]] = {
    RegionKind.BOILERPLATE: {Mutation.INLINE_TEXT, Mutation.PARAGRAPHS},
    RegionKind.HUMAN_ONLY: {Mutation.NONE},
    RegionKind.FACT: {Mutation.INLINE_TEXT},
    RegionKind.PROSE: {Mutation.INLINE_TEXT, Mutation.PARAGRAPHS},
    RegionKind.TABLE_ROWS: {Mutation.ROWS},
    RegionKind.SINGLE_CHOICE: {Mutation.CHECKBOX_STATE, Mutation.INLINE_TEXT},
    RegionKind.MULTI_SELECT: {Mutation.CHECKBOX_STATE},
}

_Strict = ConfigDict(extra="forbid")


class Anchor(BaseModel):
    model_config = _Strict

    part: str = "word/document.xml"
    section: str | None = None  # heading text that scopes the search
    text: str | None = None  # placeholder text, matched across run boundaries
    occurrence: int | Literal["all"] | None = None  # None: the match must be unique
    table_header: list[str] | None = None  # first-row cell texts of the target table
    options: list[str] | None = None  # checkbox labels, or the option set for an inline choice
    items: list[str] | None = None  # row labels for a yes/no checklist
    layout: Literal["single_box", "yes_no"] = "single_box"
    label_side: Literal["after", "before"] = "after"
    yes_label: str = "Yes"
    no_label: str = "No"


class Column(BaseModel):
    model_config = _Strict

    name: str
    source: Literal["plan", "row_number"] = "plan"
    format: str = "{n}"
    start: int = 1


class ResolvedRef(BaseModel):
    """Where a region sits in the approved template; recomputed and compared on every run."""

    model_config = _Strict

    part: str
    paths: list[str]
    spans: list[tuple[int, int]]
    context_sha256: str


class Region(BaseModel):
    model_config = _Strict

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    kind: RegionKind
    mutation: Mutation
    anchor: Anchor
    source: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
    library_key: str | None = None
    library_sha256: str | None = None
    columns: list[Column] | None = None
    row_mode: Literal["replace_prototype", "append"] | None = None
    prototype_row: int = -1
    run_props: Literal["inherit", "paragraph"] = "inherit"
    hints: dict[str, Any] = Field(default_factory=dict)
    needs_review: list[str] = Field(default_factory=list)
    resolved: ResolvedRef | None = None

    @model_validator(mode="after")
    def _mutation_fits_kind(self) -> Region:
        if self.mutation not in ALLOWED_MUTATIONS[self.kind]:
            allowed = ", ".join(sorted(ALLOWED_MUTATIONS[self.kind]))
            raise ValueError(f"region {self.id}: {self.kind} allows mutation {allowed}, not {self.mutation}")
        return self

    def completeness_problems(self) -> list[str]:
        """What still blocks approval. Proposals may be incomplete; approved manifests may not."""
        a = self.anchor
        problems = [f"needs review: {item}" for item in self.needs_review]
        if self.mutation in (Mutation.INLINE_TEXT, Mutation.PARAGRAPHS) and not a.text:
            problems.append("anchor.text is required")
        if self.kind == RegionKind.FACT and not self.source:
            problems.append("fact regions need a source path into the system record")
        if self.kind == RegionKind.BOILERPLATE and not (self.library_key and self.library_sha256):
            problems.append("boilerplate regions need library_key and library_sha256")
        if self.kind == RegionKind.TABLE_ROWS and not (a.table_header and self.columns and self.row_mode):
            problems.append("table_rows regions need anchor.table_header, columns and row_mode")
        if self.kind == RegionKind.SINGLE_CHOICE and not a.options:
            problems.append("single_choice regions need anchor.options")
        if self.kind == RegionKind.MULTI_SELECT:
            if a.layout == "single_box" and not a.options:
                problems.append("multi_select single_box regions need anchor.options")
            if a.layout == "yes_no" and not a.items:
                problems.append("multi_select yes_no regions need anchor.items")
        if self.kind == RegionKind.HUMAN_ONLY and not (a.text or a.table_header):
            problems.append("human_only regions need anchor.text or anchor.table_header")
        return problems


class TemplateRef(BaseModel):
    model_config = _Strict

    file_name: str
    sha256: str
    parts: dict[str, str]


class Approval(BaseModel):
    model_config = _Strict

    approved_by: str
    approved_at: str
    manifest_sha256: str


class Manifest(BaseModel):
    model_config = _Strict

    manifest_version: Literal[1] = 1
    template: TemplateRef
    regions: list[Region]
    warnings: list[str] = Field(default_factory=list)
    approval: Approval | None = None

    @model_validator(mode="after")
    def _unique_ids(self) -> Manifest:
        ids = [r.id for r in self.regions]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate region ids: {dupes}")
        return self

    def region(self, region_id: str) -> Region:
        for r in self.regions:
            if r.id == region_id:
                return r
        raise KeyError(region_id)

    def content_sha256(self) -> str:
        """Hash of everything except the approval block; the approval pins this value."""
        body = self.model_dump(mode="json", exclude={"approval", "warnings"})
        return canonical_sha256(body)

    def dump(self) -> str:
        return json.dumps(self.model_dump(mode="json", exclude_none=True), indent=2, ensure_ascii=False) + "\n"


def canonical_sha256(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()
