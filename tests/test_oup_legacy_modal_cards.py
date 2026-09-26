import base64
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from PIL import Image
from scripts.extraction.html_extractor import extract_html


def fixture():
    stream = BytesIO()
    Image.new('RGB', (40, 30), 'white').save(stream, format='PNG')
    image = base64.b64encode(stream.getvalue()).decode()
    controls = '<a role="button" aria-describedby="label-10" href="/view-large/figure/10/a.jpg">Open in new tab</a><a role="button" aria-describedby="label-10" href="/DownloadFile/DownloadImage.aspx?x=1">Download slide</a>'
    reference = lambda n, text: f'<div content-id="AB1C{n}"><div><span><a name="jumplink-AB1C{n}" aria-label="jumplink-AB1C{n}"></a></span></div><div><div id="ref-auto-AB1C{n}"><div><p>{n} Author,A. (</p><div>2000</div>) {text}<p></p></div></div></div></div>'
    return f'''<html><body><header><h1>Synthetic assay</h1><span id="author-flyout-1"><div><div>A. Author</div><div>Institute</div><div>Search for other works by this author on:</div><div>Oxford Academic</div><div>PubMed</div></div></span><div>Test Journal, Volume 3, Issue 2, 1 May 2000, Page e7, <a href="https://doi.org/10.1093/test/e7">https://doi.org/10.1093/test/e7</a></div><div><div>Published:</div><div>1 May 2000</div></div></header><div id="getCitation"><h3>Cite</h3></div><div id="ContentTab"><div><h2 id="1">Abstract</h2><section aria-label="Main abstract"><p>Abstract text.</p></section><h2 id="2">INTRODUCTION</h2><p>Calculate using the following formula:</p><p>F = (X<sub>C</sub> − X<sub>Q</sub>) × 100</p><p>where X is defined.</p><div swap-content-for-modal="true"><div><img src="data:image/png;base64,{image}" alt="Figure 1. Signal."/><div>{controls}</div></div><div><div><p><strong>Figure 1.</strong> Signal.</p></div></div></div><div swap-content-for-modal="true"><div><img src="data:image/png;base64,{image}" alt="Scheme 1."/><div><div><p>Scheme 1.</p></div><div>{controls}</div></div></div></div><h2 id="3">References</h2><div>{reference(1, '<em>A book</em>. Publisher, pp. <div>123</div>–145.')}{reference(2, 'A paper. <div>Journal</div>, <div>8</div>, <div>1</div>–9.')}</div></div></div></body></html>'''


class OupLegacyModalTests(unittest.TestCase):
    def extract(self, text):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'main.html'
            path.write_text(text, encoding='utf-8')
            return extract_html(path, 'test/main.html')

    def test_grouped_cards_schemes_equation_complete_references_and_header(self):
        result = self.extract(fixture())
        self.assertEqual([figure.label for figure in result.figures], ['Figure 1', 'Scheme 1'])
        self.assertEqual(len(result.references), 2)
        self.assertIn('A book', result.references[0].plain_text)
        self.assertIn('123–145', result.references[0].plain_text)
        self.assertIn('A paper.', result.references[1].plain_text)
        self.assertEqual(result.bibliographic['pages'], 'e7')
        self.assertNotIn('Cite', [section.heading for section in result.sections])
        blocks = [block for section in result.sections for block in section.blocks]
        self.assertEqual(sum(block.kind == 'equation' for block in blocks), 1)
        self.assertTrue(any('Institute' in block.plain_text for block in result.front_matter))

    def test_incomplete_control_does_not_authenticate_modal_dialect(self):
        result = self.extract(fixture().replace('Download slide', 'Unknown control'))
        self.assertEqual(result.figures, [])

    def test_noncontiguous_references_do_not_authenticate_modal_dialect(self):
        result = self.extract(fixture().replace('AB1C2', 'AB1C3'))
        self.assertEqual(result.figures, [])

    def test_table_unmarked_note_preserves_inline_scientific_emphasis(self):
        table = '''
        <div swap-content-for-modal="true"><div>
          <div id="GKH515TB1">
            <span id="label-24583"><strong>Table 1.</strong></span>
            <div><a role="button" target="_blank" href="/view-large/55557669"
              aria-describedby="label-24583">Open in new tab</a></div>
            <div id="caption-24583"><p>Thermodynamic values</p></div>
          </div>
          <div><table role="table" aria-labelledby="label-GKH515TB1"
            aria-describedby="caption-GKH515TB1"><tbody>
            <tr><td>Compound</td><td>Value</td></tr>
            <tr><td>Example</td><td>−12.5</td></tr>
          </tbody></table></div>
          <div></div>
          <div><p>Values of Δ<em>H</em>° and Δ<em>G</em>° are in kcal/mol.</p></div>
        </div></div>
        '''
        source = fixture().replace('<h2 id="3">References', table + '<h2 id="3">References')

        result = self.extract(source)

        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].label, 'Table 1')
        self.assertEqual(
            result.tables[0].footnotes_plain,
            ['Values of ΔH° and ΔG° are in kcal/mol.'],
        )

    def test_section_terminal_formula_is_an_equation_block(self):
        source = fixture().replace(
            '<h2 id="3">References',
            '<p>The values were calculated using the equation:</p>'
            '<p>ΔCp° = (0.382·ΔAnp – 0.121·ΔAp) cal/mol K</p>'
            '<h2 id="3">References',
        )

        result = self.extract(source)
        equations = [
            block
            for section in result.sections
            for block in section.blocks
            if block.kind == 'equation'
        ]

        self.assertEqual(len(equations), 2)
        self.assertEqual(
            equations[-1].plain_text,
            'ΔCp° = (0.382·ΔAnp – 0.121·ΔAp) cal/mol K',
        )
