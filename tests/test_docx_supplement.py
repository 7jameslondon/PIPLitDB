from __future__ import annotations

import hashlib
import io
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

from PIL import Image, ImageChops

from scripts.extraction.models import ContentBlock, SourceFile
from scripts.extraction.docx_supplement import (
    DOCX_FIGURE_RENDER_DPI,
    LEGACY_DOC_MEDIA_TYPE,
    _FIGURE_LABEL,
    _SCHEME_LABEL,
    _TABLE_LABEL,
    _decode_run_text,
    _heading_markdown,
    _apply_authored_crop,
    _image_before_figure_label_map,
    _is_caption_only_document_boundary,
    _is_standalone_bold_paragraph,
    _merge_wrapped_docx_text_blocks,
    _normalize_inline,
    _paragraph_relationship_widths,
    _paragraph_text,
    _render_docx_pdf,
    _word_docx_script,
    _word_pdf_script,
    RenderedDocxFigureCrop,
)
from scripts.extraction.supplements import extract_supplements


DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


class DocxSupplementTests(unittest.TestCase):
    def test_authored_crop_accepts_large_review_raster_without_leaking_pillow_limit(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (20, 10), "white").save(buffer, format="PNG")
        original_limit = Image.MAX_IMAGE_PIXELS
        try:
            Image.MAX_IMAGE_PIXELS = 50
            rendered, width, height = _apply_authored_crop(
                buffer.getvalue(), (25000, 0, 25000, 0)
            )
            self.assertEqual(Image.MAX_IMAGE_PIXELS, 50)
        finally:
            Image.MAX_IMAGE_PIXELS = original_limit

        self.assertEqual((width, height), (10, 10))
        with Image.open(io.BytesIO(rendered)) as image:
            self.assertEqual(image.size, (10, 10))

    def test_large_docx_display_derivative_is_scaled_without_clipping(self) -> None:
        source = Image.new("RGB", (12, 30), "red")
        for y in range(15, 30):
            for x in range(12):
                source.putpixel((x, y), (0, 0, 255))
        buffer = io.BytesIO()
        source.save(buffer, format="PNG")

        with mock.patch(
            "scripts.extraction.docx_supplement.DOCX_DISPLAY_MAX_EDGE", 10
        ):
            rendered, width, height = _apply_authored_crop(buffer.getvalue(), None)

        self.assertEqual((width, height), (4, 10))
        with Image.open(io.BytesIO(rendered)) as image:
            self.assertEqual(image.size, (4, 10))
            self.assertGreater(image.getpixel((2, 1))[0], 200)
            self.assertGreater(image.getpixel((2, 8))[2], 200)

    def test_adjacent_lowercase_docx_wraps_merge_without_crossing_boundaries(self) -> None:
        def block(number: int, text: str, *, kind: str = "text") -> ContentBlock:
            return ContentBlock(
                block_id=f"block-{number}",
                kind=kind,
                markdown=text,
                plain_text=text,
                source_path="papers (private)/00001/supplementary/source.docx",
                source_locator=(
                    f"docx-part=word/document.xml;body-child={number}"
                ),
            )

        merged = _merge_wrapped_docx_text_blocks(
            [
                block(1, "Treatment continued for 12"),
                block(2, "hours before collection."),
                block(3, "A complete paragraph."),
                block(4, "lowercase but separately authored."),
                block(5, "Methods", kind="subsection_heading"),
                block(6, "opening text"),
                block(7, "Cas9 Nuclease"),
                block(8, "3NLS was used."),
            ]
        )

        self.assertEqual(
            [(item.kind, item.plain_text, item.source_locator) for item in merged],
            [
                (
                    "text",
                    "Treatment continued for 12 hours before collection.",
                    "docx-part=word/document.xml;body-children=1-2",
                ),
                (
                    "text",
                    "A complete paragraph.",
                    "docx-part=word/document.xml;body-child=3",
                ),
                (
                    "text",
                    "lowercase but separately authored.",
                    "docx-part=word/document.xml;body-child=4",
                ),
                (
                    "subsection_heading",
                    "Methods",
                    "docx-part=word/document.xml;body-child=5",
                ),
                (
                    "text",
                    "opening text",
                    "docx-part=word/document.xml;body-child=6",
                ),
                (
                    "text",
                    "Cas9 Nuclease 3NLS was used.",
                    "docx-part=word/document.xml;body-children=7-8",
                ),
            ],
        )

    def test_ideographic_space_is_normalized_as_inline_whitespace(self) -> None:
        self.assertEqual(_normalize_inline("alpha\u3000beta"), "alpha beta")

    def test_nucleotide_prime_marks_normalize_without_changing_balanced_quotes(self) -> None:
        self.assertEqual(
            _normalize_inline("DNA 5’-ACGT-3’; ‘Duplex 1’"),
            "DNA 5′-ACGT-3′; ‘Duplex 1’",
        )

    def test_group_extent_is_not_assigned_to_one_of_multiple_blips(self) -> None:
        single = ET.fromstring(
            """<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
 <w:r><w:drawing><wp:inline><wp:extent cx="4000" cy="3000"/>
  <a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic>
 </wp:inline></w:drawing></w:r></w:p>"""
        )
        grouped = ET.fromstring(
            """<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
 <w:r><w:drawing><wp:inline><wp:extent cx="4000" cy="3000"/>
  <a:graphic><a:graphicData><a:blip r:embed="rId1"/><a:blip r:embed="rId2"/></a:graphicData></a:graphic>
 </wp:inline></w:drawing></w:r></w:p>"""
        )

        self.assertEqual(_paragraph_relationship_widths(single), {"rId1": 4000})
        self.assertEqual(_paragraph_relationship_widths(grouped), {})

    def test_classic_symbol_glyphs_and_adjacent_superscripts_are_canonical(self) -> None:
        self.assertEqual(
            _decode_run_text(
                "\uf02a\uf02f\uf057\uf061\uf062\uf064\uf067\uf06b\uf06c\uf06d\uf0b4",
                "Symbol",
            ),
            "*/Ωαβδγκλµ×",
        )
        self.assertEqual(_decode_run_text("k", "Symbol"), "κ")
        self.assertEqual(_decode_run_text("\uf0b0", "Symbol"), "°")
        self.assertEqual(_decode_run_text("\uf020", "Arial"), " ")
        self.assertEqual(_decode_run_text("＋", "Times"), "+")
        paragraph = ET.fromstring(
            """<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:r><w:t>Author</w:t></w:r>
 <w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t>a,b</w:t></w:r>
 <w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t>,</w:t></w:r>
 <w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t>‡</w:t></w:r>
</w:p>"""
        )
        markdown, plain = _paragraph_text(paragraph)
        self.assertEqual(
            markdown,
            "Author<sup>a,b</sup><sup>,</sup><sup>‡</sup>",
        )
        self.assertEqual(plain, "Author^{a,b,‡}")

    def test_whitespace_only_script_run_is_not_scientific_notation(self) -> None:
        paragraph = ET.fromstring(
            """<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:r><w:t xml:space="preserve">Karine </w:t></w:r>
 <w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t xml:space="preserve"> </w:t></w:r>
 <w:r><w:t>Nozeret</w:t></w:r>
</w:p>"""
        )

        markdown, plain = _paragraph_text(paragraph)

        self.assertEqual(markdown, "Karine Nozeret")
        self.assertEqual(plain, "Karine Nozeret")

        scientific = ET.fromstring(
            """<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:r><w:t>MgCl</w:t></w:r>
 <w:r><w:rPr><w:vertAlign w:val="subscript"/></w:rPr><w:t>2</w:t></w:r>
 <w:r><w:rPr><w:vertAlign w:val="subscript"/></w:rPr><w:t xml:space="preserve"> </w:t></w:r>
 <w:r><w:t>were measured</w:t></w:r>
</w:p>"""
        )

        markdown, plain = _paragraph_text(scientific)

        self.assertEqual(markdown, "MgCl<sub>2</sub> were measured")
        self.assertEqual(plain, "MgCl_{2} were measured")

    def test_word_is_preferred_and_writes_one_staged_pdf_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output_root = root / "render"
            converted_root = output_root / "converted"
            profile_root = output_root / "profile"
            output_root.mkdir()
            converted_root.mkdir()
            profile_root.mkdir()
            renderer_source = output_root / "input.docx"
            renderer_source.write_bytes(b"staged-only")

            def fake_run(command, **kwargs):
                self.assertIn("-NonInteractive", command)
                self.assertEqual(command[command.index("-SourcePath") + 1], str(renderer_source))
                output_path = Path(command[command.index("-OutputPath") + 1])
                output_path.write_bytes(b"%PDF-1.7\n")
                return subprocess.CompletedProcess(command, 0, "16.0\n", "")

            with (
                mock.patch(
                    "scripts.extraction.docx_supplement._libreoffice_executable",
                    return_value=Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
                ),
                mock.patch(
                    "scripts.extraction.docx_supplement._word_executable",
                    return_value=Path(r"C:\Program Files\Microsoft Office\WINWORD.EXE"),
                ),
                mock.patch(
                    "scripts.extraction.docx_supplement._powershell_executable",
                    return_value=Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"),
                ),
                mock.patch(
                    "scripts.extraction.docx_supplement.subprocess.run",
                    side_effect=fake_run,
                ),
            ):
                rendered, renderer, version = _render_docx_pdf(
                    renderer_source,
                    output_root,
                    converted_root,
                    profile_root,
                )

            self.assertEqual(rendered, (converted_root / "input.pdf").resolve())
            self.assertEqual(renderer, "Microsoft Word PDF compositor")
            self.assertEqual(version, "16.0")
            self.assertEqual(renderer_source.read_bytes(), b"staged-only")
            script = (output_root / "render-word-pdf.ps1").read_text(encoding="utf-8")
            self.assertEqual(script, _word_pdf_script())
            self.assertIn("$document = $documents.Open($SourcePath, $false, $true)", script)
            self.assertIn("ReleaseComObject($documents)", script)
            self.assertIn("try { $document.Close(0) } catch {}", script)
            self.assertIn("try { $word.Quit(0) } catch {}", script)
            self.assertIn("GetWindowThreadProcessId", script)
            self.assertIn("[IntPtr]$word.Hwnd", script)
            self.assertIn("$window = $document.ActiveWindow", script)
            self.assertIn("Stop-Process -Id $wordProcessId", script)

    def test_word_failure_falls_back_to_libreoffice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output_root = root / "render"
            converted_root = output_root / "converted"
            profile_root = output_root / "profile"
            output_root.mkdir()
            converted_root.mkdir()
            profile_root.mkdir()
            renderer_source = output_root / "input.docx"
            renderer_source.write_bytes(b"staged-only")
            calls: list[list[str]] = []

            def fake_run(command, **kwargs):
                calls.append(command)
                if "-NonInteractive" in command:
                    return subprocess.CompletedProcess(
                        command, 1, "", "Word could not open the converted file"
                    )
                if "--version" in command:
                    return subprocess.CompletedProcess(
                        command, 0, "LibreOffice 26.2.4.2\n", ""
                    )
                output_path = Path(command[command.index("--outdir") + 1]) / "input.pdf"
                output_path.write_bytes(b"%PDF-1.7\n")
                return subprocess.CompletedProcess(command, 0, "converted", "")

            with (
                mock.patch(
                    "scripts.extraction.docx_supplement._libreoffice_executable",
                    return_value=Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
                ),
                mock.patch(
                    "scripts.extraction.docx_supplement._word_executable",
                    return_value=Path(r"C:\Program Files\Microsoft Office\WINWORD.EXE"),
                ),
                mock.patch(
                    "scripts.extraction.docx_supplement._powershell_executable",
                    return_value=Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"),
                ),
                mock.patch(
                    "scripts.extraction.docx_supplement.subprocess.run",
                    side_effect=fake_run,
                ),
            ):
                rendered, renderer, version = _render_docx_pdf(
                    renderer_source,
                    output_root,
                    converted_root,
                    profile_root,
                )

            self.assertEqual(rendered, (converted_root / "input.pdf").resolve())
            self.assertEqual(
                renderer,
                "LibreOffice PDF compositor (Microsoft Word fallback)",
            )
            self.assertEqual(version, "LibreOffice 26.2.4.2")
            self.assertEqual(len(calls), 3)

    def test_authored_long_supplement_figure_labels_are_exactly_scoped(self) -> None:
        supplemental = _FIGURE_LABEL.fullmatch("Supplemental Figure 1. Caption")
        supplementary = _FIGURE_LABEL.fullmatch("Supplementary Figure 2: Caption")
        supplemental_s_number = _FIGURE_LABEL.fullmatch(
            "Supplemental Figure S5. Caption"
        )
        compact = _FIGURE_LABEL.fullmatch("Figure S3. Caption")
        compact_abbreviated = _FIGURE_LABEL.fullmatch("Fig. S4. Caption")
        split_compact = _FIGURE_LABEL.fullmatch("Figure S3-1. Caption")
        split_long = _FIGURE_LABEL.fullmatch("Supplementary Figure S3-2: Caption")
        abbreviated = _FIGURE_LABEL.fullmatch("Supplementary Fig. 4")

        self.assertIsNotNone(supplemental)
        self.assertEqual(supplemental.group("long_kind"), "Supplemental Figure")
        self.assertEqual(supplemental.group("long_number"), "1")
        self.assertIsNotNone(supplementary)
        self.assertEqual(supplementary.group("long_kind"), "Supplementary Figure")
        self.assertEqual(supplementary.group("long_number"), "2")
        self.assertIsNotNone(supplemental_s_number)
        self.assertEqual(
            supplemental_s_number.group("long_kind"), "Supplemental Figure"
        )
        self.assertEqual(supplemental_s_number.group("long_number"), "S5")
        self.assertIsNotNone(compact)
        self.assertEqual(compact.group("number"), "S3")
        self.assertIsNotNone(compact_abbreviated)
        self.assertEqual(compact_abbreviated.group("number"), "S4")
        self.assertIsNotNone(split_compact)
        self.assertEqual(split_compact.group("number"), "S3-1")
        self.assertIsNotNone(split_long)
        self.assertEqual(split_long.group("long_number"), "S3-2")
        self.assertIsNotNone(abbreviated)
        self.assertEqual(abbreviated.group("long_kind"), "Supplementary Fig.")
        self.assertEqual(abbreviated.group("long_number"), "4")

        for near_miss in (
            "Supplement Figure 1. Caption",
            "Supplemental Figs. 1. Caption",
            "Supplemental Figure A. Caption",
            "Supplementary Fig. S1–S3, with legends",
        ):
            with self.subTest(near_miss=near_miss):
                self.assertIsNone(_FIGURE_LABEL.fullmatch(near_miss))

    def test_legacy_doc_conversion_is_read_only_and_extracted_as_docx(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.doc"
            source_bytes = b"legacy-word-source"
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.doc",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=LEGACY_DOC_MEDIA_TYPE,
            )
            extraction_root = root / "extraction"
            rendered_buffer = io.BytesIO()
            Image.new("RGB", (30, 20), (40, 80, 120)).save(
                rendered_buffer, format="PNG"
            )
            rendered_bytes = rendered_buffer.getvalue()

            def fake_convert(renderer_source, output_root, converted_root, profile_root):
                self.assertEqual(renderer_source.read_bytes(), source_bytes)
                converted_path = converted_root / "source.docx"
                with zipfile.ZipFile(converted_path, "w") as archive:
                    archive.writestr(
                        "word/document.xml",
                        '<?xml version="1.0"?><w:document '
                        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                        '<w:body><w:p><w:r><w:t>Legacy text.</w:t></w:r></w:p>'
                        '<w:p><w:r><w:t>Figure S1. Legacy chart.</w:t></w:r></w:p>'
                        '<w:sectPr/></w:body></w:document>',
                    )
                    archive.writestr(
                        "word/_rels/document.xml.rels",
                        '<?xml version="1.0"?><Relationships '
                        'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
                    )
                return converted_path, "Test converter", "1.0"

            def fake_renderer(docx_path, output_root, requests, dpi):
                self.assertEqual(docx_path.read_bytes(), source_bytes)
                self.assertEqual(docx_path.suffix, ".doc")
                request = requests[0]
                rendered_path = output_root / "reviewed.png"
                rendered_path.write_bytes(rendered_bytes)
                return [
                    RenderedDocxFigureCrop(
                        asset_id=request.asset_id,
                        path=rendered_path,
                        pixel_width=30,
                        pixel_height=20,
                        page_count=request.expected_page_count,
                        page=request.page,
                        box=request.box,
                        dpi=dpi,
                        renderer="fake document compositor",
                        renderer_version="1.0",
                    )
                ]

            with mock.patch(
                "scripts.extraction.supplements.convert_legacy_doc_to_docx",
                side_effect=fake_convert,
            ):
                supplement = extract_supplements(
                    [source],
                    extraction_root,
                    docx_figure_renderer=fake_renderer,
                    docx_figure_crop_specs=[
                        {
                            "source_path": source.relative_path,
                            "source_sha256": source.sha256,
                            "asset_id": "supplement_001_figure_s1",
                            "page": 1,
                            "box": [10, 20, 110, 120],
                            "output_path": "figures/supplement_001/figure_s1.png",
                            "expected_page_count": 1,
                            "reason": "The legacy figure uses positioned objects.",
                            "evidence": "The reviewed page shows the authored layout.",
                        }
                    ],
                )[0]

            self.assertEqual(source_path.read_bytes(), source_bytes)
            self.assertEqual(
                (extraction_root / supplement.copied_path).read_bytes(), source_bytes
            )
            self.assertEqual(
                [block.plain_text for block in supplement.blocks], ["Legacy text."]
            )
            self.assertTrue(
                supplement.blocks[0].source_locator.startswith(
                    "legacy-doc-conversion=Test converter;version=1.0;"
                )
            )
            self.assertEqual(supplement.figures[0].output_path, "figures/supplement_001/figure_s1.png")
            self.assertEqual(
                (extraction_root / supplement.figures[0].output_path).read_bytes(),
                rendered_bytes,
            )

    def test_word_docx_script_opens_legacy_source_read_only(self) -> None:
        script = _word_docx_script()
        self.assertIn("Documents.Open($SourcePath, $false, $true)", script)
        self.assertIn("SaveAs2($OutputPath, 16)", script)

    def test_alternate_content_prefers_choice_and_reads_caption_textbox(self) -> None:
        image_bytes: list[bytes] = []
        for color in (
            (220, 10, 20),
            (10, 40, 220),
            (10, 180, 40),
            (220, 180, 10),
        ):
            buffer = io.BytesIO()
            Image.new("RGB", (4, 3), color).save(buffer, format="PNG")
            image_bytes.append(buffer.getvalue())

        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
 xmlns:v="urn:schemas-microsoft-com:vml"
 xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"><w:body>
 <w:p>
  <w:r><mc:AlternateContent>
   <mc:Choice Requires="wps">
    <w:drawing><wp:inline><wp:extent cx="4000" cy="3000"/><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic><w:txbxContent><w:p><w:r><w:t>Axis label A</w:t></w:r></w:p></w:txbxContent></wp:inline></w:drawing>
    <w:drawing><wp:inline><wp:extent cx="4000" cy="3000"/><a:graphic><a:graphicData><a:blip r:embed="rId2"/></a:graphicData></a:graphic><w:txbxContent><w:p><w:r><w:t>Axis label B</w:t></w:r></w:p></w:txbxContent></wp:inline></w:drawing>
   </mc:Choice>
   <mc:Fallback><w:pict><v:imagedata r:id="rId3"/><v:imagedata r:id="rId4"/><w:txbxContent><w:p><w:r><w:t>Fallback axis duplicate</w:t></w:r></w:p></w:txbxContent></w:pict></mc:Fallback>
  </mc:AlternateContent></w:r>
  <w:r><mc:AlternateContent>
   <mc:Choice Requires="wps"><w:drawing><w:txbxContent><w:p><w:r><w:t>Figure S1. Authored caption.</w:t></w:r></w:p></w:txbxContent></w:drawing></mc:Choice>
   <mc:Fallback><w:pict><w:txbxContent><w:p><w:r><w:t>Figure S1. Fallback duplicate.</w:t></w:r></w:p></w:txbxContent></w:pict></mc:Fallback>
  </mc:AlternateContent></w:r>
 </w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/choice1.png"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/choice2.png"/>
 <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/fallback1.png"/>
 <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/fallback2.png"/>
