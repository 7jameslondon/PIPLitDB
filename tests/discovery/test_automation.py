from __future__ import annotations

import tempfile
import unittest
import subprocess
from datetime import date
from pathlib import Path

from scripts.discovery.automation import (
    AutomationFailure,
    AutomationInventory,
    OpenBatch,
    _decode_marker,
    make_handoff,
    marker,
    inspect_bot_branch,
    read_handoff,
    validate_handoff,
    write_handoff,
)
from scripts.discovery.models import Author, Candidate, DateValue, DiscoveryReport, SourceDiagnostics
from scripts.discovery.sources import REQUIRED_DISCOVERY_SOURCES
from scripts.discovery.config import canonical_json_digest


def report() -> DiscoveryReport:
    candidate = Candidate(
        "Pyrrole-imidazole polyamide paper",
        [Author("Ada Example")],
        doi="10.1234/new",
        url="https://doi.org/10.1234/new",
        journal="Journal",
        document_type="research_article",
        publication_stage="publication",
        dates=[DateValue(date(2025, 2, 3), "online", "crossref")],
        discovered_by={"crossref": "10.1234/new"},
        score=8,
        disposition="new",
    )
    diagnostics = [SourceDiagnostics(source, complete=True, result_count=1) for source in REQUIRED_DISCOVERY_SOURCES]
    return DiscoveryReport("candidates", date(2025, 1, 1), date(2025, 6, 30), date(2025, 6, 30), [candidate], diagnostics, "a" * 64)


class AutomationTests(unittest.TestCase):
    def test_schedule_requires_explicit_rollout_variable(self) -> None:
        workflow = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "discover-papers.yml").read_text(encoding="utf-8")
        self.assertIn("vars.DISCOVERY_SCHEDULE_ENABLED == 'true'", workflow)

    def test_marker_round_trips_untrusted_identifier_as_base64(self) -> None:
        body = marker("b" * 64, ["doi:10.1234/a-->unsafe"])
        self.assertEqual(body.count("-->"), 1)
        self.assertEqual(_decode_marker(body), ("b" * 64, ("doi:10.1234/a-->unsafe",)))

    def test_complete_handoff_validates_and_round_trips_checksum(self) -> None:
        handoff = make_handoff(
            report(),
            base_sha="c" * 40,
            repository="owner/repo",
            default_branch="main",
            publication_allowed=True,
            workflow_run_url="https://github.com/owner/repo/actions/runs/1",
            automation_inventory=AutomationInventory((), ()),
        )
        self.assertTrue(handoff["publication_allowed"])
        self.assertEqual(handoff["reserved_ids"], [])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handoff.json"
            checksum = write_handoff(path, handoff)
            loaded = read_handoff(path, checksum, expected_repository="owner/repo")
            self.assertEqual(loaded["batch_key"], handoff["batch_key"])

    def test_incomplete_sources_force_publication_false(self) -> None:
        value = report()
        value.source_diagnostics[0].complete = False
        handoff = make_handoff(
            value,
            base_sha="c" * 40,
            repository="owner/repo",
            default_branch="main",
            publication_allowed=True,
            workflow_run_url="",
            automation_inventory=AutomationInventory((), ()),
        )
        self.assertFalse(handoff["publication_allowed"])

    def test_open_batch_record_ids_are_reserved_in_handoff(self) -> None:
        handoff = make_handoff(
            report(),
            base_sha="c" * 40,
            repository="owner/repo",
            default_branch="main",
            publication_allowed=True,
            workflow_run_url="",
            automation_inventory=AutomationInventory(
                (OpenBatch(1, "automation/discovery-" + "b" * 16, "b" * 64, (), ("00520",)),),
                (),
            ),
        )
        self.assertEqual(handoff["reserved_ids"], ["00520"])

    def test_checksum_mismatch_is_rejected(self) -> None:
        handoff = make_handoff(
            report(),
            base_sha="c" * 40,
            repository="owner/repo",
            default_branch="main",
            publication_allowed=True,
            workflow_run_url="",
            automation_inventory=AutomationInventory((), ()),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handoff.json"
            write_handoff(path, handoff)
            with self.assertRaisesRegex(AutomationFailure, "checksum"):
                read_handoff(path, "0" * 64, expected_repository="owner/repo")

    def test_unknown_handoff_key_is_rejected(self) -> None:
        handoff = make_handoff(
            report(),
            base_sha="c" * 40,
            repository="owner/repo",
            default_branch="main",
            publication_allowed=True,
            workflow_run_url="",
            automation_inventory=AutomationInventory((), ()),
        )
        handoff["rogue"] = "value"
        with self.assertRaisesRegex(AutomationFailure, "keys"):
            validate_handoff(handoff)

    def test_durable_branch_trailers_bind_record_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            remote = temporary / "remote.git"
            root = temporary / "work"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "init", str(root)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=root, check=True)
            (root / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "base"],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
            )
            base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()
            batch_key = "b" * 64
            branch = "automation/discovery-" + batch_key[:16]
            subprocess.run(["git", "switch", "-c", branch], cwd=root, check=True, stdout=subprocess.PIPE)
            record_path = root / "database" / "records" / "00514.yaml"
            record_path.parent.mkdir(parents=True)
            record_path.write_text(
                'document_type: "research_article"\npublication_stage: "publication"\ntitle: "PIP paper"\nauthors:\n  - name: "Ada Example"\ndoi: "10.1234/new"\nurl: "https://doi.org/10.1234/new"\npublication_year: 2025\njournal: "Journal"\n',
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            digest = canonical_json_digest(["doi:10.1234/new"])
            message = (
                "Add candidate\n\nPIP-Discovery-Format: 1\nPIP-Discovery-Dates: 2025-01-01..2025-06-30\n"
                f"PIP-Discovery-Batch: {batch_key}\nPIP-Discovery-Config: {'a' * 64}\nPIP-Discovery-Candidates: {digest}"
            )
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", message],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
            )
            subprocess.run(["git", "push", "origin", branch], cwd=root, check=True, stdout=subprocess.PIPE)
            inspected = inspect_bot_branch(root, branch, base_sha)
            self.assertEqual(inspected.batch_key, batch_key)
            self.assertEqual(inspected.generated_records, {"00514": "doi:10.1234/new"})


if __name__ == "__main__":
    unittest.main()
