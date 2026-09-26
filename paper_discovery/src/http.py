"""Small serial JSON client with bounded retries and respectful throttling."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import DiscoveryError


class JsonClient:
    def __init__(self, settings, *, opener=urlopen, sleep=time.sleep, clock=time.monotonic):
        self.settings = settings
        self.opener, self.sleep, self.clock = opener, sleep, clock
        self.last_request = None
        self.requests = 0

    def get(self, url):
        contact = self.settings.get("contact_email", "")
        agent = "PIP-LitDB-paper-discovery/1.0" + (f" (mailto:{contact})" if contact else "")
        error = ""
        for attempt in range(self.settings["retries"] + 1):
            if self.last_request is not None:
                wait = self.settings["min_interval_seconds"] - (self.clock() - self.last_request)
                if wait > 0:
                    self.sleep(wait)
            self.last_request = self.clock()
            self.requests += 1
            delay = min(2 ** attempt, 30)
            try:
                request = Request(url, headers={"Accept": "application/json", "User-Agent": agent})
                with self.opener(request, timeout=self.settings["timeout_seconds"]) as response:
                    body = response.read(32 * 1024 * 1024 + 1)
                    status = getattr(response, "status", "unknown")
                    headers = getattr(response, "headers", {}) or {}
                    content_type = headers.get("Content-Type", "unknown")
                if len(body) > 32 * 1024 * 1024:
                    raise DiscoveryError("API response exceeded 32 MiB; reduce page_size")
                if not body.strip():
                    error = (f"Empty API response from {url} (HTTP {status}; "
                             f"Content-Type {content_type}; received {len(body)} bytes); "
                             "search coverage is unknown")
                else:
                    result = json.loads(body)
                    if not isinstance(result, dict):
                        raise DiscoveryError("API response must be a JSON object")
                    return result
            except HTTPError as exc:
                error = f"HTTP {exc.code} from {url}"
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise DiscoveryError(error) from exc
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        try:
                            delay = max(delay, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError):
                            pass
                if delay > 60:
                    raise DiscoveryError(error + f"; service requests retry after {delay:.0f} seconds") from exc
            except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
                error = f"Invalid or unavailable API response from {url}: {exc}"
            if attempt < self.settings["retries"]:
                self.sleep(delay)
        attempts = self.settings["retries"] + 1
        raise DiscoveryError(f"{error}; failed after {attempts} attempt{'s' if attempts != 1 else ''}")
