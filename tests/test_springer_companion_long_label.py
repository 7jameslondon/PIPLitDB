"""Springer's reading companion may spell out Figure in duplicate cards."""
import unittest
from unittest.mock import patch

from lxml import html
from scripts.extraction.html_extractor import (
    _semantic_figure_elements, _springer_split_reference_blocks,
    _springer_post_reference_boundaries,
    _springer_front_matter,
)


class SpringerCompanionLongLabelTests(unittest.TestCase):
    def test_revised_date_in_recognized_article_history(self):
        root = html.fromstring('''<main><div id="article-info-section">
        <h2 id="article-info">About this article</h2><ul>
        <li>Received: <time>01 March 2001</time></li>
        <li>Revised: <time>11 April 2001</time></li>
        <li>Accepted: <time>23 April 2001</time></li>
        <li>Published: <time>15 June 2001</time></li>
        </ul></div></main>''')
        with patch('scripts.extraction.html_extractor._springer_header_details', return_value={'title': 'Article'}):
            blocks = _springer_front_matter(root, 'source')
        self.assertEqual(blocks[0].plain_text, 'Article history: Received: 01 March 2001; Revised: 11 April 2001; Accepted: 23 April 2001; Published: 15 June 2001')

    def test_permuted_reference_ids_keep_source_order_and_back_matter(self):
        root = html.fromstring('''<main><section><h2>References</h2><ul>
        <li><p id="ref-CR2">Alpha citation.</p><p><a aria-label="Google Scholar reference 1">Google Scholar</a></p></li>
        <li><p id="ref-CR1">Beta citation.</p><p><a aria-label="Google Scholar reference 2">Google Scholar</a></p></li>
        </ul></section><section><h2>Acknowledgements</h2><p>Thanks.</p></section>
        <section><h2>Author information</h2><p>Institute.</p></section>
        <section><h2>Rights and permissions</h2></section></main>''')
        refs = _springer_split_reference_blocks(root[0], 'source')
        self.assertEqual([ref.plain_text for ref in refs], ['1. Alpha citation.', '2. Beta citation.'])
        bounds = _springer_post_reference_boundaries(root, 'source')
        self.assertEqual([node.text for node in bounds], ['Acknowledgements', 'Rights and permissions'])
        root.xpath('.//p[@id="ref-CR1"]')[0].set('id', 'ref-CR2')
        self.assertIsNone(_springer_split_reference_blocks(root[0], 'source'))

    def test_full_label_excludes_only_matching_sidebar_card(self):
        for label in ("Fig. 1", "Figure 1"):
            with self.subTest(label=label):
                root = html.fromstring(f'''<main>
                <figure id="figure-1"><figcaption>Figure 1. Authored</figcaption></figure>
                <aside aria-label="reading companion"><div id="tabpanel-figures" role="tabpanel">
                <figure><figcaption><b id="rc-Fig1">{label}</b></figcaption><picture/>
                <p><a href="#Fig1">View in article</a></p></figure>
                </div></aside></main>''')
                self.assertEqual([item.get("id") for item in _semantic_figure_elements(root)], ["figure-1"])

    def test_mismatched_label_and_authored_aside_are_retained(self):
        root = html.fromstring('''<main><aside aria-label="reading companion">
        <figure id="authored"><figcaption><b id="rc-Fig1">Figure 1</b></figcaption></figure>
        <div id="tabpanel-figures" role="tabpanel"><figure id="mismatch">
        <figcaption><b id="rc-Fig2">Figure 1</b></figcaption></figure></div>
        </aside></main>''')
        self.assertEqual([item.get("id") for item in _semantic_figure_elements(root)], ["authored", "mismatch"])


if __name__ == "__main__":
    unittest.main()
