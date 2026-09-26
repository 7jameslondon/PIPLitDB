from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch
from scripts.extraction.pdf_article_extractor import (
    _reviewed_image_only_regions,
    _reviewed_native_regions,
    PdfArticleExtractionError,
)
from scripts.extraction.pdf_text_extractor import PdfTextDocument, PdfTextPage, PdfTextLine
from scripts.extraction.pdf_article_extractor import extract_pdf_article
from scripts.extraction.metadata import RecordMetadata

class ReviewedNativeRegionTests(unittest.TestCase):
    def example(self):
        line = PdfTextLine(page=1, bbox=(20,20,80,30), plain_text='damaged OCR', markdown='damaged OCR', source_locator='p1', source_region_id='column')
        doc = PdfTextDocument('main.pdf', [PdfTextPage(1,100,100,'native_text',[line])], [], [], True)
        cfg = {'source_sha256':'a'*64, 'native_reading_regions':[{'region_id':'column','page':1,'box':[10,10,90,90]}],
            'reviewed_native_regions':[{'region_id':'column','reason':'Corrupt hidden OCR layer','evidence':'Visible page 1 paragraph',
            'lines':[{'box':[20,20,80,30], 'reviewed_value':'ΔH = −6.7', 'reviewed_markdown':'ΔH = −6.7'}]}]}
        return doc,cfg

    def test_replaces_only_named_region_and_retains_geometry(self):
        doc,cfg=self.example()
        other=replace(doc.pages[0].lines[0], source_region_id='other')
        doc.pages[0].lines.append(other)
        _reviewed_native_regions(doc,cfg)
        self.assertEqual([x.plain_text for x in doc.pages[0].lines],['ΔH = −6.7','damaged OCR'])
        self.assertEqual(doc.pages[0].lines[0].bbox,(20,20,80,30))
        self.assertEqual(doc.diagnostic_rows[0]['original_line_count'],1)

    def test_rejects_missing_pin_evidence_duplicate_and_outside_geometry(self):
        for case in ('pin','evidence','duplicate','outside','unknown','empty'):
            with self.subTest(case=case):
                doc,cfg=self.example(); spec=cfg['reviewed_native_regions'][0]
                if case=='pin': cfg.pop('source_sha256')
                if case=='evidence': spec['evidence']=''
                if case=='duplicate': cfg['reviewed_native_regions'].append(deepcopy(spec))
                if case=='outside': spec['lines'][0]['box']=[0,0,95,95]
                if case=='unknown': spec['region_id']='missing'
                if case=='empty': spec['lines']=[]
                with self.assertRaises(PdfArticleExtractionError): _reviewed_native_regions(doc,cfg)

    @patch('scripts.extraction.pdf_article_extractor.sha256_file',return_value='a'*64)
    def test_reviewed_replacement_preserves_ocr_page_provenance_without_inventing_confidence(self,_sha):
        for via_region in (False, True):
            with self.subTest(via_region=via_region):
                doc,cfg=self.example()
                cfg['abstract_region_ids']=[]
                if via_region:
                    doc.diagnostic_rows.append({'kind':'ocr_region','page':1,'region_id':'column'})
                else:
                    doc.pages[0].lines[0]=replace(doc.pages[0].lines[0],ocr_confidence=0.71)
                metadata=RecordMetadata('00001','Example title',('A. Author',),'Journal',2000,'','research_article')
                article=extract_pdf_article(Path('unused.pdf'),'main.pdf',metadata,cfg,text_document=doc,
                    text_extraction={'source_role':'main_pdf','source_path':'main.pdf','ocr_performed':True})
                summary=next(row for row in article.page_diagnostics if row.get('kind')=='page_summary')
                self.assertTrue(summary['ocr_performed'])
                self.assertTrue(all(line.ocr_confidence is None for line in doc.pages[0].lines))

    @patch('scripts.extraction.pdf_article_extractor.sha256_file',return_value='a'*64)
    def test_reviewed_native_only_replacement_does_not_claim_ocr(self,_sha):
        doc,cfg=self.example()
        cfg['abstract_region_ids']=[]
        metadata=RecordMetadata('00001','Example title',('A. Author',),'Journal',2000,'','research_article')
        article=extract_pdf_article(Path('unused.pdf'),'main.pdf',metadata,cfg,text_document=doc)
        summary=next(row for row in article.page_diagnostics if row.get('kind')=='page_summary')
        self.assertFalse(summary['ocr_performed'])


class ReviewedImageOnlyRegionTests(unittest.TestCase):
    def example(self):
        doc = PdfTextDocument(
            'main.pdf',
            [PdfTextPage(1, 100, 100, 'image_only', [])],
            [],
            [{'code': 'image_only_page', 'page': 1}],
            True,
        )
        cfg = {
            'source_sha256': 'a' * 64,
            'native_reading_regions': [
                {'region_id': 'body-a', 'page': 1, 'box': [10, 10, 90, 45]},
                {'region_id': 'body-b', 'page': 1, 'box': [10, 55, 90, 90]},
            ],
            'reviewed_image_only_regions': [
                {
                    'region_id': 'body-a',
                    'reason': 'Rasterized publisher page',
                    'evidence': 'Visible page 1 first paragraph',
                    'lines': [
                        {'box': [15, 15, 85, 30], 'reviewed_value': 'First paragraph'}
                    ],
                },
                {
                    'region_id': 'body-b',
                    'reason': 'Rasterized publisher page',
                    'evidence': 'Visible page 1 second paragraph',
                    'lines': [
                        {'box': [15, 60, 85, 75], 'reviewed_value': 'Second paragraph'}
                    ],
                },
            ],
        }
        return doc, cfg

    def test_adds_complete_reviewed_text_and_resolves_image_only_warning(self):
        doc, cfg = self.example()
        _reviewed_image_only_regions(doc, cfg)
        self.assertEqual(
            [line.plain_text for line in doc.pages[0].lines],
            ['First paragraph', 'Second paragraph'],
        )
        self.assertEqual(doc.warnings, [])
        self.assertEqual(doc.diagnostic_rows[-1]['kind'], 'reviewed_image_only_page')
        self.assertEqual(doc.diagnostic_rows[-1]['region_ids'], ['body-a', 'body-b'])

    def test_rejects_partial_page_review(self):
        doc, cfg = self.example()
        cfg['reviewed_image_only_regions'].pop()
        with self.assertRaisesRegex(
            PdfArticleExtractionError,
            'every reading region',
        ):
            _reviewed_image_only_regions(doc, cfg)

    def test_rejects_native_or_populated_target(self):
        for mode in ('native', 'populated'):
            with self.subTest(mode=mode):
                doc, cfg = self.example()
                if mode == 'native':
                    doc.pages[0] = PdfTextPage(1, 100, 100, 'native_text', [])
                else:
                    doc.pages[0].lines.append(
                        PdfTextLine(1, (20, 20, 80, 30), 'existing', 'existing', 'p1')
                    )
                with self.assertRaisesRegex(
                    PdfArticleExtractionError,
                    'otherwise empty image-only page',
                ):
                    _reviewed_image_only_regions(doc, cfg)
