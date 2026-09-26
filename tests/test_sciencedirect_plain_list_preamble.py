import unittest
from tests import test_sciencedirect_html_extractor as helpers


class PlainListPreambleTests(unittest.TestCase):
    extract = helpers.ScienceDirectHtmlExtractionTests.extract

    def test_plain_preamble_before_only_list_child_is_not_lost(self):
        result=self.extract('''<article><h1>Example</h1><div id="body"><section id="aep-section-id1">
          <h4>Probe sequences</h4><div>Authored explanation of probe labeling.<ul>
          <li><div>Sequence A</div></li><li><div>Sequence B</div></li></ul></div>
          </section></div></article>''')
        self.assertEqual([b.kind for b in result.sections[0].blocks],['paragraph','list'])
        self.assertEqual(result.sections[0].blocks[0].plain_text,'Authored explanation of probe labeling.')
        self.assertEqual(result.sections[0].blocks[1].plain_text.count('Sequence A'),1)

    def test_list_only_wrapper_does_not_create_empty_paragraph(self):
        result=self.extract('''<article><h1>Example</h1><div id="body"><section id="aep-section-id1">
          <h4>Probe sequences</h4><div><ul><li>Sequence A</li></ul></div>
          </section></div></article>''')
        self.assertEqual([b.kind for b in result.sections[0].blocks],['list'])
