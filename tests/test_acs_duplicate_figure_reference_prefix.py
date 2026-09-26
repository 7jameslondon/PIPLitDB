import unittest

from lxml import html

from scripts.extraction.html_extractor import (
    _remove_legacy_acs_duplicate_figure_reference_prefixes,
    plain_text,
)


class LegacyAcsDuplicateFigureReferencePrefixTests(unittest.TestCase):
    def test_complete_reveal_label_replaces_redundant_literal_prefix(self) -> None:
        document = html.fromstring(
            '<p>These contacts are not indicated in Figure '
            '<a reveal-id="articlef00006">Figure 6</a> because of clarity.</p>'
        )

        _remove_legacy_acs_duplicate_figure_reference_prefixes(document)

        self.assertEqual(
            plain_text(document),
            "These contacts are not indicated in Figure 6 because of clarity.",
        )

    def test_number_only_link_and_unrelated_literal_text_are_unchanged(self) -> None:
        number_only = html.fromstring(
            '<p>See Figure <a reveal-id="articlef00006">6</a>.</p>'
        )
        unrelated = html.fromstring(
            '<p>See the <a reveal-id="articlef00006">Figure 6</a>.</p>'
        )

        _remove_legacy_acs_duplicate_figure_reference_prefixes(number_only)
        _remove_legacy_acs_duplicate_figure_reference_prefixes(unrelated)

        self.assertEqual(plain_text(number_only), "See Figure 6.")
        self.assertEqual(plain_text(unrelated), "See the Figure 6.")


if __name__ == "__main__":
    unittest.main()
