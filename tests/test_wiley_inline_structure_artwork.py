import tempfile
import unittest
from pathlib import Path
from scripts.extraction.html_extractor import extract_html


class WileyInlineStructureArtworkTests(unittest.TestCase):
    def extract(self, alt='chemical structure image', href='/cms/asset/abc-123/mfor001.jpg'):
        source = f'''<article><div id="article__content"><div id="journal-banner-text">Journal</div>
        <h1>Example</h1><section id="sec1-bdy-1"><h2>Results</h2><p>Compounds <b>1</b> and <b>3</b><span id="struct-1">
        <a href="{href}"><picture><img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
        alt="{alt}"/></picture></a></span> were linked to a ligand.</p>
        <p>Next paragraph.</p></section></div></article>'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'main.html'
            path.write_text(source, encoding='utf-8')
            return extract_html(path, 'main.html')

    def test_complete_artwork_preserves_paragraph_and_asset(self):
        result = self.extract()
        self.assertEqual([figure.figure_id for figure in result.figures], ['chemical_structure_001'])
        self.assertEqual(result.figures[0].caption_plain, '')
        texts = [block.plain_text for section in result.sections for block in section.blocks]
        self.assertIn('Compounds 1 and 3 were linked to a ligand.', texts)
        self.assertIn('Next paragraph.', texts)
        self.assertTrue(any(asset.asset_id == 'chemical_structure_001' for asset in result.embedded_assets))

    def test_unrelated_image_not_promoted_to_artwork(self):
        self.assertFalse(self.extract(alt='logo').figures)
        self.assertFalse(self.extract(href='/other/image.jpg').figures)


if __name__ == '__main__':
    unittest.main()
