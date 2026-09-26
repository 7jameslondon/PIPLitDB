import unittest
from tests import test_acs_split_references as fixtures


class AcsFundingNotesHeadingTests(unittest.TestCase):
    extract = fixtures.AcsSplitReferenceTests.extract
    def test_numbered_dagger_notes_moves_funding_and_keeps_introduction(self):
        source = fixtures.snapshot(split=False).replace(
            'Synthetic ACS snapshot</h1>',
            'Synthetic ACS snapshot<a href="#exampleAF2">†</a></h1>',
        ).replace(
            '<figure><img',
            '<h2 id="30" scrollto-destination="30">† Notes</h2><div>'
            '<div id="31-content"><p>A.B. was supported by Grant X.</p></div>'
            '<div id="32-content"><p>Complete introduction.</p></div></div><figure><img', 1,
        )
        result = self.extract(source)
        self.assertEqual(len([b for b in result.front_matter if b.plain_text.startswith('Funding (†):')]), 1)
        self.assertFalse(any(s.heading == '† Notes' for s in result.sections))
        self.assertIn('Complete introduction.', [b.plain_text for s in result.sections for b in s.blocks])
        for old, new in [('† Notes</h2>', 'Notes</h2>'), ('scrollto-destination="30"', 'scrollto-destination="31"')]:
            near = self.extract(source.replace(old, new))
            self.assertFalse(any(b.plain_text.startswith('Funding (†):') for b in near.front_matter))


if __name__ == '__main__':
    unittest.main()
