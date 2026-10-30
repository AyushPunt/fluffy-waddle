"""Builds a realistic Computer System Risk Assessment template as Word would save it.

The markup deliberately includes what breaks naive fillers: placeholders split
across runs by spell-check marks and revision IDs, a merged header cell, w14
content-control checkboxes, legacy form-field checkboxes, ☐ glyphs in a yes/no
table, a header part, a prototype row and a signature table.
"""

from __future__ import annotations

import io
import zipfile

NS = (
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
    'mc:Ignorable="w14"'
)
GREY = '<w:rPr><w:i/><w:color w:val="808080"/></w:rPr>'
SYMBOL = '<w:rPr><w:rFonts w:ascii="Segoe UI Symbol" w:hAnsi="Segoe UI Symbol"/></w:rPr>'


def p(text: str = "", style: str | None = None, rpr: str = "", pid: str | None = None) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    attrs = f' w14:paraId="{pid}" w14:textId="77777777"' if pid else ""
    run = f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>' if text else ""
    return f'<w:p{attrs} w:rsidR="00A10B2C" w:rsidRDefault="00A10B2C">{ppr}{run}</w:p>'


def heading(text: str, pid: str) -> str:
    return p(text, "Heading1", pid=pid)


def tc(content: str, width: int, extra: str = "") -> str:
    return f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{extra}</w:tcPr>{content}</w:tc>'


def cell_p(text: str = "", rpr: str = "") -> str:
    run = f"<w:r>{rpr}<w:t>{text}</w:t></w:r>" if text else ""
    return f'<w:p w:rsidR="00B11C3D"><w:pPr><w:spacing w:after="0"/></w:pPr>{run}</w:p>'


def table(grid: list[int], rows: list[str]) -> str:
    cols = "".join(f'<w:gridCol w:w="{w}"/>' for w in grid)
    return (
        '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/>'
        '<w:tblLook w:val="04A0"/></w:tblPr>'
        f"<w:tblGrid>{cols}</w:tblGrid>{''.join(rows)}</w:tbl>"
    )


def tr(*cells: str, header: bool = False) -> str:
    trpr = "<w:trPr><w:tblHeader/></w:trPr>" if header else ""
    return f'<w:tr w:rsidR="00C22D4E">{trpr}{"".join(cells)}</w:tr>'


def sdt_checkbox(label: str, n: int) -> str:
    return (
        f'<w:p w:rsidR="00D33E5F"><w:sdt><w:sdtPr><w:id w:val="-17{n}"/>'
        "<w14:checkbox><w14:checked w14:val=\"0\"/>"
        '<w14:checkedState w14:val="2612" w14:font="MS Gothic"/>'
        '<w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/></w14:checkbox></w:sdtPr>'
        '<w:sdtContent><w:r><w:rPr><w:rFonts w:ascii="MS Gothic" w:eastAsia="MS Gothic" w:hAnsi="MS Gothic" '
        'w:hint="eastAsia"/></w:rPr><w:t>☐</w:t></w:r></w:sdtContent></w:sdt>'
        f'<w:r><w:t xml:space="preserve"> {label}</w:t></w:r></w:p>'
    )


def legacy_checkbox(label: str, n: int) -> str:
    return (
        f'<w:p w:rsidR="00E44F6A"><w:bookmarkStart w:id="{n}" w:name="Check{n}"/>'
        '<w:r><w:fldChar w:fldCharType="begin"><w:ffData>'
        f'<w:name w:val="Check{n}"/><w:enabled/><w:calcOnExit w:val="0"/>'
        '<w:checkBox><w:sizeAuto/><w:default w:val="0"/></w:checkBox></w:ffData></w:fldChar></w:r>'
        '<w:r><w:instrText xml:space="preserve"> FORMCHECKBOX </w:instrText></w:r>'
        f'<w:r><w:fldChar w:fldCharType="end"/></w:r><w:bookmarkEnd w:id="{n}"/>'
        f'<w:r><w:t xml:space="preserve"> {label}</w:t></w:r></w:p>'
    )


