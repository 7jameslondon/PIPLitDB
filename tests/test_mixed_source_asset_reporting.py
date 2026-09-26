import unittest
from tests.test_extraction_reporting import _article
from scripts.extraction.reporting import _reconciliation_rows


class MixedSourceAssetReportingTests(unittest.TestCase):
    def test_word_supplement_assets_do_not_inherit_html_authority(self):
        article = _article(text_extraction={
            'source_role': 'main_html', 'source_path': 'html/main.html',
            'method': 'publisher-html-semantic-extraction', 'ocr_performed': False})
        assets = [dict(asset_id=f'word_{i}', category=category,
                       source_path=f'supplementary/source{suffix}')
                  for i, (category, suffix) in enumerate([
                      ('figure', '.doc'), ('supplement_source_image', '.doc'),
                      ('supplement_table', '.doc'), ('figure', '.docx')])]
        rows = _reconciliation_rows(article, [], assets)[-4:]
        self.assertEqual([r['supporting_source'] for r in rows], [
            'Word caption structure', 'Word caption structure',
            'Word table structure', 'Word caption structure'])

    def test_pdf_recoveries_use_asset_authority_not_article_authority(self):
        article = _article(text_extraction={
            'source_role':'main_html', 'source_path':'html/main.html',
            'method':'publisher-html-semantic-extraction', 'ocr_performed':False})
        assets = [dict(asset_id=f'asset_{i}',category=category,
                       source_path=source, label=f'Asset {i}')
                  for i,(category,source) in enumerate([
                      ('figure','html/main.html'), ('figure','pdf/main.pdf'),
                      ('scheme','pdf/main.pdf'), ('table','pdf/main.pdf')])]
        rows = _reconciliation_rows(article, [], assets)[-4:]
        self.assertEqual([r['supporting_source'] for r in rows], [
            'HTML caption structure','PDF caption structure',
            'PDF caption structure','PDF table structure'])


if __name__ == '__main__':
    unittest.main()
