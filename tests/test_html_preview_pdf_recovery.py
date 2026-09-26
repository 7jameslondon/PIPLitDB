from pathlib import Path
import unittest
import hashlib
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.extraction.models import ArticleExtraction, ContentBlock, Section, SourceFile
from scripts.extraction.metadata import RecordMetadata
from scripts.extraction.pipeline import ExtractionError, _recover_missing_html_body, _apply_main_table_overrides, _load_override
from scripts.extraction.validation import _validate_pdf_block_provenance


class HtmlPreviewPdfRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.html = SourceFile('main_html', Path('main.html'), 'private/html/main.html', 1, 'a'*64, 'text/html')
        self.pdf = SourceFile('main_pdf', Path('main.pdf'), 'private/pdf/main.pdf', 1, 'b'*64, 'application/pdf', 2)
        self.metadata = RecordMetadata(record_id='00001', title='Example', authors=('Author',), journal='Journal', publication_year=2000, doi='10.1/example', document_type='research_article')
        self.abstract = ContentBlock('main-paragraph-0001', 'paragraph', 'HTML abstract', 'HTML abstract', self.html.relative_path, '#Abs1')
        self.article = ArticleExtraction('Example', {'volume':'1'}, [Section('section-abstract','Abstract',blocks=[self.abstract]), Section('section-preview','Article PDF',blocks=[ContentBlock('preview','paragraph','Download to read the full article text','Download to read the full article text',self.html.relative_path,'#preview')])], [], [], [ContentBlock('reference-1','reference','HTML reference','HTML reference',self.html.relative_path,'#ref-1')], [], [], [])
        self.recovered = ArticleExtraction('Example', {}, [Section('section-abstract','Abstract',blocks=[]), Section('section-body','Body',blocks=[ContentBlock('main-paragraph-0001','paragraph','PDF body','PDF body',self.pdf.relative_path,'PDF page 1')])], [], [], [], [], [], [], text_extraction={'source_role':'main_pdf','source_path':self.pdf.relative_path,'ocr_performed':False})
        self.config = {'source_sha256':self.pdf.sha256,'recover_missing_html_body':{'html_sha256':self.html.sha256,'reason':'Missing body','evidence':'Exact preview placeholder'}}

    def recover(self):
        return _recover_missing_html_body(self.article,self.html,self.pdf,self.metadata,self.config,[])

    def test_preserves_html_abstract_references_and_pdf_body_without_id_collision(self):
        with patch('scripts.extraction.pipeline.extract_pdf_article',return_value=self.recovered):
            result = self.recover()
        self.assertEqual([s.heading for s in result.sections],['Abstract','Body'])
        self.assertEqual(result.sections[0].blocks[0].plain_text,'HTML abstract')
        self.assertEqual(result.sections[1].blocks[0].plain_text,'PDF body')
        self.assertNotEqual(result.sections[0].blocks[0].block_id,result.sections[1].blocks[0].block_id)
        self.assertEqual(result.references[0].plain_text,'HTML reference')
        self.assertEqual(result.bibliographic['volume'],'1')
        self.assertEqual(result.text_extraction['supporting_source'],self.html.relative_path)

    def test_unchanged_default_does_not_extract_pdf(self):
        self.config = {}
        with patch('scripts.extraction.pipeline.extract_pdf_article') as extract:
            self.assertIs(self.recover(),self.article)
        extract.assert_not_called()

    def test_changed_sources_and_populated_body_fail_closed(self):
        for mutate in ('html_hash','pdf_hash','body','placeholder'):
            with self.subTest(mutate=mutate):
                self.setUp()
                if mutate=='html_hash': self.config['recover_missing_html_body']['html_sha256']='c'*64
                elif mutate=='pdf_hash': self.config['source_sha256']='c'*64
                elif mutate=='body': self.article.sections.append(Section('body','Introduction'))
                else: self.article.sections[1].blocks[0].plain_text='Actual body text'
                with self.assertRaises(ExtractionError): self.recover()

    def test_title_identity_still_required(self):
        self.article.title='Another publication'
        with self.assertRaises(ExtractionError): self.recover()

    def test_ocr_receives_only_reviewed_configuration_and_crops(self):
        ocr_config={'page_regions':[]}
        with patch('scripts.extraction.pipeline._run_pdf_ocr',return_value=('document',{'source_role':'main_pdf'})) as ocr, patch('scripts.extraction.pipeline.extract_pdf_article',return_value=self.recovered) as extract:
            _recover_missing_html_body(self.article,self.html,self.pdf,self.metadata,self.config,[],ocr_config)
        ocr.assert_called_once_with(self.pdf.path,self.pdf.relative_path,ocr_config,[])
        self.assertEqual(extract.call_args.kwargs['text_document'],'document')

    def test_pdf_table_override_preserves_authored_roman_label(self):
        spec={'number':1,'label':'Table I','source_path':self.pdf.relative_path,
              'source_sha256':self.pdf.sha256,'source_locator':'PDF page 1 table',
              'source_kind':'pdf','title_plain':'Results','reason':'Omitted HTML table',
              'evidence':'Reviewed PDF table','parts':[{'rows':[[{'text':'Value','header':True}],[{'text':'3'}]]}]}
        _apply_main_table_overrides(self.article,[spec],[self.pdf])
        self.assertEqual(self.article.tables[0].label,'Table I')

    def test_mixed_heading_provenance_accepts_only_declared_html_source(self):
        geometry=[{'page':1,'bbox':[1,2,3,4]}]
        locator='PDF page 1, box [1.00, 2.00, 3.00, 4.00]'
        pdf_row={'source_path':self.pdf.relative_path,'source_locator':locator,'source_geometry':geometry}
        html_heading={'status':'included','content_kind':'section_heading','source_path':self.html.relative_path,'source_locator':'#Abs1','coverage_id':'heading-abstract'}
        loaded={'manifest.json':{'text_extraction':{'source_role':'main_pdf','source_path':self.pdf.relative_path,'supporting_source':self.html.relative_path}},
                'sources.json':{'sources':[self.pdf.as_dict(),self.html.as_dict()]},
                'blocks.jsonl':[dict(pdf_row,block_id='body')],
                'coverage.jsonl':[dict(pdf_row,output_id='body',status='included'),dict(pdf_row,status='included',content_kind='section_heading'),html_heading]}
        self.assertEqual(_validate_pdf_block_provenance(loaded),[])
        html_heading['source_path']='unarchived.html'
        self.assertIn('pdf_section_heading_provenance_invalid',[f.code for f in _validate_pdf_block_provenance(loaded)])
        html_heading['source_path']=self.html.relative_path
        loaded['coverage.jsonl'][1]['source_geometry']=[]
        self.assertIn('pdf_section_heading_provenance_invalid',[f.code for f in _validate_pdf_block_provenance(loaded)])

    def test_override_snapshot_retains_exact_crlf_bytes(self):
        with TemporaryDirectory() as temp:
            path=Path(temp)/'override.yaml'
            raw=b"schema_version: '1.0'\r\nrecord_id: '00001'\r\n"
            path.write_bytes(raw)
            _,snapshot,digest=_load_override(path,'00001')
        self.assertEqual(snapshot.encode('utf-8'),raw)
        self.assertEqual(digest,hashlib.sha256(raw).hexdigest())

    def test_sciencedirect_publisher_summary_only_layout_recovers_pdf_body(self):
        markup = '''<article><h1 id="screen-reader-main-title">Example</h1>
        <div id="abstracts"><div id="aep-abstract-id3"><h2>Publisher Summary</h2>
        <div id="aep-abstract-sec-id4"><div id="fsabs022">Summary text.</div></div>
        </div></div><div><section id="bibliography.0010"><h2>References</h2></section></div></article>'''
        with TemporaryDirectory() as temp:
            html_path = Path(temp) / 'main.html'
            html_path.write_text(markup, encoding='utf-8')
            self.html = SourceFile(
                'main_html',
                html_path,
                'private/html/main.html',
                len(markup),
                hashlib.sha256(markup.encode()).hexdigest(),
                'text/html',
            )
            summary = ContentBlock(
                'main-paragraph-0001',
                'paragraph',
                'Summary text.',
                'Summary text.',
                self.html.relative_path,
                '#fsabs022',
            )
            self.article = ArticleExtraction(
                'Example',
                {},
                [Section(
                    'section-publisher-summary',
                    'Publisher Summary',
                    blocks=[summary],
                )],
                [], [], [], [], [], [],
            )
            self.config = {
                'source_sha256': self.pdf.sha256,
                'recover_missing_html_body': {
                    'html_sha256': self.html.sha256,
                    'html_layout': 'sciencedirect_publisher_summary_only',
                    'reason': 'Publisher HTML contains only its summary.',
                    'evidence': 'Exact authenticated AEP topology.',
                },
            }
            with patch(
                'scripts.extraction.pipeline.extract_pdf_article',
                return_value=self.recovered,
            ):
                result = self.recover()
        self.assertEqual(
            [section.heading for section in result.sections],
            ['Publisher Summary', 'Body'],
        )
        self.assertEqual(
            result.sections[0].blocks[0].plain_text,
            'Summary text.',
        )


if __name__ == '__main__':
    unittest.main()
