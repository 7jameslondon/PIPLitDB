#!/usr/bin/env python3
"""Run retrospective six-calendar-month discovery recovery tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from time import monotonic
from pathlib import Path

from discovery.benchmark import (
    evaluate_window,
    load_manifest,
    render_backtest_reports,
    replay_candidates,
)
from discovery.config import ALLOWED_SOURCES, canonical_json_digest, load_exclusions, load_queries
from discovery.dates import benchmark_windows, parse_iso_date
from discovery.deduplicate import classify_candidates, load_database
from discovery.reconcile import reconcile_candidates
from discovery.pipeline import run_discovery
from discovery.sources import REQUIRED_DISCOVERY_SOURCES


ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="date_from", default="2018-01-01")
    parser.add_argument("--as-of", required=True, help="fixed YYYY-MM-DD date, or today for exploratory runs")
    parser.add_argument("--window-months", type=int, default=6)
    parser.add_argument("--step-months", type=int, default=6)
    parser.add_argument("--source", action="append", choices=sorted(ALLOWED_SOURCES))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / ".cache" / "discovery" / "backtests")
    parser.add_argument("--manifest", type=Path, default=ROOT / "tests" / "discovery" / "benchmark" / "known-papers.yaml")
    parser.add_argument("--replay", type=Path, default=ROOT / "tests" / "discovery" / "benchmark" / "baseline-results.jsonl")
    parser.add_argument("--query-file", type=Path, default=ROOT / "discovery" / "queries.yaml")
    parser.add_argument("--exclusions", type=Path, default=ROOT / "discovery" / "exclusions.yaml")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts" / "discovery" / "backtest")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started_at = datetime.now(timezone.utc)
    started_clock = monotonic()
    try:
        start = parse_iso_date(args.date_from, label="from")
        if args.as_of == "today":
            as_of = datetime.now(timezone.utc).date()
        else:
            as_of = parse_iso_date(args.as_of, label="as-of")
        manifest_as_of, papers = load_manifest(args.manifest)
        if manifest_as_of and as_of > manifest_as_of:
            raise ValueError("requested as-of date exceeds the reviewed benchmark manifest")
        config = load_queries(args.query_file)
        exclusions = load_exclusions(args.exclusions)
        windows = benchmark_windows(
            start,
            as_of,
            window_months=args.window_months,
            step_months=args.step_months,
        )
        selected = tuple(args.source or REQUIRED_DISCOVERY_SOURCES)
        if len(selected) != len(set(selected)):
            raise ValueError("--source values must be unique")
        repository_commit = subprocess.run(
            ["git", "-c", f"safe.directory={ROOT.as_posix()}", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        identity = {
            "as_of": as_of.isoformat(),
            "from": start.isoformat(),
            "window_months": args.window_months,
            "step_months": args.step_months,
            "sources": selected,
            "config_digest": config.digest,
            "repository_commit": repository_commit,
            "windows": [
                {"start": item.start.isoformat(), "end": item.end.isoformat(), "kind": item.kind}
                for item in windows
            ],
        }
        run_id = canonical_json_digest(identity)[:16]
        if args.refresh_cache:
            if args.offline:
                raise ValueError("--refresh-cache cannot be combined with --offline")
            run_id += "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_cache = args.cache_dir / run_id
        if args.refresh_cache and run_cache.exists():
            raise ValueError("fresh cache namespace already exists; retry after the timestamp changes")
        full_database = load_database(ROOT / "database" / "records")
        results = []
        for window in windows:
            hidden = [
                paper.record_id
                for paper in papers
                if paper.union_eligible and window.start <= paper.canonical_date <= window.end
            ]
            hidden_database = load_database(ROOT / "database" / "records", hidden_ids=hidden)
            replayed = replay_candidates(args.replay, window) if args.offline else None
            if replayed is not None:
                candidates = classify_candidates(
                    reconcile_candidates(replayed),
                    hidden_database,
                    exclusions,
                    threshold=config.record_threshold,
                )
            else:
                report = run_discovery(
                    config=config,
                    exclusions=exclusions,
                    database=hidden_database,
                    start=window.start,
                    end=window.end,
                    resolved_today=as_of,
                    cache_dir=run_cache / window.key,
                    offline=args.offline,
                    sources=selected,
                )
                if report.status == "failed":
                    raise RuntimeError(f"source completeness failed for window {window.key}")
                candidates = report.candidates
            full_classified = classify_candidates(
                candidates,
                full_database,
                exclusions,
                threshold=config.record_threshold,
            )
            existing = sum(candidate.disposition == "existing" for candidate in full_classified)
            results.append(
                evaluate_window(
                    window,
                    papers,
                    candidates,
                    sources=selected,
                    full_database_existing=existing,
                )
            )
        payload, markdown, csv_text = render_backtest_reports(
            results,
            as_of=as_of,
            config_digest=config.digest,
            run_id=run_id,
        )
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / f"{run_id}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (args.output_dir / f"{run_id}.md").write_text(markdown, encoding="utf-8")
        (args.output_dir / f"{run_id}.csv").write_text(csv_text, encoding="utf-8")
        (run_cache / "run-manifest.json").parent.mkdir(parents=True, exist_ok=True)
        request_evidence = []
        if run_cache.exists():
            for metadata_path in sorted(run_cache.rglob("*.json")):
                if metadata_path.name == "run-manifest.json":
                    continue
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                if isinstance(metadata, dict) and metadata.get("sha256"):
                    request_evidence.append(
                        {"path": metadata_path.relative_to(run_cache).as_posix(), **metadata}
                    )
        (run_cache / "run-manifest.json").write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "run_id": run_id,
                    "started_at": started_at.isoformat(),
                    "duration_seconds": round(monotonic() - started_clock, 3),
                    "requests": request_evidence,
                    **identity,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Backtest {run_id}: {len(results)} window(s); reports in {args.output_dir}")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Backtest failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
