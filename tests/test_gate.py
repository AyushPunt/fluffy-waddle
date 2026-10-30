"""The gate must reject every change outside approved regions, and odd changes inside them."""

import pytest

from docagent.gate import run_gate
from docagent.ooxml.package import Package


def _tamper(output: bytes, old: bytes, new: bytes, part: str = "word/document.xml") -> bytes:
    pkg = Package.from_bytes(output)
    data = pkg.part(part)
    assert data.count(old) >= 1, old
    return pkg.rebuild({part: data.replace(old, new, 1)})


def test_genuine_output_passes_without_a_witness(template, manifest, filled):
    result = run_gate(template, filled.output, manifest)
    assert result.passed, result.failures


def test_unchanged_template_passes(template, manifest):
    assert run_gate(template, template, manifest).passed


@pytest.mark.parametrize(
    "old, new, why",
    [
        (b"Instructions: replace", b"Instructions: Replace", "instruction text"),
        (b" Medium</w:t>", b" Medlum</w:t>", "option text"),
        (b"Req ID", b"Req No", "table header"),
        (b'<w:gridCol w:w="1200"/>', b'<w:gridCol w:w="1300"/>', "table grid"),
        (b'<w:gridSpan w:val="2"/>', b"", "merged cell"),
        (b"QA Approver", b"QA Approved", "signature table"),
        (b'w:val="Heading1"', b'w:val="Heading2"', "heading style"),
    ],
)
def test_change_outside_regions_is_rejected(template, manifest, filled, old, new, why):
    result = run_gate(template, _tamper(filled.output, old, new), manifest, filled.report["witness"])
    assert not result.passed, why
    assert any("outside the approved regions" in f for f in result.failures)


@pytest.mark.parametrize(
    "old, new, expected",
    [
        (b"QC analysts and reviewers use it; ",
         b"QC</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p/></w:tc></w:tr></w:tbl><w:p><w:r><w:t>analysts ",
         "adds structure"),
        (b"<w:p><w:r><w:t xml:space=\"preserve\">QC analysts",
         b"<w:p w14:paraId=\"1A2B3C01\"><w:r><w:t xml:space=\"preserve\">QC analysts", "paragraph id"),
        (b'<w14:checked w14:val="1"/>', b'<w14:checked w14:val="1" w14:extra="x"/>', "beyond its value"),
        (b'<w14:checked w14:val="1"/>', b'<w14:checked w14:val="7"/>', "invalid value"),
        ("<w:t>☒</w:t>".encode(), b"<w:t>X</w:t>", "glyph"),
        (b'preserve">4.2.1</w:t>', b'preserve">4.2.1</w:t><w:br/><w:t></w:t>', "inline text fragment"),
    ],
)
def test_odd_change_inside_a_region_is_rejected(template, manifest, filled, old, new, expected):
    result = run_gate(template, _tamper(filled.output, old, new), manifest)
    assert not result.passed
    assert any(expected in f for f in result.failures), result.failures


def test_change_to_a_part_without_regions_is_rejected(template, manifest, filled):
    out = _tamper(filled.output, b'w:percent="100"', b'w:percent="120"', part="word/settings.xml")
    result = run_gate(template, out, manifest)
    assert any("word/settings.xml" in f for f in result.failures)


def test_reordered_parts_are_rejected(template, manifest, filled):
    pkg = Package.from_bytes(filled.output)
    pkg.entries.reverse()
    assert not run_gate(template, pkg.rebuild({}), manifest).passed


def test_a_false_witness_cannot_widen_the_mask(template, manifest, filled):
    witness = {k: [n + 1 for n in v] for k, v in filled.report["witness"].items()}
    assert not run_gate(template, filled.output, manifest, witness).passed


def test_gate_refuses_an_unapproved_manifest(template, manifest, filled):
    unapproved = manifest.model_copy(deep=True)
    unapproved.approval = None
    result = run_gate(template, filled.output, unapproved)
    assert not result.passed and "not approved" in result.failures[0]
