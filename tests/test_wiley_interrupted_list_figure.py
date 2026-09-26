import base64
import tempfile
import unittest
from pathlib import Path

from scripts.extraction.html_extractor import extract_html


class WileyInterruptedListFigureTests(unittest.TestCase):
    def test_semantic_figure_card_does_not_duplicate_into_list_text(self) -> None:
        pixel = base64.b64encode(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        ).decode("ascii")
        source = f"""<!doctype html><html><body><article>
<h1>Interrupted Wiley list</h1><section><h2>Results</h2>
<ol><li><p>Authored text before Figure 2</p><section>
<figure id="tcr123-fig-0002"><img src="data:image/png;base64,{pixel}" alt="Figure 2">
<figcaption><div><strong>Figure 2</strong><div><a href="#">Open in figure viewer</a>
<a href="/download">PowerPoint</a></div></div><div>Authored diﬀerential caption.</div></figcaption>
</figure></section><p>a). Authored text after the figure card.</p></li></ol>
</section></article></body></html>"""

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "main.html"
            path.write_text(source, encoding="utf-8")
            result = extract_html(path, Path(temporary) / "assets")

        block = result.sections[0].blocks[0]
        self.assertEqual(
            block.plain_text,
            "1. Authored text before Figure 2 a). Authored text after the figure card.",
        )
        self.assertNotIn("Open in figure viewer", block.plain_text)
        self.assertNotIn("PowerPoint", block.plain_text)
        self.assertNotIn("Authored differential caption", block.plain_text)
        self.assertEqual(len(result.figures), 1)
        self.assertEqual(
            result.figures[0].caption_plain,
            "Authored differential caption.",
        )


if __name__ == "__main__":
    unittest.main()
