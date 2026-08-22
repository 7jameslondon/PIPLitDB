from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts.extraction.models import SourceFile
from scripts.extraction.xml_supplement import extract_xml_fields


def _source(path: Path) -> SourceFile:
    data = path.read_bytes()
    return SourceFile(
        role="supplement",
        path=path,
        relative_path="papers (private)/00001/supplementary/data.xml",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        detected_format="text/xml",
    )


class XmlSupplementTests(unittest.TestCase):
    def test_recovers_undeclared_publisher_prefix_and_preserves_empty_field(self) -> None:
        raw = (
            b'<?xml version="1.0"?><data:data><datasets><author>a@example.test'
            b'</author><dataset><reason>Available on request</reason><comments/>'
            b'</dataset></datasets></data:data>'
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "data.xml"
            path.write_bytes(raw)
            blocks, warnings = extract_xml_fields(
                _source(path), "supplement_001", path
            )

            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(
                [block.plain_text for block in blocks],
                [
                    "author: a@example.test",
                    "reason: Available on request",
                    "comments:",
                ],
            )
            self.assertEqual(
                [warning["code"] for warning in warnings],
                ["supplement_xml_recovered_malformed_source"],
            )

    def test_external_entity_is_never_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret = root / "secret.txt"
            secret.write_text("must-not-appear", encoding="utf-8")
            path = root / "data.xml"
            path.write_text(
                '<!DOCTYPE root [<!ENTITY external SYSTEM "secret.txt">]>'
                "<root>&external;</root>",
                encoding="utf-8",
            )
            blocks, _ = extract_xml_fields(_source(path), "supplement_001", path)
            rendered = "\n".join(block.plain_text for block in blocks)
            self.assertNotIn("must-not-appear", rendered)


if __name__ == "__main__":
    unittest.main()
