import tempfile
import unittest
from pathlib import Path
from tests.test_pdf_text_extractor import _write_pdf
from scripts.extraction.pdf_text_extractor import extract_pdf_text, _FontInfo, _decode_char
from scripts.extraction.pdf_article_extractor import _clean, _split_heading
from tests.test_pdf_article_pipeline import _line


class LegacyPublisherPiTests(unittest.TestCase):
    def test_literal_chemical_arrow_is_font_scoped(self):
        f=_FontInfo('Universal-ChemicalPi','Universal-ChemicalPi',{},False,False,False)
        c={'text':'3','fontname':f.name,'x0':0,'x1':6,'top':0,'bottom':10}
        self.assertEqual(_decode_char(c,1,{f.name:f},{}).text,'→')
        self.assertEqual(_decode_char(c,1,{f.name:f},{f.name.lower():{'U+0033':'X'}}).text,'X')
        self.assertEqual(_decode_char(dict(c,fontname='Text'),1,{},{}).text,'3')

    def test_standard_headings_and_ligatures_preserve_scientific_notation(self):
        for heading in ('MATERIALS AND METHODS','RESULTS','DISCUSSION'):
            self.assertEqual(_split_heading(_line(1,100,heading)),(heading,'',''))
        self.assertEqual(_clean('ﬁnal ﬂow α² ₃'), 'final flow α² ₃')

    def test_font_scoped_aliases_unknowns_and_override_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'aliases.pdf'
            _write_pdf(path, fonts={
                'F1': ('ABCDEF+Universal-GreekwithMathPi', [
                    'H11001','H11002','H11005','H11011','H11021','H11022',
                    'H11032','H9251','H9252','H9253','H9262','H19999']),
                'F2': ('Universal-NewswithCommPi', ['H18528']),
                'F3': ('MathematicalPi-Six', ['H11569']),
                'F4': ('Unrelated', ['H9251']),
            }, operations=[('F1',12,40,240,bytes(range(1,13))),
                           ('F2',12,40,210,bytes([1])),
                           ('F3',12,40,180,bytes([1])),
                           ('F4',12,40,150,bytes([1]))])
            result=extract_pdf_text(path,'pdf/aliases.pdf')
            self.assertEqual([l.plain_text for l in result.lines],
                             ['+−=∼<>′αβγμ�','·','*','�'])
            self.assertEqual(len(result.warnings),2)
            fixed=extract_pdf_text(path,'pdf/aliases.pdf',glyph_overrides={
                'Universal-GreekwithMathPi': {'H9251':'x'}})
            self.assertIn('′xβ',fixed.lines[0].plain_text)
