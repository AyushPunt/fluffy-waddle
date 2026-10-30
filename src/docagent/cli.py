"""Command line for the template engine.

    docagent inspect  TEMPLATE.docx
    docagent propose  TEMPLATE.docx -o manifest.json
    docagent approve  manifest.json --template TEMPLATE.docx --by "Name (role)"
    docagent author   --manifest m.json --record r.json --kb kb.json -o plan.json [--audit audit.json]
    docagent label    --golden golden.json --manifest m.json -o labels.jsonl      (SMEs then fill `label`)
    docagent evaluate labels.jsonl [--split tune|test] [--target 0.99] [--report eval.json]
    docagent fill     --template T.docx --manifest m.json --record r.json [--plan p.json] [--library l.json] -o out.docx
    docagent gate     --template T.docx --output out.docx --manifest m.json [--report report.json]

Output is limited to identifiers, statuses and counts. Document content is never
printed by fill or gate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from docagent.author import author
from docagent.compiler import TemplateDoc, approve, propose
from docagent.errors import DocAgentError
from docagent.eval import dump, evaluate, export_labels, load_golden, read_labels, summarise, write_labels
from docagent.gate import run_gate
from docagent.kb import KnowledgeBase
from docagent.llm import DEFAULT_MODEL, AnthropicLLM
from docagent.manifest import Manifest
from docagent.ooxml.wordml import norm
from docagent.plan import BoilerplateLibrary, FillPlan, SystemRecord
from docagent.writer import fill


def _read(path: str) -> bytes:
    return Path(path).read_bytes()


def _write(path: str, data: bytes | str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8")
    else:
        p.write_bytes(data)


def _inspect(args) -> int:
    doc = TemplateDoc(_read(args.template), Path(args.template).name)
    for part in doc.text_parts():
        pi = doc.part(part)
        print(f"== {part}")
        for p in pi.paragraphs:
            pt = pi.text(p)
            text = norm(pt.text)
            if not text and not pt.boxes:
                continue
            level = next((lvl for h, lvl, _ in pi.headings if h is p), None)
            marker = f"H{level}" if level else "  "
            boxes = f"  [{len(pt.boxes)} checkbox]" if pt.boxes else ""
            print(f"{marker} {text[:100]}{boxes}")
        for i, t in enumerate(pi.tables, 1):
            rows = pi.rows(t)
            header = [pi.cell_text(c) for c in rows[0].children_named("w:tc")] if rows else []
            print(f"table {i}: {len(rows)} rows, header {header}")
    return 0


def _propose(args) -> int:
    doc = TemplateDoc(_read(args.template), Path(args.template).name)
    manifest = propose(doc)
    _write(args.output, manifest.dump())
    review = sum(1 for r in manifest.regions if r.needs_review)
    print(f"proposed {len(manifest.regions)} regions ({review} need review) -> {args.output}")
    for w in manifest.warnings:
        print(f"warning: {w}")
    return 0


def _approve(args) -> int:
    manifest = Manifest.model_validate_json(_read(args.manifest))
    approved = approve(manifest, _read(args.template), args.by)
    out = args.output or args.manifest
    _write(out, approved.dump())
    print(f"approved {len(approved.regions)} regions by {args.by}; manifest sha256 {approved.approval.manifest_sha256}")
    return 0


def _fill(args) -> int:
    manifest = Manifest.model_validate_json(_read(args.manifest))
    record = SystemRecord.model_validate_json(_read(args.record))
    plan = FillPlan.model_validate_json(_read(args.plan)) if args.plan else None
    library = BoilerplateLibrary.model_validate_json(_read(args.library)) if args.library else None
    result = fill(_read(args.template), manifest, record, plan, library)
    _write(args.output, result.output)
    report_path = args.report or str(Path(args.output).with_suffix(".report.json"))
    _write(report_path, json.dumps(result.report, indent=2, ensure_ascii=False) + "\n")
    r = result.report
    print(f"wrote {args.output} (sha256 {r['output_sha256'][:16]}…), gate passed")
    print(f"gaps: {len(r['gaps'])}  blocking items: {len(r['blocking'])}  awaiting human: {r['awaiting_human']}")
    for region in r["regions"]:
        print(f"  {region['status']:<15} {region['id']}")
    print(f"report -> {report_path}")
    return 0


def _author(args) -> int:
    manifest = Manifest.model_validate_json(_read(args.manifest))
    record = SystemRecord.model_validate_json(_read(args.record))
    kb = KnowledgeBase.model_validate_json(_read(args.kb))
    result = author(manifest, record, kb, AnthropicLLM(model=args.model), samples=args.samples)
    _write(args.output, result.plan.model_dump_json(indent=2) + "\n")
    audit_path = args.audit or str(Path(args.output).with_suffix(".audit.json"))
    _write(audit_path, json.dumps(result.audit, indent=2) + "\n")
    s = result.audit["summary"]
    print(f"drafted with {args.model}: {s['accepted']}/{s['claims']} claims verified, "
          f"{s['declared_gaps']} gaps declared by the model")
    for region in result.audit["regions"]:
        ok = sum(1 for c in region["claims"] if c["accepted"])
        note = f"  ({region['note']})" if region["note"] else ""
        print(f"  {region['region']:<26} {ok}/{len(region['claims'])} verified, {len(region['calls'])} calls{note}")
    print(f"plan -> {args.output}\naudit -> {audit_path}")
    return 0


def _label(args) -> int:
    golden = load_golden(Path(args.golden))
    manifest = Manifest.model_validate_json(_read(args.manifest))
    labels = export_labels(golden, manifest, AnthropicLLM(model=args.model))
    write_labels(labels, Path(args.output))
    accepted = sum(1 for x in labels if x.accepted)
    print(f"{len(labels)} claims from {len(golden.cases)} cases ({accepted} accepted, {len(labels) - accepted} gapped)")
    print(f"labels -> {args.output}; set \"label\" to true or false on every line, then run `docagent evaluate`")
    return 0


def _evaluate(args) -> int:
    ev = evaluate(read_labels(Path(args.labels)), args.split, args.target)
    print(summarise(ev))
    if args.report:
        _write(args.report, dump(ev))
        print(f"report -> {args.report}")
    return 0 if ev.passed else 1


def _gate(args) -> int:
    manifest = Manifest.model_validate_json(_read(args.manifest))
    witness = json.loads(_read(args.report))["witness"] if args.report else None
    result = run_gate(_read(args.template), _read(args.output), manifest, witness)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.passed else 1


def load_dotenv(path: Path = Path(".env")) -> None:
    """Read KEY=VALUE lines from .env without overriding variables that are already set."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="docagent", description="Template engine for regulated documents")
    sub = ap.add_subparsers(dest="command", required=True)

    s = sub.add_parser("inspect", help="show paragraphs, headings, checkboxes and tables")
    s.add_argument("template")
    s.set_defaults(run=_inspect)

    s = sub.add_parser("propose", help="draft a region manifest from the template")
    s.add_argument("template")
    s.add_argument("-o", "--output", required=True)
    s.set_defaults(run=_propose)

    s = sub.add_parser("approve", help="resolve and pin a reviewed manifest")
    s.add_argument("manifest")
    s.add_argument("--template", required=True)
    s.add_argument("--by", required=True, help="approver name and role")
    s.add_argument("-o", "--output", help="defaults to overwriting the manifest")
    s.set_defaults(run=_approve)

    s = sub.add_parser("fill", help="fill approved regions; refuses output the gate rejects")
    s.add_argument("--template", required=True)
    s.add_argument("--manifest", required=True)
    s.add_argument("--record", required=True)
    s.add_argument("--plan")
    s.add_argument("--library")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--report")
    s.set_defaults(run=_fill)

    s = sub.add_parser("author", help="draft a fill plan with the model, verifying every claim")
    s.add_argument("--manifest", required=True)
    s.add_argument("--record", required=True)
    s.add_argument("--kb", required=True, help="knowledge base of past documents (JSON)")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--audit")
    s.add_argument("--model", default=DEFAULT_MODEL)
    s.add_argument("--samples", type=int, default=3, help="samples that must agree on a single choice")
    s.set_defaults(run=_author)

    s = sub.add_parser("label", help="run the golden set and export every claim for SME labelling")
    s.add_argument("--golden", required=True)
    s.add_argument("--manifest", required=True)
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--model", default=DEFAULT_MODEL)
    s.set_defaults(run=_label)

    s = sub.add_parser("evaluate", help="precision with 95% lower bound per region kind; exit 1 below target")
    s.add_argument("labels")
    s.add_argument("--split", default="tune", choices=["tune", "test"])
    s.add_argument("--target", type=float, default=0.99)
    s.add_argument("--report")
    s.set_defaults(run=_evaluate)

    s = sub.add_parser("gate", help="verify an output against its template and manifest")
    s.add_argument("--template", required=True)
    s.add_argument("--output", required=True)
    s.add_argument("--manifest", required=True)
    s.add_argument("--report", help="fill report whose witness to use")
    s.set_defaults(run=_gate)

    args = ap.parse_args(argv)
    try:
        return args.run(args)
    except DocAgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
