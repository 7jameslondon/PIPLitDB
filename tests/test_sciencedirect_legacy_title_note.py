import unittest

from tests.test_sciencedirect_html_extractor import ScienceDirectHtmlExtractionTests


class LegacyTitleNoteTests(unittest.TestCase):
    extract = ScienceDirectHtmlExtractionTests.extract

    def source(self, name="bFN1", marker="1"):
        return f'''<article><h1 id="screen-reader-main-title"><span>Sample title
        <a href="#FN1" name="{name}"><span><span><sup>{marker}</sup></span></span></a>
        </span></h1><section><h2>Body</h2><p>Authored text.</p></section></article>'''

    def test_exact_legacy_title_note_removed(self):
        self.assertEqual(self.extract(self.source()).title, "Sample title")

    def test_linked_title_note_is_retained_as_front_matter(self):
        source = self.source().replace('</article>', '<dl><dt><a href="#bFN1">1</a></dt><dd><div>Edited by A. Example</div></dd></dl></article>')
        self.assertIn('Article note 1: Edited by A. Example', [b.plain_text for b in self.extract(source).front_matter])

    def test_wrong_backref_preserved(self):
        self.assertIn("1", self.extract(self.source(name="bFN2")).title)

    def test_different_scientific_marker_preserved(self):
        self.assertIn("2", self.extract(self.source(marker="2")).title)

    def test_bare_scientific_superscript_preserved(self):
        result = self.extract('<article><h1 id="screen-reader-main-title">H<sup>2</sup></h1><p>Body.</p></article>')
        self.assertIn("2", result.title)
