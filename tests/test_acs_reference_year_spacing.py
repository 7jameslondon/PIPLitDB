import unittest

from lxml import html

from scripts.extraction.html_extractor import (
    _normalize_current_acs_inline_word_spacing,
    _normalize_legacy_acs_caption_compound_spacing,
    _normalize_legacy_acs_line_wrapped_hyphens,
    _normalize_legacy_acs_micit_author_commas,
    _normalize_legacy_acs_reference_year_spacing,
    plain_text,
)


class LegacyAcsReferenceYearSpacingTests(unittest.TestCase):
    def test_adjacent_styled_year_keeps_visible_boundary(self) -> None:
        document = html.fromstring(
            """
            <html><body><div id="article-record">
              <div id="micit1"><span>Author, A.</span><em>J. Test</em><strong>2001</strong>, <em>5</em></div>
              <div id="micit2"><span>Author, B.</span><strong>1995</strong>, <em>24</em></div>
              <div id="unrelated"><em>Control</em><strong>2002</strong></div>
            </div></body></html>
            """
        )

        _normalize_legacy_acs_reference_year_spacing(document)

        self.assertEqual(
            plain_text(document.get_element_by_id("micit1")),
            "Author, A.J. Test 2001, 5",
        )
        self.assertEqual(
            plain_text(document.get_element_by_id("micit2")),
            "Author, B. 1995, 24",
        )
        self.assertEqual(
            plain_text(document.get_element_by_id("unrelated")),
            "Control2002",
        )

    def test_titled_acs_snapshot_uses_the_same_boundary_repair(self) -> None:
        document = html.fromstring(
            """
            <html><body><div id="ContentColumn">
              <h1 id="aria1">Article</h1>
              <a href="https://doi.org/10.1021/example">DOI</a>
              <div id="ContentTab">
                <div id="micit89"><em>Proc. Natl. Acad. Sci. U.S.A.</em><strong>1994</strong></div>
              </div>
            </div></body></html>
            """
        )

        _normalize_legacy_acs_reference_year_spacing(document)

        self.assertEqual(
            plain_text(document.get_element_by_id("micit89")),
            "Proc. Natl. Acad. Sci. U.S.A. 1994",
        )

    def test_current_acs_div_year_keeps_visible_boundary(self) -> None:
        document = html.fromstring(
            """
            <html><body><div id="article-record">
              <div id="elcit1"><div>Nat. Rev. Cancer</div><div>2002</div>, <div>2</div></div>
            </div></body></html>
            """
        )

        _normalize_legacy_acs_reference_year_spacing(document)

        self.assertEqual(
            plain_text(document.get_element_by_id("elcit1")),
            "Nat. Rev. Cancer 2002, 2",
        )

    def test_current_acs_caption_keeps_boundary_before_compound_number(self) -> None:
        document = html.fromstring(
            """
            <html><body><div id="article-record"><figure><figcaption>
              <p>Chemical structures of conjugates<strong>11</strong>−<strong>14</strong>.</p>
            </figcaption></figure></div></body></html>
            """
        )

        _normalize_legacy_acs_caption_compound_spacing(document)

        self.assertEqual(
            plain_text(document.xpath("//figcaption/p")[0]),
            "Chemical structures of conjugates 11−14.",
        )

    def test_current_acs_wrapper_restores_narrow_inline_word_boundaries(self) -> None:
        document = html.fromstring(
            """
            <html><body><div id="acs-article-3782333">
              <h1>Article title</h1>
              <a href="https://doi.org/10.1021/example">DOI</a>
              <div id="micit1"><span>Author, W.</span><em>Drug Future</em><strong>2001</strong></div>
              <p><strong>General.</strong><em>N</em>,<em>N</em>-Reagent.</p>
              <figure><figcaption><p>
                Structures represent<em>N</em>,<em>N</em>-linked compounds tested with the<em>Eco</em>RI fragment,
                including compound<strong>3</strong>.
              </p></figcaption></figure>
            </div></body></html>
            """
        )

        _normalize_legacy_acs_reference_year_spacing(document)
        _normalize_legacy_acs_caption_compound_spacing(document)
        _normalize_current_acs_inline_word_spacing(document)

        self.assertEqual(
            plain_text(document.get_element_by_id("micit1")),
            "Author, W. Drug Future 2001",
        )
        self.assertEqual(
            plain_text(document.xpath("//p[strong]")[0]),
            "General. N,N-Reagent.",
        )
        self.assertEqual(
            plain_text(document.xpath("//figcaption/p")[0]),
            "Structures represent N,N-linked compounds tested with the EcoRI fragment, including compound 3.",
        )

    def test_numeric_chemical_name_is_rejoined_across_source_line_wrap(self) -> None:
        document = html.fromstring(
            """
            <html><body><div id="article-record"><p>
              commercially available 1,3-\n naphthalenediol
            </p></div></body></html>
            """
        )

        _normalize_legacy_acs_line_wrapped_hyphens(document)

        self.assertEqual(
            plain_text(document.xpath("//p")[0]),
            "commercially available 1,3-naphthalenediol",
        )

    def test_micit_author_parts_keep_surname_initial_comma(self) -> None:
        document = html.fromstring(
            """
            <html><body><div id="article-record"><div id="micit1">
              <span><div><div>Wahnert</div> <div>U.</div></div>; <div><div>Zimmer</div> <div>O.</div></div></span>
              Article title. <em>Journal</em><strong>1975</strong>
            </div></div></body></html>
            """
        )

        _normalize_legacy_acs_micit_author_commas(document)

        self.assertIn("Wahnert, U.; Zimmer, O.", plain_text(document.get_element_by_id("micit1")))


if __name__ == "__main__":
    unittest.main()
