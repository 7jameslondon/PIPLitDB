import unittest

from lxml import html

from scripts.extraction.html_extractor import _oup_silverchair_reference_blocks


class OupSinglePageReferenceTests(unittest.TestCase):
    @staticmethod
    def reference_heading(body):
        return html.fromstring(
            '<div><h2>References</h2><div><div content-id="TESTC1">'
            '<div><span><a name="jumplink-TESTC1" '
            'aria-label="jumplink-TESTC1"></a></span></div>'
            '<div><div id="ref-auto-TESTC1"><span>1.</span><div>'
            f'{body}<p></p>'
            '</div></div></div></div></div></div>'
        )[0]

    def test_toolbar_free_single_page_citation_is_complete(self):
        heading = self.reference_heading(
            '<p>Dunitz,J.D. (</p><div>1994</div>) The entropic cost of bound '
            'water in crystals and biomolecules. <div>Science</div>, '
            '<div>264</div>, <div>670</div>.'
        )

        references = _oup_silverchair_reference_blocks(heading, "synthetic.html")

        self.assertIsNotNone(references)
        self.assertEqual(len(references), 1)
        self.assertEqual(
            references[0].plain_text,
            (
                "1. Dunitz,J.D. (1994) The entropic cost of bound water in "
                "crystals and biomolecules. Science, 264, 670."
            ),
        )

    def test_single_page_locator_without_terminal_period_fails_closed(self):
        heading = self.reference_heading(
            '<p>Author,A. (</p><div>2000</div>) Article. '
            '<div>Journal</div>, <div>1</div>, <div>7</div>'
        )

        self.assertIsNone(
            _oup_silverchair_reference_blocks(heading, "synthetic.html")
        )


if __name__ == "__main__":
    unittest.main()
