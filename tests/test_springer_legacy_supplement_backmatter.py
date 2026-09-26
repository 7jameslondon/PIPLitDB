import unittest
from unittest.mock import patch
from lxml import html
from scripts.extraction.html_extractor import (
    _springer_post_reference_boundaries, _springer_download_descriptions,
    _springer_front_matter,
    _springer_header_details,
    _springer_split_reference_blocks,
)


class SpringerLegacySupplementBackmatterTests(unittest.TestCase):
    def test_nomad_before_multi_link_toolbar_is_not_reference_text(self):
        root = html.fromstring('''<section><ol><li><p id="ref-CR1">Author. Citation. 10.1000/example</p>
        <div id="libkey-nomad-10-1000-example-0"><div/></div><p>
        <a aria-label="Article reference 1" href="https://doi.org/10.1000%2Fexample">Article</a>
        <a aria-label="CAS reference 1">CAS</a><a aria-label="Google Scholar reference 1">Google Scholar</a>
        </p></li></ol></section>''')
        refs = _springer_split_reference_blocks(root, 'source')
        self.assertEqual(len(refs), 1)
        self.assertNotIn('Google Scholar', refs[0].plain_text)
        self.assertIn('Citation.', refs[0].plain_text)
        root.xpath('.//a')[1].text = 'Authored commentary'
        self.assertIsNone(_springer_split_reference_blocks(root, 'source'))

    def test_breadcrumb_header_article_number_enables_metadata(self):
        root = html.fromstring('''<main><div><nav>Journal Article</nav><h1>Example</h1>
        <ul><li>Research article</li><li>Published: 04 March 2001</li></ul>
        <ul><li>Volume 12, article number 7 (2001)</li><li>Cite this article</li></ul></div>
        <p id="ref-CR1">Citation.</p></main>''')
        result = _springer_header_details(root)
        self.assertEqual((result['volume'],result['article_number'],result['date']), ('12','7','04 March 2001'))
        root.xpath('.//nav')[0].text = 'Unrelated navigation'
        self.assertIsNone(_springer_header_details(root))

    def test_legacy_additional_information_resumes_after_bibliography(self):
        root = html.fromstring('''<main><section><h2>References</h2><ul>
        <li><p id="ref-CR1">Citation.</p><p><a aria-label="Google Scholar reference 1">Google Scholar</a></p></li>
        </ul></section><section><h2>Acknowledgements</h2><p>Grant.</p></section>
        <section><h2>Author information</h2></section>
        <section><h2>Additional information</h2><h3>Authors' contributions</h3><p>Work.</p></section>
        <section><h2>Electronic supplementary material</h2></section></main>''')
        self.assertEqual([n.text for n in _springer_post_reference_boundaries(root, 'source')],
                         ['Acknowledgements', 'Electronic supplementary material'])
        root.xpath('.//p[@id="ref-CR1"]')[0].attrib.clear()
        self.assertIsNone(_springer_post_reference_boundaries(root, 'source'))

    def test_description_and_original_figure_label_without_download_chrome(self):
        root = html.fromstring('''<main>
        <div id="MOESM1"><h3><a href="https://media.springernature.com/original/springer-static/esm/art%3A10.1000%2Fsample/MediaObjects/sample_MOESM1_ESM.DOC">file (download DOC)</a></h3><div><p>Additional file 1: OOH<sup>-</sup> structures.</p></div></div>
        <div id="MOESM2"><h3><a href="https://media.springernature.com/original/springer-static/esm/art%3A10.1000%2Fsample/MediaObjects/sample_MOESM2_ESM.pdf">Authors’ original file for figure 1 (download PDF )</a></h3></div>
        <div id="MOESM3"><h3><a href="https://unrelated.example/file">Other file</a></h3><div><p>Unrelated</p></div></div>
        </main>''')
        blocks = _springer_download_descriptions(root, 'source')
        self.assertEqual(len(blocks), 2)
        self.assertIn('<sup>-</sup>', blocks[0].markdown)
        self.assertEqual(blocks[1].plain_text, 'Authors’ original file for figure 1')

    def test_multiple_correspondents_retain_each_address(self):
        root = html.fromstring('''<main><h3 id="corresponding-author">Corresponding authors</h3>
        <p id="corresponding-author-list">Correspondence to <a aria-label="email Alpha" href="mailto:a@example.org">Alpha</a> or <a aria-label="email Beta" href="mailto:b@example.org">Beta</a>.</p>
        <div id="article-info-section"><h2 id="article-info">About this article</h2><ul>
        <li>Received: <time>01 March 2001</time></li><li>Accepted: <time>03 March 2001</time></li><li>Published: <time>04 March 2001</time></li></ul></div></main>''')
        with patch('scripts.extraction.html_extractor._springer_header_details', return_value={'title':'Example'}):
            blocks = _springer_front_matter(root, 'source')
        self.assertEqual([b.plain_text for b in blocks[:2]], ['Correspondence: Alpha — a@example.org', 'Correspondence: Beta — b@example.org'])
