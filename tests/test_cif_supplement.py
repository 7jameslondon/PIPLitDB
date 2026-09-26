from pathlib import Path
import hashlib
import tempfile
import unittest
from scripts.extraction.cif_supplement import parse_cif, extract_cif_supplement
from scripts.extraction.models import SourceFile
from scripts.extraction.supplements import extract_supplements
from scripts.extraction.rich_text import inline_markup_to_safe_html, rich_text_matches_plain


class CifSupplementTests(unittest.TestCase):
    def test_multiple_blocks_loops_primes_uncertainties_and_missing_values(self):
        text="""# source notice
data_one
_formula 'C16 H25 N3 O6'
_details
;
Refinement of F^2^ with <b>literal</b> and _tag.
;
loop_
_atom_label
_atom_x
O5' 0.34381(13)
C2'' ?
data_two
_missing .
_quoted 'data_literal'
"""
        data,comments=parse_cif(text)
        self.assertEqual([b['name'] for b in data],['one','two'])
        self.assertEqual(data[0]['loops'][0][2],[["O5'",'0.34381(13)'],["C2''",'?']])
        self.assertEqual(data[1]['scalars'][-1][-1],'data_literal')
        self.assertEqual(comments,[(1,'# source notice')])
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'data.cif';p.write_text(text,encoding='utf8');payload=p.read_bytes()
            s=SourceFile('supplement',p,'papers (private)/00001/supplementary/data.cif',len(payload),hashlib.sha256(payload).hexdigest(),'application/octet-stream')
            blocks,tables,warnings=extract_cif_supplement(s,'supplement_001',cif_path=p)
            self.assertFalse(warnings)
            self.assertEqual(len(tables),3)
            for t in tables:
                for row in t.parts[0].rows:
                    for c in row:
                        self.assertTrue(rich_text_matches_plain(c.text,inline_markup_to_safe_html(c.markdown)))
            result=extract_supplements([s],Path(d)/'output')[0]
            self.assertEqual((Path(d)/'output'/result.copied_path).read_bytes(),payload)
            self.assertEqual(len(result.tables),3)
            self.assertEqual(result.warnings,[])

    def test_rejects_incomplete_or_unsupported_streams(self):
        for text in ['data_a\nloop_\n_x\n_y\n1','data_a\n_x','data_a\n_x 1\n_X 2',
                     'data_a\n_x\n;never closed','data_a\n_x \'never closed','data_a\nsave_frame\n_x 1',
                     'data_a\n_x 1\ndata_a\n_y 2','#\\#CIF_2.0\ndata_a\n_x 1']:
            with self.subTest(text=text),self.assertRaises(ValueError):parse_cif(text)

    def test_quote_inside_unquoted_atom_label_is_literal(self):
        data,_=parse_cif("data_a\nloop_\n_label\n_value\nH5'1 0.047\nO1'' .\n")
        self.assertEqual(data[0]['loops'][0][2],[["H5'1",'0.047'],["O1''",'.']])

    def test_quoted_reserved_words_and_hashes_remain_values(self):
        data,_=parse_cif("data_a\n_x 'loop_'\n_y 'a # b'\n_z atom#1\n")
        self.assertEqual([v for _,_,v in data[0]['scalars']],['loop_','a # b','atom#1'])


if __name__=='__main__':unittest.main()
