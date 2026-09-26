from pathlib import Path
import tempfile
import unittest

from scripts.extraction.pdf_text_extractor import extract_pdf_text, _DecodedChar, _canonicalize_script_token_order
from tests.test_pdf_text_extractor import _write_pdf


class CnnVariantTests(unittest.TestCase):
    def test_parallel_numeric_superscript_does_not_split_word_subscript(self):
        chars = [_DecodedChar(t, {}, False, False, s) for t, s in
                 [('I', None), ('t', 'sub'), ('0', 'sup'), ('o', 'sub'), ('t', 'sub')]]
        _canonicalize_script_token_order(chars)
        self.assertEqual(''.join(c.text for c in chars), 'Itot0')
        self.assertEqual([c.script for c in chars], [None, 'sub', 'sub', 'sub', 'sup'])

    def test_alternate_outline_does_not_disable_byte_remapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'variant.pdf'
            _write_pdf(path, fonts={'F': ('AdvPS_TTR', 32,
                ['C65', 'C40._', 'C66', 'C41'])},
                operations=[('F', 12, 40, 240, bytes([32, 33, 34, 35]))])
            result = extract_pdf_text(path, 'pdf/variant.pdf')
            self.assertEqual([line.plain_text for line in result.lines], ['A(B)'])
            self.assertFalse(result.warnings)

    def test_unknown_base_stays_unresolved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'unknown.pdf'
            _write_pdf(path, fonts={'F': ('AdvPS_TTR', ['C999._'])},
                operations=[('F', 12, 40, 240, bytes([1]))])
            result = extract_pdf_text(path, 'pdf/unknown.pdf')
            self.assertEqual(result.lines[0].plain_text, '\ufffd')
            self.assertTrue(result.warnings)