</Relationships>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)
            for name, data in zip(
                ("choice1.png", "choice2.png", "fallback1.png", "fallback2.png"),
                image_bytes,
            ):
                archive.writestr(f"word/media/{name}", data)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )
            extraction_root = root / "extraction"
            supplement = extract_supplements([source], extraction_root)[0]

            self.assertEqual(supplement.blocks, [])
            self.assertEqual(len(supplement.figures), 1)
            self.assertEqual(supplement.figures[0].label, "Figure S1")
            self.assertEqual(
                supplement.figures[0].caption_plain,
                "Figure S1. Authored caption.",
            )
            source_assets = [
                asset
                for asset in supplement.assets
                if asset["category"] == "supplement_source_image"
            ]
            self.assertEqual(len(source_assets), 2)
            self.assertEqual(
                [
                    (extraction_root / asset["output_path"]).read_bytes()
                    for asset in source_assets
                ],
                image_bytes[:2],
            )

    def test_textbox_caption_can_coexist_with_image_before_label_layout(self) -> None:
        image_bytes: list[bytes] = []
        for color in ((220, 10, 20), (10, 40, 220)):
            buffer = io.BytesIO()
            Image.new("RGB", (4, 3), color).save(buffer, format="PNG")
            image_bytes.append(buffer.getvalue())

        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><w:body>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic>
  <w:txbxContent><w:p><w:r><w:t>Axis label</w:t></w:r></w:p></w:txbxContent>
  <w:txbxContent><w:p><w:r><w:t>Figure S1. In-paragraph caption.</w:t></w:r></w:p></w:txbxContent>
 </w:drawing></w:r></w:p>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId2"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>
 <w:p><w:r><w:t>Figure S2. Following image-before-label caption.</w:t></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image2.png"/>
