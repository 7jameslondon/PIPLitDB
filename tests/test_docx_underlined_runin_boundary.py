"""A run-in methods heading ends a preceding native figure legend."""
import io
import tempfile
import unittest
import zipfile
import struct
import subprocess
from pathlib import Path
from unittest import mock
from PIL import Image
from scripts.extraction.docx_supplement import extract_docx_supplement, DOCX_MEDIA_TYPE, _is_caption_only_document_boundary, _image_before_figure_label_map, _single_bitmap_emf, _png_derivative, convert_legacy_doc_to_docx, _word_docx_script
import xml.etree.ElementTree as ET
from scripts.extraction.models import SourceFile


class UnderlinedRuninBoundaryTests(unittest.TestCase):
    def test_explicitly_counted_gene_grid_has_no_invented_headers(self):
        from scripts.extraction.docx_supplement import _native_table
        ns='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
        def parse(title, explicit=False):
            rows='<w:tr><w:tc><w:p><w:r><w:t>'+title+'</w:t></w:r></w:p></w:tc></w:tr>'
            for index, values in enumerate((('RAD50','POLK'),('POLE2','SHFM1'))):
                rows+='<w:tr>'+('<w:trPr><w:tblHeader/></w:trPr>' if explicit and index==0 else '')
                rows+=''.join('<w:tc><w:p><w:r><w:t>'+v+'</w:t></w:r></w:p></w:tc>' for v in values)+'</w:tr>'
            element=ET.fromstring('<w:tbl xmlns:w="'+ns+'">'+rows+'</w:tbl>')
            source=SourceFile(role='supplement',path=Path('source.docx'),relative_path='source.docx',size=1,sha256='x',detected_format=DOCX_MEDIA_TYPE)
            return _native_table(element,source,'supplement_001',1,None,1)
        table=parse('Table S3: All 4 Genes in an siRNA Library')
        self.assertEqual([[c.text for c in r] for r in table.parts[0].rows],[['RAD50','POLK'],['POLE2','SHFM1']])
        self.assertFalse(any(c.header for r in table.parts[0].rows for c in r))
        self.assertTrue(all(c.header for c in parse('Table S3: All 5 Genes in an siRNA Library').parts[0].rows[0]))
        self.assertTrue(all(c.header for c in parse('Table S3: All 4 Genes in an siRNA Library',True).parts[0].rows[0]))
        self.assertTrue(all(c.header for c in parse('Table S3: Gene comparison').parts[0].rows[0]))

    def test_symbol_delta_is_font_gated(self):
        from scripts.extraction.docx_supplement import _decode_run_text, _paragraph_text
        self.assertEqual(_decode_run_text('\uf044D','Symbol'),'ΔΔ')
        self.assertEqual(_decode_run_text('\uf044D','Arial'),'\uf044D')
        p=ET.fromstring('<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:r><w:sym w:font="Symbol" w:char="F044"/><w:sym w:font="Symbol" w:char="F044"/><w:t>Ct</w:t></w:r></w:p>')
        self.assertEqual(_paragraph_text(p),('ΔΔCt','ΔΔCt'))

    def test_statistical_runin_heading_ends_figure(self):
        for lead in ('Graphical Activity Display (Scatter Plot)', 'Clustering', 'Experimental Quality Assessment'):
            self.assertTrue(_is_caption_only_document_boundary(f'<strong>{lead}</strong><strong>.</strong> Method text.',f'{lead}. Method text.'))
        self.assertFalse(_is_caption_only_document_boundary('<strong>Panel A.</strong> Signal.', 'Panel A. Signal.'))
        self.assertFalse(_is_caption_only_document_boundary('Clustering. Method text.', 'Clustering. Method text.'))

    def test_symbol_capital_iota_is_decoded_only_in_symbol_font(self):
        from scripts.extraction.docx_supplement import _decode_run_text
        self.assertEqual(_decode_run_text('\uf049','Symbol'),'Ι')
        self.assertEqual(_decode_run_text('\uf049','Arial'),'\uf049')

    def test_runin_heading_keeps_prose_and_later_drawing_out_of_caption(self):
        payload=io.BytesIO()
        Image.new('RGB',(8,8),'blue').save(payload,format='PNG')
        xml='''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
<w:p><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p>
<w:p><w:r><w:t>Fig. S1. The first drawing.</w:t></w:r></w:p>
<w:p><w:r><w:rPr><w:u w:val="single"/></w:rPr><w:t>Synthesis of compounds.</w:t></w:r><w:r><w:t xml:space="preserve"> Samples were prepared.</w:t></w:r></w:p>
<w:p><w:r><w:drawing><a:blip r:embed="rId2"/></w:drawing></w:r></w:p>
<w:p><w:r><w:t>The analytical result was recorded.</w:t></w:r></w:p>
</w:body></w:document>'''
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); path=root/'source.docx'
            with zipfile.ZipFile(path,'w') as z:
                z.writestr('word/document.xml',xml)
                z.writestr('word/_rels/document.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="media/one.png"/><Relationship Id="rId2" Target="media/two.png"/></Relationships>')
                z.writestr('word/media/one.png',payload.getvalue()); z.writestr('word/media/two.png',payload.getvalue())
            source=SourceFile(role='supplement',path=path,relative_path='source.docx',size=path.stat().st_size,sha256='x',detected_format=DOCX_MEDIA_TYPE)
            blocks,figures,tables,assets,warnings=extract_docx_supplement(source,'supplement_001',docx_path=path,extraction_root=root)
            self.assertEqual(figures[0].caption_plain,'Fig. S1. The first drawing.')
            self.assertEqual([b.plain_text for b in blocks],['Synthesis of compounds. Samples were prepared.','The analytical result was recorded.'])
            self.assertTrue(any(a['label'].endswith('(two.png)') for a in assets))
            self.assertFalse(warnings)

    def test_panel_underlining_does_not_end_caption(self):
        self.assertFalse(_is_caption_only_document_boundary('<u>Panel A.</u> The signal increased.','Panel A. The signal increased.'))

    def test_split_underlined_runin_heading_is_a_boundary(self):
        self.assertTrue(_is_caption_only_document_boundary('<u>Synthesis and </u><u>characterization.</u> The samples were measured.','Synthesis and characterization. The samples were measured.'))

    def test_address_before_drawing_is_not_part_of_the_drawing(self):
        body=ET.fromstring('''<w:body xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:p><w:r><w:t>Example City, CA 90210</w:t></w:r></w:p><w:p/><w:p><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p><w:p><w:r><w:t>Figure S1. A diagram.</w:t></w:r></w:p></w:body>''')
        self.assertEqual(_image_before_figure_label_map(list(body)),{4:([3],['rId1'])})

    def test_zero_frame_single_bitmap_emf_recovers_exact_pixels(self):
        bmp=io.BytesIO(); original=Image.new('RGB',(3,2),(12,75,139)); original.save(bmp,format='BMP'); raw=bmp.getvalue()
        offset=struct.unpack_from('<I',raw,10)[0]; info=raw[14:offset]; pixels=raw[offset:]
        header=bytearray(88); struct.pack_into('<II',header,0,1,88); header[40:44]=b' EMF'
        record=bytearray(80); struct.pack_into('<II',record,0,81,80+len(info)+len(pixels)); struct.pack_into('<14i',record,24,0,0,0,0,3,2,80,len(info),80+len(info),len(pixels),0,0xCC0020,3,2)
        emf=bytes(header)+bytes(record)+info+pixels+struct.pack('<IIIII',14,20,0,0,20)
        recovered=_single_bitmap_emf(emf)
        self.assertIsNotNone(recovered)
        self.assertEqual(Image.open(io.BytesIO(recovered)).tobytes(),original.tobytes())
        png,width,height=_png_derivative(emf)
        self.assertEqual((width,height),(3,2)); self.assertEqual(Image.open(io.BytesIO(png)).tobytes(),original.tobytes())
        # A second drawing operation or a truncated wrapper is not safe recovery.
        self.assertIsNone(_single_bitmap_emf(emf+struct.pack('<II',54,8)))
        self.assertIsNone(_single_bitmap_emf(emf[:-1]))

    def test_native_word_conversion_is_preferred_when_available(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); source=root/'input.doc'; source.write_bytes(b'original'); out=root/'converted';out.mkdir();profile=root/'profile';profile.mkdir()
            def run(command,**kwargs):
                self.assertIn('-NonInteractive',command)
                target=Path(command[command.index('-OutputPath')+1])
                with zipfile.ZipFile(target,'w') as z:
                    z.writestr('word/document.xml','<doc/>'); z.writestr('word/_rels/document.xml.rels','<Relationships/>')
                return subprocess.CompletedProcess(command,0,'16.0','')
            with mock.patch('scripts.extraction.docx_supplement._word_executable',return_value=Path('word.exe')),mock.patch('scripts.extraction.docx_supplement._powershell_executable',return_value=Path('powershell.exe')),mock.patch('scripts.extraction.docx_supplement._libreoffice_executable') as libre,mock.patch('scripts.extraction.docx_supplement.subprocess.run',side_effect=run):
                path,name,version=convert_legacy_doc_to_docx(source,root,out,profile)
            libre.assert_not_called(); self.assertEqual(name,'Microsoft Word legacy Word converter');self.assertEqual(version,'16.0');self.assertEqual(source.read_bytes(),b'original')
            self.assertIn('Close([ref]$discardChanges)',_word_docx_script())

    def test_native_word_conversion_failure_falls_back_to_libreoffice(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); source=root/'input.doc'; source.write_bytes(b'original'); out=root/'converted';out.mkdir();profile=root/'profile';profile.mkdir()
            calls=[]
            def run(command,**kwargs):
                calls.append(command)
                if '-NonInteractive' in command:
                    return subprocess.CompletedProcess(command,1,'','Word COM unavailable')
                if '--convert-to' in command:
                    target=out/'input.docx'
                    with zipfile.ZipFile(target,'w') as z:
                        z.writestr('word/document.xml','<doc/>'); z.writestr('word/_rels/document.xml.rels','<Relationships/>')
                    return subprocess.CompletedProcess(command,0,'converted','')
                return subprocess.CompletedProcess(command,0,'LibreOffice 25.2','')
            with mock.patch('scripts.extraction.docx_supplement._word_executable',return_value=Path('word.exe')),mock.patch('scripts.extraction.docx_supplement._powershell_executable',return_value=Path('powershell.exe')),mock.patch('scripts.extraction.docx_supplement._libreoffice_executable',return_value=Path('soffice.exe')),mock.patch('scripts.extraction.docx_supplement.subprocess.run',side_effect=run):
                path,name,version=convert_legacy_doc_to_docx(source,root,out,profile)
            self.assertEqual(path,out/'input.docx')
            self.assertEqual(name,'LibreOffice legacy Word converter (Microsoft Word fallback)')
            self.assertEqual(version,'LibreOffice 25.2')
            self.assertTrue(any('--convert-to' in command for command in calls))
            self.assertEqual(source.read_bytes(),b'original')
