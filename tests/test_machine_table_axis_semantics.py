import unittest
from scripts.extraction.models import TableCell, TableItem, TablePart
from scripts.extraction.renderer import machine_table_records


class MachineTableAxisSemanticsTests(unittest.TestCase):
    def table(self, row_header, column_header):
        return TableItem(table_id='t1', source_id='t1', label='Table 1',
            title_markdown='Measured K<sub>a</sub>', title_plain='Measured K_{a}',
            parts=[TablePart(part_id='p1', rows=[
                [TableCell(row_header,row_header,True),TableCell(column_header,column_header,True)],
                [TableCell('A·T','A·T',False),TableCell('2.0 × 10^{6}','2.0 × 10<sup>6</sup>',False)]] )],
            footnotes_markdown=[],footnotes_plain=[],source_path='source.html',source_locator='//table')

    def test_base_pair_and_residue_axes_do_not_become_compounds_and_sequences(self):
        self.assertEqual(machine_table_records(self.table('X·Y','Residue')),[])

    def test_property_column_is_not_a_dna_sequence(self):
        self.assertEqual(machine_table_records(self.table('Compound','Temperature')),[])

    def test_explicit_compound_sequence_matrix_remains_supported(self):
        table=self.table('Compound','5′-ACGTA-3′')
        table.parts[0].rows[1][0]=TableCell('7','7',False)
        records=machine_table_records(table)
        self.assertEqual(records[0]['compound_id'],'7')
        self.assertEqual(records[0]['sequence'],'5′-ACGTA-3′')