</Relationships>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)
            archive.writestr("word/media/image1.png", image_bytes[0])
            archive.writestr("word/media/image2.png", image_bytes[1])

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )
            extraction_root = root / "extraction"
            supplement = extract_supplements([source], extraction_root)[0]

            self.assertEqual(
                [(figure.label, figure.output_path) for figure in supplement.figures],
                [
                    ("Figure S1", "figures/supplement_001/figure_s1.png"),
                    ("Figure S2", "figures/supplement_001/figure_s2.png"),
                ],
            )
            source_assets = [
                asset
                for asset in supplement.assets
                if asset["category"] == "supplement_source_image"
            ]
            self.assertEqual(len(source_assets), 2)
            self.assertEqual(
                [
                    (extraction_root / asset["output_path"]).read_bytes()
                    for asset in source_assets
                ],
                image_bytes,
            )

    def test_drawing_between_label_first_captions_stays_with_earlier_figure(self) -> None:
        image_bytes: list[bytes] = []
        for color in ((220, 10, 20), (10, 40, 220)):
            buffer = io.BytesIO()
            Image.new("RGB", (4, 3), color).save(buffer, format="PNG")
            image_bytes.append(buffer.getvalue())

        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
 <w:p><w:r><w:t>Figure S1. First label-first caption.</w:t></w:r></w:p>
 <w:p/>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>
 <w:p/>
 <w:p><w:r><w:t>Figure S2. Second label-first caption.</w:t></w:r></w:p>
 <w:p/>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId2"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image2.png"/>
</Relationships>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)
            archive.writestr("word/media/image1.png", image_bytes[0])
            archive.writestr("word/media/image2.png", image_bytes[1])

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )
            extraction_root = root / "extraction"
            supplement = extract_supplements([source], extraction_root)[0]

            self.assertEqual(
                [(figure.label, figure.output_path) for figure in supplement.figures],
                [
                    ("Figure S1", "figures/supplement_001/figure_s1.png"),
                    ("Figure S2", "figures/supplement_001/figure_s2.png"),
                ],
            )
            for index, figure in enumerate(supplement.figures):
                with Image.open(extraction_root / figure.output_path) as rendered:
                    self.assertEqual(rendered.getpixel((0, 0)), ((220, 10, 20), (10, 40, 220))[index])

    def test_distinct_drawn_figures_with_reused_authored_number_get_unique_ids(self) -> None:
        image_bytes: list[bytes] = []
        for color in ((220, 10, 20), (10, 40, 220)):
            buffer = io.BytesIO()
            Image.new("RGB", (4, 3), color).save(buffer, format="PNG")
            image_bytes.append(buffer.getvalue())

        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
 <w:p><w:r><w:t>Figure S8. First authored drawing.</w:t></w:r></w:p>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>
 <w:p><w:r><w:t>Figure S8. Second authored drawing.</w:t></w:r></w:p>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId2"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image2.png"/>
