"""Strict configuration and explicit, repeatable search windows."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path

import yaml


SOURCES = ("europe_pmc", "crossref", "biorxiv")
FEATURE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = FEATURE_ROOT.parent


class DiscoveryError(ValueError):
    """An actionable configuration, state or source error."""


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise DiscoveryError("YAML keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def read_yaml(path: Path):
    try:
        return yaml.load(path.read_text(encoding="utf-8-sig"), Loader=UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise DiscoveryError(f"Cannot read YAML {path}: {exc}") from exc


def _keys(value, allowed, label):
    if not isinstance(value, dict):
        raise DiscoveryError(f"{label} must be a mapping")
    extra = set(value) - set(allowed)
    if extra:
        raise DiscoveryError(f"Unknown {label} keys: {', '.join(sorted(extra))}")


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise DiscoveryError(f"{label} must be an integer from {low} to {high}")


def load_config(path: Path) -> dict:
    config = read_yaml(path)
    _keys(config, {"version", "match_rules", "http", "sources", "filters"}, "configuration")
    if type(config.get("version")) is not int or config["version"] != 1:
        raise DiscoveryError("Configuration version must be 1")
    filters = config.setdefault("filters", {})
    _keys(filters, {"exclude_attached_material"}, "filters")
    filters.setdefault("exclude_attached_material", True)
    if type(filters["exclude_attached_material"]) is not bool:
        raise DiscoveryError("exclude_attached_material must be true or false")
    rules = config.get("match_rules")
    if not isinstance(rules, list) or not rules:
        raise DiscoveryError("match_rules must be a nonempty list")
    names = set()
    for rule in rules:
        _keys(rule, {"name", "all", "unless_title"}, "match rule")
        name, terms = rule.get("name"), rule.get("all")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise DiscoveryError("Match rule names must be unique, nonempty strings")
        names.add(name)
        if not isinstance(terms, list) or not terms or any(
            not isinstance(term, str) or not any(char.isalnum() for char in term) for term in terms
        ):
            raise DiscoveryError(f"Rule {name} needs nonempty text terms in all")
        excluded = rule.get("unless_title", [])
        if not isinstance(excluded, list) or any(
            not isinstance(term, str) or not any(char.isalnum() for char in term) for term in excluded
        ):
            raise DiscoveryError(f"Rule {name} unless_title must be a list of nonempty text terms")
    http = config.setdefault("http", {})
    _keys(http, {"timeout_seconds", "retries", "min_interval_seconds", "contact_email"}, "http")
    http.setdefault("timeout_seconds", 30)
    http.setdefault("retries", 3)
    http.setdefault("min_interval_seconds", 1.2)
    http.setdefault("contact_email", "")
    _integer(http["timeout_seconds"], 1, 120, "timeout_seconds")
    _integer(http["retries"], 0, 5, "retries")
    interval = http["min_interval_seconds"]
    if type(interval) not in (int, float) or not 1 <= interval <= 60:
        raise DiscoveryError("min_interval_seconds must be between 1 and 60")
    contact = http["contact_email"]
    if not isinstance(contact, str) or any(c in contact for c in "\r\n"):
        raise DiscoveryError("contact_email must be a single-line string")
    sources = config.get("sources")
    _keys(sources, SOURCES, "sources")
    if not sources:
        raise DiscoveryError("Configure at least one source")
    for name, source in sources.items():
        common = {"enabled", "lookback_years", "max_pages"}
        allowed = common | ({"category"} if name == "biorxiv" else {"queries", "page_size"})
        _keys(source, allowed, name)
        source.setdefault("enabled", True)
        if type(source["enabled"]) is not bool:
            raise DiscoveryError(f"{name}.enabled must be true or false")
        source.setdefault("lookback_years", 5)
        years = source["lookback_years"]
        if years is not None:
            _integer(years, 1, 200, f"{name}.lookback_years")
        source.setdefault("max_pages", 100)
        _integer(source["max_pages"], 1, 100000, f"{name}.max_pages")
        if name == "biorxiv":
            source.setdefault("category", "")
            if not isinstance(source["category"], str):
                raise DiscoveryError("biorxiv.category must be a string")
        else:
            source.setdefault("page_size", 250)
            _integer(source["page_size"], 1, 1000, f"{name}.page_size")
            queries = source.get("queries")
            if not isinstance(queries, list) or not queries or any(
                not isinstance(query, str) or not query.strip() for query in queries
            ) or len(set(queries)) != len(queries):
                raise DiscoveryError(f"{name}.queries must contain unique nonempty strings")
    return config


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def search_plan(config: dict, *, selected=None, since=None, until=None, max_pages=None) -> list[dict]:
    end = date.fromisoformat(until) if until else date.today()
    start_override = date.fromisoformat(since) if since else None
    if start_override and start_override > end:
        raise DiscoveryError("--since must be on or before --until")
    if max_pages is not None:
        _integer(max_pages, 1, 100000, "max_pages")
    names = selected or [name for name, spec in config["sources"].items() if spec["enabled"]]
    if not names or len(set(names)) != len(names):
        raise DiscoveryError("Select at least one source, without duplicates")
    result = []
    for name in names:
        if name not in config["sources"]:
            raise DiscoveryError(f"Source {name} is not configured")
        spec = deepcopy(config["sources"][name])
        years = spec["lookback_years"]
        start = start_override
        if start is None and years is not None:
            try:
                start = end.replace(year=end.year - years)
            except ValueError:  # leap day
                start = end.replace(year=end.year - years, day=28)
        if name == "biorxiv":
            start = max(start or date(2013, 1, 1), date(2013, 1, 1))
            if start > end:
                raise DiscoveryError("bioRxiv search must end in 2013 or later")
        spec.update(source=name, enabled=True, since=start.isoformat() if start else None, until=end.isoformat())
        if max_pages is not None:
            spec["max_pages"] = max_pages
        # Rolling 'today' is intentionally excluded so a partial feed scan can finish.
        spec["checkpoint_key"] = fingerprint({
            "source": name, "settings": {key: value for key, value in config["sources"][name].items()
                                          if key not in {"enabled", "max_pages", "page_size"}},
            "match_rules": config["match_rules"], "since": since, "until": until,
        })
        result.append(spec)
    return result
