from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts.private_directory import create_unique_private_directory


class PrivateDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.parent = Path(self.directory.name) / "private-output"
        self.parent.mkdir()

    def test_success_creates_one_new_child_with_private_posix_mode(self):
        created = create_unique_private_directory(self.parent, "run-")

        self.assertEqual(created.parent, self.parent)
        self.assertTrue(created.name.startswith("run-"))
        self.assertTrue(created.is_dir())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o700)

    def test_collision_is_retried_without_overwriting_existing_directory(self):
        collision = self.parent / "run-aaaaaaaaaaaaaaaa"
        collision.mkdir()
        sentinel = collision / "sentinel.txt"
        sentinel.write_text("preserve", encoding="utf-8")

        with patch(
            "scripts.private_directory.secrets.token_hex",
            side_effect=["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"],
        ):
            created = create_unique_private_directory(self.parent, "run-", attempts=2)

        self.assertEqual(created.name, "run-bbbbbbbbbbbbbbbb")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_permission_failure_is_not_retried_as_a_name_collision(self):
        with patch(
            "scripts.private_directory.Path.mkdir",
            side_effect=PermissionError("sandbox denied directory creation"),
        ) as mkdir:
            with self.assertRaisesRegex(PermissionError, "sandbox denied"):
                create_unique_private_directory(self.parent, "run-")

        mkdir.assert_called_once()

    def test_collision_budget_fails_without_reusing_existing_directory(self):
        collision = self.parent / "run-aaaaaaaaaaaaaaaa"
        collision.mkdir()
        with patch(
            "scripts.private_directory.secrets.token_hex",
            return_value="aaaaaaaaaaaaaaaa",
        ):
            with self.assertRaises(FileExistsError):
                create_unique_private_directory(self.parent, "run-", attempts=2)
        self.assertEqual(list(self.parent.iterdir()), [collision])


if __name__ == "__main__":
    unittest.main()