</Relationships>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)
            archive.writestr("word/media/image1.png", image_bytes[0])
            archive.writestr("word/media/image2.png", image_bytes[1])

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )
            extraction_root = root / "extraction"
            supplement = extract_supplements([source], extraction_root)[0]

            self.assertEqual(
                [
                    (
                        figure.figure_id,
                        figure.label,
                        figure.caption_plain,
                        figure.output_path,
                    )
                    for figure in supplement.figures
                ],
                [
                    (
                        "supplement_001_figure_s8",
                        "Figure S8",
                        "Figure S8. First authored drawing.",
                        "figures/supplement_001/figure_s8.png",
                    ),
                    (
                        "supplement_001_figure_s8_occurrence_02",
                        "Figure S8",
                        "Figure S8. Second authored drawing.",
                        "figures/supplement_001/figure_s8_occurrence_02.png",
                    ),
                ],
            )
            self.assertIn(
                "authored-identity-occurrence=2",
                supplement.figures[1].source_locator,
            )
            for index, figure in enumerate(supplement.figures):
                with Image.open(extraction_root / figure.output_path) as rendered:
                    self.assertEqual(
                        rendered.getpixel((0, 0)),
                        ((220, 10, 20), (10, 40, 220))[index],
                    )

    def test_inline_references_are_split_from_caption_and_symbol_space_is_decoded(self) -> None:
        image_buffer = io.BytesIO()
        Image.new("RGB", (4, 3), (40, 80, 120)).save(image_buffer, format="PNG")
        document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
 <w:p><w:r><w:t>General: before</w:t></w:r><w:r><w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol"/></w:rPr><w:t>\uf020</w:t></w:r><w:r><w:t>β-alanine.</w:t></w:r></w:p>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></w:drawing><w:t>Figure S8. Authored caption.</w:t></w:r></w:p>
 <w:p><w:r><w:t>References:</w:t></w:r></w:p>
 <w:p><w:r><w:t>1. First reference.</w:t></w:r></w:p>
 <w:p><w:r><w:t>2. Second reference.</w:t></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>""".encode("utf-8")
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
</Relationships>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)
            archive.writestr("word/media/image1.png", image_buffer.getvalue())

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )
            supplement = extract_supplements([source], root / "extraction")[0]

            self.assertEqual(
                supplement.figures[0].caption_plain,
                "Figure S8. Authored caption.",
            )
            self.assertEqual(
                [(block.kind, block.plain_text) for block in supplement.blocks],
                [
                    ("text", "General: before β-alanine."),
                    ("subsection_heading", "References:"),
                    ("text", "1. First reference."),
                    ("text", "2. Second reference."),
                ],
            )

    def test_bare_label_before_drawing_coalesces_with_repeated_full_caption(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (4, 3), (40, 80, 120)).save(buffer, format="PNG")
        image_bytes = buffer.getvalue()
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
 <w:p><w:r><w:t>Figure S1</w:t></w:r></w:p>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>
 <w:p/>
 <w:p><w:r><w:t>Figure S1. Complete authored caption.</w:t></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
</Relationships>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)
            archive.writestr("word/media/image1.png", image_bytes)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )

            supplement = extract_supplements([source], root / "extraction")[0]

            self.assertEqual(len(supplement.figures), 1)
            self.assertEqual(supplement.figures[0].label, "Figure S1")
            self.assertEqual(
                supplement.figures[0].caption_plain,
                "Figure S1. Complete authored caption.",
            )
            with Image.open(root / "extraction" / supplement.figures[0].output_path) as rendered:
                self.assertEqual(rendered.getpixel((0, 0)), (40, 80, 120))

    def test_contents_caption_is_superseded_by_same_id_drawn_figure(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (4, 3), (60, 100, 140)).save(buffer, format="PNG")
        image_bytes = buffer.getvalue()
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
 <w:p><w:r><w:t>Supplementary information Contents</w:t></w:r></w:p>
 <w:p><w:r><w:t>Figure S1. Contents-list description.</w:t></w:r></w:p>
 <w:p><w:r><w:t>Supplementary Data</w:t></w:r></w:p>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></w:drawing><w:t>Figure S1. Complete authored caption.</w:t></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
</Relationships>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)
            archive.writestr("word/media/image1.png", image_bytes)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )

            supplement = extract_supplements([source], root / "extraction")[0]

            self.assertEqual(len(supplement.figures), 1)
            figure = supplement.figures[0]
            self.assertEqual(figure.figure_id, "supplement_001_figure_s1")
            self.assertEqual(figure.caption_plain, "Figure S1. Complete authored caption.")
            self.assertEqual(figure.output_path, "figures/supplement_001/figure_s1.png")

    def test_authored_supplement_scheme_labels_are_exactly_scoped(self) -> None:
        compact = _SCHEME_LABEL.fullmatch("Scheme S1. Synthesis route")
        long = _SCHEME_LABEL.fullmatch("Supplementary Scheme 2: Reaction")
        self.assertIsNotNone(compact)
        self.assertEqual(compact.group("number"), "S1")
        self.assertEqual(compact.group("title"), "Synthesis route")
        self.assertIsNotNone(long)
        self.assertEqual(long.group("long_kind"), "Supplementary Scheme")
        self.assertEqual(long.group("long_number"), "2")
        self.assertIsNone(_SCHEME_LABEL.fullmatch("Schemes S1. Not one scheme"))

    def test_authored_long_supplement_table_labels_are_exactly_scoped(self) -> None:
        supplemental = _TABLE_LABEL.fullmatch("Supplemental Table S1")
        supplementary = _TABLE_LABEL.fullmatch("Supplementary Table 2: Primers")
        compact = _TABLE_LABEL.fullmatch("Table S3. Antibodies")
        document_numbered = _TABLE_LABEL.fullmatch(
            "Table 4. Atom Types and the RESP Charges of HPY."
        )

        self.assertIsNotNone(supplemental)
        self.assertEqual(supplemental.group("long_kind"), "Supplemental Table")
        self.assertEqual(supplemental.group("long_number"), "S1")
        self.assertIsNotNone(supplementary)
        self.assertEqual(supplementary.group("long_kind"), "Supplementary Table")
        self.assertEqual(supplementary.group("long_number"), "2")
        self.assertEqual(supplementary.group("title"), "Primers")
        self.assertIsNotNone(compact)
        self.assertEqual(compact.group("number"), "S3")
        self.assertIsNotNone(document_numbered)
        self.assertEqual(document_numbered.group("number"), "4")
        self.assertEqual(
            document_numbered.group("title"),
            "Atom Types and the RESP Charges of HPY.",
        )

        for near_miss in (
            "Supplement Table S1",
            "Supplemental Tables S1",
            "Supplemental Table A",
        ):
            with self.subTest(near_miss=near_miss):
                self.assertIsNone(_TABLE_LABEL.fullmatch(near_miss))

    def test_plain_numbered_document_table_captions_do_not_become_prior_footnotes(self) -> None:
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
 <w:p><w:r><w:t>Table 1. First authored values.</w:t></w:r></w:p>
 <w:tbl>
  <w:tr><w:tc><w:p><w:r><w:t>Name</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>1.0</w:t></w:r></w:p></w:tc></w:tr>
 </w:tbl>
 <w:p><w:r><w:t>Table 2. Second authored values.</w:t></w:r></w:p>
 <w:tbl>
  <w:tr><w:tc><w:p><w:r><w:t>Name</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>2.0</w:t></w:r></w:p></w:tc></w:tr>
 </w:tbl>
 <w:p><w:r><w:t>Note: applies only to Table 2.</w:t></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )

            supplement = extract_supplements([source], root / "extraction")[0]

        self.assertEqual([table.label for table in supplement.tables], ["Table 1", "Table 2"])
        self.assertEqual(
            [table.title_plain for table in supplement.tables],
            ["Table 1. First authored values.", "Table 2. Second authored values."],
        )
        self.assertEqual(supplement.tables[0].footnotes_plain, [])
        self.assertEqual(
            supplement.tables[1].footnotes_plain,
            ["Note: applies only to Table 2."],
        )

    def test_complete_bold_paragraph_is_a_heading_but_bold_lead_is_not(self) -> None:
        self.assertTrue(
            _is_standalone_bold_paragraph(
                "<strong><em>In vivo</em></strong><strong> study</strong>",
                "In vivo study",
            )
        )
        self.assertFalse(
            _is_standalone_bold_paragraph(
                "<strong>Fig. S1.</strong> Authored caption.",
                "Fig. S1. Authored caption.",
            )
        )
        self.assertTrue(
            _is_standalone_bold_paragraph(
                "<strong>2.</strong><strong> Data for polyamides </strong>"
                "<strong>1</strong>−<strong>6</strong>",
                "2. Data for polyamides 1−6",
            )
        )
        self.assertEqual(
            _heading_markdown(
                "<strong>Supplementary <em>in vivo</em> Methods</strong>"
            ),
            "Supplementary <em>in vivo</em> Methods",
        )
        self.assertTrue(
            _is_caption_only_document_boundary(
                "<strong>Supplementary Materials and Methods</strong>",
                "Supplementary Materials and Methods",
            )
        )
        self.assertTrue(
            _is_caption_only_document_boundary(
                "<strong>SUPPLEMENTARY FIGURES</strong>",
                "SUPPLEMENTARY FIGURES",
            )
        )
        self.assertTrue(
            _is_caption_only_document_boundary(
                "<strong>Supplementary Movie 1-8.</strong> Live imaging.",
                "Supplementary Movie 1-8. Live imaging.",
            )
        )
        self.assertTrue(
            _is_caption_only_document_boundary(
                "<strong>Supplementary Text</strong>",
                "Supplementary Text",
            )
        )
        self.assertFalse(
            _is_caption_only_document_boundary(
                "<strong>(A) Quantification details</strong>",
                "(A) Quantification details",
            )
        )

    def test_caption_only_figure_stops_at_explicit_methods_section(self) -> None:
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
 <w:p><w:r><w:t>Supplementary Fig. 3</w:t></w:r></w:p>
 <w:p><w:r><w:t>Authored figure caption.</w:t></w:r></w:p>
 <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Supplementary Materials and Methods</w:t></w:r></w:p>
 <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>PCR analysis</w:t></w:r></w:p>
 <w:p><w:r><w:t>The primer sequence was reviewed.</w:t></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )

            supplement = extract_supplements([source], root / "extraction")[0]

        self.assertEqual(len(supplement.figures), 1)
        self.assertEqual(
            supplement.figures[0].caption_plain,
            "Supplementary Fig. 3\nAuthored figure caption.",
        )
        self.assertEqual(
            [(block.kind, block.plain_text, block.markdown) for block in supplement.blocks],
            [
                (
                    "subsection_heading",
                    "Supplementary Materials and Methods",
                    "Supplementary Materials and Methods",
                ),
                ("subsection_heading", "PCR analysis", "PCR analysis"),
                ("text", "The primer sequence was reviewed.", "The primer sequence was reviewed."),
            ],
        )

    def test_caption_only_figure_stops_at_word_heading_style(self) -> None:
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
 <w:p><w:r><w:t>Figure S1. Authored caption.</w:t></w:r></w:p>
 <w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>RMSF</w:t></w:r></w:p>
 <w:p><w:r><w:t>Authored section text.</w:t></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )

            supplement = extract_supplements([source], root / "extraction")[0]

        self.assertEqual(supplement.figures[0].caption_plain, "Figure S1. Authored caption.")
        self.assertEqual(
            [(block.kind, block.plain_text) for block in supplement.blocks],
            [("subsection_heading", "RMSF"), ("text", "Authored section text.")],
        )

    def test_caption_only_figure_stops_before_media_legend_and_supplement_text(self) -> None:
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
 <w:p><w:r><w:t>Supplementary Figure 8. Authored figure caption.</w:t></w:r></w:p>
 <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Supplementary Movie 1-8.</w:t></w:r><w:r><w:t> Live imaging.</w:t></w:r></w:p>
 <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Supplementary Text</w:t></w:r></w:p>
 <w:p><w:r><w:t>Polyamide synthesis details.</w:t></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )

            supplement = extract_supplements([source], root / "extraction")[0]

        self.assertEqual(len(supplement.figures), 1)
        self.assertEqual(
            supplement.figures[0].caption_plain,
            "Supplementary Figure 8. Authored figure caption.",
        )
        self.assertEqual(
            [(block.kind, block.plain_text) for block in supplement.blocks],
            [
                ("text", "Supplementary Movie 1-8. Live imaging."),
                ("subsection_heading", "Supplementary Text"),
                ("text", "Polyamide synthesis details."),
            ],
        )

    @staticmethod
    def _fixture_bytes() -> bytes:
        image_buffer = io.BytesIO()
        Image.new("RGB", (3, 2), (17, 33, 65)).save(
            image_buffer, format="TIFF", compression="tiff_lzw"
        )
        document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
 <w:body>
  <w:p><w:r><w:t>Figure S1. Wnt/</w:t></w:r><w:r><w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol"/></w:rPr><w:t>b</w:t></w:r><w:r><w:t>-catenin</w:t></w:r></w:p>
  <w:p><w:r><w:t>Damage </w:t></w:r><w:r><w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol"/></w:rPr><w:t>g</w:t></w:r><w:r><w:t>H2A.X at 2.5 </w:t></w:r><w:r><w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol"/></w:rPr><w:t>m</w:t></w:r><w:r><w:t>M; regular bgm.</w:t></w:r></w:p>
  <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>
  <w:p><w:r><w:t>Legend text authored after the inline image.</w:t></w:r></w:p>
  <w:p><w:r><w:t>Table S1. Antibodies</w:t></w:r></w:p>
  <w:tbl>
   <w:tr><w:tc><w:p><w:r><w:t>Antigen</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>
   <w:tr><w:tc><w:p><w:r><w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol"/></w:rPr><w:t></w:t></w:r><w:r><w:t>-catenin</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>#8480</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl>
  <w:p><w:r><w:t>Note: exact OOXML.</w:t></w:r></w:p>
  <w:sectPr/>
 </w:body>
</w:document>""".encode("utf-8")
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.tiff"/>
</Relationships>"""
        content_types = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="xml" ContentType="application/xml"/>
 <Default Extension="tiff" ContentType="image/tiff"/>
 <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types)
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)
            archive.writestr("word/media/image1.tiff", image_buffer.getvalue())
        return package.getvalue()

    def test_native_docx_figures_tables_and_symbol_runs_are_lossless(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = self._fixture_bytes()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )
            extraction_root = root / "extraction"

            supplements = extract_supplements([source], extraction_root)

            self.assertEqual(source_path.read_bytes(), source_bytes)
            self.assertEqual(len(supplements), 1)
            supplement = supplements[0]
            self.assertEqual(supplement.warnings, [])
            self.assertEqual(supplement.blocks, [])
            self.assertEqual(len(supplement.figures), 1)
            figure = supplement.figures[0]
            self.assertEqual(figure.label, "Figure S1")
            self.assertEqual(
                figure.caption_plain,
                "Figure S1. Wnt/β-catenin\nDamage γH2A.X at 2.5 µM; regular bgm.\n"
                "Legend text authored after the inline image.",
            )
            self.assertFalse(any(asset.get("ocr_performed") for asset in supplement.assets))

            copied = extraction_root / Path(supplement.copied_path)
            self.assertEqual(copied.read_bytes(), source_bytes)
            self.assertEqual(len(supplement.assets), 2)
            source_asset = next(
                asset
                for asset in supplement.assets
                if asset["category"] == "supplement_source_image"
            )
            display_asset = next(
                asset for asset in supplement.assets if asset["category"] == "figure"
            )
            self.assertEqual(
                (extraction_root / Path(source_asset["output_path"])).read_bytes(),
                zipfile.ZipFile(io.BytesIO(source_bytes)).read("word/media/image1.tiff"),
            )
            with Image.open(extraction_root / Path(source_asset["output_path"])) as source_image:
                with Image.open(extraction_root / Path(display_asset["output_path"])) as display_image:
                    self.assertIsNone(
                        ImageChops.difference(
                            source_image.convert("RGB"), display_image.convert("RGB")
                        ).getbbox()
                    )

            self.assertEqual(len(supplement.tables), 1)
            table = supplement.tables[0]
            self.assertEqual(table.source_kind, "document")
            self.assertFalse(table.requires_source_image)
            self.assertEqual(table.label, "Table S1")
            self.assertEqual(
                [[cell.text for cell in row] for row in table.parts[0].rows],
                [["Antigen", "Value"], ["β-catenin", "#8480"]],
            )
            self.assertEqual(table.footnotes_plain, ["Note: exact OOXML."])

    def test_captioned_legacy_vector_is_preserved_for_reviewed_page_crop(self) -> None:
        vector_bytes = b"\x01\x00\x09\x00\x00\x03legacy-wmf-not-pillow-readable"
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
 <w:p><w:r><w:t>Figure S1. Contents entry.</w:t></w:r></w:p>
 <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Supplementary Figures</w:t></w:r></w:p>
 <w:p><w:r><w:t>Figure S1. Authored vector figure.</w:t></w:r></w:p>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/figure.wmf"/>
</Relationships>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)
            archive.writestr("word/media/figure.wmf", vector_bytes)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )

            supplement = extract_supplements([source], root / "extraction")[0]

            self.assertEqual(len(supplement.figures), 1)
            figure = supplement.figures[0]
            self.assertEqual(figure.label, "Figure S1")
            self.assertEqual(figure.caption_plain, "Figure S1. Authored vector figure.")
            self.assertIsNone(figure.output_path)
            self.assertIn("requires-reviewed-native-layout-render", figure.source_locator)
            self.assertEqual(len(supplement.assets), 1)
            source_asset = supplement.assets[0]
            self.assertEqual(source_asset["category"], "supplement_source_image")
            self.assertEqual(source_asset["media_type"], "image/wmf")
            self.assertEqual(
                (root / "extraction" / source_asset["output_path"]).read_bytes(),
                vector_bytes,
            )

    def test_uncaptioned_docx_visual_is_exposed_without_inferred_figure(self) -> None:
        image_buffer = io.BytesIO()
        Image.new("RGB", (40, 20), (12, 80, 190)).save(
            image_buffer, format="PNG"
        )
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"><w:body>
 <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Copies of NMR Spectra</w:t></w:r></w:p>
 <w:p><w:r><w:drawing><wp:inline><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/spectrum.png"/>
</Relationships>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)
            archive.writestr("word/media/spectrum.png", image_buffer.getvalue())

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )

            supplement = extract_supplements([source], root / "extraction")[0]

            self.assertEqual(supplement.figures, [])
            self.assertEqual(len(supplement.assets), 1)
            asset = supplement.assets[0]
            self.assertEqual(asset["category"], "supplement_image")
            self.assertEqual(asset["media_type"], "image/png")
            self.assertFalse(asset["ocr_performed"])
            self.assertIn("word/media/spectrum.png", asset["source_locator"])
            with Image.open(root / "extraction" / asset["output_path"]) as rendered:
                self.assertEqual(rendered.size, (40, 20))

    def test_reviewed_crop_materializes_unsupported_uncaptioned_visual(self) -> None:
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"><w:body>
 <w:p><w:r><w:t>Compound structure</w:t></w:r></w:p>
 <w:p><w:r><w:drawing><wp:inline><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/structure.pct"/>
</Relationships>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)
            archive.writestr("word/media/structure.pct", b"unsupported-pict")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            source_relative = "papers (private)/00001/supplementary/source.docx"
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path=source_relative,
                size=len(source_bytes),
                sha256=source_hash,
                detected_format=DOCX_MEDIA_TYPE,
            )
            rendered_buffer = io.BytesIO()
            Image.new("RGB", (90, 45), (20, 110, 180)).save(
                rendered_buffer, format="PNG"
            )
            rendered_bytes = rendered_buffer.getvalue()

            def fake_renderer(docx_path, output_root, requests, dpi):
                request = requests[0]
                rendered_path = output_root / "reviewed.png"
                rendered_path.write_bytes(rendered_bytes)
                return [
                    RenderedDocxFigureCrop(
                        asset_id=request.asset_id,
                        path=rendered_path,
                        pixel_width=90,
                        pixel_height=45,
                        page_count=1,
                        page=1,
                        box=request.box,
                        dpi=dpi,
                        renderer="fake document compositor",
                        renderer_version="1.0",
                    )
                ]

            extraction_root = root / "extraction"
            supplement = extract_supplements(
                [source],
                extraction_root,
                docx_figure_renderer=fake_renderer,
                docx_figure_crop_specs=[
                    {
                        "source_path": source_relative,
                        "source_sha256": source_hash,
                        "asset_id": "supplement_001_embedded_visual_001",
                        "page": 1,
                        "box": [10, 20, 110, 120],
                        "output_path": "figures/supplement_001/embedded_visual_001.png",
                        "expected_page_count": 1,
                        "reason": "The legacy PICT cannot be decoded directly.",
                        "evidence": "The reviewed page render contains the authored structure.",
                    }
                ],
            )[0]

            self.assertEqual(supplement.figures, [])
            self.assertEqual(supplement.warnings, [])
            self.assertEqual(supplement.asset_ids, ["supplement_001_embedded_visual_001"])
            asset = supplement.assets[0]
            self.assertEqual(asset["category"], "supplement_image")
            self.assertEqual(asset["render_method"], "reviewed_docx_pdf_crop")
            self.assertIn("word/media/structure.pct", asset["source_locator"])
            self.assertEqual(
                (extraction_root / asset["output_path"]).read_bytes(), rendered_bytes
            )

    def test_reviewed_docx_crop_replaces_only_the_primary_figure_display(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = self._fixture_bytes()
            source_path.write_bytes(source_bytes)
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            source_relative = (
                "papers (private)/00001/supplementary/source.docx"
            )
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path=source_relative,
                size=len(source_bytes),
                sha256=source_hash,
                detected_format=DOCX_MEDIA_TYPE,
            )
            rendered_buffer = io.BytesIO()
            Image.new("RGB", (60, 40), (12, 120, 210)).save(
                rendered_buffer, format="PNG"
            )
            rendered_bytes = rendered_buffer.getvalue()

            def fake_renderer(docx_path, output_root, requests, dpi):
                self.assertEqual(docx_path.read_bytes(), source_bytes)
                self.assertEqual(dpi, DOCX_FIGURE_RENDER_DPI)
                self.assertEqual(len(requests), 1)
                request = requests[0]
                rendered_path = output_root / "reviewed.png"
                rendered_path.write_bytes(rendered_bytes)
                return [
                    RenderedDocxFigureCrop(
                        asset_id=request.asset_id,
                        path=rendered_path,
                        pixel_width=60,
                        pixel_height=40,
                        page_count=request.expected_page_count,
                        page=request.page,
                        box=request.box,
                        dpi=dpi,
                        renderer="fake document compositor",
                        renderer_version="1.0",
                    )
                ]

            extraction_root = root / "extraction"
            supplement = extract_supplements(
                [source],
                extraction_root,
                docx_figure_renderer=fake_renderer,
                docx_figure_crop_specs=[
                    {
                        "source_path": source_relative,
                        "source_sha256": source_hash,
                        "asset_id": "supplement_001_figure_s1",
                        "page": 2,
                        "box": [10, 20, 110, 120],
                        "output_path": "figures/supplement_001/figure_s1.png",
                        "expected_page_count": 3,
                        "reason": "The figure includes native positioned labels.",
                        "evidence": "The reviewed page render contains the complete layout.",
                    }
                ],
            )[0]

            self.assertEqual(source_path.read_bytes(), source_bytes)
            primary = next(
                asset
                for asset in supplement.assets
                if asset["asset_id"] == "supplement_001_figure_s1"
            )
            self.assertEqual(
                (extraction_root / primary["output_path"]).read_bytes(),
                rendered_bytes,
            )
            self.assertEqual((primary["width"], primary["height"]), (60, 40))
            self.assertEqual(primary["render_method"], "reviewed_docx_pdf_crop")
            source_asset = next(
                asset
                for asset in supplement.assets
                if asset["category"] == "supplement_source_image"
            )
            self.assertNotEqual(
                (extraction_root / source_asset["output_path"]).read_bytes(),
                rendered_bytes,
            )
            self.assertIn(
                "docx-render=complete-authored-layout",
                supplement.figures[0].source_locator,
            )

    def test_reviewed_docx_crop_materializes_captioned_composite_without_media(self) -> None:
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
 <w:p><w:r><w:t>Figure S1. Composite chart with native labels.</w:t></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            source_relative = "papers (private)/00001/supplementary/source.docx"
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path=source_relative,
                size=len(source_bytes),
                sha256=source_hash,
                detected_format=DOCX_MEDIA_TYPE,
            )
            rendered_buffer = io.BytesIO()
            Image.new("RGB", (80, 50), (50, 100, 150)).save(
                rendered_buffer, format="PNG"
            )
            rendered_bytes = rendered_buffer.getvalue()

            def fake_renderer(docx_path, output_root, requests, dpi):
                request = requests[0]
                rendered_path = output_root / "reviewed.png"
                rendered_path.write_bytes(rendered_bytes)
                return [
                    RenderedDocxFigureCrop(
                        asset_id=request.asset_id,
                        path=rendered_path,
                        pixel_width=80,
                        pixel_height=50,
                        page_count=1,
                        page=1,
                        box=request.box,
                        dpi=dpi,
                        renderer="fake document compositor",
                        renderer_version="1.0",
                    )
                ]

            extraction_root = root / "extraction"
            supplement = extract_supplements(
                [source],
                extraction_root,
                docx_figure_renderer=fake_renderer,
                docx_figure_crop_specs=[
                    {
                        "source_path": source_relative,
                        "source_sha256": source_hash,
                        "asset_id": "supplement_001_figure_s1",
                        "page": 1,
                        "box": [10, 20, 110, 120],
                        "output_path": "figures/supplement_001/figure_s1.png",
                        "expected_page_count": 1,
                        "reason": "The figure is composed from native chart objects.",
                        "evidence": "The reviewed page render contains the complete layout.",
                    }
                ],
            )[0]

            self.assertEqual(supplement.warnings, [])
            figure = supplement.figures[0]
            self.assertEqual(
                figure.output_path, "figures/supplement_001/figure_s1.png"
            )
            asset = next(
                item for item in supplement.assets if item["asset_id"] == figure.figure_id
            )
            self.assertEqual(asset["category"], "figure")
            self.assertEqual(asset["render_method"], "reviewed_docx_pdf_crop")
            self.assertEqual(
                (extraction_root / asset["output_path"]).read_bytes(), rendered_bytes
            )

    def test_separate_supplemental_table_title_stops_before_methods(self) -> None:
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
 <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Supplemental Table S1</w:t></w:r></w:p>
 <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Target sequences for shRNA knockdown experiments.</w:t></w:r></w:p>
 <w:tbl>
  <w:tr><w:tc><w:p><w:r><w:t>shRNA</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Sequence</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>shLuc</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>CGTACGCGGAATACTTCGA</w:t></w:r></w:p></w:tc></w:tr>
 </w:tbl>
 <w:p/>
 <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>SUPPLEMENTARY FIGURES</w:t></w:r></w:p>
 <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Supplemental Methods</w:t></w:r></w:p>
 <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>ChIP-Seq data analysis</w:t></w:r></w:p>
 <w:p><w:r><w:t>Authored methods text.</w:t></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )

            supplement = extract_supplements([source], root / "extraction")[0]

        self.assertEqual(len(supplement.tables), 1)
        table = supplement.tables[0]
        self.assertEqual(table.label, "Supplemental Table S1")
        self.assertEqual(
            table.title_plain,
            "Supplemental Table S1\nTarget sequences for shRNA knockdown experiments.",
        )
        self.assertEqual(table.footnotes_plain, [])
        self.assertEqual(
            [(block.kind, block.plain_text) for block in supplement.blocks],
            [
                ("subsection_heading", "SUPPLEMENTARY FIGURES"),
                ("subsection_heading", "Supplemental Methods"),
                ("subsection_heading", "ChIP-Seq data analysis"),
                ("text", "Authored methods text."),
            ],
        )

    def test_decimal_numbered_references_after_table_are_not_footnotes(self) -> None:
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
 <w:p><w:r><w:t>Table S2. Energy efficiencies.</w:t></w:r></w:p>
 <w:tbl>
  <w:tr><w:tc><w:p><w:r><w:t>Construct</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Efficiency</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>3WJ</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>27</w:t></w:r></w:p></w:tc></w:tr>
 </w:tbl>
 <w:p><w:r><w:t>3.0 References</w:t></w:r></w:p>
 <w:p><w:r><w:t>[1] First exact reference.</w:t></w:r></w:p>
 <w:p><w:r><w:t>[2] Second exact reference.[3] Third exact reference.</w:t></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )

            supplement = extract_supplements([source], root / "extraction")[0]

        self.assertEqual(len(supplement.tables), 1)
        self.assertEqual(supplement.tables[0].footnotes_plain, [])
        self.assertEqual(
            [(block.kind, block.plain_text) for block in supplement.blocks],
            [
                ("subsection_heading", "3.0 References"),
                ("text", "[1] First exact reference."),
                ("text", "[2] Second exact reference."),
                ("text", "[3] Third exact reference."),
            ],
        )

    @staticmethod
    def _image_before_label_fixture_bytes() -> tuple[bytes, bytes, bytes]:
        images: list[bytes] = []
        for color in ((220, 10, 20), (10, 40, 220)):
            buffer = io.BytesIO()
            Image.new("RGB", (4, 3), color).save(buffer, format="PNG")
            images.append(buffer.getvalue())
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
 <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Experimental observations</w:t></w:r></w:p>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>
 <w:p><w:r><w:t>Supplemental Figure S1</w:t></w:r></w:p>
 <w:p><w:r><w:t>First authored caption.</w:t></w:r></w:p><w:p/>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId2"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>
 <w:p><w:r><w:t>Supplementary Fig. 2</w:t></w:r></w:p>
 <w:p><w:r><w:t>Second authored caption.</w:t></w:r></w:p><w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image2.png"/>
</Relationships>"""
        content_types = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="xml" ContentType="application/xml"/>
 <Default Extension="png" ContentType="image/png"/>
 <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types)
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)
            archive.writestr("word/media/image1.png", images[0])
            archive.writestr("word/media/image2.png", images[1])
        return package.getvalue(), images[0], images[1]

    def test_image_before_label_layout_links_each_drawing_to_its_own_figure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            package, first_image, second_image = self._image_before_label_fixture_bytes()
            source_path = root / "source.docx"
            source_path.write_bytes(package)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(package),
                sha256=hashlib.sha256(package).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )

            supplement = extract_supplements([source], root / "extraction")[0]

            self.assertEqual(
                [(figure.label, figure.caption_plain) for figure in supplement.figures],
                [
                    (
                        "Supplemental Figure S1",
                        "Supplemental Figure S1\nFirst authored caption.",
                    ),
                    ("Supplementary Fig. 2", "Supplementary Fig. 2\nSecond authored caption."),
                ],
            )
            self.assertEqual(
                [(block.kind, block.plain_text) for block in supplement.blocks],
                [("subsection_heading", "Experimental observations")],
            )
            source_assets = [
                asset
                for asset in supplement.assets
                if asset["category"] == "supplement_source_image"
            ]
            self.assertEqual(len(source_assets), 2)
            self.assertEqual(
                (root / "extraction" / source_assets[0]["output_path"]).read_bytes(),
                first_image,
            )
            self.assertEqual(
                (root / "extraction" / source_assets[1]["output_path"]).read_bytes(),
                second_image,
            )

    def test_new_page_label_does_not_steal_previous_label_first_image(self) -> None:
        images: list[bytes] = []
        for color in ((220, 10, 20), (10, 40, 220)):
            buffer = io.BytesIO()
            Image.new("RGB", (4, 3), color).save(buffer, format="PNG")
            images.append(buffer.getvalue())
        blank_rows = b"<w:p/>" * 8
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
 <w:p><w:r><w:t>Figure S2. First authored caption.</w:t></w:r></w:p>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>
