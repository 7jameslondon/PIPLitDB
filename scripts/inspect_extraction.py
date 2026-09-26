#!/usr/bin/env python3
"""Read local extraction evidence in bounded, explicitly paginated views.

This helper never writes files or changes extraction content. JSON selections
retain the original source spelling, including number precision and escapes.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterator


DEFAULT_OUTPUT_CHARS = 12_000
MAX_OUTPUT_CHARS = 32_000
MAX_BATCH_REQUESTS = 100


class OutputBudgetError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON keys make pointers ambiguous; use a raw read")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-standard JSON numeric constant; use a raw read")


# Numbers are used only to locate source spans, never rounded or rewritten.
_JSON = json.JSONDecoder(
    parse_float=str, parse_int=str,
    parse_constant=_reject_constant, object_pairs_hook=_unique_object,
)


@dataclass(frozen=True)
class Span:
    start: int
    end: int


@dataclass(frozen=True)
class Source:
    path: Path
    raw: bytes
    sha256: str


def _load_source(path: Path) -> Source:
    path = path.resolve(strict=True)
    raw = path.read_bytes()
    return Source(path, raw, hashlib.sha256(raw).hexdigest())


def _skip_space(text: str, start: int) -> int:
    while start < len(text) and text[start] in " \t\r\n":
        start += 1
    return start


def _root(text: str) -> Span:
    start = _skip_space(text, 0)
    _, end = _JSON.raw_decode(text, start)
    if text[end:].strip(" \t\r\n"):
        raise ValueError("content follows the JSON value; use a raw read for JSONL")
    return Span(start, end)


def _kind(text: str, span: Span) -> str:
    first = text[span.start]
    return {"{": "object", "[": "array", '"': "string",
            "t": "boolean", "f": "boolean", "n": "null"}.get(first, "number")


def _children(text: str, span: Span) -> Iterator[tuple[str, Span]]:
    kind = _kind(text, span)
    if kind not in {"object", "array"}:
        raise ValueError("JSON selection is a scalar; use read to inspect its content")
    position = _skip_space(text, span.start + 1)
    index = 0
    while position < span.end - 1:
        if kind == "object":
            key, position = _JSON.raw_decode(text, position)
            position = _skip_space(text, position)
            position = _skip_space(text, position + 1)  # validated colon
        else:
            key = str(index)
        _, end = _JSON.raw_decode(text, position)
        yield key, Span(position, end)
        index += 1
        position = _skip_space(text, end)
        if position < span.end - 1:
            position = _skip_space(text, position + 1)  # validated comma


def _pointer_token(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _resolve(text: str, pointer: str) -> Span:
    span = _root(text)
    if not pointer:
        return span
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ValueError("use a JSON Pointer starting with /; escape ~ as ~0 and / as ~1")
    for encoded in pointer[1:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if _kind(text, span) == "array" and not re.fullmatch(r"0|[1-9][0-9]*", token):
            raise ValueError("array pointers require a zero-based integer index")
        match = next((child for key, child in _children(text, span) if key == token), None)
        if match is None:
            raise ValueError("JSON Pointer does not exist in this file")
        span = match
    return span


def _preview(text: str, span: Span) -> dict[str, Any]:
    """Supply navigation labels only; never stand in for a content read."""
    if _kind(text, span) != "object":
        return {}
    value = _JSON.raw_decode(text, span.start)[0]
    for key in ("heading", "title", "label", "section_id", "block_id", "asset_id", "path"):
        label = value.get(key)
        if isinstance(label, dict):
            label = label.get("plain_text")
        if isinstance(label, str) and label:
            return {"preview": label[:120], "preview_truncated": len(label) > 120}
    return {}


def _serialized(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _fits(result: dict[str, Any], budget: int) -> bool:
    return len(_serialized(result)) <= budget


def _read_page(base: dict[str, Any], view: str, start: int, budget: int, fits=_fits) -> dict[str, Any]:
    if start > len(view):
        raise ValueError("start exceeds the selected view's character count")

    def page(size: int) -> dict[str, Any]:
        end = start + size
        return {**base, "range_unit": "decoded_unicode_characters",
                "start": start, "end": end, "total_chars": len(view),
                "next_start": end if end < len(view) else None,
                "content": view[start:end]}

    low, high = 0, min(budget, len(view) - start)
    if not fits(page(0), budget):
        raise OutputBudgetError("output metadata exceeds the budget; increase --max-output-chars")
    # Completing a view removes its continuation metadata, so check the full
    # remainder before searching for a smaller page.
    if fits(page(high), budget):
        return page(high)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(page(middle), budget):
            low = middle
        else:
            high = middle - 1
    if low == 0 and start < len(view):
        raise OutputBudgetError("no content fits the budget; increase --max-output-chars")
    return page(low)


def _outline_page(
    base: dict[str, Any], text: str, span: Span, pointer: str,
    start: int, limit: int, budget: int, fits=_fits,
) -> dict[str, Any]:
    children = list(_children(text, span))
    if start > len(children):
        raise ValueError("start exceeds the number of children in this selection")
    result = {**base, "range_unit": "immediate_children", "start": start,
              "total_children": len(children), "next_start": None, "entries": []}
    for index in range(start, len(children)):
        key, child = children[index]
        if len(result["entries"]) == limit:
            result["next_start"] = index
            break
        entry = {"pointer": pointer + "/" + _pointer_token(key),
                 "type": _kind(text, child), "source_start": child.start,
                 "source_end": child.end, "chars": child.end - child.start,
                 **_preview(text, child)}
        trial = {**result, "entries": [*result["entries"], entry],
                 "next_start": index + 1 if index + 1 < len(children) else None}
        if not fits(trial, budget):
            result["next_start"] = index
            break
        result = trial
    if not result["entries"] and start < len(children):
        raise OutputBudgetError("one outline entry exceeds the budget; use a bounded raw read")
    return result


def _find_page(
    base: dict[str, Any], view: str, query: str, start: int,
    context: int, limit: int, budget: int, fits=_fits,
) -> dict[str, Any]:
    if start > len(view):
        raise ValueError("start exceeds the selected view's character count")
    result = {**base, "range_unit": "decoded_unicode_characters",
              "query": query, "case_sensitive": True, "start": start,
              "total_chars": len(view), "next_start": None, "matches": []}
    position = view.find(query, start)
    while position != -1:
        if len(result["matches"]) == limit:
            result["next_start"] = position
            break
        left = max(0, position - context)
        right = min(len(view), position + len(query) + context)
        entry = {"match_start": position, "match_end": position + len(query),
                 "excerpt_start": left, "excerpt_end": right,
                 "excerpt": view[left:right]}
        trial = {**result, "matches": [*result["matches"], entry],
                 "next_start": position + 1}
        if not fits(trial, budget):
            result["next_start"] = position
            break
        result = trial
        # Advancing by one retains overlapping occurrences as well.
        position = view.find(query, position + 1)
    else:
        result["next_start"] = None
    if not result["matches"] and result["next_start"] is not None:
        raise OutputBudgetError("one match exceeds the budget; reduce --context or increase the budget")
    return result


def _bounded_int(minimum: int, maximum: int):
    def parse(value: str) -> int:
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"must be between {minimum} and {maximum}")
        return number
    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("stat", "outline", "read", "find"):
        sub = commands.add_parser(name)
        sub.add_argument("path", type=Path)
        sub.add_argument("--encoding", default="utf-8-sig",
                         help="strict source decoding; defaults to UTF-8 with optional BOM")
        sub.add_argument("--expect-sha256", help="refuse a changed source between reads")
        sub.add_argument("--max-output-chars", type=_bounded_int(2_000, MAX_OUTPUT_CHARS),
                         default=DEFAULT_OUTPUT_CHARS,
                         help="bound the entire JSON response, including metadata and escapes")
        if name != "stat":
            sub.add_argument("--if-view-token",
                             help="omit an identical view already inspected by this agent; use its returned view_token")
            sub.add_argument("--pointer", default="" if name == "outline" else None,
                             help="select an exact JSON value using JSON Pointer")
            sub.add_argument("--start", type=_bounded_int(0, sys.maxsize), default=0,
                             help="resume at next_start from the preceding response")
        if name in {"outline", "find"}:
            sub.add_argument("--limit", type=_bounded_int(1, 100), default=20)
        if name == "find":
            sub.add_argument("query", help="case-sensitive literal text, never a regex")
            sub.add_argument("--context", type=_bounded_int(0, 500), default=160)
    batch = commands.add_parser("batch", help="read a JSON request list under one shared output limit")
    batch.add_argument("path", type=Path, help="JSON array of inspection requests; paths are relative to the working directory")
    batch.add_argument("--cursor", help="resume with the exact next_cursor from the previous response")
    batch.add_argument("--max-output-chars", type=_bounded_int(2_000, MAX_OUTPUT_CHARS),
                       default=DEFAULT_OUTPUT_CHARS, help="bound the complete batch response")
    return parser


def inspect(args: argparse.Namespace, *, source: Source | None = None,
            fits=_fits, view_context: str | None = None) -> dict[str, Any]:
    source = source or _load_source(args.path)
    path, raw, digest = source.path, source.raw, source.sha256
    if args.expect_sha256 and args.expect_sha256.lower() != digest:
        raise ValueError("source SHA-256 changed; refresh the outline and invalidate prior read coverage")
    base = {"path": str(path), "source_sha256": digest, "source_bytes": len(raw),
            "operation": args.command}
    if args.command == "stat":
        return base
    # A token binds the exact request, file, and reader implementation. A hash
    # from a different file, pointer, page, encoding, or search cannot skip it.
    request = {key: getattr(args, key, None) for key in (
        "command", "encoding", "pointer", "start", "limit", "query", "context", "max_output_chars",
    )}
    request.update({"path": str(path), "source_sha256": digest,
                    "reader_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    if view_context is not None:
        request["batch_view_context"] = view_context
    token = hashlib.sha256(json.dumps(request, sort_keys=True).encode("utf-8")).hexdigest()
    base["view_token"] = token
    if args.if_view_token:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", args.if_view_token):
            raise ValueError("--if-view-token requires a returned 64-character SHA-256 token")
        base["view_unchanged"] = args.if_view_token.lower() == token
        if base["view_unchanged"]:
            return {**base, "content_omitted": True,
                    "note": "Reuse only your retained, inspected view and its next_start. No new coverage or diagnostic freshness is established."}
    text = raw.decode(args.encoding, errors="strict")
    if "\x00" in text:
        raise ValueError("binary content is unsupported; use the appropriate visual or format reader")
    span = _resolve(text, args.pointer) if args.pointer is not None else Span(0, len(text))
    view = text[span.start:span.end]
    base.update({"encoding": args.encoding, "json_pointer": args.pointer,
                 "source_range": {"start": span.start, "end": span.end},
                 "representation": "original_json_value" if args.pointer is not None else "decoded_source_text"})
    if args.command == "read":
        return _read_page(base, view, args.start, args.max_output_chars, fits)
    if args.command == "outline":
        return _outline_page(base, text, span, args.pointer, args.start, args.limit, args.max_output_chars, fits)
    if not 1 <= len(args.query) <= 512:
        raise ValueError("query must contain between 1 and 512 characters")
    return _find_page(base, view, args.query, args.start, args.context, args.limit, args.max_output_chars, fits)


def _batch_request(value: Any, budget: int) -> argparse.Namespace:
    if (not isinstance(value, dict) or not isinstance(value.get("command"), str)
            or value["command"] not in {"stat", "read", "outline", "find"}):
        raise ValueError("each batch request needs a stat, read, outline, or find command")
    command = value["command"]
    allowed = {"command", "path", "encoding", "expect_sha256"}
    if command != "stat":
        allowed.update({"pointer", "start"})
    if command in {"outline", "find"}:
        allowed.add("limit")
    if command == "find":
        allowed.update({"query", "context"})
    if value.keys() - allowed:
        raise ValueError("unsupported batch request fields: " + ", ".join(sorted(value.keys() - allowed)))
    options = {"encoding": "utf-8-sig", "expect_sha256": None,
               "pointer": "" if command == "outline" else None, "start": 0,
               "limit": 20, "context": 160, "if_view_token": None,
               "max_output_chars": budget, **value}
    for key in ("path", "encoding"):
        if not isinstance(options.get(key), str) or not options[key]:
            raise ValueError(f"batch request {key} must be a nonempty string")
    if options["pointer"] is not None and not isinstance(options["pointer"], str):
        raise ValueError("batch pointer must be a string or null")
    if command == "outline" and options["pointer"] is None:
        raise ValueError("outline requires a JSON Pointer; use an empty string for the root")
    digest = options["expect_sha256"]
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
        raise ValueError("batch expect_sha256 must be a 64-character SHA-256")
    for key, low, high in (("start", 0, sys.maxsize), ("limit", 1, 100), ("context", 0, 500)):
        if type(options[key]) is not int or not low <= options[key] <= high:
            raise ValueError(f"batch {key} must be an integer between {low} and {high}")
    if command == "find" and (not isinstance(options.get("query"), str) or not 1 <= len(options["query"]) <= 512):
        raise ValueError("batch find query must contain between 1 and 512 characters")
    options["path"] = Path(options["path"])
    return argparse.Namespace(**options)


def _decode_cursor(token: str) -> dict[str, Any]:
    if len(token) > 512 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise ValueError("invalid batch cursor; use the returned next_cursor unchanged")
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        cursor = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid batch cursor; use the returned next_cursor unchanged") from exc
    if not isinstance(cursor, dict) or cursor.keys() != {"batch_sha256", "request_index", "start"}:
        raise ValueError("invalid batch cursor fields")
    if not isinstance(cursor["batch_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", cursor["batch_sha256"]):
        raise ValueError("invalid batch cursor fingerprint")
    if any(type(cursor[k]) is not int or not 0 <= cursor[k] <= sys.maxsize for k in ("request_index", "start")):
        raise ValueError("invalid batch cursor position")
    return cursor


def inspect_batch(args: argparse.Namespace) -> dict[str, Any]:
    manifest = _load_source(args.path)
    if len(manifest.raw) > 1_000_000:
        raise ValueError("batch request file exceeds 1 MB")
    values = json.loads(manifest.raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                        parse_constant=_reject_constant)
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_BATCH_REQUESTS:
        raise ValueError(f"batch request file must contain 1 to {MAX_BATCH_REQUESTS} requests")
    requests = [_batch_request(value, args.max_output_chars) for value in values]
    cursor = _decode_cursor(args.cursor) if args.cursor is not None else None

    # Snapshot every unique input once per call. Pin the entire set, including
    # later requests, so continuation cannot silently mix versions of sources.
    sources: dict[Path, Source] = {}
    for index, request in enumerate(requests):
        request.path = request.path.resolve(strict=True)
        if request.path not in sources:
            sources[request.path] = _load_source(request.path)
        if request.expect_sha256 and request.expect_sha256.lower() != sources[request.path].sha256:
            raise ValueError(f"batch request {index}: source SHA-256 changed; refresh the batch")
    identity = {"manifest_path": str(manifest.path), "manifest_sha256": manifest.sha256,
                "reader_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "sources": [(str(path), source.sha256) for path, source in sources.items()]}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
    if cursor and cursor["batch_sha256"] != fingerprint:
        raise ValueError("batch sources, request file, or reader changed; start a new batch and reassess affected coverage")
    index = cursor["request_index"] if cursor else 0
    if index >= len(requests):
        raise ValueError("batch cursor request index is out of range")
    start = cursor["start"] if cursor else requests[index].start
    if start < requests[index].start:
        raise ValueError("batch cursor precedes the requested start")
    initial_index, initial_start = index, start
    results: list[dict[str, Any]] = []

    def response(pages: list[dict[str, Any]], next_index: int, next_start: int) -> dict[str, Any]:
        next_cursor = None
        if next_index < len(requests):
            value = {"batch_sha256": fingerprint, "request_index": next_index, "start": next_start}
            next_cursor = base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")
        return {"operation": "batch", "manifest_path": str(manifest.path),
                "manifest_sha256": manifest.sha256, "batch_sha256": fingerprint,
                "request_count": len(requests), "results": pages, "next_cursor": next_cursor}

    def after(page: dict[str, Any]) -> tuple[int, int]:
        if page.get("next_start") is not None:
            return index, page["next_start"]
        following = index + 1
        return following, requests[following].start if following < len(requests) else 0

    while index < len(requests):
        request = argparse.Namespace(**vars(requests[index]))
        request.start = start

        def fits(page: dict[str, Any], budget: int) -> bool:
            wrapped = {"request_index": index, **page}
            return _fits(response([*results, wrapped], *after(page)), budget)

        try:
            page = inspect(request, source=sources[request.path], fits=fits,
                           view_context=f"batch:{fingerprint}:{initial_index}:{initial_start}:{index}")
            if not fits(page, args.max_output_chars):
                raise OutputBudgetError("batch metadata exceeds the budget; increase --max-output-chars")
        except OutputBudgetError:
            if not results:
                raise
            break
        except (OSError, ValueError, LookupError, RecursionError) as exc:
            raise ValueError(f"batch request {index}: {exc}") from exc
        results.append({"request_index": index, **page})
        index, start = after(page)
        if page.get("next_start") is not None:
            break
    return response(results, index, start)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = inspect_batch(args) if args.command == "batch" else inspect(args)
        output = _serialized(result)
        if len(output) > args.max_output_chars:
            raise ValueError("response metadata exceeds the output budget")
        sys.stdout.write(output)
    except (OSError, ValueError, LookupError, RecursionError) as exc:
        print(f"inspection failed: {str(exc)[:1500]}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
