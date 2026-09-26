from pathlib import Path
import tempfile
import unittest
from scripts.extraction.html_extractor import extract_html


class AbbreviationSeparatorTests(unittest.TestCase):
    def test_aep_abbreviation_cards_with_separator_spans(self):
        source = '''<html><body><h1>Synthetic article</h1>
        <div id="aep-keywords-id19"><h2>Abbreviations</h2>
        <div><span>AX</span><span>, </span><div><span>alpha example</span></div></div><span>; </span>
        <div><span>BX</span><span>, </span><div><span><em>N</em>,<em>N</em>-beta H<sub>2</sub>O example</span></div></div></div>
        <div id="aep-keywords-id20"><h2>Keywords</h2><div><span>Binding</span></div></div>
        <section><h2>Introduction</h2><p>Authored prose.</p></section></body></html>'''
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'main.html'
            path.write_text(source, encoding='utf8')
            result = extract_html(path, 'html/main.html')
        section = next(s for s in result.sections if s.heading == 'Abbreviations')
        self.assertEqual(len(section.blocks), 1)
        self.assertIn('AX: alpha example', section.blocks[0].plain_text)
        self.assertIn('BX: N,N-beta', section.blocks[0].plain_text)
        self.assertIn('<sub>2</sub>', section.blocks[0].markdown)
        self.assertNotIn('Binding', section.blocks[0].plain_text)

    def test_stable_abbreviation_cards_with_separator_spans(self):
        source = '''<html><body><h1>Synthetic article</h1>
        <div id="kwrds0015"><h2>Abbreviations</h2>
        <div id="kwrd0040"><span>AX</span><span>, </span><div id="kwrd0045"><span>alpha example</span></div></div><span>; </span>
        <div id="kwrd0050"><span>BX</span><span>, </span><div id="kwrd0055"><span>beta H<sub>2</sub>O example</span></div></div></div>
        <section><h2>Introduction</h2><p>Authored prose.</p></section></body></html>'''
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'main.html'
            path.write_text(source, encoding='utf8')
            result = extract_html(path, 'html/main.html')
        section = next(s for s in result.sections if s.heading == 'Abbreviations')
        self.assertEqual(len(section.blocks), 1)
        self.assertIn('AX: alpha example', section.blocks[0].plain_text)
        self.assertIn('BX: beta', section.blocks[0].plain_text)
        self.assertIn('<sub>2</sub>', section.blocks[0].markdown)