""" + blank_rows + b"""
 <w:p><w:r><w:lastRenderedPageBreak/><w:t>Figure S3. New-page caption.</w:t></w:r></w:p>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId2"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image2.png"/>
</Relationships>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)
            archive.writestr("word/media/image1.png", images[0])
            archive.writestr("word/media/image2.png", images[1])

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )
            supplement = extract_supplements([source], root / "extraction")[0]

        self.assertEqual(
            [(figure.label, figure.output_path) for figure in supplement.figures],
            [
                ("Figure S2", "figures/supplement_001/figure_s2.png"),
                ("Figure S3", "figures/supplement_001/figure_s3.png"),
            ],
        )
        self.assertNotIn("component", supplement.figures[1].output_path or "")

    def test_image_before_caption_stops_at_preceding_hard_page_break(self) -> None:
        document = ET.fromstring(
            """<w:body xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
 <w:p><w:r><w:t>Supplemental Figures S2</w:t></w:r></w:p>
 <w:p><w:r><w:t>References S15</w:t><w:br w:type="page"/></w:r></w:p>
 <w:p/>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>
 <w:p><w:r><w:t>Figure S1. Authored caption.</w:t></w:r></w:p>
</w:body>"""
        )

        mapping = _image_before_figure_label_map(list(document))

        self.assertEqual(mapping, {5: ([4], ["rId1"])})

    def test_image_before_caption_accepts_panel_markers_and_drawing_text(self) -> None:
        image_bytes: list[bytes] = []
        for color in ((30, 40, 50), (60, 70, 80), (90, 100, 110), (120, 130, 140)):
            buffer = io.BytesIO()
            Image.new("RGB", (4, 3), color).save(buffer, format="PNG")
            image_bytes.append(buffer.getvalue())

        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><w:body>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></w:drawing><w:t>Figure S20. First caption.</w:t></w:r></w:p>
 <w:p><w:r><w:t>2.7 FRET analysis along 3WJ</w:t></w:r></w:p>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId2"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>
 <w:p><w:r><w:t>a)</w:t></w:r></w:p>
 <w:p><w:r><w:t>Propeller</w:t></w:r></w:p>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId3"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>
 <w:p><w:r><w:t>b)</w:t></w:r></w:p>
 <w:p><w:r><w:t>Figure S21. Two-panel caption.</w:t></w:r></w:p>
 <w:p><w:r><w:drawing><a:graphic><a:graphicData><pic:blipFill><a:blip r:embed="rId4"/><a:srcRect l="25000" r="25000"/></pic:blipFill></a:graphicData></a:graphic></w:drawing><w:t>N3HNT</w:t></w:r></w:p>
 <w:p><w:r><w:t>Figure S22. Annotated-image caption.</w:t></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image2.png"/>
 <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image3.png"/>
 <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image4.png"/>
