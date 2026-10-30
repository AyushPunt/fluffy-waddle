"""End-to-end demo of the phase 1 template engine, driven through the CLI.

    uv run python examples/demo.py

Writes everything to examples/out/: the template, the proposed and approved
manifests, the run inputs, the filled draft and its report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from docagent.cli import main
from docagent.manifest import Manifest
from tests.fixtures import review
from tests.fixtures.csra import build_template

OUT = ROOT / "examples" / "out"


def run(*args: str) -> None:
    print(f"\n$ docagent {' '.join(args)}")
    code = main(list(args))
    if code != 0:
        raise SystemExit(code)


def main_demo() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    template = OUT / "csra_template.docx"
    template.write_bytes(build_template())

    run("inspect", str(template))
    run("propose", str(template), "-o", str(OUT / "manifest.proposed.json"))

    # The template owner reviews the proposal: maps facts to system-record fields,
    # turns the purpose placeholder into approved boilerplate and sets row numbering.
    reviewed = review.reviewed_manifest(template.read_bytes())
    (OUT / "manifest.json").write_text(reviewed.dump(), encoding="utf-8")
    run("approve", str(OUT / "manifest.json"), "--template", str(template), "--by", "Template Owner (QA)")

    approved = Manifest.model_validate_json((OUT / "manifest.json").read_bytes())
    (OUT / "record.json").write_text(review.record().model_dump_json(indent=2), encoding="utf-8")
    (OUT / "library.json").write_text(review.library().model_dump_json(indent=2), encoding="utf-8")
    # In phase 3 the grounded LLM pipeline produces this plan; here it is hand-written.
    (OUT / "plan.json").write_text(review.plan(approved).model_dump_json(indent=2), encoding="utf-8")

    run(
        "fill", "--template", str(template), "--manifest", str(OUT / "manifest.json"),
        "--record", str(OUT / "record.json"), "--plan", str(OUT / "plan.json"),
        "--library", str(OUT / "library.json"), "-o", str(OUT / "csra_filled.docx"),
    )
    run(
        "gate", "--template", str(template), "--output", str(OUT / "csra_filled.docx"),
        "--manifest", str(OUT / "manifest.json"), "--report", str(OUT / "csra_filled.report.json"),
    )
    report = json.loads((OUT / "csra_filled.report.json").read_text())
    print(f"\ngap markers: {[g['description'] for g in report['gaps']]}")


if __name__ == "__main__":
    main_demo()
