"""Local stress test: 15 synthetic systems with traps, graded against an answer key.

    uv run python examples/stress_test.py [--cases 15]

Writes examples/out/stress/{golden.json,labels.jsonl,eval.tune.json,eval.test.json}.
Needs Anthropic credentials. This measures whether the pipeline's checks hold against
known traps; it is not a substitute for SME-labelled customer documents.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from docagent.cli import load_dotenv
from docagent.eval import dump, evaluate, export_labels, read_labels, summarise, write_labels
from docagent.llm import AnthropicLLM
from docagent.manifest import Manifest
from tests.fixtures.synthetic import auto_label, build, golden_set

OUT = ROOT / "examples" / "out" / "stress"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=15)
    ap.add_argument("--reuse", action="store_true", help="skip the model and regrade the existing labels")
    args = ap.parse_args()
    load_dotenv()
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = Manifest.model_validate_json((ROOT / "examples" / "out" / "manifest.json").read_bytes())
    cases = build(args.cases)
    (OUT / "golden.json").write_text(golden_set(cases).model_dump_json(indent=2), encoding="utf-8")
    if args.reuse:
        labels = read_labels(OUT / "labels.raw.jsonl")
    else:
        labels = export_labels(golden_set(cases), manifest, AnthropicLLM())
        write_labels(labels, OUT / "labels.raw.jsonl")
    graded = auto_label(labels, {c.case.case_id: c.truth for c in cases})
    write_labels(graded, OUT / "labels.jsonl")
    for split in ("tune", "test"):
        ev = evaluate(graded, split)
        print(summarise(ev))
        (OUT / f"eval.{split}.json").write_text(dump(ev), encoding="utf-8")
        for e in ev.errors:
            label = next(x for x in graded if (x.case_id, x.ref, x.region) == (e["case_id"], e["ref"], e["region"]))
            print(f"    ERROR {e['case_id']} {e['region']}/{e['ref']}: {label.text[:90]!r}  [{label.note}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
