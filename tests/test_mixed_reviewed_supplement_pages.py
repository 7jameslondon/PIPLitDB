import unittest
from scripts.extraction.supplements import _resolved_fully_reviewed_pdf_native_text_warning


class MixedReviewedSupplementPagesTest(unittest.TestCase):
    def test_text_page_and_captioned_visual_pages_jointly_cover_scanned_source(self):
        warnings = [{'code': 'supplement_pdf_native_text_empty'}, {'code': 'unrelated'}]
        result = _resolved_fully_reviewed_pdf_native_text_warning(
            warnings, page_count=3,
            reviewed_semantic_locators=['page=1;reviewed text', 'page=2;Figure S1', 'page=3;Figure S2'],
            reviewed_visual_pages=[2, 3], reviewed_text_pages=[1])
        self.assertEqual(result, [{'code': 'unrelated'}])

    def test_incomplete_or_unpaired_review_keeps_warning(self):
        warnings = [{'code': 'supplement_pdf_native_text_empty'}]
        for locators, visuals, texts in [
            (['page=1', 'page=2', 'page=3'], [2], [1]),
            (['page=1', 'page=2'], [2, 3], [1]),
            (['page=1', 'page=2', 'page=3'], [], [1]),
        ]:
            with self.subTest(locators=locators, visuals=visuals, texts=texts):
                self.assertEqual(_resolved_fully_reviewed_pdf_native_text_warning(
                    warnings, page_count=3, reviewed_semantic_locators=locators,
                    reviewed_visual_pages=visuals, reviewed_text_pages=texts), warnings)


if __name__ == '__main__':
    unittest.main()