def yes_no_cell() -> str:
    return (
        '<w:p w:rsidR="00F55A7B">'
        f"<w:r>{SYMBOL}<w:t>☐</w:t></w:r>"
        '<w:r><w:t xml:space="preserve"> Yes   </w:t></w:r>'
        f"<w:r>{SYMBOL}<w:t>☐</w:t></w:r>"
        '<w:r><w:t xml:space="preserve"> No</w:t></w:r></w:p>'
    )


SPLIT_VERSION = (
    '<w:p w:rsidR="00B11C3D"><w:pPr><w:spacing w:after="0"/></w:pPr>'
    '<w:r w:rsidR="00B11C3D"><w:t>[System Ver</w:t></w:r>'
    '<w:proofErr w:type="spellStart"/>'
    '<w:r w:rsidR="00C77E21"><w:t>sion</w:t></w:r>'
    '<w:proofErr w:type="spellEnd"/>'
    '<w:r w:rsidR="00D88F32"><w:t>]</w:t></w:r></w:p>'
)


def document_xml() -> str:
    body = [
        p("Computer System Risk Assessment", "Title", pid="1A2B3C01"),
        p(
            "Instructions: replace bracketed text. Do not change headings, tables, option text or this "
            "instruction. Template QA-TPL-014 rev 3 &amp; approved by QA.",
            "Instruction",
            GREY,
        ),
        table(
            [3000, 6000],
            [
                tr(tc(cell_p("Document Information"), 9000, '<w:gridSpan w:val="2"/>'), header=True),
                tr(tc(cell_p("System Name"), 3000), tc(cell_p("[System Name]"), 6000)),
                tr(tc(cell_p("System ID"), 3000), tc(cell_p("[System ID]"), 6000)),
                tr(tc(cell_p("System Version"), 3000), tc(SPLIT_VERSION, 6000)),
                tr(tc(cell_p("Vendor"), 3000), tc(cell_p("[Vendor]"), 6000)),
            ],
        ),
        heading("1 Purpose", "1A2B3C02"),
        p("[Purpose and scope boilerplate]"),
        heading("2 System Description", "1A2B3C03"),
        p("[Describe the system, its intended use and its users]", rpr=GREY),
        '<w:p w:rsidR="00A10B2C"><w:r><w:t xml:space="preserve">The business owner of </w:t></w:r>'
        "<w:r><w:t>[System Name]</w:t></w:r>"
        '<w:r><w:t xml:space="preserve"> is accountable for this assessment.</w:t></w:r></w:p>',
        heading("3 Requirements", "1A2B3C04"),
        table(
            [1200, 4800, 1500, 1500],
            [
                tr(
                    tc(cell_p("Req ID"), 1200), tc(cell_p("Requirement"), 4800),
                    tc(cell_p("GxP Impact"), 1500), tc(cell_p("Risk"), 1500),
                    header=True,
                ),
                tr(
                    tc(cell_p(rpr='<w:rPr><w:sz w:val="18"/></w:rPr>'), 1200),
                    tc(cell_p(), 4800),
                    tc(cell_p(), 1500),
                    tc(cell_p(), 1500),
                ),
            ],
        ),
        heading("4 GxP Applicability", "1A2B3C05"),
        p("Tick every regulation that applies to the system."),
        sdt_checkbox("21 CFR Part 11", 1),
        sdt_checkbox("EU GMP Annex 11", 2),
        sdt_checkbox("GAMP 5 Category 4", 3),
        sdt_checkbox("Data integrity (ALCOA+)", 4),
        heading("5 Data Integrity Controls", "1A2B3C06"),
        table(
            [6000, 3000],
            [
                tr(tc(cell_p("Control"), 6000), tc(cell_p("Response"), 3000), header=True),
                tr(tc(cell_p("Audit trail enabled"), 6000), tc(yes_no_cell(), 3000)),
                tr(tc(cell_p("Electronic signatures used"), 6000), tc(yes_no_cell(), 3000)),
                tr(tc(cell_p("Backups verified"), 6000), tc(yes_no_cell(), 3000)),
            ],
        ),
        heading("6 Overall Risk", "1A2B3C07"),
        p("Select the overall risk level from the matrix in SOP-QA-031."),
        legacy_checkbox("Low", 11),
        legacy_checkbox("Medium", 12),
        legacy_checkbox("High", 13),
        heading("7 Approvals", "1A2B3C08"),
        table(
            [2250, 2250, 2250, 2250],
            [
                tr(*(tc(cell_p(h), 2250) for h in ("Role", "Name", "Signature", "Date")), header=True),
                tr(tc(cell_p("Author"), 2250), tc(cell_p(), 2250), tc(cell_p(), 2250), tc(cell_p(), 2250)),
                tr(tc(cell_p("QA Approver"), 2250), tc(cell_p(), 2250), tc(cell_p(), 2250), tc(cell_p(), 2250)),
            ],
        ),
        '<w:sectPr w:rsidR="00A10B2C"><w:headerReference w:type="default" r:id="rId8"/>'
        '<w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
        'w:left="1440" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>',
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        f"<w:document {NS}><w:body>{''.join(body)}</w:body></w:document>"
    )


