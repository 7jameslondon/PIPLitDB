from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

from scripts.extraction.models import SourceFile
from scripts.extraction.pptx_supplement import (
    _block_kind,
    _paragraph_text,
    _write_asset,
    extract_pptx_supplement,
)


CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="tiff" ContentType="image/tiff"/>
  <Default Extension="jpeg" ContentType="image/jpeg"/>
  <Default Extension="xlsx" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
  <Override PartName="/ppt/notesSlides/notesSlide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"/>
  <Override PartName="/ppt/charts/chart1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>
</Types>
"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail" Target="docProps/thumbnail.jpeg"/>
</Relationships>
"""

PRESENTATION = """<?xml version="1.0" encoding="UTF-8"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>
  <p:sldSz cx="10000000" cy="10000000"/>
</p:presentation>
"""

PRESENTATION_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
</Relationships>
"""


def text_shape(shape_id: int, name: str, x: int, y: int, paragraphs: str) -> str:
    return f"""
    <p:sp>
      <p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
      <p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="1000" cy="1000"/></a:xfrm></p:spPr>
      <p:txBody><a:bodyPr/><a:lstStyle/>{paragraphs}</p:txBody>
    </p:sp>
    """


SLIDE = f"""<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
 <p:cSld><p:spTree>
  <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>
  {text_shape(2, "Second", 0, 200, '<a:p><a:r><a:t>Second</a:t></a:r></a:p>')}
  {text_shape(3, "Rich", 0, 100, '''<a:p>
    <a:r><a:rPr b="1"/><a:t xml:space="preserve">Bold </a:t></a:r>
    <a:r><a:rPr i="1"/><a:t xml:space="preserve">italic </a:t></a:r>
    <a:r><a:t>H</a:t></a:r><a:r><a:rPr baseline="-25000"/><a:t>2</a:t></a:r><a:r><a:t xml:space="preserve">O 10</a:t></a:r><a:r><a:rPr baseline="30000"/><a:t>6</a:t></a:r>
    <a:r><a:t xml:space="preserve"> Symbol: </a:t></a:r>
    <a:r><a:rPr><a:latin typeface="Symbol"/></a:rPr><a:t>b g m</a:t></a:r>
  </a:p>''')}
  {text_shape(4, "Caption", 0, 150, '<a:p><a:r><a:t>Supplementary Figure SI1. Synthetic caption.</a:t></a:r></a:p>')}
  <p:pic>
    <p:nvPicPr><p:cNvPr id="5" name="Path picture" descr="C:\\author\\panel.tif"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>
    <p:blipFill><a:blip r:embed="rIdImage"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
    <p:spPr><a:xfrm><a:off x="0" y="250"/><a:ext cx="1000" cy="1000"/></a:xfrm></p:spPr>
  </p:pic>
  <p:pic>
    <p:nvPicPr><p:cNvPr id="6" name="Semantic picture" descr="Microscopy panel"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>
    <p:blipFill><a:blip r:embed="rIdImage"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
    <p:spPr><a:xfrm><a:off x="0" y="260"/><a:ext cx="1000" cy="1000"/></a:xfrm></p:spPr>
  </p:pic>
  <p:graphicFrame>
    <p:nvGraphicFramePr><p:cNvPr id="7" name="Native table"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>
    <p:xfrm><a:off x="0" y="300"/><a:ext cx="2000" cy="1000"/></p:xfrm>
    <a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl>
      <a:tblPr/><a:tblGrid><a:gridCol w="1000"/><a:gridCol w="1000"/></a:tblGrid>
      <a:tr h="500"><a:tc gridSpan="2"><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr b="1"/><a:t>Header</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc><a:tc hMerge="1"><a:txBody><a:bodyPr/><a:lstStyle/><a:p/></a:txBody><a:tcPr/></a:tc></a:tr>
      <a:tr h="500"><a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>A</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc><a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>B</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc></a:tr>
    </a:tbl></a:graphicData></a:graphic>
  </p:graphicFrame>
  <p:graphicFrame>
    <p:nvGraphicFramePr><p:cNvPr id="8" name="Native chart"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>
    <p:xfrm><a:off x="0" y="400"/><a:ext cx="2000" cy="1000"/></p:xfrm>
    <a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart r:id="rIdChart"/></a:graphicData></a:graphic>
  </p:graphicFrame>
 </p:spTree></p:cSld>
</p:sld>
"""

