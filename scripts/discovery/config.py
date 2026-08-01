"""Strict tracked configuration and exclusion loading."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode
from yaml.tokens import AliasToken, AnchorToken

from .normalize import normalize_doi, normalize_url


ALLOWED_SOURCES = frozenset({"openalex", "pubmed", "crossref", "arxiv"})
MAX_CONFIG_BYTES = 1024 * 1024


class StrictConfigLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: StrictConfigLoader, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise yaml.constructor.ConstructorError(None, None, "configuration keys must be strings", key_node.start_mark)
        if key in mapping:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate configuration key {key!r}", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


StrictConfigLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


@dataclass(frozen=True)
class QueryGroup:
    name: str
    phrases: tuple[str, ...]
    weight: int
    supporting_terms: tuple[str, ...]
    weight_per_supporting_term: int
    match_fields: tuple[str, ...]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveryConfig:
    version: int
    record_threshold: int
    max_candidates: int
    groups: tuple[QueryGroup, ...]
    digest: str

    def groups_for(self, source: str) -> tuple[QueryGroup, ...]:
        return tuple(group for group in self.groups if source in group.sources)


@dataclass(frozen=True)
class Exclusion:
    identifier: str
    reason: str
    decision_date: date
    title: str | None = None
    source: str | None = None


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a mapping with string keys")
    return value


def _strict_yaml(path: Path) -> Any:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError(f"configuration file exceeds {MAX_CONFIG_BYTES} bytes")
        text = raw.decode("utf-8")
        if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(text)):
            raise ValueError("configuration aliases and anchors are not allowed")
        value = yaml.load(text, Loader=StrictConfigLoader)
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise ValueError(f"cannot load {path}: {exc}") from exc
    return value


def load_queries(path: Path) -> DiscoveryConfig:
    raw_bytes = path.read_bytes()
    root = _mapping(_strict_yaml(path), "query configuration")
    allowed = {"version", "record_threshold", "max_candidates", "groups"}
    unknown = set(root) - allowed
    if unknown:
        raise ValueError(f"unknown query configuration keys: {', '.join(sorted(unknown))}")
    if root.get("version") != 1:
        raise ValueError("query configuration version must be 1")
    threshold = root.get("record_threshold")
    maximum = root.get("max_candidates", 500)
    if not isinstance(threshold, int) or threshold <= 0:
        raise ValueError("record_threshold must be a positive integer")
    if not isinstance(maximum, int) or not 1 <= maximum <= 5000:
        raise ValueError("max_candidates must be between 1 and 5000")
    groups: list[QueryGroup] = []
    seen: set[str] = set()
    if not isinstance(root.get("groups"), list) or not root["groups"]:
        raise ValueError("groups must be a non-empty list")
    for index, item in enumerate(root["groups"]):
        item = _mapping(item, f"groups[{index}]")
        group_allowed = {
            "name", "phrases", "weight", "supporting_terms",
            "weight_per_supporting_term", "match_fields", "sources",
        }
        if set(item) - group_allowed:
            raise ValueError(f"groups[{index}] contains unknown keys")
        name = item.get("name")
        phrases = item.get("phrases")
        sources = item.get("sources")
        if not isinstance(name, str) or not name or name in seen:
            raise ValueError(f"groups[{index}].name must be unique and non-empty")
        seen.add(name)
        if not isinstance(phrases, list) or not phrases or any(
            not isinstance(value, str) or not value.strip() for value in phrases
        ):
            raise ValueError(f"groups[{index}].phrases must contain non-empty strings")
        if len(set(phrases)) != len(phrases):
            raise ValueError(f"groups[{index}].phrases must be unique")
        if not isinstance(sources, list) or not sources or set(sources) - ALLOWED_SOURCES:
            raise ValueError(f"groups[{index}].sources contains an unsupported source")
        if len(set(sources)) != len(sources):
            raise ValueError(f"groups[{index}].sources must be unique")
        weight = item.get("weight")
        support_weight = item.get("weight_per_supporting_term", 0)
        if not isinstance(weight, int) or weight < 0 or not isinstance(support_weight, int) or support_weight < 0:
            raise ValueError(f"groups[{index}] weights must be non-negative integers")
        supporting = item.get("supporting_terms", [])
        match_fields = item.get("match_fields", ["title", "abstract"])
        if not isinstance(supporting, list) or any(not isinstance(value, str) for value in supporting):
            raise ValueError(f"groups[{index}].supporting_terms must be strings")
        if not isinstance(match_fields, list) or not match_fields or set(match_fields) - {"title", "abstract"}:
            raise ValueError(f"groups[{index}].match_fields is invalid")
        groups.append(
            QueryGroup(
                name,
                tuple(value.strip() for value in phrases),
                weight,
                tuple(value.strip() for value in supporting),
                support_weight,
                tuple(match_fields),
                tuple(sources),
            )
        )
    return DiscoveryConfig(1, threshold, maximum, tuple(groups), hashlib.sha256(raw_bytes).hexdigest())


def load_exclusions(path: Path) -> list[Exclusion]:
    root = _mapping(_strict_yaml(path), "exclusions")
    if set(root) - {"version", "exclusions"}:
        raise ValueError("exclusion file contains unknown top-level keys")
    if root.get("version") != 1 or not isinstance(root.get("exclusions"), list):
        raise ValueError("exclusion file must have version 1 and an exclusions list")
    exclusions: list[Exclusion] = []
    seen: set[str] = set()
    for index, raw in enumerate(root["exclusions"]):
        item = _mapping(raw, f"exclusions[{index}]")
        if set(item) - {"doi", "source_id", "url", "reason", "decision_date", "title", "source"}:
            raise ValueError(f"exclusions[{index}] contains unknown keys")
        identifiers = [key for key in ("doi", "source_id", "url") if item.get(key)]
        if len(identifiers) != 1:
            raise ValueError(f"exclusions[{index}] must contain exactly one stable identifier")
        kind = identifiers[0]
        raw_identifier = item[kind]
        if not isinstance(raw_identifier, str):
            raise ValueError(f"exclusions[{index}].{kind} must be a string")
        if kind == "doi":
            normalized = normalize_doi(raw_identifier)
            if not normalized:
                raise ValueError(f"exclusions[{index}].doi is malformed")
            identifier = f"doi:{normalized}"
        elif kind == "url":
            normalized_url = normalize_url(raw_identifier)
            if not normalized_url:
                raise ValueError(f"exclusions[{index}].url is malformed")
            identifier = f"url:{normalized_url}"
        else:
            source = item.get("source")
            if not isinstance(source, str) or not source.strip() or len(source) > 100:
                raise ValueError(f"exclusions[{index}] with source_id requires source")
            raw_identifier = raw_identifier.strip()
            if not raw_identifier or len(raw_identifier) > 2048:
                raise ValueError(f"exclusions[{index}].source_id must be 1-2048 characters")
            identifier = f"{source.casefold()}:{raw_identifier}"
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ValueError(f"exclusions[{index}].reason must be 1-500 characters")
        try:
            raw_decision = item.get("decision_date", "")
            decision = raw_decision if isinstance(raw_decision, date) else date.fromisoformat(raw_decision)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"exclusions[{index}].decision_date must use YYYY-MM-DD") from exc
        if identifier in seen:
            raise ValueError(f"duplicate exclusion identifier: {identifier}")
        seen.add(identifier)
        exclusions.append(Exclusion(identifier, reason.strip(), decision, item.get("title"), item.get("source")))
    return exclusions


def canonical_json_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
