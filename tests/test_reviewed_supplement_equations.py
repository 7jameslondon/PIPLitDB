import unittest
from pathlib import Path

from scripts.extraction.models import SourceFile
from scripts.extraction.supplements import _supplement_reviewed_page_blocks


class ReviewedSupplementEquationsTest(unittest.TestCase):
    def test_equation_preserves_kind_notation_and_exact_page_gate(self):
        source = SourceFile('supplement', Path('example.pdf'), 'example.pdf', 3,
                            'a' * 64, 'application/pdf', 2)
        entry = dict(page=2, block_kind='equation', plain_text='K2 = 0',
                     markdown='K<sub>2</sub> = 0', source_locator='page=2;formula',
                     reason='Native formula ordering is unreliable.',
                     evidence='Visually transcribed the exact source formula.')
        blocks = _supplement_reviewed_page_blocks(
            {'reviewed_page_blocks': [entry]}, source, 'supplement_001')
        self.assertEqual(blocks[2][0].kind, 'equation')
        self.assertEqual(blocks[2][0].markdown, 'K<sub>2</sub> = 0')
        self.assertEqual(blocks[2][0].plain_text, 'K2 = 0')
        for invalid in ({'page': 3}, {'source_locator': 'page=1;formula'},
                        {'block_kind': 'invented'}, {'evidence': ''}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _supplement_reviewed_page_blocks(
                    {'reviewed_page_blocks': [{**entry, **invalid}]},
                    source, 'supplement_001')


if __name__ == '__main__':
    unittest.main()