SLIDE_RELS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdImage" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="{media_target}"/>
  <Relationship Id="rIdChart" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="../charts/chart1.xml"/>
  <Relationship Id="rIdNotes" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/notesSlide1.xml"/>
</Relationships>
"""

NOTES = """<?xml version="1.0" encoding="UTF-8"?>
<p:notes xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
 <p:cSld><p:spTree>
  <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>
  <p:sp><p:nvSpPr><p:cNvPr id="2" name="Notes body"/><p:cNvSpPr/><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr><p:spPr><a:xfrm><a:off x="0" y="100"/><a:ext cx="1000" cy="1000"/></a:xfrm></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Speaker insight</a:t></a:r></a:p></p:txBody></p:sp>
  <p:sp><p:nvSpPr><p:cNvPr id="3" name="Slide number"/><p:cNvSpPr/><p:nvPr><p:ph type="sldNum"/></p:nvPr></p:nvSpPr><p:spPr><a:xfrm><a:off x="0" y="200"/><a:ext cx="1000" cy="1000"/></a:xfrm></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>1</a:t></a:r></a:p></p:txBody></p:sp>
 </p:spTree></p:cSld>
</p:notes>
"""

CHART = """<?xml version="1.0" encoding="UTF-8"?>
<c:chartSpace xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">
 <c:chart>
  <c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Signal chart</a:t></a:r></a:p></c:rich></c:tx></c:title>
  <c:plotArea><c:layout/><c:barChart><c:ser>
   <c:idx val="0"/><c:order val="0"/>
   <c:tx><c:strRef><c:f>Sheet1!$B$1</c:f><c:strCache><c:ptCount val="1"/><c:pt idx="0"><c:v>Signal</c:v></c:pt></c:strCache></c:strRef></c:tx>
   <c:cat><c:strRef><c:f>Sheet1!$A$2:$A$3</c:f><c:strCache><c:ptCount val="2"/><c:pt idx="0"><c:v>A</c:v></c:pt><c:pt idx="1"><c:v>B</c:v></c:pt></c:strCache></c:strRef></c:cat>
   <c:val><c:numRef><c:f>Sheet1!$B$2:$B$3</c:f><c:numCache><c:formatCode>0.0</c:formatCode><c:ptCount val="2"/><c:pt idx="0"><c:v>1</c:v></c:pt><c:pt idx="1"><c:v>2</c:v></c:pt></c:numCache></c:numRef></c:val>
   <c:errBars><c:errDir val="y"/><c:errBarType val="both"/><c:errValType val="cust"/><c:plus><c:numLit><c:ptCount val="2"/><c:pt idx="0"><c:v>0.1</c:v></c:pt><c:pt idx="1"><c:v>0.2</c:v></c:pt></c:numLit></c:plus><c:minus><c:numLit><c:ptCount val="2"/><c:pt idx="0"><c:v>0.05</c:v></c:pt><c:pt idx="1"><c:v>0.1</c:v></c:pt></c:numLit></c:minus></c:errBars>
  </c:ser></c:barChart>
  <c:catAx><c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Category axis</a:t></a:r></a:p></c:rich></c:tx></c:title></c:catAx>
  <c:valAx><c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Intensity</a:t></a:r></a:p></c:rich></c:tx></c:title></c:valAx>
  </c:plotArea>
 </c:chart>
</c:chartSpace>
"""

CHART_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdWorkbook" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/package" Target="../embeddings/Microsoft_Excel_Worksheet1.xlsx"/>
</Relationships>
"""

