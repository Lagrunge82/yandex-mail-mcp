"""Stress the IMAP connection pool with read-only calls against a real mailbox.

Runs read-only calls one after another, then all at once, and reports failures,
response times and the pool's warnings about dropped sessions. It changes nothing
in the mailbox and prints no message content: only counts, times and the reasons
the server gave for closing sessions.

    uv run python scripts/imap_stress.py --count 20

Credentials come from the environment, as for the server: YANDEX_EMAIL with
YANDEX_OAUTH_TOKEN, or a sign-in stored by `yandex-mail-mcp login`. The pool size
is YANDEX_IMAP_POOL_SIZE unless --pool-size is given.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

from yandex_mail_mcp.config import Settings
from yandex_mail_mcp.mail import Mailbox


@dataclass
class Call:
    name: str
    seconds: float
    error: str | None


class Collect(logging.Handler):
    """Keeps the pool's log records; they name errors, never message content."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def operations(box: Mailbox, folder: str) -> list[tuple[str, Callable[[], Any]]]:
    since = (date.today() - timedelta(days=30)).isoformat()
    return [
        ("list_recent_emails", lambda: box.recent(limit=5, folder=folder)),
        ("search_emails", lambda: box.search(since=since, limit=10, folder=folder)),
        ("list_folders", box.folders),
    ]


def timed(name: str, operation: Callable[[], Any]) -> Call:
    started = time.monotonic()
    try:
        operation()
    except Exception as exc:
        return Call(name, time.monotonic() - started, type(exc).__name__)
    return Call(name, time.monotonic() - started, None)


def summary(phase: str, calls: list[Call], wall: float) -> dict[str, Any]:
    times = sorted(call.seconds for call in calls)
    return {
        "phase": phase,
        "calls": len(calls),
        "failures": sum(call.error is not None for call in calls),
        "errors": dict(Counter(call.error for call in calls if call.error)),
        "median_s": round(statistics.median(times), 3),
        "p95_s": round(times[min(len(times) - 1, int(len(times) * 0.95))], 3),
        "max_s": round(times[-1], 3),
        "wall_s": round(wall, 3),
    }


def run(args: argparse.Namespace, box: Mailbox, log: Collect) -> list[dict[str, Any]]:
    ops = operations(box, args.folder)
    results = []

    marks = len(log.records)
    started = time.monotonic()
    sequential = []
    for index in range(args.count):
        if index and args.pause:
            time.sleep(args.pause)
        sequential.append(timed(*ops[index % len(ops)]))
    results.append(summary("sequential", sequential, time.monotonic() - started))
    results[-1]["log"] = log.records[marks:]

    marks = len(log.records)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.count) as executor:
        parallel = list(
            executor.map(lambda index: timed(*ops[index % len(ops)]), range(args.count))
        )
    results.append(summary("parallel", parallel, time.monotonic() - started))
    results[-1]["log"] = log.records[marks:]
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--count", type=int, default=20, help="calls per phase (default 20)")
    parser.add_argument("--folder", default="INBOX", help="folder to read (default INBOX)")
    parser.add_argument("--pool-size", type=int, help="override YANDEX_IMAP_POOL_SIZE")
    parser.add_argument(
        "--pause",
        type=float,
        default=0,
        help="seconds between sequential calls, to let idle sessions age (default 0)",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    if args.count < 1:
        parser.error("--count must be at least 1")

    settings = Settings.from_env()
    if args.pool_size is not None:
        settings = replace(settings, imap_pool_size=args.pool_size)
    log = Collect()
    logger = logging.getLogger("yandex_mail_mcp.mail")
    logger.addHandler(log)
    logger.setLevel(logging.INFO)
    box = Mailbox(settings)
    try:
        results = run(args, box, log)
    finally:
        box.close()

    for result in results:
        records = result.pop("log")
        result["dropped_sessions"] = sum(r.levelno >= logging.WARNING for r in records)
        result["retries"] = sum(r.getMessage().startswith("Retrying") for r in records)
        result["noop_replacements"] = sum("failed NOOP" in r.getMessage() for r in records)
        result["log"] = [
            f"{r.levelname} {r.getMessage()}" for r in records if r.levelno >= logging.INFO
        ]
    report = {"pool_size": settings.imap_pool_size, "phases": results}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"IMAP pool size: {settings.imap_pool_size}")
        for result in results:
            print(
                f"\n{result['phase']}: {result['calls']} calls, {result['failures']} failed "
                f"{result['errors'] or ''}\n"
                f"  median {result['median_s']} s, p95 {result['p95_s']} s, "
                f"max {result['max_s']} s, wall {result['wall_s']} s\n"
                f"  dropped sessions {result['dropped_sessions']}, "
                f"retries {result['retries']}, NOOP replacements {result['noop_replacements']}"
            )
            for line in result["log"]:
                print(f"  {line}")
    return 1 if any(result["failures"] for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
