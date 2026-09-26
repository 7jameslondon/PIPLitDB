from tests.test_sciencedirect_unheaded_bib_body import UnheadedBibBodyTests


class ScienceDirectHeadingCardTests(UnheadedBibBodyTests):
    def heading_source(self):
        return '''<main id="main"><h1 id="screen-reader-main-title">Synthetic title</h1>
        <a href="https://doi.org/10.1016/example">DOI</a>
        <div id="body"><section id="aep-section-id1"><h2>Results</h2>
        <section id="aep-section-id2"><h3>Test <em>method</em> (<a href="#FIGGR1">Fig. 1</a><figure id="FIGGR1"><img src="figure.png"/><span><span><p><span>Figure 1</span>. Authored caption.</p></span></span></figure>)</h3>
        <div>Preserved prose.</div></section>
        <section id="aep-section-id3"><h3>Other section</h3><div>Other prose.</div></section>
        </section></div></main>'''

    def test_heading_excludes_cards_but_retains_citation_tail_and_markup(self):
        result = self.extract(self.heading_source())
        self.assertEqual([section.heading for section in result.sections],
                         ['Results', 'Test <em>method</em> (Fig. 1)', 'Other section'])
        self.assertEqual([block.plain_text for section in result.sections for block in section.blocks],
                         ['Preserved prose.', 'Other prose.'])
        self.assertEqual(len(result.figures), 1)
        self.assertIn('Authored caption.', result.figures[0].caption_plain)

    def test_unrelated_document_does_not_activate_dialect_filter(self):
        result = self.extract(self.heading_source().replace('10.1016/example', '10.9999/example'))
        self.assertTrue(any('Authored caption.' in section.heading for section in result.sections))
