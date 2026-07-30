from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from scripts.validate_metadata import validate_repository


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


VALID_RECORD = """
document_type: research_article
publication_stage: publication
title: "A valid paper"
authors:
  - name: "Alex Example"
doi: "10.1234/example.1"
url: "https://doi.org/10.1234/example.1"
publication_year: 2024
journal: "Example Journal"
pip_litdb_status: needs_review
"""


class MetadataValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "database" / "schema").mkdir(parents=True)
        (self.root / "database" / "vocabularies").mkdir(parents=True)
        (self.root / "database" / "records").mkdir(parents=True)
        shutil.copy2(
            REPOSITORY_ROOT / "database" / "schema" / "paper.schema.json",
            self.root / "database" / "schema" / "paper.schema.json",
        )
        self.write_vocabularies()
        self.write_record("00001", VALID_RECORD)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_vocabularies(self) -> None:
        vocabularies = {
            "document-types.yaml": """
                research_article:
                  label: Research article
                  description: Reports original research.
                review:
                  label: Review
                  description: Reviews published research.
                correction:
                  label: Correction
                  description: Corrects another document.
            """,
            "publication-stages.yaml": """
                preprint:
                  label: Preprint
                  description: A public manuscript.
                publication:
                  label: Publication
                  description: A formally published document.
            """,
            "record-statuses.yaml": """
                needs_review:
                  label: Needs review
                  description: Requires review.
                verified:
                  label: Verified
                  description: Independently verified.
            """,
            "relationship-types.yaml": """
                is_preprint_of:
                  label: Is preprint of
                  description: Preprint relationship.
                  inverse: has_preprint
                has_preprint:
                  label: Has preprint
                  description: Publication relationship.
                  inverse: is_preprint_of
                is_version_of:
                  label: Is version of
                  description: Symmetric version relationship.
                  inverse: is_version_of
            """,
        }
        for filename, content in vocabularies.items():
            (self.root / "database" / "vocabularies" / filename).write_text(
                textwrap.dedent(content).lstrip(), encoding="utf-8"
            )

    def write_record(self, record_id: str, content: str) -> None:
        (self.root / "database" / "records" / f"{record_id}.yaml").write_text(
            textwrap.dedent(content).lstrip(), encoding="utf-8"
        )

    def error_codes(self) -> set[str]:
        return {finding.code for finding in validate_repository(self.root).errors}

    def create_symlink(
        self, link: Path, target: Path, *, target_is_directory: bool = False
    ) -> None:
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"Symbolic links are unavailable in this environment: {exc}")

    def test_valid_database_passes(self) -> None:
        report = validate_repository(self.root)
        self.assertTrue(report.passed, report.findings)
        self.assertEqual(report.record_count, 1)

    def test_duplicate_yaml_key_is_rejected(self) -> None:
        self.write_record("00001", VALID_RECORD + "title: Duplicate key\n")
        self.assertIn("record.yaml", self.error_codes())

    def test_multiple_yaml_documents_are_rejected(self) -> None:
        self.write_record("00001", VALID_RECORD + "---\ntitle: second document\n")
        self.assertIn("record.document_count", self.error_codes())

    def test_invalid_yaml_timestamp_is_reported_without_crashing(self) -> None:
        invalid_values = (
            "2024-13-40",
            "!!int nope",
            "!!bool nope",
            "!!timestamp nope",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                self.write_record(
                    "00001",
                    VALID_RECORD.replace(
                        "publication_year: 2024", f"publication_year: {value}"
                    ),
                )
                self.assertIn("record.yaml", self.error_codes())

    def test_recursive_yaml_alias_is_rejected(self) -> None:
        self.write_record(
            "00001",
            VALID_RECORD.replace(
                'authors:\n  - name: "Alex Example"',
                "authors: &authors [*authors]",
            ),
        )
        self.assertIn("record.recursive_alias", self.error_codes())

    def test_yaml_alias_fanout_is_rejected_without_expansion(self) -> None:
        lines = [VALID_RECORD.rstrip(), 'alias_0: &alias_0 ["seed"]']
        for depth in range(1, 13):
            references = ", ".join([f"*alias_{depth - 1}"] * 10)
            lines.append(f"alias_{depth}: &alias_{depth} [{references}]")
        self.write_record("00001", "\n".join(lines) + "\n")
        self.assertIn("record.alias", self.error_codes())

    def test_excessive_yaml_nesting_is_rejected(self) -> None:
        nested_title = "[" * 150 + '"A valid paper"' + "]" * 150
        self.write_record(
            "00001",
            VALID_RECORD.replace('title: "A valid paper"', f"title: {nested_title}"),
        )
        self.assertIn("record.nesting_depth", self.error_codes())

    def test_yaml_mapping_keys_must_be_json_compatible_strings(self) -> None:
        self.write_record("00001", VALID_RECORD + "1: unsupported key\n")
        self.assertIn("record.non_string_key", self.error_codes())

    def test_schema_root_must_be_an_object(self) -> None:
        schema_path = self.root / "database" / "schema" / "paper.schema.json"
        for root in ("true", "false", "null", "1", "[]"):
            with self.subTest(root=root):
                schema_path.write_text(root, encoding="utf-8")
                self.assertIn("schema.root", self.error_codes())

    def test_unresolved_schema_reference_is_reported_without_crashing(self) -> None:
        schema_path = self.root / "database" / "schema" / "paper.schema.json"
        schema_path.write_text(
            schema_path.read_text(encoding="utf-8").replace(
                '"$ref": "#/$defs/author"',
                '"$ref": "#/$defs/missing-author"',
            ),
            encoding="utf-8",
        )
        self.assertIn("schema.reference", self.error_codes())

    def test_external_schema_references_are_rejected_without_retrieval(self) -> None:
        schema_path = self.root / "database" / "schema" / "paper.schema.json"
        references = (
            "missing.json",
            "file:///definitely/missing/private-schema.json",
            "https://example.test/private-schema.json",
        )
        for reference in references:
            with self.subTest(reference=reference):
                schema_path.write_text(
                    '{"$schema": "https://json-schema.org/draft/2020-12/schema", '
                    f'"$ref": "{reference}"}}',
                    encoding="utf-8",
                )
                self.assertIn("schema.reference", self.error_codes())

    def test_schema_file_symlink_is_rejected(self) -> None:
        schema_path = self.root / "database" / "schema" / "paper.schema.json"
        target = schema_path.with_name("paper.schema.target.json")
        schema_path.rename(target)
        self.create_symlink(schema_path, target)
        self.assertIn("schema.symlink", self.error_codes())

    def test_vocabulary_file_symlink_is_rejected(self) -> None:
        vocabulary_path = (
            self.root / "database" / "vocabularies" / "document-types.yaml"
        )
        target = vocabulary_path.with_name("document-types.target.yaml")
        vocabulary_path.rename(target)
        self.create_symlink(vocabulary_path, target)
        self.assertIn("vocabulary.symlink", self.error_codes())

    def test_records_directory_symlink_is_rejected(self) -> None:
        records = self.root / "database" / "records"
        target = self.root / "records-target"
        records.rename(target)
        self.create_symlink(records, target, target_is_directory=True)
        self.assertIn("record.symlink", self.error_codes())

    def test_vocabulary_directory_symlink_is_rejected(self) -> None:
        vocabularies = self.root / "database" / "vocabularies"
        target = self.root / "vocabularies-target"
        vocabularies.rename(target)
        self.create_symlink(vocabularies, target, target_is_directory=True)
        self.assertIn("vocabulary.symlink", self.error_codes())

    def test_record_filename_and_id_range_are_enforced(self) -> None:
        valid_path = self.root / "database" / "records" / "00001.yaml"
        valid_path.rename(self.root / "database" / "records" / "record-one.yml")
        self.write_record("00000", VALID_RECORD)
        codes = self.error_codes()
        self.assertIn("record.filename", codes)
        self.assertIn("record.id_range", codes)

    def test_schema_and_controlled_vocabulary_are_enforced(self) -> None:
        self.write_record(
            "00001",
            VALID_RECORD.replace("publication_year: 2024", 'publication_year: "2024"').replace(
                "document_type: research_article", "document_type: invented_type"
            ),
        )
        codes = self.error_codes()
        self.assertIn("schema.type", codes)
        self.assertIn("record.unknown_vocabulary_value", codes)

    def test_unhashable_invalid_year_is_reported_without_crashing(self) -> None:
        self.write_record(
            "00001",
            VALID_RECORD.replace("publication_year: 2024", "publication_year: []"),
        )
        self.assertIn("schema.type", self.error_codes())

    def test_duplicate_doi_is_case_insensitive(self) -> None:
        second = (
            VALID_RECORD.replace("A valid paper", "A second paper")
            .replace("10.1234/example.1", "10.1234/EXAMPLE.1")
        )
        self.write_record("00002", second)
        self.assertIn("database.duplicate_doi", self.error_codes())

    def test_probable_duplicates_warn_without_failing(self) -> None:
        second = VALID_RECORD.replace("10.1234/example.1", "10.1234/example.2")
        self.write_record("00002", second)
        report = validate_repository(self.root)
        warning_codes = {finding.code for finding in report.warnings}
        self.assertTrue(report.passed)
        self.assertIn("database.possible_duplicate", warning_codes)

    def test_duplicate_author_and_padding_are_rejected(self) -> None:
        self.write_record(
            "00001",
            VALID_RECORD.replace(
                '  - name: "Alex Example"',
                '  - name: " Alex Example "\n  - name: "alex example"',
            ),
        )
        codes = self.error_codes()
        self.assertIn("record.whitespace", codes)
        self.assertIn("record.duplicate_author", codes)

    def test_doi_url_must_agree(self) -> None:
        self.write_record(
            "00001",
            VALID_RECORD.replace(
                "https://doi.org/10.1234/example.1",
                "https://doi.org/10.1234/different",
            ),
        )
        self.assertIn("record.doi_url_mismatch", self.error_codes())

    def test_url_credentials_are_rejected(self) -> None:
        self.write_record(
            "00001",
            VALID_RECORD.replace(
                "https://doi.org/10.1234/example.1",
                "https://user:secret@example.test/paper",
            ),
        )
        self.assertIn("record.url_credentials", self.error_codes())

    def test_missing_relationship_target_is_rejected(self) -> None:
        self.write_record(
            "00001",
            VALID_RECORD
            + "related_papers:\n"
            + '  - pip_litdb_id: "00002"\n'
            + "    relationship_type: is_preprint_of\n",
        )
        self.assertIn("relationship.target_missing", self.error_codes())

    def test_relationship_requires_exact_inverse(self) -> None:
        self.write_record(
            "00001",
            VALID_RECORD
            + "related_papers:\n"
            + '  - pip_litdb_id: "00002"\n'
            + "    relationship_type: is_preprint_of\n",
        )
        self.write_record(
            "00002",
            VALID_RECORD.replace("A valid paper", "Published paper")
            .replace("10.1234/example.1", "10.1234/example.2"),
        )
        self.assertIn("relationship.inverse_count", self.error_codes())

        self.write_record(
            "00002",
            VALID_RECORD.replace("A valid paper", "Published paper")
            .replace("10.1234/example.1", "10.1234/example.2")
            + "related_papers:\n"
            + '  - pip_litdb_id: "00001"\n'
            + "    relationship_type: has_preprint\n",
        )
        self.assertTrue(validate_repository(self.root).passed)

    def test_relationship_vocabulary_inverse_must_be_reciprocal(self) -> None:
        path = self.root / "database" / "vocabularies" / "relationship-types.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "inverse: is_preprint_of", "inverse: is_version_of"
            ),
            encoding="utf-8",
        )
        self.assertIn("relationship.inverse_not_reciprocal", self.error_codes())

    def test_private_filesystem_references_are_rejected(self) -> None:
        references = (
            "papers (private)/00001/main.pdf",
            r"D:\Papers\00001\main.pdf",
            r"\\labserver\restricted\00001\main.pdf",
            "//labserver/restricted/00001/main.pdf",
            "/tmp/00001/main.pdf",
            "`/tmp/private/00001/main.pdf`",
            "/mnt/restricted/00001/main.pdf",
            "/Volumes/Papers/00001/main.pdf",
            "~/papers/00001/main.pdf",
            "./paper.pdf",
            "../paper.pdf",
            "../restricted/00001/main.pdf",
            r".\restricted\00001\main.pdf",
            "/secret.pdf",
            "C:private.pdf",
            "file:/tmp/00001/main.pdf",
        )
        for reference in references:
            with self.subTest(reference=reference):
                self.write_record(
                    "00001",
                    VALID_RECORD + f"pip_litdb_notes: '{reference}'\n",
                )
                self.assertIn("record.private_reference", self.error_codes())

    def test_web_url_in_notes_is_not_a_private_reference(self) -> None:
        urls = (
            "https://example.test/private/00001/main.pdf",
            "https://example.test/?download=/tmp/00001/main.pdf",
        )
        for url in urls:
            with self.subTest(url=url):
                self.write_record(
                    "00001",
                    VALID_RECORD + f"pip_litdb_notes: 'See {url}'\n",
                )
                self.assertNotIn("record.private_reference", self.error_codes())

    def test_blank_notes_are_rejected(self) -> None:
        self.write_record(
            "00001",
            VALID_RECORD + 'pip_litdb_notes: "   "\n',
        )
        self.assertIn("record.blank_string", self.error_codes())

    def test_git_diff_classifies_add_remove_modify_copy_and_rename(self) -> None:
        self.write_record(
            "00002",
            VALID_RECORD.replace("A valid paper", "Paper to remove").replace(
                "10.1234/example.1", "10.1234/example.2"
            ),
        )
        self.write_record(
            "00003",
            VALID_RECORD.replace("A valid paper", "Paper to modify").replace(
                "10.1234/example.1", "10.1234/example.3"
            ),
        )
        self.write_record(
            "00004",
            VALID_RECORD.replace("A valid paper", "Paper to rename").replace(
                "10.1234/example.1", "10.1234/example.4"
            ),
        )
        self.git("init")
        self.git("config", "user.email", "validator@example.test")
        self.git("config", "user.name", "Metadata Validator")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        base = self.git("rev-parse", "HEAD").strip()

        (self.root / "database" / "records" / "00002.yaml").unlink()
        path = self.root / "database" / "records" / "00003.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace("Paper to modify", "Modified paper"),
            encoding="utf-8",
        )
        (self.root / "database" / "records" / "00004.yaml").rename(
            self.root / "database" / "records" / "00006.yaml"
        )
        self.write_record(
            "00005",
            VALID_RECORD.replace("A valid paper", "Added paper").replace(
                "10.1234/example.1", "10.1234/example.5"
            ),
        )
        self.write_record("00007", VALID_RECORD)
        self.git("add", "--all")
        self.git("commit", "-m", "change records")

        report = validate_repository(self.root, base=base, head="HEAD")
        self.assertEqual(
            {change.kind for change in report.changes},
            {"added", "removed", "modified", "copied", "renamed"},
        )
        modified = next(change for change in report.changes if change.kind == "modified")
        self.assertEqual(modified.new_id, "00003")
        self.assertEqual(modified.changed_fields, ("title",))
        renamed = next(change for change in report.changes if change.kind == "renamed")
        self.assertEqual((renamed.old_id, renamed.new_id), ("00004", "00006"))
        copied = next(change for change in report.changes if change.kind == "copied")
        self.assertEqual((copied.old_id, copied.new_id), ("00001", "00007"))
        warning_codes = {finding.code for finding in report.warnings}
        self.assertIn("change.record_removed", warning_codes)
        self.assertIn("change.id_changed", warning_codes)

    def test_git_diff_handles_excessively_nested_record(self) -> None:
        self.git("init")
        self.git("config", "user.email", "validator@example.test")
        self.git("config", "user.name", "Metadata Validator")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        base = self.git("rev-parse", "HEAD").strip()

        nested_title = "[" * 1500 + '"A valid paper"' + "]" * 1500
        self.write_record(
            "00001",
            VALID_RECORD.replace('title: "A valid paper"', f"title: {nested_title}"),
        )
        self.git("add", "--all")
        self.git("commit", "-m", "add excessive nesting")

        report = validate_repository(self.root, base=base, head="HEAD")
        self.assertIn("record.nesting_depth", {finding.code for finding in report.errors})
        self.assertEqual([change.kind for change in report.changes], ["modified"])

    def test_git_diff_handles_invalid_yaml_scalar(self) -> None:
        self.git("init")
        self.git("config", "user.email", "validator@example.test")
        self.git("config", "user.name", "Metadata Validator")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        base = self.git("rev-parse", "HEAD").strip()

        self.write_record(
            "00001",
            VALID_RECORD.replace("publication_year: 2024", "publication_year: !!int nope"),
        )
        self.git("add", "--all")
        self.git("commit", "-m", "add invalid scalar")

        report = validate_repository(self.root, base=base, head="HEAD")
        self.assertIn("record.yaml", {finding.code for finding in report.errors})
        self.assertEqual([change.kind for change in report.changes], ["modified"])

    def git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return completed.stdout


if __name__ == "__main__":
    unittest.main()
