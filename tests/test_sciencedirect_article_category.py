import unittest

from tests import test_sciencedirect_html_extractor as fixture


class ScienceDirectArticleCategoryTests(unittest.TestCase):
    extract = fixture.ScienceDirectHtmlExtractionTests.extract

    def test_recognized_title_badge_is_preserved_as_front_matter(self):
        for category in (
            "Communication",
            "Minireview",
            "Short communication",
            "Regular Article",
        ):
            with self.subTest(category=category):
                result = self.extract(f'''<article>
                <h1 id="screen-reader-main-title"><div><span>{category}</span></div>
                <span>Scientific title</span></h1>
                <section><h2>Body</h2><p>Authored text.</p></section></article>''')
                self.assertEqual(result.title, "Scientific title")
                self.assertEqual(
                    [b.plain_text for b in result.front_matter],
                    [f"Article category: {category}"],
                )
                self.assertTrue(result.front_matter[0].source_locator.endswith("/h1/div"))

    def test_unrecognized_title_content_is_not_reclassified(self):
        result = self.extract('''<article><h1 id="screen-reader-main-title">
        <div>Protein interactions</div><span>in cells</span></h1>
        <section><h2>Body</h2><p>Authored text.</p></section></article>''')
        self.assertEqual(result.title, "Protein interactionsin cells")
        self.assertFalse(result.front_matter)

    def test_generic_h1_is_not_a_sciencedirect_category_badge(self):
        result = self.extract('''<article><h1><div>Communication</div>
        <span>in cells</span></h1><section><h2>Body</h2><p>Text.</p></section></article>''')
        self.assertFalse(result.front_matter)


if __name__ == "__main__":
    unittest.main()
