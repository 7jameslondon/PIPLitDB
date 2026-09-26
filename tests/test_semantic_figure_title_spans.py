import unittest
from lxml import html
from scripts.extraction.html_extractor import _figure_caption


class SemanticFigureTitleSpanTests(unittest.TestCase):
    def figure(self):
        return html.fromstring('<figure id="fig2"><a><img src="local.png"></a>'
            '<figcaption><div id="fig2-title"><span>Figure 2</span> '
            '<span><b>Authored title.</b> H<sub>2</sub>O <i>panel A</i>.</span>'
            '</div></figcaption></figure>')

    def test_exact_separate_label_keeps_entire_formatted_caption(self):
        self.assertEqual(_figure_caption(self.figure()),
            ('Figure 2', '<strong>Authored title.</strong> H<sub>2</sub>O <em>panel A</em>.',
             'Authored title. H_{2}O panel A.'))

    def test_mismatched_figure_identity_does_not_strip_label(self):
        figure = self.figure()
        figure.set('id', 'fig3')
        self.assertIn('Figure 2', _figure_caption(figure)[2])

    def test_extra_scientific_child_is_not_discarded(self):
        figure = self.figure()
        wrapper = figure.xpath('.//figcaption/div')[0]
        wrapper.append(html.fromstring('<span>Additional caption.</span>'))
        self.assertIn('Additional caption.', _figure_caption(figure)[2])
