from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.extraction.models import SourceFile
from scripts.extraction.supplements import extract_supplements
from scripts.extraction.xlsx_supplement import (
    INLINE_CELL_LIMIT,
    XLSX_MEDIA_TYPE,
    extract_xlsx_supplement,
)


CONTENT_TYPES = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="xml" ContentType="application/xml"/>
 <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
 <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
 <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
 <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

WORKBOOK = b"""<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets><sheet name="Results &amp; values" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

RELATIONSHIPS = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
 <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

SHARED_STRINGS = b"""<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="2" uniqueCount="2">
 <si><t>Group</t></si><si><r><t>repeat </t></r><r><t>length</t></r></si>
</sst>"""

STYLES = b"""<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <fonts count="2"><font/><font><b/></font></fonts>
 <fills count="1"><fill/></fills><borders count="1"><border/></borders>
 <cellStyleXfs count="1"><xf fontId="0"/></cellStyleXfs>
 <cellXfs count="2"><xf fontId="0"/><xf fontId="1"/></cellXfs>
</styleSheet>"""

FORMATTED_STYLES = b"""<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <numFmts count="1"><numFmt numFmtId="176" formatCode="0.0000"/></numFmts>
 <fonts count="1"><font/></fonts>
 <fills count="1"><fill/></fills><borders count="1"><border/></borders>
 <cellStyleXfs count="1"><xf fontId="0"/></cellStyleXfs>
 <cellXfs count="4"><xf fontId="0" numFmtId="0"/><xf fontId="0" numFmtId="16"/><xf fontId="0" numFmtId="11"/><xf fontId="0" numFmtId="176"/></cellXfs>
