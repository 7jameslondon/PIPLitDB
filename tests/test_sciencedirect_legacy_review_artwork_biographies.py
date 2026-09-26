import base64
import unittest

from tests import test_sciencedirect_html_extractor as helpers


class ScienceDirectLegacyReviewArtworkBiographyTests(unittest.TestCase):
    extract = helpers.ScienceDirectHtmlExtractionTests.extract

    def test_captionless_structure_art_and_author_biographies_are_preserved(self):
        image = base64.b64encode(helpers.PNG_BYTES).decode('ascii')
        download = '''<span><img src="data:image/png;base64,{image}" alt=""><ol><li>
          <a title="Download full-size image"
             href="https://ars.els-cdn.com/content/image/example.jpg">
             Download: Download full-size image</a></li></ol></span>'''.format(
            image=image
        )
        source = f'''<body><div>
          <div id="article"><div>
            <h1 id="screen-reader-main-title">ReviewExample review</h1>
            <a href="https://doi.org/10.1016/j.synthetic.2006.1">DOI</a>
            <div id="author-group">Ada ExampleBea Sample</div>
          </div></div>
          <div>
            <div id="abstracts">
              <div id="aep-abstract-id1"><h2>Abstract</h2>
                <div id="aep-abstract-sec-id2"><div>Authored abstract.</div></div>
              </div>
              <div id="aep-abstract-id3"><h2>Graphical abstract</h2>
                <figure id="aep-figure-id4">{download}</figure>
              </div>
            </div>
            <div id="body"><section id="aep-section-id1"><h2>1. Introduction</h2>
              <div>Prose before the structure.<span>
                <figure id="aep-figure-id5">{download}</figure>
              </span> Prose after the structure.</div>
            </section></div>
            <div><section id="aep-bibliography-id6"><h2>References</h2>
              <section id="aep-bibliography-sec-id7">
                <ol id="reference-links-aep-bibliography-sec-id7"><li>
                  <span><a id="ref-id-BIB1"
                    href="https://www.sciencedirect.com/science/article/pii/S123456789?via%3Dihub#bBIB1">Example et al., 2001</a></span>
                  <span><div>Reference one.</div><div lang="en">
                    <a href="https://doi.org/10.1000/one">Crossref</a>
                    <a href="https://www.scopus.com/example">View in Scopus</a>
                  </div></span>
                </li></ol>
              </section>
            </section></div>
            <div>
              <div id="vt1"><div><img src="data:image/png;base64,{image}"
                alt="image of the author" width="111" height="155"></div>
                <div><div><strong>Ada Example</strong> received her degree in Chemistry.</div></div>
              </div>
              <div id="vt2"><div><img src="data:image/png;base64,{image}"
                alt="image of the author" width="111" height="155"></div>
                <div><div><strong>Bea Sample</strong> studies medicinal chemistry.</div></div>
              </div>
            </div>
          </div>
        </div></body>'''

        result = self.extract(source)

        self.assertEqual(
            [(figure.figure_id, figure.kind, figure.label) for figure in result.figures],
            [
                ('graphical_abstract', 'graphical_abstract', 'Graphical Abstract'),
                ('unnumbered_artwork_001', 'figure', 'Unnumbered artwork 1'),
                ('author_portrait_001', 'figure', 'Author portrait: Ada Example'),
                ('author_portrait_002', 'figure', 'Author portrait: Bea Sample'),
            ],
        )
        biographies = next(
            section for section in result.sections
            if section.heading == 'Author biographies'
        )
        self.assertEqual(
            [block.plain_text for block in biographies.blocks],
            [
                'Ada Example received her degree in Chemistry.',
                'Bea Sample studies medicinal chemistry.',
            ],
        )
        self.assertEqual(
            [asset.asset_id for asset in result.embedded_assets],
            [
                'graphical_abstract',
                'unnumbered_artwork_001',
                'author_portrait_001',
                'author_portrait_002',
            ],
        )
        introduction = next(
            section for section in result.sections
            if section.heading == '1. Introduction'
        )
        self.assertEqual(
            [block.plain_text for block in introduction.blocks],
            ['Prose before the structure. Prose after the structure.'],
        )
        self.assertEqual(len(result.references), 1)
        self.assertTrue(
            result.references[0].plain_text.startswith('Example et al., 2001.')
        )
        self.assertIn('Reference one.', result.references[0].plain_text)
        self.assertIn(
            'DOI: https://doi.org/10.1000/one',
            result.references[0].plain_text,
        )
        self.assertNotIn('Crossref', result.references[0].plain_text)
        self.assertNotIn('View in Scopus', result.references[0].plain_text)

    def test_unheaded_preamble_and_plain_multi_paragraph_biography_are_preserved(self):
        image = base64.b64encode(helpers.PNG_BYTES).decode('ascii')
        source = f'''<body><div>
          <div id="article"><div>
            <h1 id="screen-reader-main-title">ReviewExample review</h1>
            <a href="https://doi.org/10.1016/j.synthetic.2006.5">DOI</a>
            <div id="author-group"><a href="/author/ada-example">Ada Example</a></div>
          </div></div>
          <div>
            <div id="body"><div>
              <div>First authored preamble paragraph.</div>
              <div>Second authored preamble paragraph.</div>
              <section id="aep-section-id1"><h2>Introduction</h2>
                <div>Article prose.</div>
              </section>
            </div></div>
            <div><section id="aep-bibliography-id6"><h2>References</h2>
              <section id="aep-bibliography-sec-id7">
                <ol id="reference-links-aep-bibliography-sec-id7"><li>
                  <span><a id="ref-id-BIB1" href="#bBIB1">1.</a></span>
                  <span><div>Reference one.</div></span>
                </li></ol>
              </section>
            </section></div>
            <div>
              <div id="VT1"><div><img src="data:image/png;base64,{image}"
                alt="image of the author"></div><div>
                <div>Ada Example, PhD is a synthetic chemist.</div>
                <div>Her research concerns molecular recognition.</div>
              </div></div>
            </div>
          </div>
        </div></body>'''

        result = self.extract(source)

        main_text = next(
            section for section in result.sections
            if section.heading == 'Main text'
        )
        self.assertEqual(
            [block.plain_text for block in main_text.blocks],
            [
                'First authored preamble paragraph.',
                'Second authored preamble paragraph.',
            ],
        )
        biographies = next(
            section for section in result.sections
            if section.heading == 'Author biographies'
        )
        self.assertEqual(
            [block.plain_text for block in biographies.blocks],
            [
                'Ada Example, PhD is a synthetic chemist.',
                'Her research concerns molecular recognition.',
            ],
        )
        self.assertIn(
            ('author_portrait_001', 'figure', 'Author portrait: Ada Example'),
            [
                (figure.figure_id, figure.kind, figure.label)
                for figure in result.figures
            ],
        )

    def test_unheaded_preamble_retains_aep_table_title(self):
        source = '''<body><div>
          <div id="article"><div>
            <h1 id="screen-reader-main-title">ReviewTable review</h1>
            <a href="https://doi.org/10.1016/j.synthetic.2006.6">DOI</a>
            <div id="author-group"><a href="/author/ada-example">Ada Example</a></div>
          </div></div>
          <div><div id="body"><div>
            <div><div>Authored preamble before the table.</div>
              <div id="TBL1"><span><span><p><span>Table 1</span>. Complete authored title</p></span></span>
                <div><table><thead><tr><th>Measure</th><th>Value</th></tr></thead>
                <tbody><tr><td>Binding</td><td>1</td></tr></tbody></table></div>
              </div>
            </div>
            <section id="aep-section-id1"><h2>Introduction</h2><div>Article prose.</div></section>
          </div></div></div>
        </div></body>'''

        result = self.extract(source)

        self.assertEqual(len(result.tables), 1)
        self.assertEqual(
            result.tables[0].title_plain,
            'Table 1. Complete authored title',
        )

    def test_terminal_further_reading_is_preserved_as_context_and_reference(self):
        source = '''<body><div>
          <div id="article"><div>
            <h1 id="screen-reader-main-title">ReviewFurther reading example</h1>
            <a href="https://doi.org/10.1016/j.synthetic.2006.2">DOI</a>
            <div id="author-group">Ada Example</div>
          </div></div>
          <div>
            <div id="body"><section id="aep-section-id1"><h2>Introduction</h2>
              <div>Article prose.</div>
            </section></div>
            <div><section id="aep-bibliography-id6"><h2>References</h2>
              <section id="aep-bibliography-sec-id7">
                <ol id="reference-links-aep-bibliography-sec-id7"><li>
                  <span><a id="ref-id-BIB1"
                    href="https://www.sciencedirect.com/science/article/pii/S123456789#bBIB1">1.</a></span>
                  <span><div>Reference one.</div><div lang="en">
                    <a href="https://scholar.google.com/example">Google Scholar</a>
                  </div></span>
                </li></ol>
              </section>
            </section><section id="aep-further-reading-id8"><h2>Further reading</h2>
              <section id="aep-further-reading-sec-id9"><h3>Now in press</h3>
                <div>The work referred to in the text is now in press:</div>
                <ol><li><span>2.</span><span><div>Example A: <strong>Follow-up work</strong>.
                  <em>Synthetic Journal</em> 2006, in press.</div><div lang="en">
                  <a href="https://scholar.google.com/follow-up">Google Scholar</a>
                </div></span></li></ol>
              </section>
            </section></div>
          </div>
        </div></body>'''

        result = self.extract(source)

        self.assertEqual(
            [reference.block_id for reference in result.references],
            ['reference-001', 'reference-002'],
        )
        self.assertEqual(
            result.references[1].plain_text,
            '2. Example A: Follow-up work. Synthetic Journal 2006, in press.',
        )
        self.assertNotIn('Google Scholar', result.references[1].plain_text)
        self.assertEqual(
            result.references[1].source_locator,
            '/html/body/div/div[2]/div[2]/section[2]/section/ol/li',
        )
        now_in_press = next(
            section for section in result.sections
            if section.heading == 'Now in press'
        )
        self.assertEqual(
            [block.plain_text for block in now_in_press.blocks],
            ['The work referred to in the text is now in press:'],
        )

    def test_direct_aep_article_retains_terminal_further_reading(self):
        source = '''<article>
          <h1 id="screen-reader-main-title">AEP review</h1>
          <a href="https://doi.org/10.1016/j.synthetic.2006.3">DOI</a>
          <div id="body"><section id="aep-section-id1"><h2>Introduction</h2>
            <div>Article prose.</div>
          </section></div>
          <div><section id="aep-bibliography-id6"><h2>References</h2>
            <section id="aep-bibliography-sec-id7">
              <ol id="reference-links-aep-bibliography-sec-id7"><li>
                <span><a id="ref-id-BIB1" href="#bBIB1">1.</a></span>
                <span><div>Reference one.</div><div lang="en"><span><span></span></span>
                  <a href="https://scholar.google.com/example">Google Scholar</a>
                </div></span>
              </li></ol>
            </section>
          </section><section id="aep-further-reading-id8"><h2>Further reading</h2>
            <section id="aep-further-reading-sec-id9"><h3>Now in press</h3>
              <div>One cited work is now in press:</div>
              <ol><li><span>2.</span><span><div>Example A: Follow-up work.</div>
                <div lang="en"><a href="https://scholar.google.com/follow-up">Google Scholar</a></div>
              </span></li></ol>
            </section>
          </section></div>
        </article>'''

        result = self.extract(source)

        self.assertEqual(len(result.references), 2)
        self.assertEqual(
            result.references[-1].plain_text,
            '2. Example A: Follow-up work.',
        )
        self.assertEqual(
            [
                (section.heading, [block.plain_text for block in section.blocks])
                for section in result.sections
                if section.heading in {'Further reading', 'Now in press'}
            ],
            [
                ('Further reading', []),
                ('Now in press', ['One cited work is now in press:']),
            ],
        )

    def test_direct_aep_article_restores_unbracketed_citation_groups(self):
        source = '''<article>
          <h1 id="screen-reader-main-title">AEP citation groups</h1>
          <a href="https://doi.org/10.1016/j.synthetic.2006.4">DOI</a>
          <div id="body"><section id="aep-section-id1"><h2>Introduction</h2>
            <div>Chemical tail NH(CH<sub>2</sub>)<sub>2</sub>OH. Grouped result <a href="#BIB1" name="bBIB1"><span><span>1.</span></span></a>,
              <a href="#BIB2" name="bBIB2"><span><span>2.•</span></span></a> and
              single result <a href="#BIB3" name="bBIB3"><span><span>3.••</span></span></a>.</div>
          </section></div>
          <section id="aep-bibliography-id6"><h2>References</h2>
            <section id="aep-bibliography-sec-id7">
              <ol id="reference-links-aep-bibliography-sec-id7">
                <li><span><a id="ref-id-BIB1" href="#bBIB1">1.</a></span><span><div>Reference one.</div></span></li>
                <li><span><a id="ref-id-BIB2" href="#bBIB2">2.•</a></span><span><div>Reference two.</div></span></li>
                <li><span><a id="ref-id-BIB3" href="#bBIB3">3.••</a></span><span><div>Reference three.</div></span></li>
              </ol>
            </section>
          </section>
        </article>'''

        result = self.extract(source)

        block = result.sections[0].blocks[0]
        self.assertEqual(
            block.plain_text,
            'Chemical tail –NH(CH_{2})_{2}OH. Grouped result ^{[1, 2•]} and single result ^{[3••]}.',
        )
        self.assertEqual(
            block.markdown,
            'Chemical tail –NH(CH<sub>2</sub>)<sub>2</sub>OH. Grouped result <sup>[1, 2•]</sup> and single result <sup>[3••]</sup>.',
        )
        self.assertEqual(
            [reference.plain_text for reference in result.references],
            [
                '1. Reference one.',
                '2.• Reference two.',
                '3.•• Reference three.',
            ],
        )


if __name__ == '__main__':
    unittest.main()
