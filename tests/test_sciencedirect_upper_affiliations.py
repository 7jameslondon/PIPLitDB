import unittest
from tests import test_sciencedirect_html_extractor as helpers


class ScienceDirectUpperAffiliationTests(unittest.TestCase):
    extract = helpers.ScienceDirectHtmlExtractionTests.extract
    def test_uppercase_affiliation_and_anonymous_abbreviation_rows(self):
        result = self.extract('''<body><article><div id="publication">Journal</div>
        <h1 id="screen-reader-main-title">Synthetic record</h1>
        <div id="author-group"><span>Author links open overlay panel</span>
        <span><span>Ada Example</span><span id="bAFF1"><sup>1</sup></span></span>
        <a><span>Bea Sample</span><span id="baff2"><sup>2</sup></span></a></div>
        <div id="aep-keywords-id16"><h2>Abbreviations</h2>
        <div><span>ABC, authored definition</span></div>
        <div><span>XYZ, other definition</span></div></div>
        <div id="body"><section><h2>Introduction</h2><p>Body text.</p></section></div>
        </article></body>''')
        self.assertIn('Affiliation assignments: Ada Example (1); Bea Sample (2)',
                      [block.plain_text for block in result.front_matter])
        abbreviations = next(s for s in result.sections if s.heading == 'Abbreviations')
        self.assertEqual([b.plain_text for b in abbreviations.blocks],
                         ['ABC, authored definition', 'XYZ, other definition'])


if __name__ == '__main__':
    unittest.main()
