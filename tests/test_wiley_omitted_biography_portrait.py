import tempfile
import unittest
from pathlib import Path

from scripts.extraction.html_extractor import extract_html


class WileyOmittedBiographyPortraitTests(unittest.TestCase):
    def extract(
        self,
        biography_heading: str = "Biographical Information",
        portrait_markup: str | None = None,
        portrait_href: str = (
            "/cms/asset/e6d22f55-3e0c-4ed5-b774-6ab3fa798b3c/"
            "article-bio-0001-m.jpg"
        ),
    ):
        portrait_markup = portrait_markup or '''<span>[Image omitted: magnified image]</span>'''
        source = f'''<!doctype html><html><body><article>
<h1>Wiley biography placeholder</h1>
<section id="abstract-graphical-en"><h2>Graphical Abstract</h2>
  <p>Authored graphical summary.</p>
  <figure id="visual-abstract"><img
    src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    alt="Graphical Abstract"></figure>
</section>
<section><h2>{biography_heading}</h2>
  <p><i>Author biography.</i></p>
  <div><figure><div><a target="_blank"
    href="{portrait_href}">
    <picture>{portrait_markup}</picture></a><p></p></div></figure></div>
</section>
</article></body></html>'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "main.html"
            path.write_text(source, encoding="utf-8")
            return extract_html(path, "main.html")

    def test_exact_remote_only_biography_portrait_is_not_graphical_abstract(self):
        result = self.extract()

        self.assertEqual(
            [(figure.figure_id, figure.kind) for figure in result.figures],
            [("graphical_abstract", "graphical_abstract")],
        )
        self.assertEqual(
            [asset.asset_id for asset in result.embedded_assets],
            ["graphical_abstract"],
        )

    def test_same_placeholder_outside_biography_section_fails_closed(self):
        result = self.extract("Results")

        self.assertEqual(
            [(figure.figure_id, figure.kind) for figure in result.figures],
            [
                ("graphical_abstract", "graphical_abstract"),
                ("graphical_abstract_002", "graphical_abstract"),
            ],
        )

    def test_embedded_wiley_biography_portrait_is_not_graphical_abstract(self):
        source_image = (
            '<img src="data:image/gif;base64,'
            'R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==" '
            'alt="magnified image" title="magnified image">'
        )
        source = self.extract(portrait_markup=source_image)

        self.assertEqual(
            [(figure.figure_id, figure.kind) for figure in source.figures],
            [("graphical_abstract", "graphical_abstract")],
        )

    def test_opaque_wiley_biography_portrait_name_is_not_graphical_abstract(self):
        source_image = (
            '<img src="data:image/gif;base64,'
            'R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==" '
            'alt="magnified image" title="magnified image">'
        )
        source = self.extract(
            portrait_markup=source_image,
            portrait_href=(
                "/cms/asset/129bfd58-ea1f-4600-a20d-29d69c2e01bb/"
                "mbph001.jpg"
            ),
        )

        self.assertEqual(
            [(figure.figure_id, figure.kind) for figure in source.figures],
            [("graphical_abstract", "graphical_abstract")],
        )


if __name__ == "__main__":
    unittest.main()
