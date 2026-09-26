import unittest
from lxml import html
from scripts.extraction.html_extractor import _figure_caption


class InlineSemanticCaptionTests(unittest.TestCase):
    def test_inline_caption_preserves_text_and_tails(self):
        figure = html.fromstring('<figure id="scheme-1"><img src="x.png"><figcaption>Scheme 1. <sup>a</sup> Reagents: H<sub>2</sub>O, RNH<sub>3</sub><sup>+</sup>; end.</figcaption></figure>')
        label, markdown, plain = _figure_caption(figure)
        self.assertIn('Reagents:', plain)
        self.assertIn('end.', plain)
        self.assertIn('H<sub>2</sub>O', markdown)
        self.assertIn('RNH<sub>3</sub><sup>+</sup>', markdown)

    def test_block_control_caption_retains_control_filter(self):
        figure = html.fromstring('<figure><img src="x.png"><figcaption><div><a>Open in figure viewer</a></div><div>Authored body</div></figcaption></figure>')
        _, markdown, plain = _figure_caption(figure)
        self.assertEqual(plain, 'Authored body')
        self.assertNotIn('Open in figure viewer', markdown)

    def test_inline_caption_with_internal_figure_references_preserves_prose(self):
        figure = html.fromstring('<figure id="F4"><img src="x.png"><figcaption>Substitution of the linker. (<i>A</i>) See Fig. <a href="#F1">1</a><i>B</i>; compounds <b>5</b> and <b>8</b> at 500 nM. (<i>C</i>) Isotherm (<i>n</i> = 1) from a nonlinear least-squares algorithm.</figcaption></figure>')
        _, markdown, plain = _figure_caption(figure)
        self.assertEqual(plain, 'Substitution of the linker. (A) See Fig. 1B; compounds 5 and 8 at 500 nM. (C) Isotherm (n = 1) from a nonlinear least-squares algorithm.')
        self.assertIn('500 nM', markdown)
        self.assertIn('nonlinear least-squares algorithm.', markdown)
