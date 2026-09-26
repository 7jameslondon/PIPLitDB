from tests.test_sciencedirect_unheaded_bib_body import UnheadedBibBodyTests


class SpanEquationTests(UnheadedBibBodyTests):
    def source_with_equation(self):
        return self.source().replace('<div>Second paragraph.</div>', '''
        <section id="sec2"><h2>Methods</h2><div id="p0100">Before:
        <span><span id="fd1"><span>Eqn. 1</span><span>x<sub>t</sub> = e<sup>−kt</sup></span></span></span>
        After.</div></section>''')

    def test_numbered_plain_span_display_splits_prose(self):
        result = self.extract(self.source_with_equation())
        methods = next(s for s in result.sections if s.heading == 'Methods')
        self.assertEqual([b.kind for b in methods.blocks], ['paragraph', 'equation', 'paragraph'])
        self.assertEqual([b.plain_text for b in methods.blocks], ['Before:', 'Eqn. 1 x_{t} = e^{−kt}', 'After.'])

    def test_other_publisher_is_not_reinterpreted(self):
        result = self.extract(self.source_with_equation().replace('10.1016/example', '10.9999/example'))
        self.assertFalse(any(b.kind == 'equation' for s in result.sections for b in s.blocks))

    def test_numeric_label_plain_span_display_splits_prose(self):
        source = self.source_with_equation().replace('Eqn. 1', '1')
        result = self.extract(source)
        methods = next(s for s in result.sections if s.heading == 'Methods')
        self.assertEqual([b.kind for b in methods.blocks], ['paragraph', 'equation', 'paragraph'])
        self.assertEqual([b.plain_text for b in methods.blocks], ['Before:', '1 x_{t} = e^{−kt}', 'After.'])

    def test_non_numeric_span_label_remains_prose(self):
        result = self.extract(self.source_with_equation().replace('Eqn. 1', 'Note'))
        self.assertFalse(any(b.kind == 'equation' for s in result.sections for b in s.blocks))
