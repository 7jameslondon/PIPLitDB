import unittest
from scripts.extraction.pdf_text_extractor import _decode_char


class AdvpsLiteralGreekTests(unittest.TestCase):
    def test_upright_greek_micro_and_omega_are_not_latin_units(self):
        for token, expected in [('m', 'μ'), ('W', 'Ω')]:
            char = {'fontname': 'ABCDEF+AdvPS_GRTU', 'text': token, 'x0': 10,
                    'x1': 15, 'top': 10, 'bottom': 20, 'size': 10}
            self.assertEqual(_decode_char(char, 1, {}, {}).text, expected)
            char['fontname'] = 'Times-Roman'
            self.assertEqual(_decode_char(char, 1, {}, {}).text, token)

    def test_literal_pi_degree_and_prime_stay_font_scoped(self):
        for token, expected in [('8', '°'), ("'", '′')]:
            char = {'fontname': 'ABCDEF+AdvPi1', 'text': token, 'x0': 10,
                    'x1': 15, 'top': 10, 'bottom': 20, 'size': 10}
            self.assertEqual(_decode_char(char, 1, {}, {}).text, expected)
            char['fontname'] = 'Times-Roman'
            self.assertEqual(_decode_char(char, 1, {}, {}).text, token)

    def test_literal_greek_font_tokens_and_latin_control(self):
        for font in ('AdvPS_GRTI', 'ABCDEF+AdvPS_GRTBI'):
            for token, expected in [('b', 'β'), ('g', 'γ')]:
                with self.subTest(font=font, token=token):
                    char = {'fontname': font, 'text': token, 'x0': 10,
                            'x1': 15, 'top': 10, 'bottom': 20, 'size': 10}
                    result = _decode_char(char, 1, {}, {})
                    self.assertEqual(result.text, expected)
                    self.assertEqual(result.diagnostic['status'], 'resolved')
                    char['fontname'] = 'AdvPS_TTR'
                    self.assertEqual(_decode_char(char, 1, {}, {}).text, token)
