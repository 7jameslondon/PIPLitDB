import tempfile
import unittest
from pathlib import Path
from lxml import html
from tests import test_cell_legacy_doi_sbref as fixture
from scripts.extraction.html_extractor import extract_html, _normalize_cell_press_semantic_snapshot


class CellFormulaChtmlTests(unittest.TestCase):
    def source(self, identifier='formula-0020', label='(4)'):
        doc = fixture.CellLegacyDoiReferencesTests().source()
        doc.xpath('.//section[@id="sec-1"]')[0].append(html.fromstring(
            '<div role="paragraph" id="para-0020">Before.'
            f'<div id="{identifier}"><div role="math"><div>'
            '<mjx-container jax="CHTML" aria-label="y squared equals a over b">'
            '<mjx-math aria-hidden="true"><mjx-msup><mjx-mi><mjx-c>y</mjx-c></mjx-mi>'
            '<mjx-script><mjx-mn><mjx-c>2</mjx-c></mjx-mn></mjx-script></mjx-msup>'
            '<mjx-mo><mjx-c>=</mjx-c></mjx-mo><mjx-mfrac><mjx-frac>'
            '<mjx-num><mjx-mi><mjx-c>a</mjx-c></mjx-mi></mjx-num>'
            '<mjx-dbox><mjx-dtable><mjx-row><mjx-den><mjx-mi><mjx-c>b</mjx-c></mjx-mi>'
            '</mjx-den></mjx-row></mjx-dtable></mjx-dbox></mjx-frac></mjx-mfrac>'
            f'</mjx-math></mjx-container></div></div><div>{label}</div></div>After.</div>'
        ))
        return doc

    def test_formula_identifier_preserves_equation_and_surrounding_order(self):
        doc = self.source()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'main.html'
            path.write_text(html.tostring(doc, encoding='unicode'), encoding='utf8')
            article = extract_html(path, 'main.html', [])
        blocks = [b for s in article.sections for b in s.blocks]
        equations = [b for b in blocks if b.kind == 'equation']
        self.assertEqual([b.plain_text for b in equations], [r'y^{2}=\frac{a}{b} (4)'])
        index = blocks.index(equations[0])
        self.assertEqual(blocks[index - 1].plain_text, 'Before.')
        self.assertEqual(blocks[index + 1].plain_text, 'After.')

    def test_near_miss_identifiers_or_labels_are_not_equations(self):
        for identifier, label in [('formula-notes', '(4)'), ('formula-0020', 'note')]:
            with self.subTest(identifier=identifier, label=label):
                doc = self.source(identifier, label)
                _normalize_cell_press_semantic_snapshot(doc)
                self.assertFalse(doc.xpath('.//*[@data-extraction-reviewed-equation="true"]'))

    def test_direct_fraction_between_prose_and_definition_is_preserved(self):
        doc = fixture.CellLegacyDoiReferencesTests().source()
        paragraph = html.fromstring(
            '<div role="paragraph" id="para-0021">Calculate the ratio: '
            '<mjx-container jax="CHTML" aria-label="y equals a over b">'
            '<mjx-math aria-hidden="true"><mjx-mi><mjx-c>y</mjx-c></mjx-mi>'
            '<mjx-mo><mjx-c>=</mjx-c></mjx-mo><mjx-mfrac><mjx-frac>'
            '<mjx-num><mjx-mi><mjx-c>a</mjx-c></mjx-mi></mjx-num>'
            '<mjx-dbox><mjx-dtable><mjx-row><mjx-den><mjx-mi><mjx-c>b</mjx-c>'
            '</mjx-mi></mjx-den></mjx-row></mjx-dtable></mjx-dbox>'
            '</mjx-frac></mjx-mfrac></mjx-math></mjx-container>'
            ' where a is the numerator and b is the denominator.</div>'
        )
        doc.xpath('.//section[@id="sec-1"]')[0].append(paragraph)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'main.html'
            path.write_text(html.tostring(doc, encoding='unicode'), encoding='utf8')
            article = extract_html(path, 'main.html', [])
        blocks = [block for section in article.sections for block in section.blocks]
        equations = [block for block in blocks if block.kind == 'equation']
        self.assertEqual([block.plain_text for block in equations], [r'y=\frac{a}{b}'])
        index = blocks.index(equations[0])
        self.assertEqual(blocks[index - 1].plain_text, 'Calculate the ratio:')
        self.assertEqual(
            blocks[index + 1].plain_text,
            'where a is the numerator and b is the denominator.',
        )


if __name__ == '__main__':
    unittest.main()
