import unittest
from tests import test_acs_split_references as fixtures


class AcsAbstractPublicationNoteTests(unittest.TestCase):
    extract = fixtures.AcsSplitReferenceTests.extract

    def source(self):
        return fixtures.snapshot(split=False).replace(
            '<figure><img',
            '<div><div id="17-content"><p>Abstract published in <em>Advance ACS\n'
            'Abstracts,</em> July 15, 1997.</p></div></div>'
            '<h2 id="18">Introduction</h2><div id="19-content"><p>Body content.</p></div>'
            '<figure><img', 1)

    def test_publication_note_is_front_matter_once(self):
        result = self.extract(self.source())
        note = 'Abstract published in Advance ACS Abstracts, July 15, 1997.'
        self.assertEqual([b.plain_text for b in result.front_matter].count(note), 1)
        self.assertFalse(any(b.plain_text == note for s in result.sections for b in s.blocks))
        self.assertEqual([s.heading for s in result.sections].count('Introduction'), 1)
        self.assertTrue(any(b.plain_text == 'Body content.' for s in result.sections for b in s.blocks))

    def test_near_misses_remain_body_content(self):
        for old, new in [('https://doi.org/10.1021/', 'https://doi.org/10.1000/'),
                         ('id="17-content"', 'id="ordinary"'),
                         ('Advance ACS\nAbstracts,', 'Another Journal,')]:
            result = self.extract(self.source().replace(old, new))
            self.assertFalse(any('Abstract published in' in b.plain_text for b in result.front_matter))
            self.assertTrue(any('Abstract published in' in b.plain_text for s in result.sections for b in s.blocks))

    def test_unheaded_introduction_note_is_front_matter_once(self):
        source = self.source().replace('<h2 id="18">Introduction</h2>', '')
        source = source.replace('<figure><img', '<h2>Discussion</h2><figure><img', 1)
        result = self.extract(source)
        note = 'Abstract published in Advance ACS Abstracts, July 15, 1997.'
        self.assertEqual([b.plain_text for b in result.front_matter].count(note), 1)
        self.assertFalse(any(b.plain_text == note for s in result.sections for b in s.blocks))
        self.assertTrue(any(b.plain_text == 'Body content.' for s in result.sections for b in s.blocks))

    def test_note_after_body_heading_is_not_reclassified(self):
        source = self.source().replace('<div><div id="17-content">',
                                     '<h2>Discussion</h2><div><div id="17-content">')
        result = self.extract(source)
        self.assertFalse(any('Abstract published in' in b.plain_text for b in result.front_matter))
        self.assertTrue(any('Abstract published in' in b.plain_text for s in result.sections for b in s.blocks))


if __name__ == '__main__':
    unittest.main()
