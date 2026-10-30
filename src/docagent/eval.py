"""Golden-set evaluation: measure claim precision on SME labels and prove the 99% target.

Flow
  1. `export_labels` runs the drafter on each golden case and writes one line per
     claim the pipeline accepted, with its text and quote, for SMEs to label.
  2. SMEs set `label` to true (correct) or false (wrong) on every accepted claim.
  3. `evaluate` computes precision per region kind with a one-sided 95%
     Clopper-Pearson lower bound, coverage, and a pass/fail against the target.

Precision counts only asserted claims. A gap marker is abstention, not an error.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from docagent.author import author
from docagent.errors import DocAgentError
from docagent.kb import KnowledgeBase
from docagent.llm import LLM
from docagent.manifest import Manifest
from docagent.plan import SystemRecord

TARGET = 0.99
CONFIDENCE = 0.95
_Strict = ConfigDict(extra="forbid")


class GoldenCase(BaseModel):
    model_config = _Strict

    case_id: str
    split: str = "tune"  # "tune" guides fixes; "test" is touched only for the final verdict
    record: SystemRecord
    kb: KnowledgeBase


class GoldenSet(BaseModel):
    model_config = _Strict

    name: str
    cases: list[GoldenCase]


class Label(BaseModel):
    model_config = _Strict

    case_id: str
    split: str
    region: str
    kind: str
    ref: str
    text: str
    chunk_id: str
    quote: str
    doc_version: str
    accepted: bool
    decision: str = ""  # checklist answer the claim supports (multi_select only)
    label: bool | None = None  # SME verdict on the claim; None until labelled
    note: str = ""


# --- statistics ------------------------------------------------------------------------------------


def _log_binom_pmf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 0.0 if k == 0 else -math.inf
    if p >= 1.0:
        return 0.0 if k == n else -math.inf
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * math.log(p) + (n - k) * math.log1p(-p)


def _binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    return sum(math.exp(_log_binom_pmf(i, n, p)) for i in range(k, n + 1))


def lower_bound(correct: int, total: int, confidence: float = CONFIDENCE) -> float:
    """One-sided Clopper-Pearson lower bound on precision: the smallest p with P(X >= correct) >= 1 - confidence."""
    if total == 0:
        return 0.0
    if correct == 0:
        return 0.0
    alpha = 1.0 - confidence
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if _binom_sf(correct, total, mid) < alpha:
            lo = mid
        else:
            hi = mid
    return lo


def sample_size(errors: int, target: float = TARGET, confidence: float = CONFIDENCE) -> int:
    """Smallest number of labelled claims that proves `target` with exactly `errors` wrong."""
    n = errors + 1
    while lower_bound(n - errors, n, confidence) < target:
        n += 1
    return n


# --- labelling -------------------------------------------------------------------------------------


def export_labels(golden: GoldenSet, manifest: Manifest, llm: LLM, judge: LLM | None = None) -> list[Label]:
    labels: list[Label] = []
    for case in golden.cases:
        result = author(manifest, case.record, case.kb, llm, judge)
        kinds = {r.id: r.kind.value for r in manifest.regions}
        for region_id, d in result.drafts:
            chunk = case.kb.get(d.chunk_id)
            labels.append(
                Label(
                    case_id=case.case_id,
                    split=case.split,
                    region=region_id,
                    kind=kinds[region_id],
                    ref=d.ref,
                    text=d.text,
                    chunk_id=d.chunk_id,
                    quote=d.quote,
                    doc_version=chunk.doc_version if chunk else "",
                    accepted=d.accepted,
                    decision=d.decision,
                )
            )
    return labels


def write_labels(labels: list[Label], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for label in labels:
            f.write(label.model_dump_json() + "\n")


def read_labels(path: Path) -> list[Label]:
    return [Label.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- evaluation ------------------------------------------------------------------------------------


@dataclass
class Bucket:
    asserted: int = 0
    correct: int = 0
    unlabelled: int = 0
    rejected: int = 0  # claims the pipeline turned into gaps
    rejected_but_correct: int = 0  # SME says the rejected claim was fine: lost coverage, not an error

    @property
    def precision(self) -> float | None:
        return self.correct / self.asserted if self.asserted else None

    @property
    def bound(self) -> float:
        return lower_bound(self.correct, self.asserted)

    @property
    def coverage(self) -> float | None:
        total = self.asserted + self.rejected
        return self.asserted / total if total else None

    def to_dict(self, target: float) -> dict[str, Any]:
        return {
            "asserted": self.asserted,
            "correct": self.correct,
            "errors": self.asserted - self.correct,
            "unlabelled": self.unlabelled,
            "precision": round(self.precision, 4) if self.precision is not None else None,
            "lower_bound_95": round(self.bound, 4),
            "meets_target": self.asserted > 0 and self.unlabelled == 0 and self.bound >= target,
            "rejected": self.rejected,
            "rejected_but_correct": self.rejected_but_correct,
            "coverage": round(self.coverage, 4) if self.coverage is not None else None,
            "claims_needed_at_current_errors": sample_size(self.asserted - self.correct, target),
        }


@dataclass
class Evaluation:
    split: str
    target: float
    overall: Bucket = field(default_factory=Bucket)
    by_kind: dict[str, Bucket] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        kinds = list(self.by_kind.values())
        return bool(kinds) and all(b.asserted > 0 and b.unlabelled == 0 and b.bound >= self.target for b in kinds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "split": self.split,
            "target": self.target,
            "confidence": CONFIDENCE,
            "passed": self.passed,
            "overall": self.overall.to_dict(self.target),
            "by_kind": {k: b.to_dict(self.target) for k, b in sorted(self.by_kind.items())},
            "errors": self.errors,
        }


def evaluate(labels: list[Label], split: str = "tune", target: float = TARGET) -> Evaluation:
    ev = Evaluation(split, target)
    for label in labels:
        if label.split != split:
            continue
        buckets = (ev.overall, ev.by_kind.setdefault(label.kind, Bucket()))
        for b in buckets:
            if not label.accepted:
                b.rejected += 1
                if label.label is True:
                    b.rejected_but_correct += 1
                continue
            b.asserted += 1
            if label.label is None:
                b.unlabelled += 1
            elif label.label:
                b.correct += 1
        if label.accepted and label.label is False:
            ev.errors.append({"case_id": label.case_id, "region": label.region, "ref": label.ref,
                              "chunk_id": label.chunk_id, "note": label.note})
    return ev


def load_golden(path: Path) -> GoldenSet:
    try:
        return GoldenSet.model_validate_json(path.read_bytes())
    except ValueError as exc:
        raise DocAgentError(f"{path}: not a valid golden set ({exc})") from None


def summarise(ev: Evaluation) -> str:
    lines = [f"split={ev.split}  target={ev.target:.0%}  {'PASS' if ev.passed else 'FAIL'}"]
    rows = [("overall", ev.overall), *sorted(ev.by_kind.items())]
    for name, b in rows:
        if b.asserted == 0 and b.rejected == 0:
            continue
        prec = f"{b.precision:.2%}" if b.precision is not None else "n/a"
        cov = f"{b.coverage:.0%}" if b.coverage is not None else "n/a"
        need = sample_size(b.asserted - b.correct, ev.target)
        flag = "ok " if (b.asserted and not b.unlabelled and b.bound >= ev.target) else "   "
        lines.append(
            f"  {flag}{name:<14} asserted {b.asserted:>4}  errors {b.asserted - b.correct:>3}  "
            f"precision {prec:>7}  95% lower bound {b.bound:.2%}  coverage {cov:>4}"
            + (f"  unlabelled {b.unlabelled}" if b.unlabelled else "")
            + (f"  need {need} claims at this error count" if b.bound < ev.target else "")
        )
    return "\n".join(lines)


def dump(ev: Evaluation) -> str:
    return json.dumps(ev.to_dict(), indent=2) + "\n"
