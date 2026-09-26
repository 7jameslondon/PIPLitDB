from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.extraction.html_extractor import extract_html
from scripts.extraction.record_json import _block
from scripts.extraction.validation import _validate_diagnostic_warnings
from scripts.extraction.rich_text import (
    block_markup_to_safe_html,
    plain_text_from_safe_html,
    rich_text_matches_plain,
)


HELPER = 'xmlns:helper="urn:XsltStringHelper"'
PIXEL = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
)


def controls(number: int) -> str:
    return (
        f'<div><a href="https://example.org/crossref/{number}">Crossref</a></div>'
        f'<div><a href="https://example.org/ads/{number}">Search ADS</a></div>'
        f'<div><span> <a href="https://example.org/openurl/{number}">'
        'OpenURL</a> </span></div>'
        f'<div><div><a href="https://example.org/scholar/{number}">'
        'Google Scholar</a></div></div>'
    )


def reference(number: int, body: str, source_id: str) -> str:
    return (
        f'<div {HELPER}><div><div><span>{number}.</span>'
        f'<div id="{source_id}">{body}</div></div></div></div>'
    )


def snapshot(*, split: bool = True) -> str:
    entries = [
        reference(1, 'Alpha A. First study. <em>Journal</em> 2001.', 'elcit1'),
        reference(
            2,
            'Beta B. Second study. <div><a href="https://doi.org/10.1000/two">'
            'https://doi.org/10.1000/two</a></div>.' + controls(2),
            'elcit2',
        ),
        reference(3, 'Gamma G. <em>K</em><sub>d</sub> and Mg<sup>2+</sup>.', 'elcit3'),
        reference(4, 'Delta D. In <em>Example Methods</em>, pp 4–5.', 'micit1'),
        # Printed numbers, rather than source IDs, identify and order entries.
        reference(5, 'Epsilon E. Study of Crossref indexing.', 'elcit3'),
    ]
    if split:
        prefix = ''.join(entries[:2]) + f'<div {HELPER}><divv></divv></div>.' + controls(2)
        continuation = ''.join(entries[2:])
    else:
        prefix, continuation = ''.join(entries), ''
    return f'''<!doctype html><html><body>
<h1 id="aria1">Synthetic ACS snapshot</h1>
<a href="https://doi.org/10.1021/example">Article DOI</a>
<div id="ContentTab"><div><div><div>
<h2 id="1">Abstract</h2><div id="1-content"><p>Complete abstract.</p></div>
<figure><img src="data:image/png;base64,{PIXEL}" alt="Figure 1.">
<figcaption>Figure 1. Complete caption.</figcaption></figure>
<h2 id="2">References</h2><div><div id="2-content"><div>{prefix}</div></div></div>
</div>{continuation}</div></div></div>
</body></html>'''


