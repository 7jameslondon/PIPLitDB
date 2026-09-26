import unittest
from lxml import html
from scripts.extraction.html_extractor import (
    _normalize_oup_silverchair_snapshot,
    _oup_silverchair_article_root,
    _oup_silverchair_figure_caption,
)


def fixture(scheme=1):
    cards = ''.join(f'''<div swap-content-for-modal="true"><div><img src="data:image/png;base64,AA==" alt="{kind} {number}"/><div><div id="label-{i}">{kind} {number}.</div><div><p>Authored <sup>13</sup>C caption.</p></div><div><a aria-describedby="label-{i}" href="/view-large/figure/{i}/a.png">Open in new tab</a><a aria-describedby="label-{i}" href="/DownloadFile/DownloadImage.aspx?x={i}">Download slide</a></div></div></div></div>''' for i, (kind, number) in enumerate([('Figure', 1), ('Scheme', scheme), ('Figure', 2)], 1))
    return html.fromstring(f'''<div><h2 id="1">Abstract</h2><section aria-label="Main abstract"><p>Abstract.</p></section><h2 id="2">Introduction</h2>{cards}<h2 id="3">References</h2><div><div content-id="B1"><div><span><a name="jumplink-B1" aria-label="jumplink-B1"></a></span></div><div><div id="ref-auto-B1"><span>1.</span><div>Author. Complete citation.<div><a href="https://scholar.google.com/scholar_lookup?title=Example">Google Scholar</a><div><a href="https://doi.org/10.1000/example">Crossref</a></div></div></div></div></div></div></div></div>''')


class ModernSchemeCardsTests(unittest.TestCase):
    def test_affiliation_correspondence_and_orcid_coexist(self):
        from tests.test_oup_legacy_modal_cards import fixture as article_fixture
        from scripts.extraction.html_extractor import _oup_silverchair_front_matter
        source=article_fixture().replace('<div>Search for other works by this author on:</div>', '<div><div content-id="COR1">Contact: author@example.org</div></div><div><a id="contrib-orcid-0000-0000-0000-000X" href="https://orcid.org/0000-0000-0000-000X">https://orcid.org/0000-0000-0000-000X</a></div><div>Search for other works by this author on:</div>')
        blocks=_oup_silverchair_front_matter(html.fromstring(source),'synthetic.html')
        self.assertTrue(any('Institute' in b.plain_text for b in blocks))
        self.assertTrue(any('Contact:' in b.plain_text for b in blocks))
        self.assertTrue(any('ORCID:' in b.plain_text for b in blocks))

    def test_distinct_contiguous_series_and_caption(self):
        doc = fixture()
        _normalize_oup_silverchair_snapshot(doc)
        self.assertIs(_oup_silverchair_article_root(doc), doc)
        self.assertEqual([f.get('id') for f in doc.xpath('./figure')], ['figure-1', 'scheme-1', 'figure-2'])
        self.assertIn('<sup>13</sup>', _oup_silverchair_figure_caption(doc.xpath('./figure')[1])[1])

    def test_noncontiguous_scheme_fails_closed(self):
        doc = fixture(2)
        _normalize_oup_silverchair_snapshot(doc)
        self.assertFalse(doc.xpath('./figure'))

    def test_mismatched_id_and_label_fails_closed(self):
        doc = fixture()
        _normalize_oup_silverchair_snapshot(doc)
        scheme = doc.xpath('./figure')[1]
        scheme.set('id', 'figure-1')
        self.assertIsNone(_oup_silverchair_figure_caption(scheme))
