import unittest
from tests import test_sciencedirect_html_extractor as helpers


class AepTableDownloadTests(unittest.TestCase):
    extract = helpers.ScienceDirectHtmlExtractionTests.extract

    def test_graphical_cell_control_removed_but_value_and_authored_link_retained(self):
        result = self.extract('''<article><h1>Example</h1><div id="body">
        <section id="aep-section-id1"><h2>Results</h2><div id="TBL1">
        <span><span><p>Table 1. Values</p></span></span><div><table><tr><td>Value</td></tr>
        <tr><td><span><figure id="aep-figure-id8"><span><ol><li>
        <a download="" title="Download full-size image" href="https://ars.els-cdn.com/content/image/example.gif">
        <span>Download: <span>Download full-size image</span></span></a></li></ol></span></figure></span>
        4.7×10<sup>10</sup> <a href="https://example.org/data">Authored data</a></td></tr>
        </table></div></div></section></div></article>''')
        cell = result.tables[0].parts[0].rows[1][0]
        self.assertNotIn('Download:', cell.text)
        self.assertNotIn('els-cdn', cell.markdown)
        self.assertIn('4.7×10', cell.text)
        self.assertIn('Authored data', cell.text)
        self.assertIn('https://example.org/data', cell.markdown)

    def test_unrelated_figure_id_keeps_download_wording(self):
        result = self.extract('''<article><h1>Example</h1><div id="body">
        <section id="aep-section-id1"><h2>Results</h2><div id="TBL1">
        <span><span><p>Table 1. Values</p></span></span><div><table><tr><td>Value</td></tr>
        <tr><td><figure id="authored"><a download="" title="Download full-size image"
        href="https://ars.els-cdn.com/content/image/example.gif">Download: Download full-size image</a>
        </figure></td></tr></table></div></div></section></div></article>''')
        self.assertIn('Download:', result.tables[0].parts[0].rows[1][0].text)
