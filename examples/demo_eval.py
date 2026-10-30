"""Golden-set loop: run the drafter on golden cases, export every claim for SME labelling, evaluate.

    uv run python examples/demo_eval.py

Writes examples/out/golden.json (one demo case) and examples/out/labels.jsonl. SMEs set
`label` on each line; `docagent evaluate labels.jsonl` then reports precision with its
95% lower bound per region kind and fails below 99%. Needs Anthropic credentials.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from docagent.cli import main
from docagent.eval import GoldenCase, GoldenSet
from tests.fixtures import review
from tests.fixtures.kb import build_kb

OUT = ROOT / "examples" / "out"


def run(*args: str) -> int:
    print(f"\n$ docagent {' '.join(args)}")
    return main(list(args))


if __name__ == "__main__":
    if not (OUT / "manifest.json").exists():
        raise SystemExit("run examples/demo.py first")
    golden = GoldenSet(name="demo", cases=[GoldenCase(case_id="lims-4.2.1", record=review.record(), kb=build_kb())])
    (OUT / "golden.json").write_text(golden.model_dump_json(indent=2), encoding="utf-8")
    if run("label", "--golden", str(OUT / "golden.json"), "--manifest", str(OUT / "manifest.json"),
           "-o", str(OUT / "labels.jsonl")) != 0:
        raise SystemExit(1)
    # Before anyone labels, every asserted claim is unlabelled, so evaluate must refuse to pass.
    run("evaluate", str(OUT / "labels.jsonl"))
