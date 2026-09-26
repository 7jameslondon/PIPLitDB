import unittest
from scripts.extraction.pdf_text_extractor import _font_name_is_bold, _font_name_is_italic

class AdvOTStylesTests(unittest.TestCase):
    def test_explicit_style_suffixes(self):
        for suffix, bold, italic in [('B',True,False),('I',False,True),('BI',True,True)]:
            for variant in ['', '+fb', '+20']:
                with self.subTest(suffix=suffix,variant=variant):
                    name='AdvOT85fe19e1.'+suffix+variant
                    self.assertEqual(_font_name_is_bold(name),bold)
                    self.assertEqual(_font_name_is_italic(name),italic)
    def test_does_not_guess_from_hash_or_unrelated_suffix(self):
        for name in ['AdvOT88ac8687','AdvOT88ac8687+fb','Arbitrary.B','AdvOT85fe19e1.Blah','AdvOTname.I']:
            with self.subTest(name=name):
                self.assertFalse(_font_name_is_bold(name))
                self.assertFalse(_font_name_is_italic(name))