HEADER_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    f"<w:hdr {NS}><w:p><w:pPr><w:pStyle w:val=\"Header\"/></w:pPr>"
    '<w:r><w:t xml:space="preserve">Risk Assessment [System ID] — Document Version [Document Version]</w:t></w:r>'
    "</w:p></w:hdr>"
)

STYLES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>'
    '<w:rPr><w:sz w:val="48"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
    '<w:pPr><w:keepNext/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:customStyle="1" w:styleId="Instruction"><w:name w:val="Instruction"/>'
    '<w:basedOn w:val="Normal"/><w:rPr><w:i/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Header"><w:name w:val="header"/><w:basedOn w:val="Normal"/></w:style>'
    '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/></w:style>'
    "</w:styles>"
)

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '<Override PartName="/word/settings.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
    '<Override PartName="/word/header1.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>'
    '<Override PartName="/docProps/core.xml" '
    'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    "</Types>"
)

ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'officeDocument" Target="word/document.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/'
    'core-properties" Target="docProps/core.xml"/>'
    "</Relationships>"
)

DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
    'Target="styles.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" '
    'Target="settings.xml"/>'
    '<Relationship Id="rId8" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" '
    'Target="header1.xml"/>'
    "</Relationships>"
)

SETTINGS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:zoom w:percent="100"/><w:defaultTabStop w:val="720"/></w:settings>'
)

CORE_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>CSRA template</dc:title></cp:coreProperties>'
)


def package(parts: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, content.encode("utf-8") if isinstance(content, str) else content)
    return buf.getvalue()


def build_docx(document: str, header: str = HEADER_XML) -> bytes:
    return package(
        {
            "[Content_Types].xml": CONTENT_TYPES,
            "_rels/.rels": ROOT_RELS,
            "docProps/core.xml": CORE_XML,
            "word/document.xml": document,
            "word/styles.xml": STYLES_XML,
            "word/settings.xml": SETTINGS_XML,
            "word/header1.xml": header,
            "word/_rels/document.xml.rels": DOC_RELS,
        }
    )


def build_template() -> bytes:
    return build_docx(document_xml())


def minimal(body: str) -> bytes:
    """A small document with the given body, for focused tests."""
    sect = (
        '<w:sectPr><w:headerReference w:type="default" r:id="rId8"/>'
        '<w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'
    )
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        f"<w:document {NS}><w:body>{body}{sect}</w:body></w:document>"
    )
    return build_docx(doc)
