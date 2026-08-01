from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.resolve_pr_merge import (
    MergeResolutionError,
    resolve_pull_request_merge,
    write_github_outputs,
)


class PullRequestMergeResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.remote = self.root / "remote.git"
        self.author = self.root / "author"
        self.runner = self.root / "runner"

        self.git(self.root, "init", "--bare", str(self.remote))
        self.git(self.root, "init", str(self.author))
        self.git(self.author, "config", "user.email", "resolver@example.test")
        self.git(self.author, "config", "user.name", "Merge Resolver")

        record = self.author / "record.txt"
        record.write_text("base\n", encoding="utf-8")
        self.git(self.author, "add", "record.txt")
        self.git(self.author, "commit", "-m", "base")
        self.base_sha = self.git(self.author, "rev-parse", "HEAD")

        self.git(self.author, "switch", "-c", "feature")
        record.write_text("head\n", encoding="utf-8")
        self.git(self.author, "add", "record.txt")
        self.git(self.author, "commit", "-m", "head")
        self.head_sha = self.git(self.author, "rev-parse", "HEAD")
        self.head_tree = self.git(self.author, "rev-parse", "HEAD^{tree}")

        record.write_text("alternate merge result\n", encoding="utf-8")
        self.git(self.author, "add", "record.txt")
        self.alternate_tree = self.git(self.author, "write-tree")
        self.git(self.author, "reset", "--hard", self.head_sha)

        self.git(self.author, "remote", "add", "origin", str(self.remote))
        self.git(
            self.author,
            "push",
            "origin",
            f"{self.base_sha}:refs/heads/main",
            f"{self.head_sha}:refs/heads/feature",
        )
        self.git(self.root, "clone", str(self.remote), str(self.runner))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def git(root: Path, *arguments: str, input_text: str | None = None) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return result.stdout.strip()

    def make_merge(
        self,
        *,
        tree: str | None = None,
        first_parent: str | None = None,
        second_parent: str | None = None,
        message: str,
    ) -> str:
        return self.git(
            self.author,
            "commit-tree",
            tree or self.head_tree,
            "-p",
            first_parent or self.base_sha,
            "-p",
            second_parent or self.head_sha,
            input_text=f"{message}\n",
        )

    def publish_merge(self, commit_sha: str) -> None:
        self.git(
            self.author,
            "push",
            "--force",
            "origin",
            f"{commit_sha}:refs/pull/7/merge",
        )

    def resolve(self, **overrides: object):
        arguments = {
            "remote": "origin",
            "pr_number": 7,
            "expected_base_sha": self.base_sha,
            "expected_head_sha": self.head_sha,
            "max_attempts": 1,
            "delay_seconds": 0,
        }
        arguments.update(overrides)
        return resolve_pull_request_merge(self.runner, **arguments)

    def test_resolves_matching_parent_commit_and_tree(self) -> None:
        merge_sha = self.make_merge(message="matching merge")
        self.publish_merge(merge_sha)

        identity = self.resolve()

        self.assertEqual(identity.commit_sha, merge_sha)
        self.assertEqual(identity.tree_sha, self.head_tree)
        self.assertEqual(identity.base_sha, self.base_sha)
        self.assertEqual(identity.head_sha, self.head_sha)

    def test_retries_until_missing_ref_appears(self) -> None:
        merge_sha = self.make_merge(message="eventual merge")
        delays: list[float] = []

        def publish_after_first_attempt(delay: float) -> None:
            delays.append(delay)
            self.publish_merge(merge_sha)

        identity = self.resolve(
            max_attempts=2,
            delay_seconds=0.25,
            sleeper=publish_after_first_attempt,
        )

        self.assertEqual(identity.commit_sha, merge_sha)
        self.assertEqual(delays, [0.25])

    def test_retries_until_parent_order_matches(self) -> None:
        wrong_merge = self.make_merge(
            first_parent=self.head_sha,
            second_parent=self.base_sha,
            message="wrong parents",
        )
        correct_merge = self.make_merge(message="correct parents")
        self.publish_merge(wrong_merge)

        def publish_correct_merge(_delay: float) -> None:
            self.publish_merge(correct_merge)

        identity = self.resolve(max_attempts=2, sleeper=publish_correct_merge)

        self.assertEqual(identity.commit_sha, correct_merge)

    def test_same_content_survives_merge_commit_regeneration(self) -> None:
        first_merge = self.make_merge(message="first metadata")
        regenerated_merge = self.make_merge(message="regenerated metadata")
        self.publish_merge(first_merge)
        first_identity = self.resolve()

        self.publish_merge(regenerated_merge)
        regenerated_identity = self.resolve()

        self.assertNotEqual(first_identity.commit_sha, regenerated_identity.commit_sha)
        self.assertEqual(first_identity.content_key, regenerated_identity.content_key)

    def test_changed_merge_tree_changes_content_identity(self) -> None:
        first_merge = self.make_merge(message="original tree")
        changed_merge = self.make_merge(
            tree=self.alternate_tree,
            message="changed tree",
        )
        self.publish_merge(first_merge)
        first_identity = self.resolve()

        self.publish_merge(changed_merge)
        changed_identity = self.resolve()

        self.assertEqual(first_identity.base_sha, changed_identity.base_sha)
        self.assertEqual(first_identity.head_sha, changed_identity.head_sha)
        self.assertNotEqual(first_identity.content_key, changed_identity.content_key)

    def test_missing_ref_times_out_with_actionable_error(self) -> None:
        delays: list[float] = []

        with self.assertRaisesRegex(
            MergeResolutionError,
            r"refs/pull/7/merge.*after 2 attempts",
        ):
            self.resolve(
                max_attempts=2,
                delay_seconds=0.5,
                sleeper=delays.append,
            )

        self.assertEqual(delays, [0.5])

    def test_writes_all_github_outputs(self) -> None:
        merge_sha = self.make_merge(message="output merge")
        self.publish_merge(merge_sha)
        identity = self.resolve()
        output_path = self.root / "github-output.txt"

        write_github_outputs(output_path, identity)

        self.assertEqual(
            output_path.read_text(encoding="utf-8").splitlines(),
            [
                f"commit_sha={identity.commit_sha}",
                f"tree_sha={identity.tree_sha}",
                f"base_sha={identity.base_sha}",
                f"head_sha={identity.head_sha}",
            ],
        )


if __name__ == "__main__":
    unittest.main()
