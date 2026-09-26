from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.extraction.html_extractor import extract_html
from tests.test_pnas_html_extractor import PNAS_HTML


class PnasCorrespondenceFootnoteTests(unittest.TestCase):
    def test_legacy_correspondence_footnote_is_preserved_once(self) -> None:
        notes = '''<section id="tab-contributors"><section><h4>Notes</h4>
          <div id="cor1" role="doc-footnote">*To whom correspondence should be addressed at:
          Example Institute. E-mail: <a href="mailto:author@example.org">author@example.org</a></div>
          <div role="doc-footnote">Contributed by A. Editor, December 1, 2023</div>
          <div role="doc-footnote">Author contributions: A.B. designed research.</div>
        </section></section>'''
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(PNAS_HTML.replace('</article>', notes + '</article>'), encoding="utf-8")
            article = extract_html(path, "article.html")
        values = [block.plain_text for block in article.front_matter]
        self.assertEqual(sum('Correspondence:' in value for value in values), 1)
        self.assertIn('Correspondence: *To whom correspondence should be addressed at: Example Institute. E-mail: author@example.org', values)
        self.assertEqual(sum(value.startswith('Editorial history:') for value in values), 1)
        self.assertIn('Author contributions: A.B. designed research.', values)


if __name__ == '__main__':
    unittest.main()
