import pytest

from docagent.compiler import TemplateDoc
from docagent.errors import TemplateError
from docagent.ooxml.text import decode_with_offsets
from docagent.ooxml.xmlindex import index_xml
from tests.fixtures.csra import minimal


@pytest.fixture(scope="module")
def doc(template):
    return TemplateDoc(template).part("word/document.xml")


def test_spans_slice_exact_elements():
    data = b'<?xml version="1.0"?>\n<w:a xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' \
        b'<w:b x="1&gt;2"/><w:t xml:space="preserve">h&amp;\xc3\xa9</w:t></w:a>'
    root = index_xml(data)
    b, t = root.children
    assert data[b.start : b.end] == b'<w:b x="1&gt;2"/>' and b.is_empty
    assert data[t.start : t.start_end] == b'<w:t xml:space="preserve">'
    assert data[t.end_start : t.end] == b"</w:t>"
    assert t.attrs["xml:space"] == "preserve" and t.text == "h&é"


def test_decode_maps_each_character_to_its_bytes():
    data = b"a&amp;&#x2610;\xc3\xa9"
    text, spans = decode_with_offsets(data, 0, len(data))
    assert text == "a&☐é"
    assert [data[s:e] for s, e in spans] == [b"a", b"&amp;", b"&#x2610;", b"\xc3\xa9"]


def test_refuses_doctype():
    with pytest.raises(TemplateError, match="DOCTYPE"):
        index_xml(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>')


def test_placeholder_split_across_runs_reads_as_one_string(doc):
    texts = [doc.text(p).text for p in doc.paragraphs]
    assert "[System Version]" in texts
    pt = next(doc.text(p) for p in doc.paragraphs if doc.text(p).text == "[System Version]")
    assert len({id(r.t) for r in pt.refs}) == 3


def test_entities_decode_in_paragraph_text(doc):
    assert any("rev 3 & approved" in doc.text(p).text for p in doc.paragraphs)


def test_all_three_checkbox_forms_are_found(doc):
    kinds = [b.kind for b in doc.checkboxes]
    assert kinds.count("sdt") == 4 and kinds.count("glyph") == 6 and kinds.count("legacy") == 3
    assert [b.label_after for b in doc.checkboxes if b.kind == "legacy"] == ["Low", "Medium", "High"]
    assert {b.row_label for b in doc.checkboxes if b.kind == "glyph"} == {
        "Audit trail enabled", "Electronic signatures used", "Backups verified"
    }
    assert not any(b.checked for b in doc.checkboxes)


def test_sections_follow_headings(doc):
    assert [t for _, _, t in doc.headings][:2] == ["1 Purpose", "2 System Description"]
    lo, hi = doc.scope("4 GxP Applicability")
    inside = [b for b in doc.checkboxes if lo <= b.para.start < hi]
    assert len(inside) == 4


def test_tracked_changes_are_refused():
    raw = minimal('<w:p><w:ins w:id="1" w:author="x"><w:r><w:t>new</w:t></w:r></w:ins></w:p>')
    with pytest.raises(TemplateError, match="tracked change"):
        TemplateDoc(raw).part("word/document.xml")
