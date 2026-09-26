from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from scripts.extraction.html_extractor import extract_html


class KeywordNavigationBoundaryTests(unittest.TestCase):
    def test_nested_aep_leading_paragraphs_get_introduction_scope(self):
        source='''<article><h1 id="screen-reader-main-title">A study</h1><a href="https://doi.org/10.1016/synthetic">DOI</a><div><div id="aep-keywords-id2"><h2>Keywords</h2><div><span>Binding</span></div></div><div id="body"><div><div id="p0005">Opening prose.</div><div id="p0010">More prose.</div><section id="s001"><h2>Methods</h2><div id="p0015">A method.</div></section></div></div><section id="aep-bibliography-id3"><h2>References</h2><ol><li><a id="ref-id-b0005">1</a><span>A reference.</span></li></ol></section></div></article>'''
        with TemporaryDirectory() as temp:
            path=Path(temp)/'main.html';path.write_text(source,encoding='utf8');result=extract_html(path,'html/main.html')
        self.assertEqual([s.heading for s in result.sections],['Keywords','Introduction','Methods'])
        self.assertEqual([b.plain_text for b in result.sections[1].blocks],['Opening prose.','More prose.'])
        self.assertEqual([b.plain_text for b in result.sections[2].blocks],['A method.'])

    def test_issue_navigation_does_not_attach_intro_to_keywords(self):
        source='''<article><h1 id="screen-reader-main-title">A study</h1><div id="article-identifier-links"><a href="https://doi.org/10.1016/synthetic">DOI</a></div><div id="abstracts"><div id="aep-abstract-id1"><h2>Abstract</h2><div>A summary.</div></div></div><div><div><div id="aep-keywords-id2"><h2>Keywords</h2><div><span>Binding</span></div></div></div></div><ul><li><a href="/science/article/pii/one">Previous article in this issue</a></li><li><a href="/science/article/pii/two">Next article in this issue</a></li></ul><div id="body"><div><div id="p0005">Opening prose.</div><div id="p0010">More prose.</div></div><section id="s001"><h2>Methods</h2><div id="p0015">A method.</div></section></div><section id="aep-bibliography-id3"><h2>References</h2><ol><li><a id="ref-id-b0005">1</a><span>A reference.</span></li></ol></section></article>'''
        with TemporaryDirectory() as temp:
            path=Path(temp)/'main.html';path.write_text(source,encoding='utf8')
            result=extract_html(path,'html/main.html')
        self.assertEqual([s.heading for s in result.sections],['Abstract','Keywords','Main text','Methods'])
        self.assertEqual([b.plain_text for b in result.sections[1].blocks],['Binding'])
        self.assertEqual([b.plain_text for b in result.sections[2].blocks],['Opening prose.','More prose.'])
