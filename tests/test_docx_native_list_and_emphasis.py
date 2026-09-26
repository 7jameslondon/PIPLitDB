import unittest
import io
import tempfile
import zipfile
from pathlib import Path
from PIL import Image
from xml.etree import ElementTree as ET
from scripts.extraction.docx_supplement import (
    _decimal_list_labels, _inherit_paragraph_emphasis,
    _paragraph_has_heading_style, _paragraph_text, NS,
    _is_standalone_bold_paragraph,
    extract_docx_supplement, DOCX_MEDIA_TYPE,
)
from scripts.extraction.models import SourceFile

W = NS['w']

class DocxNativeListAndEmphasisTests(unittest.TestCase):
    def test_uncaptioned_image_keeps_adjacent_label_and_list_numbers_are_prose(self):
        document=f'''<w:document xmlns:w="{W}" xmlns:r="{NS['r']}" xmlns:a="{NS['a']}"><w:body>
        <w:p><w:r><w:t>Sample 2: retention time 9 min</w:t></w:r></w:p>
        <w:p><w:r><w:drawing><a:blip r:embed="img"/></w:drawing></w:r></w:p>
        <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>References</w:t></w:r></w:p>
        <w:p><w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>Writer A. Journal 2020.</w:t></w:r></w:p>
        </w:body></w:document>'''
        numbering=f'''<w:numbering xmlns:w="{W}"><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>'''
        pixels=io.BytesIO(); Image.new('RGB',(8,8),'red').save(pixels,format='PNG')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); path=root/'input.docx'
            with zipfile.ZipFile(path,'w') as package:
                package.writestr('word/document.xml',document)
                package.writestr('word/numbering.xml',numbering)
                package.writestr('word/_rels/document.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="img" Target="media/chart.png"/></Relationships>')
                package.writestr('word/media/chart.png',pixels.getvalue())
            source=SourceFile('supplement',path,'input.docx',path.stat().st_size,'x',DOCX_MEDIA_TYPE)
            blocks,figures,tables,assets,warnings=extract_docx_supplement(source,'supplement_001',docx_path=path,extraction_root=root)
            self.assertEqual(assets[0]['label'],'Sample 2: retention time 9 min (chart.png)')
            self.assertEqual(blocks[-1].plain_text,'1. Writer A. Journal 2020.')
            self.assertEqual(blocks[-1].kind,'text')
            self.assertFalse(figures or tables or warnings)

    def test_nested_color_and_underline_do_not_hide_bold_headings(self):
        self.assertTrue(_is_standalone_bold_paragraph('<span style="color:#000000"><strong>Methods</strong></span>','Methods'))
        self.assertTrue(_is_standalone_bold_paragraph('<u><strong>Supplement</strong></u>','Supplement'))
        self.assertFalse(_is_standalone_bold_paragraph('<strong>Methods.</strong><span> Body text.</span>','Methods. Body text.'))

    def test_decimal_bibliography_starts_and_distinct_lists(self):
        body = ET.fromstring(f'''<w:body xmlns:w="{W}">
        <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="4"/></w:numPr></w:pPr><w:r><w:t>First citation.</w:t></w:r></w:p>
        <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="4"/></w:numPr></w:pPr><w:r><w:t>Second citation.</w:t></w:r></w:p>
        <w:p><w:pPr><w:numPr><w:numId w:val="5"/></w:numPr></w:pPr><w:r><w:t>Restart.</w:t></w:r></w:p>
        </w:body>''')
        numbering=ET.fromstring(f'''<w:numbering xmlns:w="{W}"><w:abstractNum w:abstractNumId="2"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum><w:num w:numId="4"><w:abstractNumId w:val="2"/></w:num><w:num w:numId="5"><w:abstractNumId w:val="2"/><w:lvlOverride w:ilvl="0"><w:startOverride w:val="7"/></w:lvlOverride></w:num></w:numbering>''')
        self.assertEqual(_decimal_list_labels(body, numbering), {1:'1.',2:'2.',3:'7.'})
        numbering.find('.//w:numFmt',NS).set(f'{{{W}}}val','bullet')
        self.assertEqual(_decimal_list_labels(body,numbering),{})

    def test_heading_style_with_direct_bold_cancellation_keeps_prose(self):
        doc=ET.fromstring(f'''<w:document xmlns:w="{W}"><w:body><w:p><w:pPr><w:pStyle w:val="Heading4"/><w:rPr><w:b w:val="0"/></w:rPr></w:pPr><w:r><w:rPr><w:b w:val="0"/></w:rPr><w:t>Assays used compound </w:t></w:r><w:r><w:t>2</w:t></w:r><w:r><w:rPr><w:b w:val="0"/></w:rPr><w:t>.</w:t></w:r></w:p></w:body></w:document>''')
        styles=ET.fromstring(f'''<w:styles xmlns:w="{W}"><w:style w:styleId="Base"><w:rPr><w:b/></w:rPr></w:style><w:style w:styleId="Heading4"><w:basedOn w:val="Base"/></w:style></w:styles>''')
        _inherit_paragraph_emphasis(doc,styles)
        paragraph=doc.find('w:body/w:p',NS)
        self.assertFalse(_paragraph_has_heading_style(paragraph))
        rich,plain=_paragraph_text(paragraph)
        self.assertEqual(plain,'Assays used compound 2.')
        self.assertEqual(rich,'Assays used compound <strong>2</strong>.')

if __name__ == '__main__': unittest.main()
