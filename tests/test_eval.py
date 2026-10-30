from pathlib import Path

import pytest

from docagent.eval import (
    GoldenCase,
    GoldenSet,
    Label,
    evaluate,
    export_labels,
    lower_bound,
    read_labels,
    sample_size,
    write_labels,
)
from tests.fixtures import review
from tests.fixtures.kb import build_kb
from tests.test_author import FakeLLM


@pytest.mark.parametrize("correct, total, ok", [(299, 299, True), (298, 298, False), (472, 473, True),
                                                 (471, 472, False), (626, 628, True), (770, 773, True)])
def test_clopper_pearson_lower_bound_matches_the_design_doc(correct, total, ok):
    assert (lower_bound(correct, total) >= 0.99) is ok


def test_sample_sizes_for_99_percent():
    assert [sample_size(e) for e in range(4)] == [299, 473, 628, 773]


def test_lower_bound_edge_cases():
    assert lower_bound(0, 0) == 0.0 and lower_bound(0, 50) == 0.0
    assert 0.94 < lower_bound(100, 100) < 0.98


def _label(kind, accepted, label, split="tune", ref="x"):
    return Label(case_id="c", split=split, region="r", kind=kind, ref=ref, text="t", chunk_id="k",
                 quote="q", doc_version="1", accepted=accepted, label=label)


def test_evaluate_counts_precision_coverage_and_errors():
    labels = [_label("prose", True, True) for _ in range(299)] + [_label("prose", False, True), _label("prose", False, None)]
    ev = evaluate(labels)
    assert ev.passed and ev.by_kind["prose"].bound >= 0.99
    assert ev.by_kind["prose"].coverage == pytest.approx(299 / 301)
    assert ev.by_kind["prose"].rejected_but_correct == 1
    labels[0] = _label("prose", True, False, ref="bad")
    ev = evaluate(labels)
    assert not ev.passed and ev.errors == [{"case_id": "c", "region": "r", "ref": "bad", "chunk_id": "k", "note": ""}]
    assert ev.to_dict()["by_kind"]["prose"]["claims_needed_at_current_errors"] == 473


def test_unlabelled_claims_block_a_pass():
    labels = [_label("prose", True, True) for _ in range(299)] + [_label("prose", True, None)]
    assert not evaluate(labels).passed


def test_test_split_is_separate():
    labels = [_label("prose", True, True, split="test") for _ in range(299)]
    assert evaluate(labels, "tune").overall.asserted == 0
    assert evaluate(labels, "test").passed


def test_export_labels_lists_every_claim_with_text(manifest, tmp_path: Path):
    golden = GoldenSet(name="demo", cases=[GoldenCase(case_id="lims", record=review.record(), kb=build_kb())])
    labels = export_labels(golden, manifest, FakeLLM())
    assert {label.kind for label in labels} == {"prose", "table_rows", "multi_select"}
    accepted = [label for label in labels if label.accepted]
    rejected = [label for label in labels if not label.accepted]
    assert accepted and rejected and all(label.label is None for label in labels)
    assert any(label.doc_version == "3.1" for label in accepted)
    path = tmp_path / "labels.jsonl"
    write_labels(labels, path)
    assert read_labels(path) == labels