</styleSheet>"""


def fixture_bytes(
    *,
    large: bool = False,
    external: bool = False,
    stale_formatting: bool = False,
) -> bytes:
    dimension = (
        "A1:XFD1000"
        if stale_formatting
        else "A1:K1000" if large else "A1:C3"
    )
    final_cell = '<c r="K1000"><v>42</v></c>' if large else ""
    stale_first_row = '<c r="XFD1" s="1"/>' if stale_formatting else ""
    stale_final_row = (
        '<row r="1000"><c r="C1000" s="1"/></row>'
        if stale_formatting
        else ""
    )
    worksheet = f"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <dimension ref="{dimension}"/>
 <sheetData>
  <row r="1"><c r="A1" t="s" s="1"><v>0</v></c><c r="B1" t="s" s="1"><v>1</v></c><c r="C1" t="inlineStr" s="1"><is><t>ΔTm (°C)</t></is></c>{stale_first_row}</row>
  <row r="2"><c r="A2" t="inlineStr"><is><t>CAG₁₀</t></is></c><c r="B2"><v>10</v></c><c r="C2"><v>2.5</v></c>{final_cell}</row>
  <row r="3"><c r="A3" t="inlineStr"><is><t>merged note</t></is></c></row>
  {stale_final_row}
 </sheetData>
 <mergeCells count="1"><mergeCell ref="A3:C3"/></mergeCells>
</worksheet>""".encode("utf-8")
    relationships = RELATIONSHIPS
    if external:
        relationships = relationships.replace(
            b"</Relationships>",
            b'<Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLinkPath" Target="https://example.invalid/data.xlsx" TargetMode="External"/></Relationships>',
        )
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("xl/workbook.xml", WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
        archive.writestr("xl/sharedStrings.xml", SHARED_STRINGS)
        archive.writestr("xl/styles.xml", STYLES)
    return package.getvalue()


def formatted_fixture_bytes() -> bytes:
    worksheet = b"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <dimension ref="A1:F1"/>
 <sheetData><row r="1"><c r="A1" s="1"><v>44814</v></c><c r="B1" s="2"><v>2.77647188435929E-10</v></c><c r="C1" s="3"><v>0.004</v></c><c r="D1"><v>5834.848000000002</v></c><c r="E1"><v>1.07000000000001</v></c><c r="F1"><v>-1.5000000000007674E-2</v></c></row></sheetData>
</worksheet>"""
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("xl/workbook.xml", WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", RELATIONSHIPS)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
        archive.writestr("xl/sharedStrings.xml", SHARED_STRINGS)
        archive.writestr("xl/styles.xml", FORMATTED_STYLES)
    return package.getvalue()


def symbol_rich_text_fixture_bytes() -> bytes:
    worksheet = b"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <dimension ref="A1:C1"/><sheetData><row r="1">
  <c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>
  <c r="C1" t="s"><v>2</v></c>
 </row></sheetData>
</worksheet>"""
    shared_strings = """<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="3" uniqueCount="3">
 <si><r><t>IL-1</t></r><r><rPr><rFont val="Symbol"/></rPr><t>b</t></r></si>
 <si><r><t>Near </t></r><r><rPr><rFont val="Symbolic"/></rPr><t>b</t></r></si>
 <si><r><t>Plain </t></r><r><rPr><rFont val="Arial"/></rPr><t>b</t></r></si>
</sst>""".encode("utf-8")
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("xl/workbook.xml", WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", RELATIONSHIPS)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
        archive.writestr("xl/sharedStrings.xml", shared_strings)
        archive.writestr("xl/styles.xml", STYLES)
    return package.getvalue()


def source_for(path: Path, payload: bytes) -> SourceFile:
    return SourceFile(
        role="supplement",
        path=path,
        relative_path=f"papers (private)/00001/supplementary/{path.name}",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        detected_format=XLSX_MEDIA_TYPE,
    )


class XlsxSupplementTests(unittest.TestCase):
    def test_exact_symbol_font_rich_runs_decode_to_unicode_greek(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "symbol.xlsx"
            payload = symbol_rich_text_fixture_bytes()
            path.write_bytes(payload)

            _, tables, warnings = extract_xlsx_supplement(
                source_for(path, payload), "supplement_001", xlsx_path=path
            )

            self.assertEqual(warnings, [])
            self.assertEqual(
                [cell.text for cell in tables[0].parts[0].rows[0]],
                ["IL-1β", "Near b", "Plain b"],
            )

    def test_authored_number_formats_are_displayed_with_exact_values_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "formatted.xlsx"
            payload = formatted_fixture_bytes()
            path.write_bytes(payload)

            _, tables, warnings = extract_xlsx_supplement(
                source_for(path, payload), "supplement_001", xlsx_path=path
            )

            self.assertEqual(warnings, [])
            table = tables[0]
            self.assertEqual(
                [cell.text for cell in table.parts[0].rows[0]],
                ["10-Sep", "2.78E-10", "0.0040", "5834.848", "1.07", "-0.015"],
            )
            self.assertEqual(
                table.machine_records,
                [
                    {
                        "record_type": "spreadsheet_cell_value",
                        "sheet": "Results & values",
                        "coordinate": "A1",
                        "stored_value": "44814",
                        "display_text": "10-Sep",
                        "number_format": "d-mmm",
                    },
                    {
                        "record_type": "spreadsheet_cell_value",
                        "sheet": "Results & values",
                        "coordinate": "B1",
                        "stored_value": "2.77647188435929E-10",
                        "display_text": "2.78E-10",
                        "number_format": "0.00E+00",
                    },
                    {
                        "record_type": "spreadsheet_cell_value",
                        "sheet": "Results & values",
                        "coordinate": "C1",
                        "stored_value": "0.004",
                        "display_text": "0.0040",
                        "number_format": "0.0000",
                    },
                    {
                        "record_type": "spreadsheet_cell_value",
                        "sheet": "Results & values",
                        "coordinate": "D1",
                        "stored_value": "5834.848000000002",
                        "display_text": "5834.848",
                        "number_format": "General",
                    },
                    {
                        "record_type": "spreadsheet_cell_value",
                        "sheet": "Results & values",
                        "coordinate": "E1",
                        "stored_value": "1.07000000000001",
                        "display_text": "1.07",
                        "number_format": "General",
                    },
                    {
                        "record_type": "spreadsheet_cell_value",
                        "sheet": "Results & values",
                        "coordinate": "F1",
                        "stored_value": "-1.5000000000007674E-2",
                        "display_text": "-0.015",
                        "number_format": "General",
                    },
                ],
            )

    def test_small_worksheet_is_complete_semantic_table_without_image_or_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "values.xlsx"
            payload = fixture_bytes()
            path.write_bytes(payload)
            extraction_root = root / "extraction"

            result = extract_supplements([source_for(path, payload)], extraction_root)[0]

            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual((extraction_root / result.copied_path).read_bytes(), payload)
            self.assertEqual(result.warnings, [])
            self.assertEqual(len(result.tables), 1)
            table = result.tables[0]
            self.assertEqual(table.source_kind, "spreadsheet")
            self.assertFalse(table.requires_source_image)
            self.assertIsNone(table.image_path)
            self.assertEqual(table.label, "Worksheet Results & values")
            self.assertEqual(
                [[cell.text for cell in row] for row in table.parts[0].rows],
                [["Group", "repeat length", "ΔTm (°C)"], ["CAG₁₀", "10", "2.5"], ["merged note"]],
            )
            self.assertTrue(all(cell.header for cell in table.parts[0].rows[0]))
            self.assertEqual(table.parts[0].rows[2][0].colspan, 3)
            self.assertEqual(result.assets, [])
            self.assertEqual(list(extraction_root.rglob("*.csv")), [])
            self.assertEqual(list(extraction_root.rglob("*.png")), [])

    def test_stale_format_only_dimension_does_not_expand_semantic_table(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "stale-formatting.xlsx"
            payload = fixture_bytes(stale_formatting=True)
            path.write_bytes(payload)

            blocks, tables, warnings = extract_xlsx_supplement(
                source_for(path, payload), "supplement_001", xlsx_path=path
            )

            self.assertEqual(warnings, [])
            self.assertEqual(len(tables), 1)
            self.assertEqual(len(tables[0].parts[0].rows), 3)
            self.assertEqual(
                [len(row) for row in tables[0].parts[0].rows],
                [3, 3, 1],
            )
            self.assertIn("A1:C3", blocks[0].plain_text)
            self.assertNotIn("XFD", blocks[0].plain_text)

    def test_large_rectangular_range_stays_in_linked_original(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "large.xlsx"
            payload = fixture_bytes(large=True, external=True)
            path.write_bytes(payload)

            blocks, tables, warnings = extract_xlsx_supplement(
                source_for(path, payload), "supplement_007", xlsx_path=path
            )

            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(tables, [])
            self.assertEqual([block.kind for block in blocks], ["workbook_summary", "workbook_sheet"])
            self.assertIn("1,000 rows × 11 columns", blocks[1].plain_text)
            self.assertIn("8 content-bearing cells", blocks[1].plain_text)
            self.assertIn("complete cell data remain in the linked original workbook", blocks[1].plain_text)
            self.assertEqual(warnings[0]["code"], "spreadsheet_external_relationships_not_followed")
            self.assertGreater(11_000, INLINE_CELL_LIMIT)

    def test_unsafe_duplicate_package_member_is_rejected(self) -> None:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr("xl/workbook.xml", WORKBOOK)
            archive.writestr("XL/WORKBOOK.XML", WORKBOOK)
            archive.writestr("xl/_rels/workbook.xml.rels", RELATIONSHIPS)
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "unsafe.xlsx"
            raw = payload.getvalue()
            path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "duplicate XLSX package member"):
                extract_xlsx_supplement(
                    source_for(path, raw), "supplement_001", xlsx_path=path
                )


if __name__ == "__main__":
    unittest.main()
