#!/usr/bin/env python3
"""Prepare, check and import isolated extraction owners without approving them."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from extraction.coordination import check_owner, close_batch, import_owner_runs, prepare_batch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository-root', type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('batch_id')
    prepare.add_argument('record_ids', nargs='+')
    check = commands.add_parser('check')
    check.add_argument('batch_id')
    check.add_argument('record_id')
    handoff = commands.add_parser('import')
    handoff.add_argument('batch_id')
    handoff.add_argument('record_id')
    handoff.add_argument('--run-id', required=True)
    handoff.add_argument('--repro-run-id', required=True)
    finish = commands.add_parser('complete')
    finish.add_argument('batch_id')
    abort = commands.add_parser('abort')
    abort.add_argument('batch_id')
    abort.add_argument('--reason', required=True, help='why the primary stopped all owners and is ending this batch')
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            result = prepare_batch(args.repository_root, args.batch_id, args.record_ids)
        elif args.command == 'check':
            result = check_owner(args.repository_root, args.batch_id, args.record_id)
        elif args.command == 'import':
            result = import_owner_runs(args.repository_root, args.batch_id, args.record_id, args.run_id, args.repro_run_id)
        else:
            result = close_batch(args.repository_root, args.batch_id,
                                 abort_reason=args.reason if args.command == 'abort' else None)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f'coordination failed: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
