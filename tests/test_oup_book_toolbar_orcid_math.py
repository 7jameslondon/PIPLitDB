import unittest
from lxml import html
from scripts.extraction.html_extractor import _oup_silverchair_reference_blocks, _oup_silverchair_front_matter, _display_equation_containers


class OupBookToolbarOrcidMathTests(unittest.TestCase):
    def bibliography(self, preview='https://www.google.com/search?q=Book&btnG=Search+Books&tbm=bks&tbo=1'):
        controls = [('Google Scholar','https://scholar.google.com/scholar_lookup?title=Book'),('Crossref','https://doi.org/10.1000/book'),('Search ADS','http://adsabs.harvard.edu/'),('Google Preview',preview),('WorldCat','https://www.worldcat.org/search?q=ti:Book&qt=advanced&dblist=638'),('COPAC','http://copac.ac.uk/search?ti=Book')]
        links=''.join(f'<p><a href="{url}">{name}</a></p>' for name,url in controls)
        return html.fromstring(f'<div><h2>References</h2><div><div content-id="B1"><div><span><a name="jumplink-B1" aria-label="jumplink-B1"></a></span></div><div><div id="ref-auto-B1"><span>1.</span><div><div>Author. A book.</div><div>{links}</div></div></div></div></div></div></div>')[0]

    def test_catalog_controls_preserve_book_and_doi(self):
        refs = _oup_silverchair_reference_blocks(self.bibliography(),'synthetic.html')
        self.assertEqual(len(refs),1)
        self.assertIn('A book.', refs[0].plain_text)
        self.assertIn('https://doi.org/10.1000/book', refs[0].plain_text)
        self.assertNotIn('Google Preview',refs[0].plain_text)

    def test_catalog_impostor_is_not_removed(self):
        self.assertIsNone(_oup_silverchair_reference_blocks(self.bibliography('https://example.com/'),'synthetic.html'))

    def test_adjacent_book_fields_keep_word_boundaries(self):
        heading=self.bibliography()
        body=heading.getnext().xpath('.//div[@id="ref-auto-B1"]/div/div')[0]
        body.text=None
        for value in ('Basel', 'Publisher', '303'):
            node=html.Element('div'); node.text=value; body.append(node)
        refs=_oup_silverchair_reference_blocks(heading,'synthetic.html')
        self.assertIn('Basel Publisher 303',refs[0].plain_text)

    def test_orcid_does_not_discard_affiliations(self):
        from tests.test_oup_legacy_modal_cards import fixture
        source=fixture().replace('<div>Search for other works by this author on:</div>','<div><a id="contrib-orcid-0000-0000-0000-000X" href="https://orcid.org/0000-0000-0000-000X">https://orcid.org/0000-0000-0000-000X</a></div><div>Search for other works by this author on:</div>')
        blocks=_oup_silverchair_front_matter(html.fromstring(source),'synthetic.html')
        self.assertTrue(any('Institute' in b.plain_text for b in blocks))
        self.assertTrue(any('ORCID:' in b.plain_text for b in blocks))

    def test_named_numbered_math_container(self):
        root=html.fromstring('<div><div content-id="m1"><div id="jumplink-m1"><div><mjx-container display="true"><math display="block"><mi>x</mi><mo>=</mo><mn>1</mn></math></mjx-container></div></div></div></div>')
        self.assertEqual([e.tag for e in _display_equation_containers(root)], ['math'])
        root[0].set('content-id','m2')
        self.assertEqual(_display_equation_containers(root), [])

    def test_nested_graphical_abstract_card(self):
        from tests.test_oup_legacy_modal_cards import fixture, OupLegacyModalTests
        from lxml import html as lh
        doc=lh.fromstring(fixture())
        image=doc.xpath('//img')[0].get('src')
        card=f'<div><div swap-content-for-modal="true"><div><img src="{image}" alt="Graphical Abstract"/><div><div id="label-99">Graphical Abstract</div><div><a role="button" aria-describedby="label-99" href="/view-large/figure/99/ga.jpg">Open in new tab</a><a role="button" aria-describedby="label-99" href="/DownloadFile/DownloadImage.aspx?a=1">Download slide</a></div></div></div></div></div>'
        source=fixture().replace('<p>Abstract text.</p>', '<p>Abstract text.</p>'+card)
        result=OupLegacyModalTests().extract(source)
        self.assertTrue(any(f.label.casefold()=='graphical abstract' for f in result.figures))

    def test_numbered_equation_survives_whole_article_extraction(self):
        from tests.test_oup_legacy_modal_cards import fixture, OupLegacyModalTests
        source=fixture().replace('<p>Abstract text.</p>', '<p>Abstract text.</p>')
        equation='<div content-id="m1"><div id="jumplink-m1"><div><mjx-container display="true"><math display="block"><mi>x</mi><mo>=</mo><mn>1</mn></math></mjx-container></div></div><span>(1)</span></div>'
        source=source.replace('<h2 id="3">References', equation+'<h2 id="3">References', 1)
        result=OupLegacyModalTests().extract(source)
        self.assertTrue(any(b.kind=='equation' and b.plain_text=='x = 1 (1)' for s in result.sections for b in s.blocks))

    def test_numbered_equation_label_is_not_repeated_in_following_prose(self):
        from scripts.extraction.html_extractor import _segmented_inline_content
        source='<div>Before.<div content-id="m1"><div id="jumplink-m1"><div><mjx-container display="true"><math display="block"><mi>x</mi></math></mjx-container></div></div><span>(1)</span></div>where x is measured.</div>'
        root=html.fromstring(source)
        events=_segmented_inline_content(root,_display_equation_containers(root))
        self.assertEqual([e[2] for e in events if e[0]=='prose'],['Before.','where x is measured.'])
        near_miss=html.fromstring(source.replace('<span>(1)</span>','<span>(2)</span>'))
        events=_segmented_inline_content(near_miss,_display_equation_containers(near_miss))
        self.assertIn('(2)where x is measured.',[e[2] for e in events if e[0]=='prose'])
