from pathlib import Path
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch

from scripts.extraction.models import SourceFile
from scripts.extraction.xls_supplement import XLS_MEDIA_TYPE, extract_xls_supplement


class LegacyXlsTests(TestCase):
    def test_general_integer_double_does_not_invent_decimal_places(self):
        source = SourceFile('supplement', Path('data.xls'), 'private/data.xls', 0, 'a'*64, XLS_MEDIA_TYPE)
        book = self.book(); sheet = book.sheets()[0]; original_cell = sheet.cell
        sheet.cell = lambda r,c: NS(ctype=2, value=71912401.0, xf_index=0) if (r,c)==(1,0) else original_cell(r,c)
        with patch('xlrd.open_workbook', return_value=book):
            _, tables, _ = extract_xls_supplement(source, 'supplement_001', xls_path=source.path)
        self.assertEqual(tables[0].parts[0].rows[1][0].text, '71912401')
        self.assertEqual(tables[0].machine_records[0]['stored_value'], '71912401.0')

    def book(self):
        import xlrd
        values = [[(xlrd.XL_CELL_TEXT, 'Score'), (xlrd.XL_CELL_TEXT, 'Active')],
                  [(xlrd.XL_CELL_NUMBER, 26.89921905), (xlrd.XL_CELL_BOOLEAN, 1)]]
        sheet = NS(name='Hairpin 2', nrows=2, ncols=2, visibility=0, merged_cells=[],
                   cell=lambda r, c: NS(ctype=values[r][c][0], value=values[r][c][1], xf_index=0))
        return NS(sheets=lambda: [sheet], nsheets=1, datemode=0,
                  xf_list=[NS(format_key=0, font_index=0)], format_map={0:NS(format_str='General')},
                  font_list=[NS(bold=False)], release_resources=lambda: None)

    def test_complete_small_biff_sheet_uses_semantic_table_and_cached_values(self):
        source = SourceFile('supplement', Path('data.xls'), 'private/data.xls', 0, 'a'*64, XLS_MEDIA_TYPE)
        with patch('xlrd.open_workbook', return_value=self.book()) as opened:
            blocks, tables, warnings = extract_xls_supplement(source, 'supplement_001', xls_path=source.path)
        opened.assert_called_once_with('data.xls', formatting_info=True, on_demand=True)
        self.assertEqual(len(tables), 1)
        self.assertEqual([[c.text for c in r] for r in tables[0].parts[0].rows],
                         [['Score','Active'],['26.89921905','TRUE']])
        self.assertIn('BIFF;sheet=Hairpin 2', tables[0].source_locator)
        self.assertEqual(warnings, [])
        self.assertEqual(blocks[0].kind, 'workbook_summary')

    def test_large_sheet_is_linked_with_complete_range_and_headers(self):
        source = SourceFile('supplement', Path('data.xls'), 'private/data.xls', 0, 'a'*64, XLS_MEDIA_TYPE)
        with patch('xlrd.open_workbook', return_value=self.book()), patch('scripts.extraction.xls_supplement.INLINE_CELL_LIMIT', 3):
            blocks, tables, warnings = extract_xls_supplement(source, 'supplement_001', xls_path=source.path)
        self.assertEqual(tables, [])
        self.assertIn('A1:B2', blocks[1].plain_text)
        self.assertIn('Score | Active', blocks[1].plain_text)
        self.assertIn('linked original workbook data.xls', blocks[1].plain_text)

    def test_hidden_merged_blank_sheet_is_not_discarded(self):
        source = SourceFile('supplement', Path('data.xls'), 'private/data.xls', 0, 'a'*64, XLS_MEDIA_TYPE)
        book=self.book(); sheet=book.sheets()[0]
        sheet.visibility=2; sheet.merged_cells=[(0,1,0,2)]
        with patch('xlrd.open_workbook', return_value=book):
            blocks,tables,_=extract_xls_supplement(source,'supplement_001',xls_path=source.path)
        self.assertIn('veryHidden', blocks[0].plain_text)
        self.assertEqual(tables[0].parts[0].rows[0][0].colspan,2)
