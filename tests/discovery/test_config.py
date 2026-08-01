from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from scripts.discovery.config import load_exclusions, load_queries


ROOT = Path(__file__).resolve().parents[2]


class ConfigTests(unittest.TestCase):
    def test_tracked_queries_load(self) -> None:
        config = load_queries(ROOT / "discovery" / "queries.yaml")
        self.assertEqual(config.version, 1)
        self.assertEqual(set(group.sources[0] for group in config.groups), {"openalex"})
        self.assertEqual(len(config.digest), 64)

    def test_configuration_schemas_are_valid_json(self) -> None:
        for name in ("queries.schema.json", "exclusions.schema.json"):
            value = json.loads((ROOT / "discovery" / name).read_text(encoding="utf-8"))
            self.assertEqual(value["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_unknown_query_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queries.yaml"
            path.write_text("version: 1\nrecord_threshold: 1\ngroups: []\nrogue: true\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown"):
                load_queries(path)

    def test_duplicate_keys_and_aliases_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            duplicate = Path(directory) / "duplicate.yaml"
            duplicate.write_text("version: 1\nversion: 1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_queries(duplicate)
            alias = Path(directory) / "alias.yaml"
            alias.write_text("version: 1\nrecord_threshold: &score 6\nmax_candidates: *score\ngroups: []\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "aliases"):
                load_queries(alias)

    def test_exact_exclusion_loads_and_normalizes_doi(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "exclusions.yaml"
            path.write_text(
                "version: 1\nexclusions:\n  - doi: https://doi.org/10.1234/ABC\n    reason: Not about PIPs.\n    decision_date: 2026-01-01\n",
                encoding="utf-8",
            )
            exclusions = load_exclusions(path)
            self.assertEqual(exclusions[0].identifier, "doi:10.1234/abc")

    def test_fuzzy_exclusion_shape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "exclusions.yaml"
            path.write_text(
                "version: 1\nexclusions:\n  - doi: 10.1234/a\n    url: https://example.com/a\n    reason: Too broad.\n    decision_date: 2026-01-01\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "exactly one"):
                load_exclusions(path)


if __name__ == "__main__":
    unittest.main()