</Relationships>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)
            for number, data in enumerate(image_bytes, 1):
                archive.writestr(f"word/media/image{number}.png", data)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )

            supplement = extract_supplements([source], root / "extraction")[0]

        self.assertEqual(
            [(figure.label, figure.caption_plain) for figure in supplement.figures],
            [
                ("Figure S20", "Figure S20. First caption."),
                ("Figure S21", "Figure S21. Two-panel caption."),
                ("Figure S22", "Figure S22. Annotated-image caption."),
            ],
        )
        self.assertEqual(
            [(block.kind, block.plain_text) for block in supplement.blocks],
            [("subsection_heading", "2.7 FRET analysis along 3WJ")],
        )
        source_assets = [
            asset
            for asset in supplement.assets
            if asset["category"] == "supplement_source_image"
        ]
        self.assertEqual(len(source_assets), 4)
        composed = next(
            asset
            for asset in supplement.assets
            if asset["asset_id"] == "supplement_001_figure_s21"
        )
        self.assertEqual((composed["width"], composed["height"]), (4, 14))
        cropped = next(
            asset
            for asset in supplement.assets
            if asset["asset_id"] == "supplement_001_figure_s22"
        )
        self.assertEqual((cropped["width"], cropped["height"]), (2, 3))
        self.assertIn("srcRect=25000,0,25000,0", cropped["source_locator"])

    def test_image_before_caption_supports_scheme_and_composed_figure(self) -> None:
        image_bytes: list[bytes] = []
        for size, color in (
            ((40, 20), (220, 10, 20)),
            ((40, 20), (10, 40, 220)),
            ((20, 10), (10, 180, 40)),
        ):
            buffer = io.BytesIO()
            Image.new("RGB", size, color).save(buffer, format="PNG")
            image_bytes.append(buffer.getvalue())

        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"><w:body>
 <w:p><w:r><w:drawing><wp:inline><wp:extent cx="4000" cy="2000"/><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
 <w:p/>
 <w:p><w:r><w:t>Scheme S1. Authored synthesis scheme.</w:t></w:r></w:p>
 <w:p/>
 <w:p><w:r><w:drawing><wp:inline><wp:extent cx="4000" cy="2000"/><a:graphic><a:graphicData><a:blip r:embed="rId2"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
 <w:p/>
 <w:p><w:r><w:drawing><wp:inline><wp:extent cx="4000" cy="2000"/><a:graphic><a:graphicData><a:blip r:embed="rId3"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
 <w:p><w:r><w:t>Figure S1. One authored figure in two image paragraphs.</w:t></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image2.png"/>
 <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image3.png"/>
