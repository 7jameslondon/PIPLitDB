"""Manual CLI: keyword discovery, staged linked discovery, review and cleanup."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .config import DiscoveryError, FEATURE_ROOT, REPOSITORY_ROOT, SOURCES, load_config, search_plan
from .engine import resume_plan, run_discovery
from .linked_client import MAX_NEIGHBORS, NEIGHBOR_QUANTITIES
from .records import Catalogue
from .store import Store, writer_lock


def parser():
    result = argparse.ArgumentParser(prog="python -m paper_discovery", description="Find candidate papers new to PIP LitDB.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT, help="Read-only public catalogue root")
    common.add_argument("--work-dir", type=Path, default=FEATURE_ROOT, help="Directory containing local staging/ and state/")
    common.add_argument("--config", type=Path, default=FEATURE_ROOT / "config/default.yaml")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("plan", "run"):
        command = commands.add_parser(name, parents=[common], help="Show search scope without writing" if name == "plan" else "Search and stage candidates")
        command.add_argument("--source", action="append", choices=SOURCES, help="Repeat to select sources; explicitly selects disabled sources too")
        command.add_argument("--since", help="Search start, YYYY-MM-DD; overrides each source lookback")
        command.add_argument("--until", help="Search end, YYYY-MM-DD; defaults to today")
        command.add_argument("--max-pages", type=int, help="Override page budget per query")
        command.add_argument("--restart", action="store_true", help="Start a fresh bioRxiv scan instead of resuming its checkpoint")
    listing = commands.add_parser("list", parents=[common], help="Show the persistent review queue")
    listing.add_argument("--decision", choices=("open", "pending", "keep", "dismiss", "all"), default="open")
    listing.add_argument("--json", action="store_true", help="Include full metadata and discovery evidence")
    review = commands.add_parser("review", parents=[common], help="Record a local decision; public records stay unchanged")
    review.add_argument("candidate", help="Candidate ID, unique ID prefix of at least six hex digits, or DOI")
    review.add_argument("--decision", required=True, choices=("pending", "keep", "dismiss"))
    review.add_argument("--note", default="", help="Reason or follow-up note for this candidate")
    history = commands.add_parser("history", parents=[common], help="Show previous run coverage")
    history.add_argument("--limit", type=int, default=20)
    linked = commands.add_parser("linked", parents=[common], help="Run the standalone Linked Discoveries workflow")
    linked.add_argument("--run-dir", type=Path, required=True, help="Output/cache directory below WORK_DIR/staging")
    linked.add_argument("--step", choices=("all", "resolve", "collect", "enrich", "report"), default="all")
    linked.add_argument("--reference-run", type=Path, help="Optional prior run for metadata reuse and comparison")
    linked.add_argument("--quantity", type=int, choices=NEIGHBOR_QUANTITIES, default=MAX_NEIGHBORS,
                        help="Related papers per seed when creating a manifest; seed retained separately (default: 200, the supported maximum)")
    cleanup = commands.add_parser("cleanup", parents=[common], help="Inspect verified redundant legacy response copies")
    cleanup.add_argument("--apply", action="store_true", help="Delete verified redundant copies and save an audit report")
    return result


def _print(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def main(argv=None):
    args = parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    work, root = args.work_dir.resolve(), args.repository_root.resolve()
    state_exists = (work / "state/discovery.sqlite3").is_file()
    try:
        if args.command == "linked":
            from .linked_discoveries import run_linked
            summary = run_linked(load_config(args.config), root, work, args.run_dir, step=args.step,
                                 reference=args.reference_run, quantity=args.quantity,
                                 progress=lambda message: print(message, file=sys.stderr, flush=True))
            _print(summary)
            return 0 if summary["status"] == "complete" else 2
        if args.command == "cleanup":
            from .cleanup import cleanup
            _print(cleanup(work, apply=args.apply))
            return 0
        if args.command in {"plan", "run"}:
            config = load_config(args.config)
            plan = search_plan(config, selected=args.source, since=args.since, until=args.until, max_pages=args.max_pages)
            if args.command == "plan":
                if state_exists:
                    store = Store(work, readonly=True)
                    try:
                        plan = resume_plan(plan, store, restart=args.restart)
                    finally:
                        store.close()
                _print({"sources": plan, "work_directory": str(work), "topic_rules": config["match_rules"], "filters": config["filters"]})
                return 0
            summary = run_discovery(config, plan, root, work, restart=args.restart,
                                    progress=lambda message: print(message, file=sys.stderr, flush=True))
            _print({key: summary[key] for key in ("run_id", "status", "new_candidates", "open_candidates", "counts", "searches", "report_directory")})
            return 0 if summary["status"] == "complete" else 2
        if not state_exists:
            if args.command == "review":
                raise DiscoveryError("No discovery state exists; run a search first")
            _print([])
            return 0
        if args.command == "review":
            with writer_lock(work):
                store = Store(work)
                try:
                    identifier = store.review(args.candidate, args.decision, args.note)
                finally:
                    store.close()
            _print({"candidate_id": identifier, "decision": args.decision, "note": args.note})
            return 0
        store = Store(work, readonly=True)
        try:
            if args.command == "history":
                if args.limit < 1:
                    raise DiscoveryError("--limit must be positive")
                _print(store.history(args.limit))
            else:
                config = load_config(args.config)
                candidates = store.candidates(Catalogue.load(root), decision=args.decision,
                                              exclude_attached_material=config["filters"]["exclude_attached_material"])
                if args.json:
                    _print(candidates)
                else:
                    print(f"{len(candidates)} candidate(s)")
                    for item in candidates:
                        paper = item["paper"]
                        print(f"{item['id']}  [{item['decision']}]  {paper['title']}")
                        print(f"  {paper['publication_date']} | {paper['journal']} | {paper['doi'] or 'DOI missing'}")
                        for warning in item["warnings"]:
                            print(f"  Review: {warning}")
        finally:
            store.close()
        return 0
    except KeyboardInterrupt:
        print("Discovery interrupted; completed pages and available evidence are retained.", file=sys.stderr)
        return 130
    except (DiscoveryError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"Discovery error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
