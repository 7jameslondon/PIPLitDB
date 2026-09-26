"""Synthetic regressions for stripped Springer roles and native Word visuals."""
import base64
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from lxml import html
from PIL import Image
from scripts.extraction import html_extractor as he
from scripts.extraction import docx_supplement as ds
from scripts.extraction.models import SourceFile


class SpringerWordFidelityTests(unittest.TestCase):
    def test_equations_and_remote_table_cards_have_correct_roles(self):
        document = html.fromstring('<article><h2>Methods</h2><p>Before.</p>'
            '<div id="Equ1"><div><span>$$ x = y^2 $$</span></div><div>(1)</div></div>'
            '<p>After.</p><div id="table-1"><figure><figcaption><b id="Tab1">Table 1 Values</b>'
            '</figcaption><div><a aria-label="Full size table 1" href="/tables/1">Full size table</a>'
            '</div></figure></div></article>')
        original = html.tostring(document)
        with mock.patch.object(he, '_springer_header_details', return_value=None):
            he._normalize_springer_equations_and_table_cards(document)
        self.assertEqual(html.tostring(document), original)
        with mock.patch.object(he, '_springer_header_details', return_value={'title':'Article'}):
            he._normalize_springer_equations_and_table_cards(document)
        sections, _ = he._body_sections(document, 'source.html')
        self.assertEqual([b.kind for s in sections for b in s.blocks],
                         ['paragraph', 'equation', 'paragraph'])
        self.assertEqual(len(document.xpath('.//table/caption')), 1)
        self.assertEqual(document.xpath('.//figure'), [])

    def test_malformed_encoded_doi_keeps_authored_references_without_toolbar(self):
        target = 'https://doi.org/10.1234%2Fexample.%20Extra%20publisher%20text'
        document = html.fromstring('<section><ol><li><p id="ref-CR1">A (2001) Journal 1:1–2</p>'
            f'<p><a href="{target}">Article</a><a href="http://scholar.google.com/">Google Scholar</a>'
            '</p></li></ol></section>')
        blocks = he._springer_split_reference_blocks(document, 'source.html')
        self.assertIsNotNone(blocks)
        self.assertEqual(len(blocks), 1)
        self.assertIn(target, blocks[0].plain_text)
        self.assertNotIn('Google Scholar', blocks[0].plain_text)

    def test_plural_acknowledgements_resume_legacy_back_matter(self):
        document = html.fromstring('<article><section aria-labelledby="Bib1"><div id="Bib1-section">'
            '<h2 id="Bib1">References</h2><div id="Bib1-content"><div><ol><li>'
            '<p id="ref-CR1">A (2001)</p></li></ol></div></div></div></section>'
            '<h2>Acknowledgements</h2><p>Funding.</p><h2>Author information</h2>'
            '<p>Affiliation.</p><h2>Electronic supplementary material</h2></article>')
        boundary = he._springer_post_reference_boundaries(document, 'source.html')
        self.assertIsNotNone(boundary)
        self.assertEqual(he.plain_text(boundary[0]), 'Acknowledgements')

    def test_floating_panel_and_prose_are_not_stolen_by_captions(self):
        self.assertIsNone(ds._FIGURE_LABEL.match('Figure S4 shows the fitted data.'))
        self.assertIsNotNone(ds._FIGURE_LABEL.match('Figure S4: Shows fitted data.'))
        xml = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
          xmlns:v="urn:schemas-microsoft-com:vml" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>
          <w:p><w:r><w:pict><v:shape style="position:absolute"><v:imagedata r:id="r1"/></v:shape></w:pict>
          <w:t>The experimental body prose is longer than eighty characters and ends with a concentration of</w:t></w:r></w:p>
          <w:p><w:r><w:pict><v:shape><v:imagedata r:id="r2"/></v:shape></w:pict></w:r></w:p>
          <w:p><w:pPr><w:rPr><w:sz w:val="20"/></w:rPr></w:pPr><w:r><w:t>Fig S3: Two panels.</w:t></w:r></w:p>
          <w:p><w:pPr><w:spacing w:line="360"/></w:pPr><w:r><w:lastRenderedPageBreak/><w:t>Probe under the same conditions.</w:t></w:r></w:p>
          <w:p><w:r><w:t>Figure S4 shows the fitted data.</w:t></w:r></w:p>
          <w:p><w:r><w:pict><v:shape><v:imagedata r:id="r3"/></v:shape></w:pict></w:r></w:p>
          <w:p><w:r><w:t>Fig S4: Residuals.</w:t></w:r></w:p>
          </w:body></w:document>'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root/'source.docx'
            with zipfile.ZipFile(source, 'w') as package:
                package.writestr('word/document.xml', xml)
                package.writestr('word/_rels/document.xml.rels',
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    + ''.join(f'<Relationship Id="r{i}" Target="media/image{i}.png"/>' for i in range(1,4))
                    + '</Relationships>')
                for i,color in enumerate(['red','blue','green'],1):
                    image = io.BytesIO()
                    Image.new('RGB',(40,20),color).save(image,format='PNG')
                    package.writestr(f'word/media/image{i}.png',image.getvalue())
            output=root/'output'; output.mkdir()
            info=SourceFile('supplement',source,'source.docx',source.stat().st_size,
                hashlib.sha256(source.read_bytes()).hexdigest(),ds.DOCX_MEDIA_TYPE)
            blocks,figures,tables,assets,warnings=ds.extract_docx_supplement(
                info,'supplement_001',docx_path=source,extraction_root=output)
            self.assertEqual([f.label for f in figures],['Figure S3','Figure S4'])
            self.assertEqual([f.caption_plain for f in figures],['Fig S3: Two panels.','Fig S4: Residuals.'])
            self.assertEqual(len(blocks),3)
            self.assertEqual(blocks[-1].plain_text,'Figure S4 shows the fitted data.')
            self.assertIn('component_02', ' '.join(a['asset_id'] for a in assets))
            self.assertEqual(warnings,[])

    def test_native_metafile_renderer_uses_private_in_memory_transport(self):
        png=io.BytesIO(); Image.new('RGB',(12,8),'white').save(png,format='PNG')
        response=mock.Mock(returncode=0,stdout=base64.b64encode(png.getvalue()).decode())
        with (mock.patch.object(ds,'_powershell_executable',return_value=Path('powershell.exe')),
              mock.patch.object(ds.subprocess,'run',return_value=response) as run):
            self.assertEqual(ds._windows_metafile_png(b'private vector',12,8),png.getvalue())
        payload=json.loads(run.call_args.kwargs['input'])
        self.assertEqual(base64.b64decode(payload['data']),b'private vector')
        self.assertNotIn('private vector',' '.join(run.call_args.args[0]))


if __name__ == '__main__':
    unittest.main()
