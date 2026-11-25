"""Run with python3 -m scripts.boise_collect --help from the repository root."""

from __future__ import annotations

import argparse
import json
import math
import signal
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

from .bruno_client import BrunoClient
from .collector import (
    ORIGIN, collect, queue_refresh, read_origin, validate_deferred_divisions,
    validated_origin, writer_lock,
)
from .report import write_report
from .store import Store


def positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def day(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("must be YYYY-MM-DD")
    return parsed


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Local, resumable public Boise history collector")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("scan", "resume", "refresh", "retry", "acknowledge-gap", "report"):
        p = commands.add_parser(name)
        p.add_argument("--data-dir", type=Path, required=True, help="SQLite/evidence directory")
        if name in ("scan", "resume", "refresh"):
            p.add_argument("--base-url", help="LPS origin, or loopback fixture server; saved per database")
            p.add_argument("--max-requests", type=int, default=100, help="HTTP request budget for this invocation (default 100)")
            p.add_argument("--delay", type=positive, default=1, help="minimum seconds between request starts (live minimum 1)")
            p.add_argument("--timeout", type=positive, default=15)
        if name in ("scan", "resume"):
            p.add_argument("--defer-division", action="append", dest="deferred_division_ids",
                           help="leave a discovered division with >=5 attempts and a current HTTP 5xx failure unresolved for this invocation (repeatable)")
        if name in ("resume", "refresh"):
            p.add_argument("--job-id", help="durable refresh identifier; reuse it to resume the same bounded work")
        if name == "scan":
            p.add_argument("--start", type=day, required=True)
            p.add_argument("--end", type=day, required=True)
            p.add_argument("--include-upcoming", action="store_true", help="discover unpublished divisions from the Boise catalog")
        elif name == "refresh":
            p.add_argument("--start", type=day, help="inclusive first day for a named date-range refresh")
            p.add_argument("--end", type=day, help="inclusive final day for a named date-range refresh")
            p.add_argument("--include-upcoming", action="store_true", help="refresh and ingest the current catalog within the named job")
            p.add_argument("--division", action="append", dest="division_ids", help="refresh only this previously discovered division (repeatable)")
            p.add_argument("--active-hours", type=positive, default=24)
            p.add_argument("--historical-days", type=positive, default=30)
        elif name == "retry":
            p.add_argument("--kind", choices=("day", "division", "catalog"), required=True)
            p.add_argument("--key", required=True)
        elif name == "acknowledge-gap":
            p.add_argument("--job-id", required=True)
            p.add_argument("--kind", choices=("day",), required=True)
            p.add_argument("--key", required=True)
            p.add_argument("--body-sha256", required=True, help="exact hash shared by the two diagnosed validation failures")
            p.add_argument("--reason", choices=("confirmed_empty_game_slot",), required=True,
                           help="explicit operator diagnosis; this command makes no requests")
        elif name == "report":
            p.add_argument("--output", type=Path, help="report directory (default DATA_DIR/reports)")
    return root


def main(argv=None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    root = args.data_dir.resolve()

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    previous_handler = signal.signal(signal.SIGTERM, interrupt)
    try:
        if args.command == "report":
            output = (args.output or root / "reports").resolve()
            write_report(root, output)
            print(json.dumps({"report_directory": str(output)}))
            return 0
        if args.command == "scan" and args.start > args.end:
            raise ValueError("start must not be after end")
        if args.command == "refresh":
            bounded = bool(args.job_id or args.start or args.end or args.include_upcoming)
            if bounded and (not args.job_id or args.start is None or args.end is None):
                raise ValueError("date-range refresh requires --job-id, --start and --end")
            if bounded and args.division_ids:
                raise ValueError("--division cannot be combined with a named date-range refresh")
            if bounded and args.start > args.end:
                raise ValueError("start must not be after end")
        if hasattr(args, "max_requests") and args.max_requests < 1:
            raise ValueError("max-requests must be positive")
        with writer_lock(root):
            exists = (root / "collector.sqlite3").exists()
            if args.command != "scan" and not exists:
                raise ValueError("no collector database; start with scan and an explicit interval")
            previous = read_origin(root) if exists else ORIGIN
            base = validated_origin(getattr(args, "base_url", None) or previous)
            if hasattr(args, "delay") and base == ORIGIN and args.delay < 1:
                raise ValueError("live requests require --delay >= 1")
            client = BrunoClient(base, args.timeout) if args.command not in ("retry", "acknowledge-gap") else None
            store = Store(root, base)
            try:
                active_job = store.active_refresh_job_id()
                if active_job and (args.command == "scan" or (args.command == "refresh" and not args.job_id)):
                    raise ValueError(f"refresh job {active_job} is unfinished; use resume --job-id {active_job} first")
                if args.command == "resume" and not args.job_id:
                    args.job_id = active_job
                allowed_resources = None
                deferred_resources = validate_deferred_divisions(store, getattr(args, "deferred_division_ids", None))
                if args.command == "scan":
                    store.queue_interval(args.start, args.end)
                    if args.include_upcoming:
                        store.queue_catalog()
                elif args.command == "retry":
                    store.requeue(args.kind, args.key)
                    print(json.dumps({"queued": {"kind": args.kind, "key": args.key}}))
                    return 0
                elif args.command == "acknowledge-gap":
                    result = store.acknowledge_refresh_gap(
                        args.job_id, args.kind, args.key, args.body_sha256, args.reason, time.time(),
                    )
                    print(json.dumps(result), flush=True)
                    return 0
                elif args.command == "refresh":
                    if args.job_id:
                        job = store.begin_refresh(args.job_id, args.start, args.end, args.include_upcoming, time.time())
                        print(json.dumps({"refresh_job": job}), flush=True)
                    else:
                        allowed_resources = queue_refresh(store, time.time(), args.active_hours, args.historical_days, args.division_ids)
                        print(json.dumps({"refresh_queued": len(allowed_resources)}), flush=True)
                result = collect(
                    store, client, args.max_requests, args.delay,
                    progress=lambda row: print(json.dumps(row), flush=True),
                    allowed_resources=allowed_resources,
                    deferred_resources=deferred_resources,
                    refresh_job_id=getattr(args, "job_id", None),
                )
            finally:
                store.close()
        report = write_report(root, root / "reports")
        result["evidence_complete"] = report["evidence"]["complete"]
        print(json.dumps(result), flush=True)
        return 2 if result["unfinished"] or not result["evidence_complete"] else 0
    except KeyboardInterrupt:
        job_id = getattr(args, "job_id", None)
        continuation = "resume" + (f" --job-id {job_id}" if job_id else "")
        print(f"Interrupted; committed work is retained. Use {continuation} with the same --data-dir to continue.", file=sys.stderr)
        return 130
    except (ValueError, OSError, sqlite3.Error) as exc:
        # Our validation/SQLite errors contain local schema/configuration only.
        print("Collector stopped: " + str(exc), file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