</Relationships>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)
            for number, data in enumerate(image_bytes, 1):
                archive.writestr(f"word/media/image{number}.png", data)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )
            extraction_root = root / "extraction"
            supplement = extract_supplements([source], extraction_root)[0]

            self.assertEqual(
                [(figure.kind, figure.label) for figure in supplement.figures],
                [("scheme", "Scheme S1"), ("figure", "Figure S1")],
            )
            composed = next(
                asset
                for asset in supplement.assets
                if asset["asset_id"] == "supplement_001_figure_s1"
            )
            with Image.open(extraction_root / composed["output_path"]) as image:
                self.assertEqual(image.size, (20, 28))
                self.assertEqual(image.getpixel((10, 2)), (10, 40, 220))
                self.assertEqual(image.getpixel((10, 26)), (10, 180, 40))
            source_assets = [
                asset
                for asset in supplement.assets
                if asset["category"] == "supplement_source_image"
            ]
            self.assertEqual(len(source_assets), 3)
            self.assertEqual(
                [
                    (extraction_root / asset["output_path"]).read_bytes()
                    for asset in source_assets
                ],
                image_bytes,
            )

    def test_parallel_cell_paragraphs_form_rows_and_empty_layout_row_is_omitted(self) -> None:
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
 <w:p><w:r><w:t>Table S1. Authored values.</w:t></w:r></w:p>
 <w:tbl>
  <w:tr>
   <w:tc><w:p><w:r><w:t>Compound</w:t></w:r></w:p></w:tc>
   <w:tc><w:p><w:r><w:t>Energy</w:t></w:r></w:p></w:tc>
  </w:tr>
  <w:tr>
   <w:tc><w:p><w:r><w:t>PIP 4</w:t></w:r></w:p><w:p><w:r><w:t>PIP 5</w:t></w:r></w:p></w:tc>
   <w:tc><w:p><w:r><w:t>-288.9</w:t></w:r></w:p><w:p><w:r><w:t>-2806.5</w:t></w:r></w:p></w:tc>
  </w:tr>
  <w:tr>
   <w:tc><w:p/></w:tc><w:tc><w:p/></w:tc>
  </w:tr>
 </w:tbl>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )
            supplement = extract_supplements([source], root / "extraction")[0]

            rows = supplement.tables[0].parts[0].rows
            self.assertEqual(len(rows), 3)
            self.assertEqual(
                [[cell.text for cell in row] for row in rows],
                [
                    ["Compound", "Energy"],
                    ["PIP 4", "-288.9"],
                    ["PIP 5", "-2806.5"],
                ],
            )

    def test_heading_style_ends_drawn_figure_caption(self) -> None:
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
 <w:p><w:r><w:drawing><wp:inline><wp:extent cx="4000" cy="2000"/><a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
 <w:p><w:r><w:t>Figure S1. Authored caption.</w:t></w:r></w:p>
 <w:p><w:pPr><w:pStyle w:val="H2"/></w:pPr><w:r><w:t>Circular dichroism studies</w:t></w:r></w:p>
 <w:p><w:r><w:t>Following subsection text.</w:t></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
