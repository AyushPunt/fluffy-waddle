"""Phase 3 first slice: the model drafts the plan, every claim is verified, the writer fills.

    uv run python examples/demo_llm.py

Needs Anthropic credentials (`ant auth login` or ANTHROPIC_API_KEY). Only the synthetic,
de-identified demo knowledge base in tests/fixtures/kb.py is sent to the model.
Run examples/demo.py first; this reuses its approved manifest, record and library.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from docagent.cli import main
from tests.fixtures.kb import build_kb

OUT = ROOT / "examples" / "out"


def run(*args: str) -> None:
    print(f"\n$ docagent {' '.join(args)}")
    code = main(list(args))
    if code != 0:
        raise SystemExit(code)


if __name__ == "__main__":
    if not (OUT / "manifest.json").exists():
        raise SystemExit("run examples/demo.py first")
    (OUT / "kb.json").write_text(build_kb().model_dump_json(indent=2), encoding="utf-8")
    run(
        "author", "--manifest", str(OUT / "manifest.json"), "--record", str(OUT / "record.json"),
        "--kb", str(OUT / "kb.json"), "-o", str(OUT / "plan.llm.json"),
    )
    run(
        "fill", "--template", str(OUT / "csra_template.docx"), "--manifest", str(OUT / "manifest.json"),
        "--record", str(OUT / "record.json"), "--plan", str(OUT / "plan.llm.json"),
        "--library", str(OUT / "library.json"), "-o", str(OUT / "csra_filled_llm.docx"),
    )
