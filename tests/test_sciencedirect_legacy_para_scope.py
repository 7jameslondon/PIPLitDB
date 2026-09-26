from tests.test_sciencedirect_unheaded_bib_body import UnheadedBibBodyTests


class LegacyParaScopeTests(UnheadedBibBodyTests):
    def test_nested_table_word_card_does_not_duplicate_caption_and_notes(self):
        source = self.source().replace('TBL1', 'TABLE1')
        source = source.replace('</table></div>', '</table><dl><dt>a</dt><dd>Authored footnote.</dd></dl></div>')
        result = self.extract(source)
        main = next(s for s in result.sections if s.heading == 'Main text')
        self.assertEqual([b.plain_text for b in main.blocks], ['Opening word.^{1}', 'Second paragraph.', 'Closing paragraph.^{2}'])
        self.assertEqual(len(result.tables), 1)

    def test_legacy_doi_para_ids_after_abbreviations(self):
        source = self.source().replace('10.1016/example', '10.1006/example')
        source = source.replace('<div id="body">', '<div><div id="aep-keywords-id9"><h2>Abbreviations</h2><div><span>X</span><div><span>Example</span></div></div></div></div><div id="body">')
        source = source.replace('<div>Opening', '<div id="para0005">Opening')
        result = self.extract(source)
        main = next(s for s in result.sections if s.heading == 'Main text')
        self.assertEqual([b.plain_text for b in main.blocks], ['Opening word.^{1}', 'Second paragraph.', 'Closing paragraph.^{2}'])
        abbr = next(s for s in result.sections if s.heading == 'Abbreviations')
        self.assertNotIn('Opening', ' '.join(b.plain_text for b in abbr.blocks))