</Relationships>"""
        image_buffer = io.BytesIO()
        Image.new("RGB", (20, 10), (10, 80, 190)).save(image_buffer, format="PNG")
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)
            archive.writestr("word/media/image1.png", image_buffer.getvalue())

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )
            supplement = extract_supplements([source], root / "extraction")[0]

            self.assertEqual(
                supplement.figures[0].caption_plain,
                "Figure S1. Authored caption.",
            )
            self.assertEqual(
                [(block.kind, block.plain_text) for block in supplement.blocks],
                [
                    ("subsection_heading", "Circular dichroism studies"),
                    ("text", "Following subsection text."),
                ],
            )

    def test_internal_table_title_and_following_prose_are_structured(self) -> None:
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
 <w:tbl>
  <w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Table S1. Authored values.</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>Compound</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Mass</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>F1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>1320.64</w:t></w:r></w:p></w:tc></w:tr>
 </w:tbl>
 <w:p><w:r><w:t>3. Molecular masses of fluorescent probes.</w:t></w:r></w:p>
 <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Studies of kinetics</w:t></w:r></w:p>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )
            supplement = extract_supplements([source], root / "extraction")[0]

            table = supplement.tables[0]
            self.assertEqual(table.label, "Table S1")
            self.assertEqual(table.title_plain, "Table S1. Authored values.")
            self.assertEqual(table.footnotes_plain, [])
            self.assertEqual(
                [[cell.text for cell in row] for row in table.parts[0].rows],
                [["Compound", "Mass"], ["F1", "1320.64"]],
            )
            self.assertTrue(all(cell.header for cell in table.parts[0].rows[0]))
            self.assertEqual(
                [(block.kind, block.plain_text) for block in supplement.blocks],
                [
                    ("text", "3. Molecular masses of fluorescent probes."),
                    ("subsection_heading", "Studies of kinetics"),
                ],
            )

    def test_combined_nested_and_punctuated_authored_tables_are_separate(self) -> None:
        document = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
 <w:p><w:r><w:t>Table S1. First values.</w:t></w:r></w:p>
 <w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>
  <w:tr><w:tc><w:p><w:r><w:t>Name</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>1</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Table S2: Second values.</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>Name</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>2</w:t></w:r></w:p></w:tc></w:tr>
 </w:tbl>
 <w:tbl><w:tr><w:tc><w:p/>
   <w:tbl>
    <w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Table S15: Nested values.</w:t></w:r></w:p></w:tc></w:tr>
    <w:tr><w:tc><w:p><w:r><w:t>Name</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>
    <w:tr><w:tc><w:p><w:r><w:t>C</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>15</w:t></w:r></w:p></w:tc></w:tr>
   </w:tbl>
   <w:p><w:r><w:t>Table S16: Outer values.</w:t></w:r></w:p>
  </w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>Name</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>D</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>16</w:t></w:r></w:p></w:tc></w:tr>
 </w:tbl>
 <w:tbl>
  <w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Table S:24 Punctuated author title.</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>Name</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>E</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>24</w:t></w:r></w:p></w:tc></w:tr>
 </w:tbl>
 <w:sectPr/>
</w:body></w:document>"""
        relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/_rels/document.xml.rels", relationships)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "source.docx"
            source_bytes = package.getvalue()
            source_path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path="papers (private)/00001/supplementary/source.docx",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format=DOCX_MEDIA_TYPE,
            )
            supplement = extract_supplements([source], root / "extraction")[0]

        self.assertEqual(
            [table.label for table in supplement.tables],
            ["Table S1", "Table S2", "Table S15", "Table S16", "Table S24"],
        )
        self.assertEqual(
            [table.title_plain for table in supplement.tables],
            [
                "Table S1. First values.",
                "Table S2: Second values.",
                "Table S15: Nested values.",
                "Table S16: Outer values.",
                "Table S:24 Punctuated author title.",
            ],
        )
        self.assertEqual(
            [table.parts[0].rows[1][1].text for table in supplement.tables],
            ["1", "2", "15", "16", "24"],
        )
        self.assertTrue(
            all(
                all(cell.header for cell in table.parts[0].rows[0])
                for table in supplement.tables
            )
        )


if __name__ == "__main__":
    unittest.main()
    _word_docx_script,
