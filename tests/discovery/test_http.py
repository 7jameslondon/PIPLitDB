from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.discovery.http import HttpClient, HttpFailure
from scripts.discovery.models import SourceDiagnostics


class FakeResponse:
    status = 200

    def __init__(self, body: bytes, content_type: str = "application/json") -> None:
        self.body = body
        self.headers = {"Content-Type": content_type}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]


class HttpTests(unittest.TestCase):
    def test_live_response_is_cached_and_replayed_offline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            def opener(request: object, **_: object) -> FakeResponse:
                calls.append(request)
                return FakeResponse(b'{"ok": true}')

            diagnostics = SourceDiagnostics("test")
            client = HttpClient("test", Path(directory), diagnostics, opener=opener)
            self.assertEqual(client.get_json("https://example.org/api", {"q": "pip"}), {"ok": True})
            offline_diagnostics = SourceDiagnostics("test")
            offline = HttpClient("test", Path(directory), offline_diagnostics, offline=True)
            self.assertEqual(offline.get_json("https://example.org/api", {"q": "pip"}), {"ok": True})
            self.assertEqual(len(calls), 1)
            self.assertEqual(offline_diagnostics.cache_hits, 1)

    def test_cache_metadata_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = HttpClient(
                "test",
                Path(directory),
                SourceDiagnostics("test"),
                opener=lambda *_args, **_kwargs: FakeResponse(b"{}"),
            )
            client.get_json(
                "https://example.org/api",
                {"api_key": "super-secret", "email": "private@example.org", "q": "pip"},
            )
            metadata = next((Path(directory) / "test").glob("*.json")).read_text(encoding="utf-8")
            self.assertNotIn("super-secret", metadata)
            self.assertNotIn("private@example.org", metadata)
            self.assertIn("[redacted]", metadata)

    def test_offline_cache_miss_fails_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = HttpClient("test", Path(directory), SourceDiagnostics("test"), offline=True)
            with self.assertRaisesRegex(HttpFailure, "offline cache miss"):
                client.get_json("https://example.org/api", {})

    def test_non_https_endpoint_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = HttpClient("test", Path(directory), SourceDiagnostics("test"))
            with self.assertRaisesRegex(HttpFailure, "HTTPS"):
                client.get_json("http://example.org/api", {})

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = HttpClient(
                "test",
                Path(directory),
                SourceDiagnostics("test"),
                opener=lambda *_args, **_kwargs: FakeResponse(b'{"result": 1, "result": 2}'),
            )
            with self.assertRaisesRegex(HttpFailure, "malformed JSON"):
                client.get_json("https://example.org/api", {})


if __name__ == "__main__":
    unittest.main()
