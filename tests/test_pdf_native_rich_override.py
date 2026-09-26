import unittest
from pathlib import Path
from scripts.extraction.discovery import SourceFile
from scripts.extraction.pipeline import _apply_rich_text_overrides, ExtractionError
from tests.test_pdf_article_pipeline import _article, PDF_RELATIVE_PATH


class NativePdfRichOverrideTests(unittest.TestCase):
    def test_flat_native_scripts_accept_reviewed_nesting_without_changing_characters(self):
        for plain, replacement, accepted in [
            ('H2N binds TGTTA.', '<sup>H<sub>2</sub>N</sup> binds <u>TGTTA</u>.', True),
            ('H2N binds TGTTA.', '<sup>H<sub>3</sub>N</sup> binds <u>TGTTA</u>.', False),
            ('H_{2}N binds TGTTA.', '<sup>H<sub>3</sub>N</sup> binds <u>TGTTA</u>.', False),
        ]:
            with self.subTest(plain=plain, replacement=replacement):
                article = _article()
                block = article.sections[0].blocks[0]
                block.plain_text = plain
                block.markdown = plain
                source = SourceFile(role='main_pdf', path=Path('synthetic.pdf'),
                    relative_path=PDF_RELATIVE_PATH, size=10, sha256='a'*64,
                    detected_format='pdf', page_count=1)
                spec = dict(target_id=block.block_id, expected_plain_text=plain,
                    expected_markdown=plain, replacement_markdown=replacement,
                    source_path=PDF_RELATIVE_PATH, source_sha256='a'*64,
                    source_locator='PDF page 1, exact formula and sequence',
                    reason='Restore reviewed native script geometry and underlining',
                    evidence='Source shows a nested 2 subscript and underlined DNA')
                if accepted:
                    _apply_rich_text_overrides(article, [spec], [source])
                    self.assertEqual(block.plain_text, plain)
                    self.assertEqual(block.markdown, replacement)
                else:
                    with self.assertRaises(ExtractionError):
                        _apply_rich_text_overrides(article, [spec], [source])
