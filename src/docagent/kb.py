"""Knowledge base of de-identified chunks from a customer's past validation documents.

Phase 2 (quarantine store and layered de-identification) will be the only writer
of this store. Until then the loader accepts only chunks that carry the
`deidentified: true` attestation, which fixtures set explicitly.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from docagent.manifest import canonical_sha256
from docagent.plan import SystemRecord

_Strict = ConfigDict(extra="forbid")
MIN_MASKED_VALUE = 3


class Chunk(BaseModel):
    model_config = _Strict

    chunk_id: str
    system_id: str  # which system this text describes; claims may cite only the target's chunks
    doc_type: str
    doc_title: str
    doc_version: str  # version of the source document, shown to the drafter and the judge
    status: Literal["approved", "superseded", "draft"]  # only approved documents become evidence
    section: str
    text: str
    deidentified: Literal[True]


class KnowledgeBase(BaseModel):
    model_config = _Strict

    chunks: list[Chunk]

    def get(self, chunk_id: str) -> Chunk | None:
        return next((c for c in self.chunks if c.chunk_id == chunk_id), None)

    def for_system(self, system_id: str, doc_types: list[str] | None = None) -> list[Chunk]:
        """Metadata filter first: only the target system's approved documents can become evidence."""
        return [
            c for c in self.chunks
            if c.system_id == system_id and c.status == "approved"
            and (doc_types is None or c.doc_type in doc_types)
        ]

    def excluded_for(self, system_id: str) -> list[str]:
        return [c.chunk_id for c in self.chunks if c.system_id == system_id and c.status != "approved"]

    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


def record_tokens(record: SystemRecord) -> list[tuple[str, str]]:
    """(value, token) pairs for every system-record value, longest value first."""
    pairs: list[tuple[str, str]] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, (str, int, float)) and not isinstance(node, bool):
            value = str(node).strip()
            if len(value) >= MIN_MASKED_VALUE:
                pairs.append((value, "{{" + path + "}}"))

    walk(record.system, "system")
    return sorted(pairs, key=lambda p: -len(p[0]))


def mask_record_values(text: str, record: SystemRecord) -> str:
    """Replace system-record values with their tokens, so the model copies tokens, not values.

    The writer binds the tokens from the record after verification, which keeps a
    mistyped version or ID out of the document.
    """
    for value, token in record_tokens(record):
        text = re.sub(rf"(?<![\w.]){re.escape(value)}(?!\w|\.\w)", token, text)
    return text
