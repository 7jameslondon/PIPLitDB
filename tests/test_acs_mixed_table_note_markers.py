import unittest
from lxml import html
from scripts.extraction.html_extractor import _legacy_acs_table_notes


class AcsMixedTableNoteMarkersTests(unittest.TestCase):
    def test_reversed_marker_nesting_preserves_distinct_notes(self):
        note = html.fromstring('<p><em><sup>a</sup></em> First M<sup>-1</sup>.'
                               '<em><sup>b</sup></em> Second CaCl<sub>2</sub>.'
                               '<sup><em>c</em></sup> Third K<sub>a</sub>.</p>')
        values = _legacy_acs_table_notes(note)
        self.assertEqual([v[0] for v in values],
                         ['[a] First M^{-1}.', '[b] Second CaCl_{2}.', '[c] Third K_{a}.'])

    def test_mixed_content_superscript_is_not_a_marker(self):
        note = html.fromstring('<p><em><sup>a</sup></em> First.'
                               '<em><sup>b</sup></em> Value x<sup>2<em>n</em></sup>.</p>')
        values = _legacy_acs_table_notes(note)
        self.assertEqual(len(values), 2)
        self.assertIn('2n', values[1][0])
