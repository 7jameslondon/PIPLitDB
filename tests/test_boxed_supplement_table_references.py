"""A reviewed table must not erase other native content on its page."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.extraction.models import ContentBlock, SourceFile
from scripts.extraction.supplements import extract_supplements


class BoxedTableReferenceTests(unittest.TestCase):
    def test_boxed_table_preserves_numbered_references_outside_box(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "supplement.pdf"
            pdf.write_bytes(b"mock PDF")
            source = SourceFile("supplement", pdf, "private/supplement.pdf", 8,
                                hashlib.sha256(pdf.read_bytes()).hexdigest(),
                                "application/pdf", 1)
            reference = ContentBlock("reference-1", "footnote", "[1] Author. 2012.",
                                     "[1] Author. 2012.", source.relative_path,
                                     "page=1;native-lines=20-20")
            table = {"number": "S1", "page": 1,
                     "source_locator": "page=1;table-bbox=50,50,500,400",
                     "source_kind": "pdf", "title_plain": "Table S1. Data.",
                     "reason": "Exact reviewed table", "evidence": "Page 1",
                     "parts": [{"rows": [[{"text": "Value", "header": True}],
                                          [{"text": "1"}]]}]}
            config = {"supplements": [{"source_path": source.relative_path,
                       "source_sha256": source.sha256, "table_overrides": [table]}]}
            with patch("scripts.extraction.supplements._pdf_blocks",
                       return_value=([reference], [], [])):
                result = extract_supplements([source], root / "extraction",
                                             pdf_text_config=config)[0]
            self.assertEqual([b.plain_text for b in result.blocks], [reference.plain_text])
            self.assertEqual(len(result.tables), 1)


if __name__ == "__main__":
    unittest.main()
