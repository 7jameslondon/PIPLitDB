import unittest

from scripts.extraction.html_extractor import (
    _normalize_scientific_prime_marks,
    _normalize_scientific_prime_marks_in_markup,
)


class ScientificPrimeMinusTests(unittest.TestCase):
    def test_nucleotide_direction_with_unicode_minus(self):
        self.assertEqual(
            _normalize_scientific_prime_marks('DNA runs in the 5‘−3‘ direction.'),
            'DNA runs in the 5′−3′ direction.',
        )

    def test_balanced_quoted_phrase_stays_quoted(self):
        self.assertEqual(
            _normalize_scientific_prime_marks('‘Duplex 5’−control'),
            '‘Duplex 5’−control',
        )

    def test_rich_direction_agrees_across_styled_nodes(self):
        self.assertEqual(
            _normalize_scientific_prime_marks_in_markup('in the <em>5‘</em>−3‘ direction'),
            'in the <em>5′</em>−3′ direction',
        )
