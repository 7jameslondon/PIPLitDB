import unittest
from lxml import html
from scripts.extraction.html_extractor import _oup_silverchair_reference_blocks


class OupPunctuatedBookReferencesTests(unittest.TestCase):
    def test_received_accepted_paragraph_is_front_matter(self):
        from tests.test_oup_legacy_modal_cards import fixture, OupLegacyModalTests
        text = 'Received March 19, 2004; Revised and Accepted April 22, 2004'
        source = fixture().replace('<h2 id="2">INTRODUCTION', f'<p>{text}</p><h2 id="2">INTRODUCTION')
        result = OupLegacyModalTests().extract(source)
        self.assertTrue(any(block.plain_text == text for block in result.front_matter))
        self.assertFalse(any(block.plain_text == text for section in result.sections for block in section.blocks))

    def extract(self, bodies):
        items = ''.join(f'<div content-id="TESTC{n}"><div><span><a name="jumplink-TESTC{n}" aria-label="jumplink-TESTC{n}"></a></span></div><div><div id="ref-auto-TESTC{n}"><span>{n}.</span><div>{body}<p></p></div></div></div></div>' for n, body in enumerate(bodies, 1))
        heading = html.fromstring(f'<div><h2>References</h2><div>{items}</div></div>')[0]
        return _oup_silverchair_reference_blocks(heading, 'synthetic.html')

    def test_mixed_complete_punctuated_citations(self):
        refs = self.extract([
            '<p>Author,A. <em>et al. </em>(</p><div>2000</div>) Article. <div>J. Science</div>., <div>3</div>, <div>1</div>–9.',
            '<p>Author,B. (</p><div>2001</div>) Ph.D. Thesis, Example University, p. 99.',
            '<p>Author,C. (</p><div>2002</div>) <em>A book</em>, 4th Edn. Publisher, City.',
        ])
        self.assertEqual(len(refs), 3)
        self.assertIn('et al.', refs[0].plain_text)
        self.assertIn('J. Science., 3, 1–9.', refs[0].plain_text)
        self.assertEqual(refs[1].plain_text, '2. Author,B. (2001) Ph.D. Thesis, Example University, p. 99.')
        self.assertIn('<em>A book</em>', refs[2].markdown)

    def test_unrecognized_controls_and_incomplete_year_fail_closed(self):
        for body in [
            '<p>Author,A. (</p><div>2000</div>) A book.<a href="/share">Share</a>',
            '<p>Author,A. (</p><div>unknown</div>) A book.',
            '<p>Author,A.</p><div>2000</div>A book.',
        ]:
            with self.subTest(body=body):
                self.assertIsNone(self.extract([body]))
