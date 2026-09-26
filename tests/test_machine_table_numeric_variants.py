import unittest
from tests.test_machine_table_axis_semantics import MachineTableAxisSemanticsTests
from scripts.extraction.models import TableCell
from scripts.extraction.renderer import machine_table_records


class NumericVariantsTests(unittest.TestCase):
    def test_slanted_relation_and_decimal_spacing_preserve_source_and_numeric_value(self):
        for source,relation,value in [('⩽1.0 × 10^{7}','<=',1.0),('4. 1(±1.2) × 10^{9}','=',4.1),('8. 1(±2.3) × 10^{8}','=',8.1)]:
            with self.subTest(source=source):
                table=MachineTableAxisSemanticsTests().table('Polyamide','5′-ATGGGGT-3′')
                table.parts[0].rows[1][1]=TableCell(source,source,False)
                record=machine_table_records(table)[0]
                self.assertEqual(record['normalized_text'],source)
                self.assertEqual(record['association_constant']['relation'],relation)
                self.assertEqual(record['association_constant']['mantissa'],value)
                self.assertIsNone(record['match_site'])
