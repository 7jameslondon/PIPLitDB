from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.extraction.metadata import load_record_metadata


class ExtractionMetadataHumanOnlyNotesTests(unittest.TestCase):
    def test_jamies_human_only_notes_are_not_propagated_to_extraction(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "00001.yaml"
            path.write_text(
                "title: Synthetic article\n"
                "authors:\n"
                "  - name: Thomas G. Example\n"
                "publication_year: 2000\n"
                "journal: Synthetic Journal\n"
                "doi: 10.0000/example\n"
                "document_type: research_article\n"
                "jamies_human_only_notes:\n"
                "  tags:\n"
                "    - read_later\n"
                "    - interesting_for_cooperativity\n",
                encoding="utf-8",
            )

            rendered_metadata = load_record_metadata(path, "00001").as_dict()

        self.assertNotIn("jamies_human_only_notes", rendered_metadata)
        serialized = json.dumps(rendered_metadata)
        self.assertNotIn("read_later", serialized)
        self.assertNotIn("interesting_for_cooperativity", serialized)


if __name__ == "__main__":
    unittest.main()
