"""Synthetic, already de-identified past documents for the demo systems.

SYS-0042 (LabTrack LIMS) is the target. SYS-0017 (ChromaVault CDS) is a different
system whose chunks must never support a claim about SYS-0042: it states things
(user count, monthly backup checks) that the target's own documents leave open.
A draft risk assessment for the target states an overall risk; drafts are never evidence.
"""

from docagent.kb import Chunk, KnowledgeBase


def _c(chunk_id, system_id, doc_type, title, section, text, version="1.0", status="approved") -> Chunk:
    return Chunk(chunk_id=chunk_id, system_id=system_id, doc_type=doc_type, doc_title=title, doc_version=version,
                 status=status, section=section, text=text, deidentified=True)


URS = "User Requirements Specification, LabTrack LIMS v4"
OLD_RA = "Computer System Risk Assessment, LabTrack LIMS v3.1"
VSR = "Validation Summary Report, LabTrack LIMS v3.1"
OQ = "Operational Qualification Summary, LabTrack LIMS 4.2.1"
CDS = "Validation Summary Report, ChromaVault CDS"

CHUNKS = [
    _c("urs-0042-01", "SYS-0042", "URS", URS, "1 Purpose",
       "LabTrack LIMS manages sample login, test result entry and result review for the QC laboratory at "
       "[SITE_1]. It replaces paper worksheets for release testing of finished product.", version="4.0"),
    _c("urs-0042-02", "SYS-0042", "URS", URS, "2 Users",
       "The system is used by QC analysts, who enter results, and QC reviewers, who review and approve them. "
       "[ORG_1] IT administers the application servers.", version="4.0"),
    _c("urs-0042-03", "SYS-0042", "URS", URS, "4 Requirements",
       "URS-01: The system shall record a secure, time-stamped audit trail of every creation, modification "
       "and deletion of a result.", version="4.0"),
    _c("urs-0042-04", "SYS-0042", "URS", URS, "4 Requirements",
       "URS-02: Result approval shall use an electronic signature that shows the signer's printed name, the "
       "date and time of signing, and the meaning of the signature.", version="4.0"),
    _c("urs-0042-05", "SYS-0042", "URS", URS, "4 Requirements",
       "URS-03: The system shall back up its database every day.", version="4.0"),
    _c("ra-0042-01", "SYS-0042", "Risk Assessment", OLD_RA, "3 Requirements",
       "Audit trail of result changes: GxP impact Direct, risk High. Electronic signature on result approval: "
       "GxP impact Direct, risk High. Assessment approved by [PERSON_2], QA.", version="3.1"),
    _c("vsr-0042-01", "SYS-0042", "Validation Summary Report", VSR, "2 Regulatory scope",
       "The system creates and maintains electronic records and electronic signatures, so 21 CFR Part 11 and "
       "EU GMP Annex 11 apply. LabTrack LIMS is a configured product and is classified as GAMP 5 Category 4.", version="3.1"),
    _c("oq-0042-01", "SYS-0042", "OQ Summary", OQ, "5 Results",
       "Test OQ-07 verified that the audit trail is enabled and records the old value, new value, user and "
       "time stamp of each change. Electronic signatures are used for result approval and were tested in OQ-09.", version="4.2.1"),
    _c("oq-0042-02", "SYS-0042", "OQ Summary", OQ, "6 Open items",
       "Backup restore verification has not been performed for this release. It is planned before go-live.", version="4.2.1"),
    _c("ra-0042-draft", "SYS-0042", "Risk Assessment", "Computer System Risk Assessment, LabTrack LIMS 4.2.1 (DRAFT)",
       "6 Overall Risk", "The overall risk level of LabTrack LIMS 4.2.1 is Medium.", version="4.2.1 draft A",
       status="draft"),
    _c("vsr-0017-01", "SYS-0017", "Validation Summary Report", CDS, "2 System description",
       "ChromaVault CDS is used by 45 named analysts at [SITE_1]. Backups of ChromaVault CDS are verified "
       "monthly by [ORG_1] IT.", version="2.3"),
]


def build_kb() -> KnowledgeBase:
    return KnowledgeBase(chunks=CHUNKS)
