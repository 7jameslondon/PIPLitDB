"""Bounded HTTP transport with retry diagnostics and deterministic disk caching."""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .models import SourceDiagnostics


MAX_RESPONSE_BYTES = 10 * 1024 * 1024
SENSITIVE_PARAMETERS = frozenset({"api_key", "key", "email", "mailto"})


class HttpFailure(RuntimeError):
    """A source request could not be completed safely."""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant {value}")


def strict_json_loads(value: str) -> Any:
    return json.loads(
        value,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )


@dataclass(frozen=True)
class CachedResponse:
    body: bytes
    content_type: str
    status: int


class HttpClient:
    def __init__(
        self,
        source: str,
        cache_dir: Path,
        diagnostics: SourceDiagnostics,
        *,
        offline: bool = False,
        user_agent: str = "PIP-LitDB-discovery/1.0",
        timeout: float = 30.0,
        max_requests: int = 100,
        max_retries: int = 3,
        opener: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        minimum_interval: float = 0.0,
    ) -> None:
        self.source = source
        self.cache_dir = cache_dir / source
        self.diagnostics = diagnostics
        self.offline = offline
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_requests = max_requests
        self.max_retries = max_retries
        self._opener = opener or urlopen
        self._sleep = sleeper
        self.minimum_interval = max(0.0, minimum_interval)
        self._last_request_at = 0.0

    @staticmethod
    def _cache_identity(url: str, params: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
        safe_params = {
            str(key): "[redacted]" if str(key).casefold() in SENSITIVE_PARAMETERS else str(value)
            for key, value in sorted(params.items())
            if value is not None
        }
        parsed = urlsplit(url)
        safe_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        encoded = json.dumps(
            {"url": safe_url, "params": safe_params},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest(), safe_params

    def _paths(self, key: str) -> tuple[Path, Path]:
        return self.cache_dir / f"{key}.body", self.cache_dir / f"{key}.json"

    def _load_cache(self, key: str) -> CachedResponse | None:
        body_path, metadata_path = self._paths(key)
        if not body_path.is_file() or not metadata_path.is_file():
            return None
        try:
            body = body_path.read_bytes()
            metadata = strict_json_loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise HttpFailure(f"invalid cached response {key}: {exc}") from exc
        if len(body) > MAX_RESPONSE_BYTES:
            raise HttpFailure(f"cached response {key} exceeds the size limit")
        if hashlib.sha256(body).hexdigest() != metadata.get("sha256"):
            raise HttpFailure(f"cached response {key} checksum mismatch")
        self.diagnostics.cache_hits += 1
        return CachedResponse(body, str(metadata.get("content_type") or ""), int(metadata.get("status", 200)))

    def _store_cache(
        self,
        key: str,
        safe_url: str,
        safe_params: Mapping[str, str],
        response: CachedResponse,
    ) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        body_path, metadata_path = self._paths(key)
        body_path.write_bytes(response.body)
        metadata = {
            "format_version": 1,
            "source": self.source,
            "url": safe_url,
            "params": safe_params,
            "status": response.status,
            "content_type": response.content_type,
            "sha256": hashlib.sha256(response.body).hexdigest(),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def get(
        self,
        url: str,
        params: Mapping[str, Any],
        *,
        accept: str,
        allowed_content_types: tuple[str, ...],
    ) -> bytes:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise HttpFailure("source endpoints must be public HTTPS URLs")
        key, safe_params = self._cache_identity(url, params)
        cached = self._load_cache(key)
        if cached is not None:
            return cached.body
        if self.offline:
            raise HttpFailure(f"offline cache miss for {self.source} request {key}")
        if self.diagnostics.request_count >= self.max_requests:
            raise HttpFailure(f"{self.source} request limit exceeded")
        query = urlencode([(key, value) for key, value in params.items() if value is not None])
        request_url = f"{url}?{query}" if query else url
        safe_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        headers = {"Accept": accept, "User-Agent": self.user_agent}
        last_error = "unknown transport error"
        for attempt in range(self.max_retries + 1):
            elapsed = time.monotonic() - self._last_request_at
            if self._last_request_at and elapsed < self.minimum_interval:
                self._sleep(self.minimum_interval - elapsed)
            self.diagnostics.request_count += 1
            try:
                self._last_request_at = time.monotonic()
                with self._opener(Request(request_url, headers=headers), timeout=self.timeout) as opened:
                    status = int(getattr(opened, "status", 200))
                    content_type = str(opened.headers.get("Content-Type", "")).split(";", 1)[0].casefold()
                    if not any(content_type == item or content_type.endswith(f"+{item.split('/')[-1]}") for item in allowed_content_types):
                        raise HttpFailure(
                            f"{self.source} returned unsupported content type {content_type!r}"
                        )
                    body = opened.read(MAX_RESPONSE_BYTES + 1)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise HttpFailure(f"{self.source} response exceeds {MAX_RESPONSE_BYTES} bytes")
                    response = CachedResponse(body, content_type, status)
                    self._store_cache(key, safe_url, safe_params, response)
                    return body
            except HTTPError as exc:
                last_error = f"HTTP {exc.code}"
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if not retryable or attempt >= self.max_retries:
                    break
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    delay = min(float(retry_after), 60.0) if retry_after else 0.0
                except ValueError:
                    delay = 0.0
            except (TimeoutError, URLError) as exc:
                last_error = type(exc).__name__
                if attempt >= self.max_retries:
                    break
                delay = 0.0
            if attempt < self.max_retries:
                self.diagnostics.retries += 1
                self._sleep(delay or min(0.5 * (2**attempt) + random.random() * 0.25, 8.0))
        raise HttpFailure(f"{self.source} request failed after retries: {last_error}")

    def get_json(self, url: str, params: Mapping[str, Any]) -> Any:
        body = self.get(
            url,
            params,
            accept="application/json",
            allowed_content_types=("application/json", "text/json"),
        )
        try:
            return strict_json_loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise HttpFailure(f"{self.source} returned malformed JSON") from exc

    def get_xml(self, url: str, params: Mapping[str, Any]) -> bytes:
        return self.get(
            url,
            params,
            accept="application/xml,text/xml,application/atom+xml",
            allowed_content_types=("application/xml", "text/xml", "application/atom+xml"),
        )
