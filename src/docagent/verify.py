"""Deterministic claim checks. Any failure turns the claim into a gap marker.

1. quote_in_chunk   the quote appears verbatim in the cited chunk
2. same_system      the cited chunk describes the target system
3. facts_sourced    every number, acronym and proper name in the claim appears in the quote
4. entailed         a separate judge call says the quote supports the whole claim (see author.py)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from docagent.kb import Chunk, KnowledgeBase

MIN_QUOTE_CHARS = 12
_TOKEN = re.compile(r"\{\{[^{}]*\}\}")
_NUMBER = re.compile(r"\d+(?:[.,:/-]\d+)*")
_ACRONYM = re.compile(r"\b[A-Z][A-Z0-9]+(?:[-+][A-Z0-9]+)*\+?")
_CAPITALISED = re.compile(r"\b[A-Z][a-z][A-Za-z-]*")
_FOLD = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})
# Capitalised words that carry no factual content on their own.
_COMMON = {"The", "This", "That", "These", "Those", "It", "Its", "A", "An", "In", "On", "For", "And", "Or", "Yes", "No"}


def normalise(text: str) -> str:
    return " ".join(text.translate(_FOLD).split()).casefold()


@dataclass
class ClaimCheck:
    chunk_id: str
    quote: str
    quote_in_chunk: bool = False
    same_system: bool = False
    facts_sourced: bool = False
    unsourced: list[str] = field(default_factory=list)
    text_allowed: bool = True  # plain words and {{system.*}} tokens only; no surrogates or markup
    entailed: bool | None = None  # filled by the judge

    @property
    def deterministic_ok(self) -> bool:
        return self.quote_in_chunk and self.same_system and self.facts_sourced and self.text_allowed

    @property
    def accepted(self) -> bool:
        return self.deterministic_ok and self.entailed is True

    def as_audit(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "checks": {
                "quote_in_chunk": self.quote_in_chunk,
                "same_system": self.same_system,
                "facts_sourced": self.facts_sourced,
                "text_allowed": self.text_allowed,
                "entailed": self.entailed,
            },
            "unsourced_count": len(self.unsourced),
            "accepted": self.accepted,
        }


def unsourced_terms(claim: str, quote: str) -> list[str]:
    """Numbers, acronyms and proper names in the claim that the quote does not contain."""
    body = _TOKEN.sub(" ", claim)
    terms = set(_NUMBER.findall(body)) | set(_ACRONYM.findall(body))
    for sentence in re.split(r"(?<=[.!?;:])\s+", body.strip()):
        sentence = sentence.strip()
        for m in _CAPITALISED.finditer(sentence):
            if m.start() > 0 and m.group() not in _COMMON:  # a sentence's first word is capitalised anyway
                terms.add(m.group())
    haystack = normalise(_TOKEN.sub(" ", quote))
    return sorted(t for t in terms if normalise(t) not in haystack)


def check_claim(
    claim: str, chunk_id: str, quote: str, target_system: str, kb: KnowledgeBase, masked: dict[str, str]
) -> ClaimCheck:
    """Run checks 1-3. `masked` maps chunk ID to the masked text the model was shown."""
    result = ClaimCheck(chunk_id=chunk_id, quote=quote)
    chunk: Chunk | None = kb.get(chunk_id)
    if chunk is None:
        return result
    result.same_system = chunk.system_id == target_system
    shown = masked.get(chunk_id, chunk.text)
    q = normalise(quote)
    result.quote_in_chunk = len(q) >= MIN_QUOTE_CHARS and q in normalise(shown)
    result.unsourced = unsourced_terms(claim, quote)
    result.facts_sourced = not result.unsourced
    return result
