import unittest
from tests import test_sciencedirect_html_extractor as helpers


class SimpleParaAbstractTests(unittest.TestCase):
    extract = helpers.ScienceDirectHtmlExtractionTests.extract

    def test_numbered_simple_para_is_kept_in_aep_abstract_only(self):
        article = self.extract('''<article><h1>Example</h1>
        <div id="simple-para1">Navigation text.</div>
        <div id="abstracts"><div id="aep-abstract-id14"><h2>Abstract</h2>
        <div id="aep-abstract-sec-id15"><div id="simple-para0055">Summary with <em>N</em>-methyl and 8-fold loss.</div></div></div></div>
        <div id="body"><section id="aep-section-id16"><h2>Results</h2><p>Body.</p></section></div></article>''')
        abstract = next(s for s in article.sections if s.heading == 'Abstract')
        self.assertEqual([b.plain_text for b in abstract.blocks], ['Summary with N-methyl and 8-fold loss.'])
        self.assertNotIn('Navigation text.', [b.plain_text for s in article.sections for b in s.blocks])

    def test_direct_simple_para_table_title_retains_units_and_footnote(self):
        article = self.extract('''<article><h1>Example</h1><div id="body"><section><h2>Results</h2>
        <div id="TBL1"><span><span><p id="simple-para0045">Table 1. Binding constants (M<sup>−1</sup>)<sup>a</sup></p></span></span>
        <div><table><thead><tr><th>Compound</th><th>Match</th></tr></thead><tbody><tr><td>A</td><td>1.2</td></tr></tbody></table></div>
        <dl><dt>a</dt><dd>Three experiments.</dd></dl></div></section></div></article>''')
        table = article.tables[0]
        self.assertIn('Binding constants', table.title_plain)
        self.assertIn('M^{−1}', table.title_plain)
        self.assertIn('^{a}', table.title_plain)
        self.assertNotIn('Binding constants', ' '.join(b.plain_text for s in article.sections for b in s.blocks))
