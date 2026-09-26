import unittest
from lxml import html
from scripts.extraction.html_extractor import _article_title


class AcsRevealTitleNoteTests(unittest.TestCase):
    def test_bound_article_note_preserves_scientific_scripts_and_source(self):
        document = html.fromstring('''<html><body><h1>Study of H<sub>2</sub>O<sup><a reveal-id="ab123AF2">†</a></sup></h1><div id="ab123AF2" content-id="ab123AF2"><span><span>†</span></span><p>Supported by grant.</p></div></body></html>''')
        before = html.tostring(document)
        self.assertEqual(_article_title(document), 'Study of H_{2}O')
        self.assertEqual(html.tostring(document), before)

    def test_missing_mismatched_and_duplicate_notes_are_retained(self):
        note = '<div id="ab123AF2" content-id="ab123AF2"><span>†</span><p>Funding.</p></div>'
        for body in ('', note.replace('†', '‡'), note + note, note.replace('content-id="ab123AF2"', 'content-id="different"')):
            with self.subTest(body=body):
                document = html.fromstring('<html><body><h1>Study<sup><a reveal-id="ab123AF2">†</a></sup></h1>' + body + '</body></html>')
                self.assertEqual(_article_title(document), 'Study^{†}')
