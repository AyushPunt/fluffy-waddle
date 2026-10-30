import pytest

from docagent.compiler import TemplateDoc, approve, load_approved, propose
from docagent.errors import ManifestError, TemplateError
from docagent.manifest import Anchor, Manifest, Mutation, Region, RegionKind
from tests.fixtures import review
from tests.fixtures.csra import build_docx, document_xml, minimal


def test_proposal_finds_every_region_kind(template):
    m = propose(TemplateDoc(template))
    kinds = {r.id: r.kind for r in m.regions}
    assert kinds["approvals"] == RegionKind.HUMAN_ONLY
    assert kinds["requirements_rows"] == RegionKind.TABLE_ROWS
    assert kinds["overall_risk"] == RegionKind.SINGLE_CHOICE
    assert kinds["gxp_applicability"] == RegionKind.MULTI_SELECT
    assert kinds["data_integrity_controls"] == RegionKind.MULTI_SELECT
    assert kinds["system_version"] == RegionKind.FACT
    assert kinds["document_version_header1"] == RegionKind.FACT
    assert m.region("system_name").anchor.occurrence == "all"
    assert m.region("describe_the_system_its_intended_use").run_props == "paragraph"


def test_unreviewed_proposal_cannot_be_approved(template):
    m = propose(TemplateDoc(template))
    with pytest.raises(ManifestError, match="needs review"):
        approve(m, template, "QA")


def test_approval_pins_spans_and_hash(manifest):
    assert manifest.approval.manifest_sha256 == manifest.content_sha256()
    assert all(r.resolved is not None for r in manifest.regions)
    assert manifest.region("approvals").resolved.spans == []


def test_manifest_roundtrips_through_json(manifest, template):
    again = Manifest.model_validate_json(manifest.dump())
    assert again.content_sha256() == manifest.content_sha256()
    load_approved(again, template)


def test_edit_after_approval_is_refused(manifest, template):
    edited = manifest.model_copy(deep=True)
    edited.region("system_version").source = "system.name"
    with pytest.raises(ManifestError, match="edited after approval"):
        load_approved(edited, template)


def test_revised_template_is_refused(manifest):
    revised = build_docx(document_xml().replace("Tick every regulation", "Tick each regulation"))
    with pytest.raises(ManifestError, match="fingerprint"):
        load_approved(manifest, revised)


def test_region_inside_human_only_table_is_refused(template):
    m = review.reviewed_manifest(template)
    m.regions.append(
        Region(
            id="author_name",
            kind=RegionKind.FACT,
            mutation=Mutation.INLINE_TEXT,
            anchor=Anchor(section="7 Approvals", text="Author"),
            source="system.owner",
        )
    )
    with pytest.raises(ManifestError, match="human-only"):
        approve(m, template, "QA")


def test_ambiguous_placeholder_needs_occurrence():
    raw = minimal("<w:p><w:r><w:t>[X]</w:t></w:r></w:p><w:p><w:r><w:t>[X]</w:t></w:r></w:p>")
    doc = TemplateDoc(raw)
    m = Manifest(
        template=doc.template_ref(),
        regions=[Region(id="x", kind=RegionKind.FACT, mutation=Mutation.INLINE_TEXT,
                        anchor=Anchor(text="[X]"), source="system.x")],
    )
    with pytest.raises(TemplateError, match="occurs 2 times"):
        approve(m, raw, "QA")
    m.regions[0].anchor.occurrence = 2
    approved = approve(m, raw, "QA")
    assert len(approved.regions[0].resolved.paths) == 1


def test_paragraph_region_refuses_to_destroy_a_bookmark():
    raw = minimal('<w:p><w:bookmarkStart w:id="0" w:name="b"/><w:r><w:t>[Describe it]</w:t></w:r>'
                  '<w:bookmarkEnd w:id="0"/></w:p>')
    doc = TemplateDoc(raw)
    m = Manifest(
        template=doc.template_ref(),
        regions=[Region(id="d", kind=RegionKind.PROSE, mutation=Mutation.PARAGRAPHS,
                        anchor=Anchor(text="[Describe it]"))],
    )
    with pytest.raises(TemplateError, match="bookmarkStart"):
        approve(m, raw, "QA")


def test_hidden_placeholder_formatting_is_refused():
    raw = minimal("<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>[X]</w:t></w:r></w:p>")
    doc = TemplateDoc(raw)
    m = Manifest(
        template=doc.template_ref(),
        regions=[Region(id="x", kind=RegionKind.FACT, mutation=Mutation.INLINE_TEXT,
                        anchor=Anchor(text="[X]"), source="system.x")],
    )
    with pytest.raises(TemplateError, match="hidden"):
        approve(m, raw, "QA")


def test_mutation_must_fit_kind():
    with pytest.raises(ValueError, match="allows mutation"):
        Region(id="x", kind=RegionKind.FACT, mutation=Mutation.PARAGRAPHS, anchor=Anchor(text="[X]"))
