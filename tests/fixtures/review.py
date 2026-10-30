"""What a template owner does to the proposed CSRA manifest, plus sample run inputs."""

from __future__ import annotations

from docagent.compiler import TemplateDoc, approve, propose
from docagent.manifest import Column, Manifest, Mutation, Region, RegionKind
from docagent.plan import BoilerplateLibrary, FillPlan, LibraryEntry, SystemRecord, entry_sha256

PURPOSE = [
    "This assessment identifies the GxP risks of {{system.name}} and the controls that reduce them "
    "to an acceptable level.",
    "It follows SOP-QA-031 and covers the system as configured for production use.",
]

FACT_SOURCES = {
    "system_name": "system.name",
    "system_id": "system.id",
    "system_version": "system.version",
    "vendor": "system.vendor",
    "system_id_header1": "system.id",
    "document_version_header1": "system.document_version",
}


def reviewed_manifest(raw: bytes) -> Manifest:
    m = propose(TemplateDoc(raw, "csra_template.docx"))
    regions = []
    for r in m.regions:
        r.needs_review = []
        if r.id in FACT_SOURCES:
            r.source = FACT_SOURCES[r.id]
        elif r.id == "purpose_and_scope_boilerplate":
            r = Region(
                id="purpose",
                kind=RegionKind.BOILERPLATE,
                mutation=Mutation.PARAGRAPHS,
                anchor=r.anchor,
                library_key="csra_purpose",
                library_sha256=entry_sha256(PURPOSE),
            )
        elif r.id == "describe_the_system_its_intended_use":
            r.id = "system_description"
        elif r.id == "requirements_rows":
            r.columns = [
                Column(name="req_id", source="row_number", format="REQ-{n:03d}"),
                Column(name="requirement"),
                Column(name="gxp_impact"),
                Column(name="risk"),
            ]
        regions.append(r)
    m.regions = regions
    return m


def approved_manifest(raw: bytes) -> Manifest:
    return approve(reviewed_manifest(raw), raw, "Template Owner (QA)")


def library() -> BoilerplateLibrary:
    return BoilerplateLibrary(
        entries={
            "csra_purpose": LibraryEntry(
                paragraphs=PURPOSE, version="3", approved_by="QA", sha256=entry_sha256(PURPOSE)
            )
        }
    )


def record() -> SystemRecord:
    return SystemRecord(
        record_id="SYS-0042",
        record_version="7",
        system={"name": "LabTrack LIMS", "id": "SYS-0042", "version": "4.2.1", "document_version": "1.0"},
    )


def plan(manifest: Manifest) -> FillPlan:
    return FillPlan(
        manifest_sha256=manifest.approval.manifest_sha256,
        regions={
            "system_description": {
                "paragraphs": [
                    "{{system.name}} records sample receipt, test results & reviews for QC <Building 4>.",
                    "QC analysts and reviewers use it; {{GAP: number of named users at go-live}} users are planned.",
                ]
            },
            "requirements_rows": {
                "rows": [
                    {
                        "requirement": "The system keeps an audit trail of every result change.",
                        "gxp_impact": "Direct",
                        "risk": "High",
                    },
                    {
                        "requirement": "Results are approved with an electronic signature.",
                        "gxp_impact": "Direct",
                        "risk": "{{GAP: risk rating for the e-signature requirement}}",
                    },
                ]
            },
            "gxp_applicability": {
                "items": {"21 CFR Part 11": "yes", "EU GMP Annex 11": "yes", "GAMP 5 Category 4": "no"}
            },
            "data_integrity_controls": {
                "items": {"Audit trail enabled": "yes", "Electronic signatures used": "no", "Backups verified": "unknown"}
            },
            "overall_risk": {"choice": "Medium"},
        },
    )
