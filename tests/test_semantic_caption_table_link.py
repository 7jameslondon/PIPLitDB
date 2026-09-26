import tempfile
import unittest
from pathlib import Path
from scripts.extraction.html_extractor import extract_html


class SemanticCaptionTableLinkTests(unittest.TestCase):
    def test_table_links_keep_caption_surrounding_text_and_tails(self):
        source = ('<html><body><main><h1>Caption test</h1><h2>Results</h2>'
                  '<p>See Figure 1.</p><figure id="F1">'
                  '<img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=">'
                  '<figcaption>Figure 1. Results from Table <a href="#T1">1</a>'
                  ', followed by Table <a href="#T2">2</a>. End of caption.'
                  '</figcaption></figure></main></body></html>')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'main.html'
            path.write_text(source, encoding='utf-8')
            article = extract_html(path, 'main.html')
        self.assertIn('Results from Table 1, followed by Table 2. End of caption.',
                      article.figures[0].caption_plain)
