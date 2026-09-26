import unittest
from scripts.extraction.supplements import _supplement_table_text_regions


class ReviewedTableContinuationTests(unittest.TestCase):
    def test_multipage_table_exclusion_keeps_each_exact_region(self):
        config = {'table_overrides': [{'page': 1, 'source_locator': 'page=1;table-bbox=10,20,100,200',
                   'continuation_regions': [{'page': 2, 'box': [10, 10, 100, 80]}]}]}
        self.assertEqual(_supplement_table_text_regions(config), {1: [(10,20,100,200)], 2: [(10,10,100,80)]})
        config['table_overrides'][0]['continuation_regions'][0]['box'] = [100,10,10,80]
        with self.assertRaises(ValueError):
            _supplement_table_text_regions(config)

    def test_continuation_regions_reject_invalid_coordinates_and_pages(self):
        for region in ({'page': True, 'box': [0, 0, 1, 1]},
                       {'page': 0, 'box': [0, 0, 1, 1]},
                       {'page': 2, 'box': [0, 0, float('inf'), 1]},
                       {'page': 2, 'box': [False, 0, 1, 1]},
                       {'page': 2, 'box': [0, 0, 1]},
                       {'page': 2, 'box': [0, 1, 1, 0]}, None):
            with self.subTest(region=region):
                config = {'table_overrides': [{'page': 1,
                    'source_locator': 'page=1;table-bbox=10,20,100,200',
                    'continuation_regions': [region]}]}
                with self.assertRaises(ValueError):
                    _supplement_table_text_regions(config)

    def test_single_page_tables_remain_unchanged(self):
        config = {'table_overrides': [{'page': 3,
                   'source_locator': 'page=3;table-bbox=10,20,100,200'}]}
        self.assertEqual(_supplement_table_text_regions(config),
                         {3: [(10, 20, 100, 200)]})


if __name__ == '__main__':
    unittest.main()