WORKBOOK_BYTES = b"byte-identical-xlsx-source"


def write_package(
    path: Path,
    *,
    media_target: str = "../media/image1.tiff",
    extra_entries: dict[str, bytes] | None = None,
) -> None:
    parts: dict[str, bytes] = {
        "[Content_Types].xml": CONTENT_TYPES.encode(),
        "_rels/.rels": ROOT_RELS.encode(),
        "ppt/presentation.xml": PRESENTATION.encode(),
        "ppt/_rels/presentation.xml.rels": PRESENTATION_RELS.encode(),
        "ppt/slides/slide1.xml": SLIDE.encode(),
        "ppt/slides/_rels/slide1.xml.rels": SLIDE_RELS_TEMPLATE.format(
            media_target=media_target
        ).encode(),
        "ppt/notesSlides/notesSlide1.xml": NOTES.encode(),
        "ppt/charts/chart1.xml": CHART.encode(),
        "ppt/media/image1.tiff": b"byte-identical-tiff-source",
        "docProps/thumbnail.jpeg": b"low-resolution-thumbnail",
    }
    parts.update(extra_entries or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)


def source_for(path: Path) -> SourceFile:
    data = path.read_bytes()
    return SourceFile(
        role="supplement",
        path=path,
        relative_path=f"supplementary/{path.name}",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        detected_format=(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
    )


class PptxSupplementTests(unittest.TestCase):
    def test_caption_classification_uses_shared_parser(self) -> None:
        for caption in (
            "Supporting Figure S2. Result.",
            "Fig S2. Result.",
            "Figure S2A. Result.",
        ):
            with self.subTest(caption=caption):
                self.assertEqual(_block_kind(caption), "figure_caption")
        self.assertEqual(_block_kind("Figure SI2 Result"), "text")

    def test_symbol_typeface_inherited_from_paragraph_defaults(self) -> None:
        paragraph = ET.fromstring(
            """<a:p xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
              <a:pPr><a:defRPr><a:latin typeface="Symbol"/></a:defRPr></a:pPr>
              <a:r><a:t>b g m</a:t></a:r>
            </a:p>"""
        )
        markdown, plain = _paragraph_text(paragraph)
        self.assertEqual(markdown, "β γ μ")
        self.assertEqual(plain, "β γ μ")

    def test_semantic_extraction_and_byte_identical_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "supplementary.pptx"
            extraction = root / "extraction"
            write_package(package)

            blocks, tables, assets, warnings = extract_pptx_supplement(
                source_for(package),
                "supplement_001",
                pptx_path=package,
                extraction_root=extraction,
            )

            visible = [block.plain_text for block in blocks]
            self.assertEqual(visible[:4], [
                "Bold italic H_{2}O 10^{6} Symbol: β γ μ",
                "Supplementary Figure SI1. Synthetic caption.",
                "Second",
                "Microscopy panel",
            ])
            self.assertEqual(visible[-1], "Speaker insight")
            self.assertNotIn("1", visible)
            self.assertIn("<strong>Bold </strong>", blocks[0].markdown)
            self.assertIn("<em>italic </em>", blocks[0].markdown)
            self.assertIn("H<sub>2</sub>O 10<sup>6</sup>", blocks[0].markdown)
            self.assertIn("Symbol: β γ μ", blocks[0].plain_text)
            self.assertEqual(blocks[1].kind, "figure_caption")
            self.assertEqual(blocks[-1].kind, "speaker_note")

            self.assertEqual(len(tables), 2)
            native, chart = tables
            self.assertEqual(native.source_kind, "presentation")
            self.assertEqual(native.parts[0].rows[0][0].colspan, 2)
            self.assertEqual(native.parts[0].rows[1][1].text, "B")
            self.assertEqual(chart.source_kind, "presentation")
            self.assertEqual(chart.title_plain, "Signal chart")
            self.assertEqual(
                len({part.part_id for table in tables for part in table.parts}),
                sum(len(table.parts) for table in tables),
            )
            chart_rows = chart.parts[0].rows
            self.assertEqual(
                [cell.text for cell in chart_rows[0]],
                ["Category", "Signal", "Signal error y +", "Signal error y −"],
            )
            self.assertEqual(
                [cell.text for cell in chart_rows[2]], ["B", "2", "0.2", "0.1"]
            )
            self.assertTrue(any("value type=custom" in note for note in chart.footnotes_plain))
            self.assertTrue(any("Category axis" in note for note in chart.footnotes_plain))

            self.assertEqual(len(assets), 2)
            media = next(
                asset for asset in assets if asset["category"] == "supplement_image"
            )
            preview = next(
                asset
                for asset in assets
                if asset["category"] == "supplement_slide_preview"
            )
            self.assertEqual(media["label"], "Microscopy panel")
            self.assertEqual(media["source_path"], source_for(package).relative_path)
            self.assertEqual(media["bytes"], len(b"byte-identical-tiff-source"))
            self.assertEqual(
                media["sha256"],
                hashlib.sha256(b"byte-identical-tiff-source").hexdigest(),
            )
            self.assertFalse(media["ocr_performed"])
            self.assertEqual(
                (extraction / media["output_path"]).read_bytes(),
                b"byte-identical-tiff-source",
            )
            self.assertIn("Low-resolution", preview["label"])
            self.assertIn("not a rendered slide", preview["label"])
            self.assertEqual(preview["presentation_slide_count"], 1)
            self.assertEqual(
                (extraction / preview["output_path"]).read_bytes(),
                b"low-resolution-thumbnail",
            )

            diagnostic = next(
                warning
                for warning in warnings
                if warning["code"] == "pptx_authoring_path_accessibility_descriptions"
            )
            authoring_path = diagnostic["descriptions"][0]["description"]
            self.assertIn("author", authoring_path)
            self.assertTrue(authoring_path.endswith("panel.tif"))
            self.assertNotIn(authoring_path, "\n".join(visible))
            self.assertNotIn(authoring_path, str(assets))

    def test_media_assets_use_natural_numeric_part_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "natural-media-order.pptx"
            slide_rels = SLIDE_RELS_TEMPLATE.format(
                media_target="../media/image1.tiff"
            ).replace(
                "</Relationships>",
                """
  <Relationship Id="rIdImage2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image2.tiff"/>
  <Relationship Id="rIdImage10" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image10.tiff"/>
</Relationships>""",
            )
            write_package(
                package,
                extra_entries={
                    "ppt/slides/_rels/slide1.xml.rels": slide_rels.encode(),
                    "ppt/media/image2.tiff": b"image-two",
                    "ppt/media/image10.tiff": b"image-ten",
                },
            )

            _, _, assets, _ = extract_pptx_supplement(
                source_for(package),
                "supplement_001",
                pptx_path=package,
                extraction_root=root / "extraction",
            )

            media = [
                asset
                for asset in assets
                if asset["category"] == "supplement_image"
            ]
            self.assertEqual(
                [Path(asset["output_path"]).name for asset in media],
                ["image1.tiff", "image2.tiff", "image10.tiff"],
            )
            self.assertEqual(
                [asset["asset_id"] for asset in media],
                [
                    "supplement_001_media_001",
                    "supplement_001_media_002",
                    "supplement_001_media_003",
                ],
            )

    def test_reachable_embedded_workbook_is_preserved_and_parented_to_chart(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "workbook.pptx"
            extraction = root / "extraction"
            write_package(
                package,
                extra_entries={
                    "ppt/charts/_rels/chart1.xml.rels": CHART_RELS.encode(),
                    "ppt/embeddings/Microsoft_Excel_Worksheet1.xlsx": WORKBOOK_BYTES,
                },
            )

            _, tables, assets, warnings = extract_pptx_supplement(
                source_for(package),
                "supplement_001",
                pptx_path=package,
                extraction_root=extraction,
            )

            chart = next(table for table in tables if "_chart_" in table.table_id)
            workbook_assets = [
                asset for asset in assets if asset["category"] == "supplement_data"
            ]
            self.assertEqual(len(workbook_assets), 1)
            workbook = workbook_assets[0]
            self.assertEqual(
                workbook["asset_id"], f"{chart.table_id}_data_001"
            )
            self.assertEqual(workbook["parent_id"], chart.table_id)
            self.assertEqual(
                workbook["media_type"],
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            self.assertEqual(
                workbook["output_path"],
                "supplementary/supplement_001/embedded/"
                "Microsoft_Excel_Worksheet1.xlsx",
            )
            self.assertEqual(
                (extraction / workbook["output_path"]).read_bytes(), WORKBOOK_BYTES
            )
            self.assertEqual(
                workbook["sha256"], hashlib.sha256(WORKBOOK_BYTES).hexdigest()
            )
            self.assertEqual(workbook["content_id"], workbook["sha256"])
            self.assertIn("chart-part=ppt/charts/chart1.xml", workbook["source_locator"])
            self.assertFalse(
                any(
                    warning["code"] == "pptx_unreachable_embedded_workbooks"
                    for warning in warnings
                )
            )
            self.assertEqual(list(extraction.rglob("*.csv")), [])

    def test_unreachable_embedded_workbook_is_diagnosed_and_not_copied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "orphan-workbook.pptx"
            extraction = root / "extraction"
            orphan_part = "ppt/embeddings/Unreachable.xlsx"
            write_package(package, extra_entries={orphan_part: WORKBOOK_BYTES})

            _, _, assets, warnings = extract_pptx_supplement(
                source_for(package),
                "supplement_001",
                pptx_path=package,
                extraction_root=extraction,
            )

            self.assertFalse(
                any(asset["category"] == "supplement_data" for asset in assets)
            )
            diagnostic = next(
                warning
                for warning in warnings
                if warning["code"] == "pptx_unreachable_embedded_workbooks"
            )
            self.assertEqual(diagnostic["embedding_parts"], [orphan_part])
            self.assertFalse(
                (extraction / "supplementary/supplement_001/embedded").exists()
            )

    def test_missing_reachable_workbook_fails_before_asset_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "missing-workbook.pptx"
            extraction = root / "extraction"
            write_package(
                package,
                extra_entries={
                    "ppt/charts/_rels/chart1.xml.rels": CHART_RELS.encode()
                },
            )

            with self.assertRaisesRegex(
                ValueError, "chart package relationship references a missing part"
            ):
                extract_pptx_supplement(
                    source_for(package),
                    "supplement_001",
                    pptx_path=package,
                    extraction_root=extraction,
                )
            self.assertFalse(extraction.exists())

    def test_workbook_postcondition_detects_silent_materialization_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "lost-workbook.pptx"
            write_package(
                package,
                extra_entries={
                    "ppt/charts/_rels/chart1.xml.rels": CHART_RELS.encode(),
                    "ppt/embeddings/Microsoft_Excel_Worksheet1.xlsx": WORKBOOK_BYTES,
                },
            )

            with mock.patch(
                "scripts.extraction.pptx_supplement._materialize_embedded_workbooks",
                return_value=[],
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "reachable embedded workbooks did not materialize one-to-one",
                ):
                    extract_pptx_supplement(
                        source_for(package),
                        "supplement_001",
                        pptx_path=package,
                        extraction_root=root / "extraction",
                    )

    def test_refuses_to_overwrite_different_existing_embedded_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "workbook-collision.pptx"
            extraction = root / "extraction"
            destination = (
                extraction
                / "supplementary/supplement_001/embedded/"
                "Microsoft_Excel_Worksheet1.xlsx"
            )
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"different")
            write_package(
                package,
                extra_entries={
                    "ppt/charts/_rels/chart1.xml.rels": CHART_RELS.encode(),
                    "ppt/embeddings/Microsoft_Excel_Worksheet1.xlsx": WORKBOOK_BYTES,
                },
            )

            with self.assertRaisesRegex(FileExistsError, "refusing to replace"):
                extract_pptx_supplement(
                    source_for(package),
                    "supplement_001",
                    pptx_path=package,
                    extraction_root=extraction,
                )
            self.assertEqual(destination.read_bytes(), b"different")

    def test_asset_publish_race_cannot_replace_the_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            extraction = Path(temporary) / "extraction"
            extraction.mkdir()
            destination = extraction / "supplementary/supplement_001/embedded/data.xlsx"
            original_link = os.link

            def racing_link(source: object, target: object) -> None:
                Path(target).write_bytes(b"race-winner")
                original_link(source, target)

            with mock.patch(
                "scripts.extraction.pptx_supplement.os.link",
                side_effect=racing_link,
            ):
                with self.assertRaisesRegex(FileExistsError, "appeared during copy"):
                    _write_asset(
                        extraction,
                        "supplementary/supplement_001/embedded/data.xlsx",
                        WORKBOOK_BYTES,
                    )

            self.assertEqual(destination.read_bytes(), b"race-winner")
            self.assertEqual(list(destination.parent.glob(".*.tmp")), [])

    def test_rejects_unsafe_zip_member_before_writing_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "unsafe.pptx"
            extraction = root / "extraction"
            write_package(package, extra_entries={"../escaped.bin": b"bad"})
            with self.assertRaisesRegex(ValueError, "unsafe OOXML ZIP member"):
                extract_pptx_supplement(
                    source_for(package),
                    "supplement_001",
                    pptx_path=package,
                    extraction_root=extraction,
                )
            self.assertFalse((root / "escaped.bin").exists())

    def test_rejects_relationship_that_escapes_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "unsafe-rel.pptx"
            write_package(package, media_target="../../../../escaped.bin")
            with self.assertRaisesRegex(ValueError, "escapes the package"):
                extract_pptx_supplement(
                    source_for(package),
                    "supplement_001",
                    pptx_path=package,
                    extraction_root=root / "extraction",
                )

    def test_rejects_delayed_xml_entity_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "unsafe-entity.pptx"
            extraction = root / "extraction"
            malicious = (
                b'<?xml version="1.0" encoding="UTF-8"?>'
                + b" " * 5000
                + b'<!DOCTYPE p:presentation [<!ENTITY payload "expanded">]>'
                + b'<p:presentation xmlns:p="http://schemas.openxmlformats.org/'
                + b'presentationml/2006/main">&payload;</p:presentation>'
            )
            write_package(
                package,
                extra_entries={"ppt/presentation.xml": malicious},
            )
            with self.assertRaisesRegex(ValueError, "DTD/entity declarations"):
                extract_pptx_supplement(
                    source_for(package),
                    "supplement_001",
                    pptx_path=package,
                    extraction_root=extraction,
                )
            self.assertFalse(extraction.exists())

    def test_enforces_uncompressed_size_bound_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "bounded.pptx"
            write_package(package)
            with mock.patch(
                "scripts.extraction.pptx_supplement.MAX_TOTAL_UNCOMPRESSED_BYTES", 1
            ):
                with self.assertRaisesRegex(ValueError, "uncompressed byte limit"):
                    extract_pptx_supplement(
                        source_for(package),
                        "supplement_001",
                        pptx_path=package,
                        extraction_root=root / "extraction",
                    )

    def test_refuses_to_overwrite_different_existing_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "collision.pptx"
            extraction = root / "extraction"
            destination = (
                extraction / "supplementary/supplement_001/media/image1.tiff"
            )
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"different")
            write_package(package)
            with self.assertRaisesRegex(FileExistsError, "refusing to replace"):
                extract_pptx_supplement(
                    source_for(package),
                    "supplement_001",
                    pptx_path=package,
                    extraction_root=extraction,
                )
            self.assertEqual(destination.read_bytes(), b"different")


if __name__ == "__main__":
    unittest.main()
