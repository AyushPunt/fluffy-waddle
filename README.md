# regulated-doc-agent

The template engine for the AI authoring agent for regulated documents. This is phase 1 of the
[system design plan](https://claude.ai/artifact/Y9ZnqefWR1gAyJ9ptsgqz7): region compiler, manifest
approval, byte-splicing writer, immutability gate and the code fillers. The LLM pipeline (phases 2 and 3)
plugs in by producing a fill plan.

## Guarantees

- **The template is never re-serialised.** The writer splices new bytes into approved spans of
  `document.xml` and header/footer parts. Every other ZIP entry is copied as its raw record, compressed
  bytes included.
- **Every output passes the immutability gate, or no output is produced.** The gate re-resolves the
  approved manifest on the template and proves that every byte outside the masked spans is identical. It
  also checks each masked fragment for markup its mutation does not allow, and compares tables, grids,
  merged cells, checkbox count and option text, section breaks and fields. There is no override flag.
- **A template change needs re-approval.** The manifest pins the SHA-256 of the file and of every part,
  plus each region's byte spans and context hash.
- **Facts and boilerplate never pass through a model.** Code copies them from the system record and the
  approved library (hash-checked). A missing value becomes a numbered gap marker
  `[GAP-n: … — SME input required]`, highlighted.
- **Unknown is never a blank box.** An undecided checklist item is left as it was and listed as a blocking
  item in the report.

## Quickstart

```bash
uv sync
uv run pytest                     # 95 tests
uv run python examples/demo.py    # full CLI flow, writes examples/out/
```

Open `examples/out/csra_filled.docx` in Word to compare it with `examples/out/csra_template.docx`.

## Workflow

```bash
docagent inspect  template.docx                                   # what the compiler sees
docagent propose  template.docx -o manifest.json                  # draft regions from the template's own signals
#   template owner edits manifest.json: kinds, fact sources, boilerplate keys, columns
docagent approve  manifest.json --template template.docx --by "Name (QA)"
docagent fill     --template template.docx --manifest manifest.json --record record.json \
                  --plan plan.json --library library.json -o draft.docx
docagent gate     --template template.docx --output draft.docx --manifest manifest.json
```

`fill` writes `draft.report.json` next to the output, with the template, manifest, record, plan and
output hashes, region statuses, gaps, blocking items and the gate result. It records identifiers and
hashes, not document content.

## Region kinds

| Kind | Mutation | Filled by | Check |
|---|---|---|---|
| `boilerplate` | `paragraphs` or `inline_text` | approved library entry | entry hash equals the pinned hash |
| `human_only` | `none` | nobody; left for signature | no other region may touch it |
| `fact` | `inline_text` | system record (`source: system.version`) | value copied, record hash logged |
| `prose` | `paragraphs` or `inline_text` | fill plan | tokens only, no markup |
| `table_rows` | `rows` | fill plan, code numbers rows | row cloned from the prototype (tcPr, gridSpan, vMerge) |
| `single_choice` | `checkbox_state` or `inline_text` | fill plan | value is in the template's option set |
| `multi_select` | `checkbox_state` (`single_box` or `yes_no`) | fill plan | unknown items block sign-off |

Anchors match placeholder text across run boundaries, so placeholders that Word splits with `w:proofErr`
or revision IDs still resolve. New text takes the placeholder run's properties.

## Fill plan

```json
{
  "plan_version": 1,
  "manifest_sha256": "<approval.manifest_sha256>",
  "regions": {
    "system_description": {"paragraphs": ["{{system.name}} records QC results.", "{{GAP: number of users}} users."]},
    "requirements_rows": {"rows": [{"requirement": "…", "gxp_impact": "Direct", "risk": "High"}]},
    "overall_risk": {"choice": "Medium"},
    "gxp_applicability": {"items": {"21 CFR Part 11": "yes", "EU GMP Annex 11": "unknown"}}
  }
}
```

`{{system.*}}` is bound late from the system record, so the model never types a fact. `{{GAP: …}}` is
numbered by the writer. A plan may not name fact, boilerplate or human-only regions.

## Drafting with the model (phase 3, first slice)

```bash
docagent author --manifest manifest.json --record record.json --kb kb.json -o plan.json
uv run python examples/demo_llm.py    # after examples/demo.py; key from ANTHROPIC_API_KEY or .env
```

Put the key in `.env` (copy `.env.example`; it is gitignored) or export `ANTHROPIC_API_KEY`.

`author` produces the same `plan.json` the writer already takes:

1. **Evidence is scoped to the target system.** Only chunks whose `system_id` equals the record's ID are
   shown, and record values inside them are masked as `{{system.*}}` tokens. The model copies tokens,
   so it cannot mistype a version or ID.
2. **The model drafts atomic claims.** One call per region with a JSON schema; every claim carries a
   chunk ID and a verbatim quote, and anything unsupported comes back as a gap item.
3. **Every claim is checked.** In code: the quote is in the cited chunk, the chunk is the target system's,
   and every number, acronym and proper name in the claim is in the quote. Then a separate judge call
   asks whether the quote supports the whole claim. Any failure turns the claim into
   `{{GAP: … (no verified evidence)}}`, and a checklist item into `unknown`.
4. **A single choice needs three samples that agree** and a verified citation; otherwise it is left
   unchosen and blocks sign-off.
5. **Audit:** `plan.audit.json` records the model, request IDs, prompt and output hashes, cited chunk
   IDs, quote hashes and each check's result. No prompt, quote or output text is stored.

The model is pinned (`claude-opus-5-5` by default) and there is no automatic fallback to another model:
a refusal leaves the region for the writer to gap. Phase 2 (de-identification) will be the only writer
of the knowledge base; until then the loader accepts only chunks marked `deidentified: true`.

## Proving 99%: the golden-set loop

99% is claim-level precision: of the claims the pipeline asserts, at least 99% are correct, shown with a
one-sided 95% Clopper-Pearson lower bound per region kind. Gap markers are abstentions and do not count
against precision; they lower coverage, which is reported separately.

```bash
docagent label    --golden golden.json --manifest manifest.json -o labels.jsonl   # one line per claim, with text
#   SMEs set "label": true|false on every accepted claim (and on rejected ones, to measure lost coverage)
docagent evaluate labels.jsonl --split tune            # fix what fails here
docagent evaluate labels.jsonl --split test            # final verdict only; never used for tuning
uv run python examples/demo_eval.py                    # demo with one case
```

| Errors among labelled claims | Claims needed for a pass |
|---|---|
| 0 | 299 |
| 1 | 473 |
| 2 | 628 |
| 3 | 773 |

`evaluate` exits 1 while any region kind has an unlabelled claim or a lower bound under the target, and
prints how many claims the current error count would need. Each error goes back into the pipeline as a
new deterministic check or prompt rule, re-measured on the tune split; the test split is read once.

**Local stress test without customer data.** `uv run python examples/stress_test.py` builds 15 synthetic
systems (`tests/fixtures/synthetic.py`), each with approved documents plus traps: another system's chunk,
a draft risk assessment, a superseded summary with a different GAMP category, surrogates, and ratings that
exist only in trap documents. An answer key grades every exported claim automatically, exactly for
structured regions and by a conservative heuristic for prose. It shows which checks leak; it does not
replace SME labels on real documents.

Evidence controls that already feed this: only the target system's **approved** documents are evidence
(drafts and superseded documents are listed in the audit as excluded), the document version travels
with every chunk to the drafter and the judge, de-identification surrogates may not be copied into a
claim, and every number, acronym and proper name in a claim must appear in its quote.

## Layout

```
src/docagent/
  ooxml/package.py   ZIP reader/writer with raw entry copy
  ooxml/xmlindex.py  expat index with exact byte spans per element
  ooxml/text.py      w:t character-to-byte mapping, escaping
  ooxml/wordml.py    paragraph text across runs, sections, tables, checkboxes
  manifest.py        region manifest schema and hashing
  compiler.py        anchor resolution, approval, proposal
  plan.py            system record, boilerplate library, fill plan, tokens
  render.py          the only XML the writer emits
  writer.py          fillers per region kind, splicing
  gate.py            immutability gate
  kb.py              knowledge base, system scoping, record-value masking
  llm.py             model gateway (Anthropic adapter), content-free call metadata
  verify.py          deterministic claim checks
  author.py          grounded drafting: draft, check, judge, plan + audit
  eval.py            golden set, SME labels, precision with 95% lower bound
  cli.py
tests/fixtures/csra.py   a Computer System Risk Assessment template with the hard cases
tests/fixtures/kb.py     synthetic de-identified past documents, plus another system's
```

## Not yet supported

These are refused or reported as warnings rather than handled:

- Content controls and bookmarks as anchors (open question 2 in the design doc). Plain-text placeholders,
  tables and checkboxes work now.
- Checkbox content controls that wrap whole paragraphs, and Wingdings `w:sym` checkboxes.
- Templates with unaccepted tracked changes, Strict OOXML, ZIP64 or encrypted packages.
- Placeholders in footnotes, endnotes, comments and text boxes.

## Phase 1 exit gate

The gate already blocks any masked-diff byte. The remaining exit criterion is to run real customer
templates through and confirm that Word opens every output without repair.
