import unittest

from tests.test_sciencedirect_html_extractor import ScienceDirectHtmlExtractionTests


class ScienceDirectTitleBadgeNotesTests(unittest.TestCase):
    extract = ScienceDirectHtmlExtractionTests.extract

    def source(self, second_backref="☆☆"):
        return f'''<article><h1 id="screen-reader-main-title">
        <div><span>Regular Article</span></div><span>Scientific title</span><a
        href="#aep-article-footnote-id1" name="baep-article-footnote-id1">☆</a>,<a
        href="#aep-article-footnote-id2" name="baep-article-footnote-id2">☆☆</a></h1>
        <section><h2>Body</h2><p>Text.</p></section>
        <dl><dt><a href="#baep-article-footnote-id1"><sup>☆</sup></a></dt>
        <dd>Authored abbreviation note.</dd></dl>
        <dl><dt><a href="#baep-article-footnote-id2"><sup>{second_backref}</sup></a></dt>
        <dd>Authored editor note.</dd></dl></article>'''

    def test_badge_and_two_authenticated_notes_are_removed_together(self):
        result = self.extract(self.source())
        self.assertEqual(result.title, "Scientific title")

    def test_mismatching_backref_does_not_remove_marker(self):
        result = self.extract(self.source("☆"))
        self.assertEqual(result.title, "Scientific title☆☆")


if __name__ == "__main__":
    unittest.main()
