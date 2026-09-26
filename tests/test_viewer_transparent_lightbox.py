from pathlib import Path
import re
import unittest


class ViewerTransparentLightboxTests(unittest.TestCase):
    def test_transparent_scientific_line_art_has_opaque_light_backing(self):
        source = (Path(__file__).resolve().parents[1] / 'extraction_viewer.html').read_text(encoding='utf-8')
        rule = re.search(r'\.image-lightbox-image\s*\{([^}]+)\}', source).group(1)
        self.assertRegex(rule, r'background:\s*#fff\s*;')
        self.assertIn('id="image-lightbox-image"', source)
