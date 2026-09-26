"""Malformed native vector headers must use the reviewed-render path."""
import struct
import unittest
import tempfile
import zipfile
from pathlib import Path
from scripts.extraction.models import SourceFile
from scripts.extraction.docx_supplement import extract_docx_supplement, DOCX_MEDIA_TYPE

from scripts.extraction.docx_supplement import _png_derivative, UnsupportedDocxImageError
from scripts.extraction.docx_supplement import _word_pdf_script
from scripts.extraction.docx_supplement import _decode_run_text, _styled_run
import xml.etree.ElementTree as ET


class ZeroExtentEmfTests(unittest.TestCase):
    def test_wingdings_right_arrow_is_font_gated(self):
        self.assertEqual(_decode_run_text('\uf0e0', 'Wingdings'), '→')
        self.assertEqual(_decode_run_text('\uf0e0', 'Arial'), '\uf0e0')
        run = ET.fromstring('<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:sym w:font="Wingdings" w:char="F0E0"/></w:r>')
        self.assertEqual(_styled_run(run), ('→', '→'))

    def test_reachable_ole_object_is_preserved_without_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            docx = root / 'source.docx'
            data = b'native-ole-payload'
            with zipfile.ZipFile(docx, 'w') as archive:
                archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:o="urn:schemas-microsoft-com:office:office"><w:body><w:p><w:r><w:object><o:OLEObject r:id="rId1"/></w:object></w:r></w:p></w:body></w:document>')
                archive.writestr('word/_rels/document.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" Target="embeddings/oleObject1.bin"/></Relationships>')
                archive.writestr('word/embeddings/oleObject1.bin', data)
                archive.writestr('word/embeddings/unreachable.bin', b'unreferenced')
            source = SourceFile(role='supplement', path=docx, relative_path='source.docx', size=docx.stat().st_size, sha256='x', detected_format=DOCX_MEDIA_TYPE)
            result = extract_docx_supplement(source, 'supplement_001', docx_path=docx, extraction_root=root)
            assets = result[3]
            self.assertEqual(len(assets), 1)
            self.assertEqual(assets[0]['parent_id'], 'supplement_001')
            self.assertEqual((root / assets[0]['output_path']).read_bytes(), data)

    def test_word_optional_handles_are_guarded(self):
        script = _word_pdf_script()
        self.assertIn('if ($null -ne $word.Hwnd)', script)
        self.assertIn('if ($null -ne $window -and $null -ne $window.Hwnd)', script)
        self.assertIn('$document.ExportAsFixedFormat($OutputPath, 17)', script)

    def test_zero_frame_extent_requests_reviewed_render(self):
        # Synthetic ENHMETAHEADER: nonempty pixel bounds, zero physical frame.
        data = bytearray(108)
        struct.pack_into('<II', data, 0, 1, 108)
        struct.pack_into('<4i', data, 8, 0, 0, 100, 100)
        struct.pack_into('<4i', data, 24, 0, 0, 0, 0)
        data[40:44] = b' EMF'
        with self.assertRaisesRegex(UnsupportedDocxImageError, 'reviewed native-layout'):
            _png_derivative(bytes(data))


if __name__ == '__main__':
    unittest.main()