class AcsSplitReferenceTests(unittest.TestCase):
    def extract(self, source: str):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'article.html'
            path.write_text(source, encoding='utf-8')
            before = path.read_bytes()
            result = extract_html(path, 'article.html')
            self.assertEqual(path.read_bytes(), before)
            return result

    def test_split_bibliography_recovers_every_printed_reference(self) -> None:
        result = self.extract(snapshot())

        self.assertEqual(
            [item.plain_text for item in result.references],
            [
                '1. Alpha A. First study. Journal 2001.',
                '2. Beta B. Second study. https://doi.org/10.1000/two.',
                '3. Gamma G. K_{d} and Mg^{2+}.',
                '4. Delta D. In Example Methods, pp 4–5.',
                '5. Epsilon E. Study of Crossref indexing.',
            ],
        )
        self.assertEqual(
            [item.block_id for item in result.references],
            [f'reference-{number:03d}' for number in range(1, 6)],
        )
        self.assertEqual(len({item.source_locator for item in result.references}), 5)
        for item in result.references:
            self.assertEqual(item.source_path, 'article.html')
            self.assertNotIn('Google Scholar', item.plain_text)
            self.assertNotIn('Search ADS', item.plain_text)
            safe_html = block_markup_to_safe_html(item.markdown, kind='reference')
            self.assertTrue(rich_text_matches_plain(item.plain_text, safe_html))
            self.assertEqual(plain_text_from_safe_html(safe_html), item.plain_text)
        self.assertIn('<em>Example Methods</em>', result.references[3].markdown)
        self.assertEqual(result.sections[0].blocks[0].plain_text, 'Complete abstract.')
        self.assertEqual(len(result.figures), 1)

    def test_intact_bibliography_retains_existing_behavior(self) -> None:
        intact = self.extract(snapshot(split=False))
        self.assertEqual(len(intact.references), 5)
        self.assertIn('Study of Crossref indexing.', intact.references[-1].plain_text)

    def test_mapped_equal_contribution_is_not_an_affiliation(self):
        authors = ''.join(
            f'<div><a rel="nofollow" aria-haspopup="true">{name}</a>'
            '<span><a reveal-id="exampleAF1" aria-label="View author note">1</a>'
            '<a reveal-id="exampleAF2" aria-label="View author note">⊥</a>'
            '<a reveal-id="exampleAF3" aria-label="View author note">#</a></span></div>'
            for name in ('Alice Example', 'Bob Example')
        )
        source = ('<html><body><article><h1>Mapped author notes</h1>' + authors +
                  '<div id="exampleAF1" content-id="exampleAF1"><p>Example University.</p></div>'
                  '<div id="exampleAF2" content-id="exampleAF2"><p>These authors contributed equally to this work.</p></div>'
                  '<div id="exampleAF3" content-id="exampleAF3"><p>Current address: New Institute.</p></div>'
                  '<div>Publisher: American Chemical Society</div>'
                  '<section><h2>Results</h2><p>Scientific prose.</p></section></article></body></html>')
        result = self.extract(source)
        values = [block.plain_text for block in result.front_matter]
        for name in ('Alice Example', 'Bob Example'):
            self.assertIn(f'Affiliation ({name}): Example University.', values)
            self.assertIn(f'Present address ({name}): New Institute.', values)
            self.assertIn(f'Contribution ({name}): These authors contributed equally to this work.', values)
        self.assertFalse(any(value.startswith('Affiliation') and 'contributed' in value for value in values))

    def test_mapped_equal_contribution_study_variant_keeps_popover_affiliation(self):
        source = '''<html><body><article><h1>Mapped author-note study variant</h1>
        <div><a rel="nofollow" aria-haspopup="true">Alice Example</a>
          <span><a reveal-id="exampleAF2" aria-label="View author note">†</a></span>
          <div><div><div>Author details</div><div>
            <div>Department of Chemistry, Example University.</div>
          </div></div></div>
        </div>
        <div id="exampleAF2" content-id="exampleAF2"><p>These authors contributed equally to this study.</p></div>
        <div>Publisher: American Chemical Society</div>
        <section><h2>Results</h2><p>Scientific prose.</p></section>
        </article></body></html>'''
        result = self.extract(source)
        values = [block.plain_text for block in result.front_matter]

        self.assertIn(
            'Affiliation (Alice Example): Department of Chemistry, Example University.',
            values,
        )
        self.assertIn(
            'Contribution (Alice Example): These authors contributed equally to this study.',
            values,
        )
        self.assertFalse(
            any(value.startswith('Affiliation') and 'contributed equally' in value for value in values)
        )

    def test_title_dagger_funding_is_front_matter_without_consuming_introduction(self):
        source = snapshot(split=False).replace(
            'Synthetic ACS snapshot</h1>', 'Synthetic ACS snapshot<a href="#exampleAF2">†</a></h1>'
        ).replace(
            '<figure><img',
            '<div><span>†</span></div><div>'
            '<div id="3-content"><p>A.B. was supported by Fellowship X; C.D. and E.F. were supported by Grant Y.</p></div>'
            '<div id="4-content"><p>Complete introduction.</p></div></div><figure><img', 1,
        )
        result = self.extract(source)
        funding = [block for block in result.front_matter if block.plain_text.startswith('Funding (†):')]
        self.assertEqual(len(funding), 1)
        self.assertIn('C.D. and E.F. were supported by Grant Y.', funding[0].plain_text)
        self.assertIn('xpath-title_note=', funding[0].source_locator)
        body = [block.plain_text for section in result.sections for block in section.blocks]
        self.assertIn('Complete introduction.', body)
        self.assertFalse(any('Fellowship X' in text for text in body))
        for marker in ('<a href="#exampleAF2">†</a>', '<div><span>†</span></div>'):
            near_miss = self.extract(source.replace(marker, ''))
            self.assertFalse(any(block.plain_text.startswith('Funding (†):') for block in near_miss.front_matter))
            self.assertTrue(any('Fellowship X' in block.plain_text for section in near_miss.sections for block in section.blocks))

    def test_untranscribed_raster_formulas_keep_pixels_position_and_failing_warning(self):
        gif = 'R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=='
        display = (
            '<h2 id="3">Methods</h2><div id="3-content"><div>Before the display: '
            '<div content-id="examplee00001"><div id="jumplink-examplee00001">'
            '<div id="example_0001.gif"><a><img '
            f'src="data:image/gif;base64,{gif}" path-from-xml="example_0001.gif" '
            'alt="Formula. Refer to the image caption for details."></a></div></div></div>'
            ' After the display, complete definitions.</div></div>'
            f'<figure><img src="data:image/png;base64,{PIXEL}" alt="Figure 2.">'
            '<figcaption><p>Figure 2. Caption before 10<span><img '
            f'src="data:image/gif;base64,{gif}" path-from-xml="example_0002.gif" alt="">'
            '</span> and after the formula.</p></figcaption></figure>'
        )
        result = self.extract(snapshot(split=False).replace('<h2 id="2">References', display + '<h2 id="2">References'))
        methods = next(section for section in result.sections if section.heading == 'Methods')
        self.assertEqual([block.kind for block in methods.blocks], ['paragraph', 'equation', 'paragraph'])
        self.assertEqual(methods.blocks[0].plain_text, 'Before the display:')
        self.assertEqual(methods.blocks[-1].plain_text, 'After the display, complete definitions.')
        self.assertIn('[Unresolved formula image: acs_formula_001]', methods.blocks[1].plain_text)
        self.assertIn('10[Unresolved formula image: acs_formula_002] and after', result.figures[-1].caption_plain)
        formulas = [asset for asset in result.embedded_assets if asset.category == 'equation']
        self.assertEqual(len(formulas), 2)
        import base64
        self.assertTrue(all(asset.data == base64.b64decode(gif) for asset in formulas))
        self.assertTrue(all(not asset.ocr_performed and asset.source_locator.endswith('/img') for asset in formulas))
        warnings = [row for row in result.warnings if row['code'] == 'unresolved_raster_formula']
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all(finding.severity == 'scientific' for finding in _validate_diagnostic_warnings({'warnings.jsonl': warnings})))
        for block in methods.blocks:
            _block(block)

    def extract_nomad_reference(self, body: str):
        source = snapshot(split=False).replace(
            'Alpha A. First study. <em>Journal</em> 2001.', body
        )
        result = self.extract(source)
        self.assertEqual(len(result.references), 5)
        # Exercise the canonical serializer that rejects plain/rich mismatch.
        for item in result.references:
            _block(item)
        return result.references[0]

    def test_nomad_duplicate_doi_and_period_are_removed_without_losing_authored_tails(self):
        doi = 'https://doi.org/10.1000/example(01)00262-0'
        body = (
            f'Alpha A. First study. <div><a href="{doi}">\n {doi}</a></div>.'
            '<div><a href="https://example.org/crossref">Crossref</a></div>'
            f'<div><a clnomad-enhanced="true">\n {doi}</a>'
            '<div id="libkey-nomad-10-1000-example-01-00262-0-0"></div></div>.'
            '<div><a href="https://example.org/crossref">Crossref</a></div>'
            ' Authored note: <em>retain this</em>.'
        )
        item = self.extract_nomad_reference(body)
        self.assertEqual(
            item.plain_text,
            f'1. Alpha A. First study. {doi}. Authored note: retain this.',
        )
        self.assertEqual(item.markdown.count(doi), 1)
        self.assertIn('<em>retain this</em>', item.markdown)

    def test_nomad_sole_or_distinct_doi_preserves_boundary_whitespace(self):
        doi = 'https://doi.org/10.1000/sole'
        for prefix in (
            'Alpha A. First study.',
            'Alpha A. First study. <div><a href="https://doi.org/10.1000/other">'
            ' https://doi.org/10.1000/other</a></div>.',
        ):
            with self.subTest(prefix=prefix):
                item = self.extract_nomad_reference(
                    prefix + f'<div><a clnomad-enhanced="true">\n {doi}</a>'
                    '<div id="libkey-nomad-10-1000-sole-0"><div></div></div></div>.'
                )
                self.assertIn(f'. {doi}.', item.plain_text)
                self.assertIn(f'. {doi}.', item.markdown)
                self.assertEqual(item.plain_text.count(doi), 1)
                if '/other' in prefix:
                    self.assertIn('https://doi.org/10.1000/other.', item.plain_text)

    def test_nomad_duplicate_preserves_adjacent_authored_text_inside_wrapper(self):
        doi = 'https://doi.org/10.1000/example'
        item = self.extract_nomad_reference(
            f'Alpha A. <div><a href="{doi}"> {doi}</a></div>.'
            f'<div><a clnomad-enhanced="true"> {doi}</a> Additional <em>details</em>,'
            '<div id="libkey-nomad-10-1000-example-0"></div> with punctuation!</div>'
        )
        self.assertEqual(item.plain_text.count(doi), 1)
        self.assertIn('Additional details, with punctuation!', item.plain_text)
        self.assertIn('<em>details</em>', item.markdown)

    def test_nomad_marker_without_placeholder_does_not_remove_authored_doi(self):
        doi = 'https://doi.org/10.1000/example'
        item = self.extract_nomad_reference(
            f'Alpha A. <div><a href="{doi}">{doi}</a></div>. '
            f'<div><a clnomad-enhanced="true" href="{doi}">{doi}</a></div>.'
        )
        self.assertEqual(item.plain_text.count(doi), 2)

    def test_terminal_misc_abbreviations_move_once_to_front_matter_with_rich_text(self):
        definitions = (
            '<div>Abbreviations:&#x2009; NMR, nuclear magnetic resonance; '
            'Py, <em>N</em>-methylpyrrole; TATA<sub>ZF</sub>, example variant; '
            'ACN, acetonitrile.</div>'
        )
        source = snapshot(split=False).replace(
            reference(5, 'Epsilon E. Study of Crossref indexing.', 'elcit3'),
            reference(5, definitions, 'micit6'),
        ).replace(
            'Complete abstract.',
            'Complete abstract with citation (<a ref-data-modal-source-id="exampleb00006">6</a>).',
        )
        result = self.extract(source)
        self.assertEqual(len(result.references), 4)
        notes = [block for block in result.front_matter
                 if block.plain_text.startswith('Abbreviations:')]
        self.assertEqual(len(notes), 1)
        self.assertEqual(
            notes[0].plain_text,
            'Abbreviations: NMR, nuclear magnetic resonance; Py, N-methylpyrrole; '
            'TATA_{ZF}, example variant; ACN, acetonitrile.',
        )
        self.assertIn('<em>N</em>-methylpyrrole', notes[0].markdown)
        self.assertIn('TATA<sub>ZF</sub>', notes[0].markdown)
        self.assertIn('/div[5]/', notes[0].source_locator)
        for block in [*result.references, *notes]:
            _block(block)
        self.assertEqual(
            [block.plain_text for section in result.sections for block in section.blocks],
            ['Complete abstract with citation (6).'],
        )
        self.assertTrue(all('Abbreviations:' not in ref.plain_text for ref in result.references))

    def test_misc_abbreviation_reclassification_requires_exact_terminal_note(self):
        original = reference(5, 'Epsilon E. Study of Crossref indexing.', 'elcit3')
        note = reference(5, '<div>Abbreviations: NMR, example definition.</div>', 'micit6')
        source = snapshot(split=False).replace(original, note)
        for modified in (
            source.replace('id="micit6"', 'id="elcit6"'),
            source.replace('Abbreviations: NMR', 'A study of abbreviations: NMR'),
            source.replace(note, note + reference(6, 'A later authored citation.', 'elcit6')),
            source.replace('<span>5.</span>', '<span>5.</span>Authored text.'),
            source.replace('https://doi.org/10.1021/example', 'https://doi.org/10.1000/example'),
        ):
            with self.subTest(source=modified):
                result = self.extract(modified)
                self.assertFalse(any(block.plain_text.startswith('Abbreviations:')
                                     for block in result.front_matter))
                self.assertTrue(any('example definition.' in ref.plain_text
                                    for ref in result.references))

    def test_split_bibliography_requires_complete_publisher_signature(self) -> None:
        source = snapshot()
        for old, new in [
            ('https://doi.org/10.1021/example', 'https://doi.org/10.1000/example'),
            ('id="ContentTab"', 'id="other-content"'),
            ('id="aria1"', 'id="other-title"'),
            ('<h2 id="2">', '<h2 id="refs">'),
        ]:
            with self.subTest(old=old):
                self.assertEqual(self.extract(source.replace(old, new)).references, [])

    def test_split_bibliography_rejects_missing_repeated_or_reordered_labels(self) -> None:
        source = snapshot()
        for old, new in [
            ('<span>1.</span>', '<span>0.</span>'),
            ('<span>3.</span>', '<span>4.</span>'),
            ('<span>3.</span>', '<span>2.</span>'),
            ('<span>3.</span>', '<span>unknown.</span>'),
            ('<span>5.</span>', '<span>3.</span>'),
        ]:
            with self.subTest(old=old, new=new):
                self.assertEqual(self.extract(source.replace(old, new)).references, [])

    def test_split_bibliography_requires_the_observed_damage_and_duplicate_controls(self) -> None:
        source = snapshot()
        damage = f'<div {HELPER}><divv></divv></div>.'
        for old, new in [
            (damage, ''),
            ('<divv></divv>', '<divv>Authored note.</divv>'),
            (damage + controls(2), damage + controls(9)),
            (damage + controls(2), damage + controls(2) + '<p>Authored note.</p>'),
            (damage + controls(2), damage + controls(2).replace('Search ADS', 'Authored note')),
        ]:
            with self.subTest(new=new):
                self.assertEqual(self.extract(source.replace(old, new)).references, [])

    def test_split_bibliography_rejects_uncovered_continuation_content(self) -> None:
        source = snapshot()
        entry = reference(3, 'Gamma G. <em>K</em><sub>d</sub> and Mg<sup>2+</sup>.', 'elcit3')
        for replacement in [
            '<p>Authored note.</p>' + entry,
            '<h2>Further reading</h2>' + entry,
            entry + '<div>Authored trailing note.</div>',
            entry.replace('<span>3.</span>', '<span>3.</span>Authored inline note.'),
            entry.replace('id="elcit3"', 'id="unrelated"'),
            reference(3, '', 'elcit3'),
        ]:
            with self.subTest(replacement=replacement):
                self.assertEqual(self.extract(source.replace(entry, replacement)).references, [])

    def test_split_bibliography_does_not_collect_references_from_elsewhere(self) -> None:
        source = snapshot().replace(
            '</body>',
            '<section><h2>Related reading</h2>'
            + reference(6, 'Unrelated citation.', 'elcit6')
            + '</section></body>',
        )
        result = self.extract(source)
        self.assertEqual(len(result.references), 5)
        self.assertNotIn('Unrelated citation.', ' '.join(x.plain_text for x in result.references))


if __name__ == '__main__':
    unittest.main()
