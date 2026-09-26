import unittest
from lxml import etree
from scripts.extraction.html_extractor import _render_mathml


class MathmlStackedOperatorTests(unittest.TestCase):
    SOURCE = ('<math><mtable><mtr><mtd><msup><mi/><mi>m</mi></msup></mtd></mtr>'
              '<mtr><mtd><mrow><mo>∑</mo></mrow></mtd></mtr>'
              '<mtr><mtd><msub><mi/><mrow><mi>j</mi><mo>=</mo><mn>0</mn></mrow>'
              '</msub></mtd></mtr></mtable><mi>x</mi></math>')

    def test_exact_upper_operator_lower_stack_keeps_associated_limits(self):
        node = etree.fromstring(self.SOURCE)
        self.assertEqual(_render_mathml(node, markup='plain'), '∑_{j = 0}^{m}x')
        self.assertEqual(_render_mathml(node, markup='markdown'), '∑<sub>j = 0</sub><sup>m</sup>x')

    def test_nonempty_base_does_not_become_operator_limit(self):
        node = etree.fromstring(self.SOURCE.replace('<mi/>', '<mi>q</mi>', 1))
        result = _render_mathml(node, markup='plain')
        self.assertIn('q^{m}', result)
        self.assertNotIn('∑_{j = 0}^{m}', result)
