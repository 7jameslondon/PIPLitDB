from __future__ import annotations

import base64
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from lxml import html as lxml_html

from scripts.extraction.html_extractor import _jove_article_details, extract_html


class JoveHtmlExtractorTests(unittest.TestCase):
    def source(self) -> str:
        pixel = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
        )
        encoded = base64.b64encode(pixel).decode("ascii")
        return f"""<body><article>
<div id="sticky-header"><div>
  <div><p>Method Article</p></div>
  <div><div></div><h1>Synthetic JoVE Article</h1></div>
  <div>
    <div><div><div><p>DOI:</p><p><a href="https://dx.doi.org/10.3791/12345">10.3791/12345</a></p><p>⸱</p><p>Cited by 1</p><p>⸱</p><p>January 20th, 2016</p></div></div></div>
    <div><div><span><a rel="author" href="/author/1/ada-author"><span>Ada Author</span><sup>1</sup></a></span></div>
    <div><a href="/institutions/1/example-university"><sup>1</sup><span>Example Department, <span>Example University</span></span></a></div></div>
  </div>
  <div><div></div><div></div></div>
</div></div>
<div>
  <div><h2 id="abstract">Abstract</h2><span><div></div><div><p>Abstract prose.</p></div></span></div>
  <div><h2 id="introduction">Introduction</h2><span><div></div><div><p>Introduction prose.</p>
    <figure id="figure-1"><img alt="Synthetic figure" src="data:image/png;base64,{encoded}"><br><strong>Figure 1. Synthetic result.</strong>Complete legend. <a href="https://www.jove.com/files/ftp_upload/12345/12345fig1highres.jpg">Please click here to view a larger version of this figure.</a></figure>
  </div></span></div>
  <div><h2 id="protocol">Protocol</h2><span><div></div><div><ol><li>First step.</li></ol></div></span></div>
  <div><h2 id="results">Results</h2><span><div></div><div><p>Result prose.</p></div></span></div>
  <div><h2 id="discussion">Discussion</h2><span><div></div><div><p>Discussion prose.</p></div></span></div>
  <div><h2 id="materials">Materials</h2><p></p><table tabindex="0" aria-labelledby="materials-table-caption">
    <caption id="materials-table-caption">List of materials used in this article</caption>
    <thead><tr><th>Name</th><th>Company</th><th>Catalog Number</th><th>Comments</th></tr></thead>
    <tbody><tr><td>Reagent</td><td>Supplier</td><td>123</td><td>Authored note</td></tr></tbody>
  </table></div>
  <div><h2 id="references">References</h2><span><div></div><div><ol>
    <li aria-label="A. Author. First citation.">A. Author. First citation.</li>
    <li aria-label="B. Author. Second citation.">B. Author. Second citation.</li>
  </ol></div></span></div>
  <div><h2 id="reprints-and-permissions">Reprints and Permissions</h2><div><p>Request permission to reuse the text or figures of this JoVE article</p><a href="/reprint-permissions/12345">Request Permission</a></div></div>
</div>
</article></body>"""

    def extract(self, source: str):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "main.html"
            path.write_text(source, encoding="utf-8")
            return extract_html(path, "papers (private)/00000/html/main.html")

    def test_exact_jove_snapshot_recovers_front_matter_table_and_references(self) -> None:
        source = self.source()
        self.assertIsNotNone(_jove_article_details(lxml_html.fromstring(source)))

        result = self.extract(source)

        self.assertEqual(result.title, "Synthetic JoVE Article")
        self.assertEqual(len(result.figures), 1)
        self.assertIn("result. Complete legend", result.figures[0].caption_plain)
        self.assertNotIn("Please click", result.figures[0].caption_plain)
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(
            result.tables[0].title_plain,
            "List of materials used in this article",
        )
        self.assertEqual(result.tables[0].parts[0].rows[1][2].text, "123")
        self.assertEqual(
            [reference.plain_text for reference in result.references],
            ["A. Author. First citation.", "B. Author. Second citation."],
        )
        front = [block.plain_text for block in result.front_matter]
        self.assertIn("Article type: Method Article", front)
        self.assertIn(
            "Affiliation 1: Example Department, Example University",
            front,
        )
        self.assertIn("Publication date: January 20th, 2016", front)

    def test_near_miss_materials_caption_does_not_activate_jove_recovery(self) -> None:
        source = self.source().replace(
            'id="materials-table-caption"',
            'id="materials-caption"',
            1,
        )
        self.assertIsNone(_jove_article_details(lxml_html.fromstring(source)))

        result = self.extract(source)
        self.assertEqual(result.tables, [])
        self.assertEqual(result.references, [])
        self.assertEqual(result.front_matter, [])


if __name__ == "__main__":
    unittest.main()
