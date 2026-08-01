"""Trusted GitHub Actions handoff and idempotent publication helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import yaml

from .config import canonical_json_digest
from .models import Candidate, DiscoveryReport
from .normalize import normalize_doi, normalize_title, normalize_url
from .http import strict_json_loads
from .sources import REQUIRED_DISCOVERY_SOURCES


MAX_HANDOFF_BYTES = 5 * 1024 * 1024
MAX_HANDOFF_CANDIDATES = 500
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
BATCH_RE = re.compile(r"^[0-9a-f]{64}$")
BRANCH_RE = re.compile(r"^automation/discovery-[0-9a-f]{16}$")
MARKER_RE = re.compile(r"<!-- pip-litdb-discovery:([A-Za-z0-9_-]{1,200000}) -->")


class AutomationFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenBatch:
    number: int
    branch: str
    batch_key: str
    candidate_identifiers: tuple[str, ...]
    reserved_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AutomationInventory:
    open_batches: tuple[OpenBatch, ...]
    orphan_branches: tuple[str, ...]

    @property
    def pending_identifiers(self) -> set[str]:
        return {
            identifier
            for batch in self.open_batches
            for identifier in batch.candidate_identifiers
        }


@dataclass(frozen=True)
class BranchInspection:
    branch: str
    batch_key: str
    candidate_identifiers: tuple[str, ...]
    generated_records: dict[str, str]


class GitHubApi:
    def __init__(self, repository: str, token: str, *, opener: Any = None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise AutomationFailure("GITHUB_REPOSITORY is malformed")
        if not token:
            raise AutomationFailure("a GitHub token is required")
        self.repository = repository
        self.token = token
        self.opener = opener or urlopen
        self.base = f"https://api.github.com/repos/{repository}"

    def request(self, method: str, path: str, payload: Any = None, *, accept: str = "application/vnd.github+json") -> Any:
        if not path.startswith("/"):
            raise AutomationFailure("GitHub API paths must be absolute")
        body = None
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "PIP-LitDB-discovery/1.0",
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            with self.opener(Request(self.base + path, data=body, headers=headers, method=method), timeout=30) as response:
                response_body = response.read(5 * 1024 * 1024 + 1)
                if len(response_body) > 5 * 1024 * 1024:
                    raise AutomationFailure("GitHub API response exceeded the size limit")
                return json.loads(response_body.decode("utf-8")) if response_body else None
        except HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            raise AutomationFailure(f"GitHub API {method} {path} failed with {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AutomationFailure(f"GitHub API {method} {path} failed: {exc}") from exc

    def get(self, path: str, *, accept: str = "application/vnd.github+json") -> Any:
        return self.request("GET", path, accept=accept)

    def get_paged(
        self,
        path: str,
        *,
        accept: str = "application/vnd.github+json",
        maximum_pages: int = 10,
    ) -> list[Any]:
        values: list[Any] = []
        for page in range(1, maximum_pages + 1):
            separator = "&" if "?" in path else "?"
            response = self.get(
                f"{path}{separator}per_page=100&page={page}", accept=accept
            )
            if not isinstance(response, list):
                raise AutomationFailure(f"paged GitHub response for {path} is malformed")
            values.extend(response)
            if len(response) < 100:
                return values
        raise AutomationFailure(f"paged GitHub response for {path} exceeds configured bounds")

    def post(self, path: str, payload: Any) -> Any:
        return self.request("POST", path, payload)


def _decode_marker(body: str | None) -> tuple[str, tuple[str, ...]] | None:
    match = MARKER_RE.search(body or "")
    if not match:
        return None
    try:
        padding = "=" * (-len(match.group(1)) % 4)
        decoded = base64.urlsafe_b64decode(match.group(1) + padding)
        value = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("format_version") != 1:
        return None
    batch_key = value.get("batch_key")
    identifiers = value.get("candidate_identifiers")
    if not isinstance(batch_key, str) or not BATCH_RE.fullmatch(batch_key):
        return None
    if not isinstance(identifiers, list) or len(identifiers) > MAX_HANDOFF_CANDIDATES or any(
        not isinstance(item, str) or not 1 <= len(item) <= 4096 for item in identifiers
    ):
        return None
    return batch_key, tuple(sorted(set(identifiers)))


def marker(batch_key: str, identifiers: Iterable[str]) -> str:
    value = {
        "format_version": 1,
        "batch_key": batch_key,
        "candidate_identifiers": sorted(set(identifiers)),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"<!-- pip-litdb-discovery:{encoded} -->"


def inventory(api: GitHubApi) -> AutomationInventory:
    pulls = api.get_paged("/pulls?state=open")
    open_batches: list[OpenBatch] = []
    open_branches: set[str] = set()
    for pull in pulls:
        if not isinstance(pull, dict):
            continue
        head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
        repository = head.get("repo") if isinstance(head.get("repo"), dict) else {}
        branch = head.get("ref")
        if repository.get("full_name") != api.repository or not isinstance(branch, str) or not BRANCH_RE.fullmatch(branch):
            continue
        labels = pull.get("labels") if isinstance(pull.get("labels"), list) else []
        if not any(
            isinstance(label, dict) and label.get("name") == "automated-discovery"
            for label in labels
        ):
            continue
        parsed = _decode_marker(pull.get("body"))
        if not parsed:
            raise AutomationFailure(f"automated discovery PR #{pull.get('number')} has no valid batch marker")
        batch_key, identifiers = parsed
        files = api.get_paged(f"/pulls/{int(pull['number'])}/files", maximum_pages=6)
        reserved_ids = tuple(
            sorted(
                match.group(1)
                for item in files
                if isinstance(item, dict)
                and isinstance(item.get("filename"), str)
                and (match := re.fullmatch(r"database/records/([0-9]{5})\.yaml", item["filename"]))
            )
        )
        open_batches.append(OpenBatch(int(pull["number"]), branch, batch_key, identifiers, reserved_ids))
        open_branches.add(branch)
    refs = api.get_paged("/git/matching-refs/heads/automation/discovery-")
    bot_branches = {
        str(item.get("ref", "")).removeprefix("refs/heads/")
        for item in refs
        if isinstance(item, dict)
        and BRANCH_RE.fullmatch(str(item.get("ref", "")).removeprefix("refs/heads/"))
    }
    return AutomationInventory(
        tuple(sorted(open_batches, key=lambda item: item.number)),
        tuple(sorted(bot_branches - open_branches)),
    )


def make_handoff(
    report: DiscoveryReport,
    *,
    base_sha: str,
    repository: str,
    default_branch: str,
    publication_allowed: bool,
    workflow_run_url: str,
    automation_inventory: AutomationInventory,
) -> dict[str, Any]:
    candidates = [
        candidate
        for candidate in report.candidates
        if candidate.disposition in {"new", "related_version"} and candidate.eligible_for_record
    ]
    identifiers = sorted(candidate.stable_identifier for candidate in candidates)
    if len(identifiers) != len(set(identifiers)):
        raise AutomationFailure("actionable candidates do not have unique stable identifiers")
    batch_material = {
        "date_from": report.date_from.isoformat(),
        "date_until": report.date_until.isoformat(),
        "config_digest": report.config_digest,
        "candidate_identifiers": identifiers,
    }
    batch_key = canonical_json_digest(batch_material)
    source_status = {
        item.source: {"complete": item.complete, "result_count": item.result_count}
        for item in report.source_diagnostics
        if item.source in REQUIRED_DISCOVERY_SOURCES
    }
    complete = set(source_status) == set(REQUIRED_DISCOVERY_SOURCES) and all(
        item["complete"] for item in source_status.values()
    )
    allowed = bool(publication_allowed and report.status in {"candidates", "no_candidates"} and complete)
    return {
        "format_version": 1,
        "repository": repository,
        "default_branch": default_branch,
        "base_sha": base_sha,
        "batch_key": batch_key,
        "config_digest": report.config_digest,
        "date_from": report.date_from.isoformat(),
        "date_until": report.date_until.isoformat(),
        "candidate_identifiers": identifiers,
        "reserved_ids": sorted(
            {
                record_id
                for batch in automation_inventory.open_batches
                for record_id in batch.reserved_ids
            }
        ),
        "candidates": [candidate.to_dict() for candidate in candidates],
        "required_sources": list(REQUIRED_DISCOVERY_SOURCES),
        "source_status": source_status,
        "publication_allowed": allowed,
        "workflow_run_url": workflow_run_url,
        "resume_prs": [
            {
                "number": batch.number,
                "branch": batch.branch,
                "batch_key": batch.batch_key,
                "candidate_identifiers": list(batch.candidate_identifiers),
            }
            for batch in automation_inventory.open_batches
        ],
        "orphan_branches": list(automation_inventory.orphan_branches),
    }


def _bounded_strings(values: Any, *, count: int, length: int, label: str) -> list[str]:
    if not isinstance(values, list) or len(values) > count or any(
        not isinstance(item, str) or not 1 <= len(item) <= length for item in values
    ):
        raise AutomationFailure(f"handoff {label} is malformed or exceeds bounds")
    return list(values)


CANDIDATE_KEYS = {
    "title", "authors", "doi", "url", "journal", "work_type", "document_type",
    "publication_stage", "dates", "canonical_date", "record_year", "discovered_by",
    "enriched_by", "evidence", "relationships", "provenance", "warnings", "score",
    "disposition", "matched_record_ids", "stable_identifier", "eligible_for_record",
}


def _optional_string(value: Any, maximum: int, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise AutomationFailure(f"handoff candidate {label} is invalid")
    return value


def _validate_candidate_payload(value: Any) -> Candidate:
    if not isinstance(value, dict) or set(value) != CANDIDATE_KEYS:
        raise AutomationFailure("handoff candidate keys do not match the trusted schema")
    _optional_string(value.get("title"), 2000, "title")
    for name, maximum in (("doi", 2048), ("url", 4096), ("journal", 500), ("work_type", 200)):
        _optional_string(value.get(name), maximum, name)
    if value.get("document_type") not in {"research_article", "review", "correction"}:
        raise AutomationFailure("handoff candidate document_type is invalid")
    if value.get("publication_stage") not in {"preprint", "publication"}:
        raise AutomationFailure("handoff candidate publication_stage is invalid")
    authors = value.get("authors")
    if not isinstance(authors, list) or not 1 <= len(authors) <= 200:
        raise AutomationFailure("handoff candidate author list is invalid")
    for author in authors:
        if not isinstance(author, dict) or not {"name"} <= set(author) <= {"name", "orcid"}:
            raise AutomationFailure("handoff candidate author is invalid")
        _optional_string(author.get("name"), 300, "author name")
        _optional_string(author.get("orcid"), 100, "author ORCID")
    dates = value.get("dates")
    if not isinstance(dates, list) or not 1 <= len(dates) <= 20:
        raise AutomationFailure("handoff candidate dates are invalid")
    for item in dates:
        if not isinstance(item, dict) or set(item) != {"value", "kind", "source"}:
            raise AutomationFailure("handoff candidate date entry is invalid")
        try:
            date.fromisoformat(item["value"])
        except (TypeError, ValueError) as exc:
            raise AutomationFailure("handoff candidate date value is invalid") from exc
        _optional_string(item.get("kind"), 100, "date kind")
        _optional_string(item.get("source"), 100, "date source")
    for mapping_name in ("discovered_by", "enriched_by"):
        mapping = value.get(mapping_name)
        if not isinstance(mapping, dict) or len(mapping) > 20 or any(
            not isinstance(key, str)
            or not 1 <= len(key) <= 100
            or not isinstance(item, str)
            or not 1 <= len(item) <= 2048
            for key, item in mapping.items()
        ):
            raise AutomationFailure(f"handoff candidate {mapping_name} is invalid")
    evidence = value.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > 100:
        raise AutomationFailure("handoff candidate evidence is invalid")
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"query", "field", "matched_text", "score"}:
            raise AutomationFailure("handoff candidate evidence entry is invalid")
        for name, maximum in (("query", 200), ("field", 100), ("matched_text", 240)):
            _optional_string(item.get(name), maximum, f"evidence {name}")
        if not isinstance(item.get("score"), int) or not 0 <= item["score"] <= 1000:
            raise AutomationFailure("handoff candidate evidence score is invalid")
    relationships = value.get("relationships")
    if not isinstance(relationships, list) or len(relationships) > 50:
        raise AutomationFailure("handoff candidate relationships are invalid")
    for item in relationships:
        if not isinstance(item, dict) or len(item) > 10 or any(
            not isinstance(key, str)
            or not isinstance(field_value, str)
            or not 1 <= len(key) <= 100
            or not 1 <= len(field_value) <= 2048
            for key, field_value in item.items()
        ):
            raise AutomationFailure("handoff candidate relationship entry is invalid")
    provenance = value.get("provenance")
    if not isinstance(provenance, dict) or len(provenance) > 50 or any(
        not isinstance(key, str) or not isinstance(entries, list) or len(entries) > 50
        for key, entries in provenance.items()
    ):
        raise AutomationFailure("handoff candidate provenance is invalid")
    warnings = _bounded_strings(value.get("warnings"), count=100, length=1000, label="candidate warnings")
    matched_ids = _bounded_strings(value.get("matched_record_ids"), count=100, length=5, label="matched record IDs")
    if any(not item.isdigit() for item in matched_ids):
        raise AutomationFailure("handoff candidate matched record IDs are invalid")
    if not isinstance(value.get("score"), int) or not 0 <= value["score"] <= 100_000:
        raise AutomationFailure("handoff candidate score is invalid")
    if value.get("disposition") not in {"new", "related_version"}:
        raise AutomationFailure("handoff candidate disposition is not publishable")
    if value.get("eligible_for_record") is not True:
        raise AutomationFailure("handoff candidate is not marked record-eligible")
    candidate = Candidate.from_dict(value)
    if candidate.stable_identifier != value.get("stable_identifier"):
        raise AutomationFailure("handoff candidate stable identifier does not recompute")
    canonical = candidate.canonical_date.isoformat() if candidate.canonical_date else None
    if canonical != value.get("canonical_date") or candidate.record_year != value.get("record_year"):
        raise AutomationFailure("handoff candidate derived date fields do not recompute")
    return candidate


def validate_handoff(value: Any, *, expected_repository: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("format_version") != 1:
        raise AutomationFailure("handoff format_version must be 1")
    required_keys = {
        "format_version", "repository", "default_branch", "base_sha", "batch_key",
        "config_digest", "date_from", "date_until", "candidate_identifiers", "candidates",
        "reserved_ids", "required_sources", "source_status", "publication_allowed", "workflow_run_url",
        "resume_prs", "orphan_branches",
    }
    if set(value) != required_keys:
        raise AutomationFailure("handoff keys do not exactly match the trusted schema")
    if expected_repository and value.get("repository") != expected_repository:
        raise AutomationFailure("handoff repository does not match GITHUB_REPOSITORY")
    if not isinstance(value.get("repository"), str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]
    ):
        raise AutomationFailure("handoff repository is invalid")
    if not isinstance(value.get("default_branch"), str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,255}", value["default_branch"]):
        raise AutomationFailure("handoff default branch is invalid")
    if not SHA_RE.fullmatch(str(value.get("base_sha"))) or not BATCH_RE.fullmatch(str(value.get("batch_key"))):
        raise AutomationFailure("handoff base SHA or batch key is invalid")
    if not DIGEST_RE.fullmatch(str(value.get("config_digest"))):
        raise AutomationFailure("handoff config digest is invalid")
    start = date.fromisoformat(str(value.get("date_from")))
    end = date.fromisoformat(str(value.get("date_until")))
    if end < start:
        raise AutomationFailure("handoff date range is reversed")
    identifiers = _bounded_strings(
        value.get("candidate_identifiers"), count=MAX_HANDOFF_CANDIDATES, length=4096, label="candidate identifiers"
    )
    if identifiers != sorted(set(identifiers)):
        raise AutomationFailure("handoff candidate identifiers are not sorted and unique")
    reserved_ids = _bounded_strings(value.get("reserved_ids"), count=1000, length=5, label="reserved IDs")
    if reserved_ids != sorted(set(reserved_ids)) or any(not item.isdigit() for item in reserved_ids):
        raise AutomationFailure("handoff reserved IDs are not sorted unique five-digit IDs")
    candidates = value.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > MAX_HANDOFF_CANDIDATES:
        raise AutomationFailure("handoff candidates are malformed or exceed bounds")
    parsed_candidates = [_validate_candidate_payload(item) for item in candidates]
    if [item.stable_identifier for item in parsed_candidates] != identifiers:
        raise AutomationFailure("handoff candidates do not match the candidate identifier set")
    required_sources = _bounded_strings(value.get("required_sources"), count=10, length=50, label="required sources")
    if tuple(required_sources) != REQUIRED_DISCOVERY_SOURCES:
        raise AutomationFailure("handoff required source set is invalid")
    status = value.get("source_status")
    if not isinstance(status, dict) or set(status) != set(REQUIRED_DISCOVERY_SOURCES):
        raise AutomationFailure("handoff source completeness evidence is invalid")
    all_complete = all(
        isinstance(item, dict)
        and set(item) == {"complete", "result_count"}
        and item.get("complete") is True
        and isinstance(item.get("result_count"), int)
        and 0 <= item["result_count"] <= 100_000
        for item in status.values()
    )
    if not isinstance(value.get("publication_allowed"), bool):
        raise AutomationFailure("handoff publication_allowed is not Boolean")
    if value["publication_allowed"] and not all_complete:
        raise AutomationFailure("handoff permits publication without complete required sources")
    if not isinstance(value.get("workflow_run_url"), str) or len(value["workflow_run_url"]) > 2048:
        raise AutomationFailure("handoff workflow run URL is invalid")
    resume = value.get("resume_prs")
    if not isinstance(resume, list) or len(resume) > 100:
        raise AutomationFailure("handoff resume PR list is invalid")
    for item in resume:
        if not isinstance(item, dict) or set(item) != {"number", "branch", "batch_key", "candidate_identifiers"}:
            raise AutomationFailure("handoff resume PR entry is invalid")
        if not isinstance(item["number"], int) or item["number"] <= 0 or not BRANCH_RE.fullmatch(item["branch"]):
            raise AutomationFailure("handoff resume PR identity is invalid")
        if not BATCH_RE.fullmatch(item["batch_key"]):
            raise AutomationFailure("handoff resume PR batch key is invalid")
        _bounded_strings(item["candidate_identifiers"], count=MAX_HANDOFF_CANDIDATES, length=4096, label="resume identifiers")
    orphans = _bounded_strings(value.get("orphan_branches"), count=100, length=255, label="orphan branches")
    if any(not BRANCH_RE.fullmatch(branch) for branch in orphans):
        raise AutomationFailure("handoff contains an invalid orphan branch")
    return value


def write_handoff(path: Path, value: dict[str, Any]) -> str:
    validate_handoff(value)
    encoded = (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_HANDOFF_BYTES:
        raise AutomationFailure("handoff exceeds the maximum size")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def read_handoff(path: Path, expected_checksum: str, *, expected_repository: str) -> dict[str, Any]:
    body = path.read_bytes()
    if len(body) > MAX_HANDOFF_BYTES:
        raise AutomationFailure("handoff exceeds the maximum size")
    if not DIGEST_RE.fullmatch(expected_checksum) or hashlib.sha256(body).hexdigest() != expected_checksum:
        raise AutomationFailure("handoff checksum mismatch")
    try:
        value = strict_json_loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AutomationFailure("handoff is not valid UTF-8 JSON") from exc
    return validate_handoff(value, expected_repository=expected_repository)


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", *arguments],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode:
        raise AutomationFailure(f"git {' '.join(arguments[:2])} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _escape_table(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ")[:500]


def pr_body(handoff: dict[str, Any], generated: dict[str, str]) -> str:
    candidates = [Candidate.from_dict(item) for item in handoff["candidates"]]
    id_by_identifier = {identifier: record_id for record_id, identifier in generated.items()}
    lines = [
        marker(handoff["batch_key"], handoff["candidate_identifiers"]),
        "",
        "## Automated PIP paper discovery",
        "",
        f"Searched `{handoff['date_from']}` through `{handoff['date_until']}` (inclusive).",
        f"Query configuration: `{handoff['config_digest']}`.",
        "",
        "| Proposed ID | Title | DOI | Stage | Date | Discovered by | Enriched by | Score |",
        "| --- | --- | --- | --- | --- | --- | --- | ---: |",
    ]
    for candidate in candidates:
        lines.append(
            f"| {id_by_identifier.get(candidate.stable_identifier, '')} | {_escape_table(candidate.title)} | "
            f"{_escape_table(candidate.doi)} | {_escape_table(candidate.publication_stage)} | "
            f"{candidate.canonical_date.isoformat() if candidate.canonical_date else ''} | "
            f"{_escape_table(', '.join(sorted(candidate.discovered_by)))} | "
            f"{_escape_table(', '.join(sorted(candidate.enriched_by)))} | {candidate.score} |"
        )
    warnings = [warning for candidate in candidates for warning in candidate.warnings]
    if warnings:
        lines.extend(["", "### Duplicate and relationship warnings", ""])
        lines.extend(f"- {_escape_table(item)}" for item in warnings)
    lines.extend(
        [
            "",
            "### Validation and review",
            "",
            "Generated records passed temporary-tree validation and a committed base/head run of the metadata validator before push. The trusted `Validate metadata records` status must pass on the current PR merge commit before review is requested.",
            "",
            "To accept a paper, verify and edit its metadata as needed. To reject it permanently, add its exact stable identifier and a human reason to `discovery/exclusions.yaml`, then remove only that proposed record; remaining IDs do not need renumbering.",
            "",
            f"[Workflow run and report artifact]({handoff['workflow_run_url']})",
            "",
        ]
    )
    return "\n".join(lines)


def _find_open_pr(api: GitHubApi, branch: str) -> dict[str, Any] | None:
    owner = api.repository.split("/", 1)[0]
    pulls = api.get(f"/pulls?state=open&head={quote(owner + ':' + branch)}&per_page=10")
    if not isinstance(pulls, list):
        raise AutomationFailure("open PR lookup returned malformed data")
    return pulls[0] if pulls else None


def _create_pr(api: GitHubApi, branch: str, base: str, title: str, body: str) -> dict[str, Any]:
    pull = api.post(
        "/pulls",
        {"title": title, "head": branch, "base": base, "body": body, "draft": True},
    )
    if not isinstance(pull, dict) or not isinstance(pull.get("number"), int):
        raise AutomationFailure("created PR response is malformed")
    return pull


def _timeline_has_review_request(api: GitHubApi, number: int, reviewer: str) -> bool:
    events = api.get_paged(
        f"/issues/{number}/timeline",
        accept="application/vnd.github+json",
    )
    return any(
        isinstance(event, dict)
        and event.get("event") == "review_requested"
        and isinstance(event.get("requested_reviewer"), dict)
        and str(event["requested_reviewer"].get("login", "")).casefold() == reviewer.casefold()
        for event in events
    )


def _wait_for_trusted_status(
    api: GitHubApi,
    number: int,
    *,
    context: str = "Validate metadata records",
    attempts: int = 60,
    interval: float = 15.0,
) -> None:
    observed_sha: str | None = None
    for _ in range(attempts):
        pull = api.get(f"/pulls/{number}")
        if not isinstance(pull, dict):
            raise AutomationFailure("PR response is malformed while polling validation")
        merge_sha = pull.get("merge_commit_sha")
        if not isinstance(merge_sha, str) or not SHA_RE.fullmatch(merge_sha):
            time.sleep(interval)
            continue
        observed_sha = merge_sha
        combined = api.get(f"/commits/{merge_sha}/status")
        statuses = combined.get("statuses") if isinstance(combined, dict) else None
        if not isinstance(statuses, list):
            raise AutomationFailure("combined status response is malformed")
        matches = [item for item in statuses if isinstance(item, dict) and item.get("context") == context]
        if matches:
            state = matches[0].get("state")
            if state == "success":
                refreshed = api.get(f"/pulls/{number}")
                if isinstance(refreshed, dict) and refreshed.get("merge_commit_sha") == merge_sha:
                    return
            if state in {"failure", "error"}:
                raise AutomationFailure(f"trusted metadata status {state} on merge commit {merge_sha}")
        time.sleep(interval)
    raise AutomationFailure(
        f"trusted metadata status did not succeed on the current merge commit"
        + (f" {observed_sha}" if observed_sha else "")
    )


def finish_pr(api: GitHubApi, number: int, *, reviewer: str = "7jameslondon") -> None:
    try:
        api.post(f"/issues/{number}/labels", {"labels": ["automated-discovery"]})
    except AutomationFailure as exc:
        if "422" not in str(exc):
            raise
        try:
            api.post(
                "/labels",
                {
                    "name": "automated-discovery",
                    "color": "1f6feb",
                    "description": "Candidate records proposed by the trusted paper-discovery workflow",
                },
            )
        except AutomationFailure as create_error:
            if "422" not in str(create_error):
                raise
        api.post(f"/issues/{number}/labels", {"labels": ["automated-discovery"]})
    _wait_for_trusted_status(api, number)
    if not _timeline_has_review_request(api, number, reviewer):
        api.post(f"/pulls/{number}/requested_reviewers", {"reviewers": [reviewer]})


def inspect_bot_branch(root: Path, branch: str, base_sha: str) -> BranchInspection:
    if not BRANCH_RE.fullmatch(branch) or not SHA_RE.fullmatch(base_sha):
        raise AutomationFailure("bot branch or base SHA is invalid")
    _git(root, "fetch", "--no-tags", "origin", f"refs/heads/{branch}")
    message = _git(root, "show", "-s", "--format=%B", "FETCH_HEAD")
    trailers: dict[str, str] = {}
    for line in message.splitlines():
        match = re.fullmatch(r"(PIP-Discovery-[A-Za-z]+): (.+)", line)
        if match:
            if match.group(1) in trailers:
                raise AutomationFailure(f"bot branch {branch} repeats a durable trailer")
            trailers[match.group(1)] = match.group(2)
    expected_keys = {
        "PIP-Discovery-Format",
        "PIP-Discovery-Dates",
        "PIP-Discovery-Batch",
        "PIP-Discovery-Config",
        "PIP-Discovery-Candidates",
    }
    if set(trailers) != expected_keys or trailers["PIP-Discovery-Format"] != "1":
        raise AutomationFailure(f"bot branch {branch} has invalid durable commit trailers")
    batch_key = trailers["PIP-Discovery-Batch"]
    if not BATCH_RE.fullmatch(batch_key) or not branch.endswith(batch_key[:16]):
        raise AutomationFailure(f"bot branch {branch} does not match its durable batch key")
    if not DIGEST_RE.fullmatch(trailers["PIP-Discovery-Config"]) or not DIGEST_RE.fullmatch(
        trailers["PIP-Discovery-Candidates"]
    ):
        raise AutomationFailure(f"bot branch {branch} has malformed durable digests")
    dates = trailers["PIP-Discovery-Dates"].split("..", 1)
    if len(dates) != 2:
        raise AutomationFailure(f"bot branch {branch} has malformed durable dates")
    try:
        if date.fromisoformat(dates[1]) < date.fromisoformat(dates[0]):
            raise ValueError
    except ValueError as exc:
        raise AutomationFailure(f"bot branch {branch} has invalid durable dates") from exc
    merge_base = _git(root, "merge-base", base_sha, "FETCH_HEAD")
    changes = _git(root, "diff", "--name-status", f"{merge_base}..FETCH_HEAD", "--", "database/records")
    generated: dict[str, str] = {}
    changed_count = 0
    for line in changes.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 2 or parts[0] not in {"A", "M"}:
            raise AutomationFailure(f"bot branch {branch} has an unsupported record diff")
        status, path = parts
        match = re.fullmatch(r"database/records/([0-9]{5})\.yaml", path)
        if not match:
            raise AutomationFailure(f"bot branch {branch} changes an unexpected path")
        changed_count += 1
        if status != "A":
            continue
        raw = _git(root, "show", f"FETCH_HEAD:{path}")
        try:
            record = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise AutomationFailure(f"bot branch {branch} contains malformed record data") from exc
        if not isinstance(record, dict):
            raise AutomationFailure(f"bot branch {branch} contains non-mapping record data")
        doi = normalize_doi(record.get("doi"))
        url = normalize_url(record.get("url"))
        title = normalize_title(record.get("title"))
        identifier = f"doi:{doi}" if doi else f"url:{url}" if url else f"title:{title}"
        if not title or len(identifier) > 4096:
            raise AutomationFailure(f"bot branch {branch} contains invalid record identity data")
        generated[match.group(1)] = identifier
    if not generated or changed_count > MAX_HANDOFF_CANDIDATES * 2:
        raise AutomationFailure(f"bot branch {branch} has no bounded generated record set")
    identifiers = tuple(sorted(generated.values()))
    if canonical_json_digest(list(identifiers)) != trailers["PIP-Discovery-Candidates"]:
        raise AutomationFailure(f"bot branch {branch} candidate digest does not match its record data")
    return BranchInspection(branch, batch_key, identifiers, generated)


def publish_handoff(
    root: Path,
    handoff: dict[str, Any],
    api: GitHubApi,
    *,
    reviewer: str = "7jameslondon",
) -> list[int]:
    from .records import changed_record_paths, write_records

    if not handoff["publication_allowed"]:
        raise AutomationFailure("handoff does not permit publication")
    checked_out = _git(root, "rev-parse", "HEAD")
    if checked_out != handoff["base_sha"]:
        raise AutomationFailure("checked-out trusted publisher SHA does not match handoff base SHA")
    handled: list[int] = []
    for item in handoff["resume_prs"]:
        inspected = inspect_bot_branch(root, item["branch"], handoff["base_sha"])
        if inspected.batch_key != item["batch_key"] or inspected.candidate_identifiers != tuple(item["candidate_identifiers"]):
            raise AutomationFailure(f"resume PR #{item['number']} does not match its validated branch data")
        finish_pr(api, item["number"], reviewer=reviewer)
        handled.append(item["number"])
    for branch in handoff["orphan_branches"]:
        inspected = inspect_bot_branch(root, branch, handoff["base_sha"])
        pull = _find_open_pr(api, branch)
        if pull is None:
            pull = _create_pr(
                api,
                branch,
                handoff["default_branch"],
                f"Resume automated paper discovery batch {branch.rsplit('-', 1)[-1]}",
                marker(inspected.batch_key, inspected.candidate_identifiers)
                + "\n\nThis durable batch branch was recovered after an interrupted workflow. Inspect its record diff and workflow artifacts before review.",
            )
        finish_pr(api, int(pull["number"]), reviewer=reviewer)
        handled.append(int(pull["number"]))
    if not handoff["candidates"]:
        return handled
    branch = f"automation/discovery-{handoff['batch_key'][:16]}"
    pull = _find_open_pr(api, branch)
    if pull is not None:
        finish_pr(api, int(pull["number"]), reviewer=reviewer)
        handled.append(int(pull["number"]))
        return handled
    remote_ref = _git(root, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    if remote_ref:
        inspected = inspect_bot_branch(root, branch, handoff["base_sha"])
        if inspected.batch_key != handoff["batch_key"] or list(inspected.candidate_identifiers) != handoff["candidate_identifiers"]:
            raise AutomationFailure("existing deterministic branch does not match the current handoff")
        pull = _create_pr(
            api,
            branch,
            handoff["default_branch"],
            f"Add automatically discovered PIP papers ({handoff['date_until']})",
            pr_body(handoff, inspected.generated_records),
        )
    else:
        candidates = [Candidate.from_dict(item) for item in handoff["candidates"]]
        generated = write_records(
            root,
            candidates,
            base=handoff["base_sha"],
            reserved_ids=handoff["reserved_ids"],
        )
        paths = changed_record_paths(root)
        if not paths:
            return handled
        _git(root, "add", "--", *paths)
        staged = _git(root, "diff", "--cached", "--name-only").splitlines()
        if sorted(staged) != sorted(paths) or any(not re.fullmatch(r"database/records/[0-9]{5}\.yaml", path) for path in staged):
            raise AutomationFailure("publisher staged an unexpected path")
        message = (
            "Add automatically discovered PIP papers\n\n"
            "PIP-Discovery-Format: 1\n"
            f"PIP-Discovery-Dates: {handoff['date_from']}..{handoff['date_until']}\n"
            f"PIP-Discovery-Batch: {handoff['batch_key']}\n"
            f"PIP-Discovery-Config: {handoff['config_digest']}\n"
            f"PIP-Discovery-Candidates: {canonical_json_digest(handoff['candidate_identifiers'])}"
        )
        _git(root, "-c", "user.name=PIP LitDB Discovery", "-c", "user.email=noreply@pip-litdb.org", "commit", "-m", message)
        _git(root, "fetch", "origin", handoff["default_branch"])
        latest = _git(root, "rev-parse", "FETCH_HEAD")
        if latest != handoff["base_sha"]:
            raise AutomationFailure("default branch moved after validation; publish nothing and retry")
        _git(root, "push", "origin", f"HEAD:refs/heads/{branch}")
        pull = _create_pr(
            api,
            branch,
            handoff["default_branch"],
            f"Add automatically discovered PIP papers ({handoff['date_until']})",
            pr_body(handoff, generated),
        )
    finish_pr(api, int(pull["number"]), reviewer=reviewer)
    handled.append(int(pull["number"]))
    return handled
