import tempfile
import unittest
from pathlib import Path

from scripts.extraction.html_extractor import extract_html


class WileyFnxxTitleNoteTests(unittest.TestCase):
    def extract(self, note, link_id="link_fnxx"):
        source = f'''<html><body><article><h1>Study of H<sub>2</sub>O<a
        id="{link_id}" aria-label="Scrollable Link" href="#fnxx"><sup>†</sup></a></h1>
        {note}<section><h2>Abstract</h2><p>Authored abstract.</p></section>
        </article></body></html>'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "main.html"
            path.write_text(source, encoding="utf-8")
            return extract_html(path, "main.html")

    def test_authenticated_fnxx_note_preserves_funding_and_scientific_subscript(self):
        note = '<div id="fnxx" tabindex="0"><sup>†</sup><div><p>This work was supported by a fellowship.</p></div></div>'
        result = self.extract(note)
        self.assertEqual(result.title, "Study of H_{2}O")
        self.assertIn("Funding (†): This work was supported by a fellowship.",
                      [block.plain_text for block in result.front_matter])

    def test_missing_or_mismatched_control_does_not_remove_marker(self):
        self.assertEqual(self.extract("").title, "Study of H_{2}O^{†}")
        note = '<div id="fnxx" tabindex="0"><sup>†</sup><div><p>Authored note.</p></div></div>'
        self.assertEqual(self.extract(note, "other").title, "Study of H_{2}O^{†}")


if __name__ == "__main__":
    unittest.main()
