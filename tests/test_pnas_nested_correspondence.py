import tempfile
import unittest
from pathlib import Path
from scripts.extraction.html_extractor import extract_html
from tests.test_pnas_html_extractor import PNAS_HTML


class PnasNestedCorrespondenceTests(unittest.TestCase):
    def test_wrapped_correspondence_paragraph_is_emitted_once(self):
        notes = '''<section id="tab-contributors"><section><h4>Notes</h4>
        <div role="doc-footnote"><span>‡</span><div id="cor1" role="paragraph">To whom correspondence may be addressed. E-mail: author@example.org.</div></div>
        <div role="doc-footnote">Contributed by A. Editor, January 1, 2005</div>
        </section></section>'''
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'article.html'
            path.write_text(PNAS_HTML.replace('</article>', notes + '</article>'), encoding='utf-8')
            article = extract_html(path, 'article.html')
        values = [block.plain_text for block in article.front_matter]
        self.assertEqual([v for v in values if 'correspondence may' in v], [
            'Correspondence: To whom correspondence may be addressed. E-mail: author@example.org.'])
        self.assertEqual(values.count('Editorial history: Contributed by A. Editor, January 1, 2005'), 1)
