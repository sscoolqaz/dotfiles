#!/usr/bin/env python3
"""Live console watcher for the local agent-mailbox SQLite DB.

Polls the mailbox on an interval and prints a line for every new message
and every status transition (queued -> delivered -> processed/failed).
Read-only: opens the DB with mode=ro and never writes to it.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

STATUS_COLOR = {
    "queued": "\033[33m",
    "delivered": "\033[36m",
    "processed": "\033[32m",
    "failed": "\033[31m",
}
RESET = "\033[0m"
DIM = "\033[2m"


def default_db_path() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return data_home / "agent-mailbox" / "mailbox.sqlite3"


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def fmt_row(row: sqlite3.Row) -> str:
    subject = f' "{row["subject"]}"' if row["subject"] else ""
    body = row["body"].replace("\n", " ")
    if len(body) > 160:
        body = body[:157] + "..."
    color = STATUS_COLOR.get(row["status"], "")
    return (
        f"{DIM}{row['created_at']}{RESET} "
        f"{row['sender']} -> {row['recipient']} "
        f"[{row['channel']}/{row['kind']}]{subject} "
        f"{color}{row['status']}{RESET}: {body}"
    )


def watch(db_path: Path, interval: float, channel: str | None) -> None:
    last_status: dict[str, str] = {}
    first_pass = True
    while True:
        try:
            conn = connect(db_path)
            try:
                query = "SELECT * FROM messages"
                params: list[str] = []
                if channel:
                    query += " WHERE channel = ?"
                    params.append(channel)
                query += " ORDER BY created_at, id"
                rows = conn.execute(query, params).fetchall()
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            print(f"{DIM}[watch] db unavailable: {exc}{RESET}", file=sys.stderr, flush=True)
            time.sleep(interval)
            continue

        for row in rows:
            if last_status.get(row["id"]) == row["status"]:
                continue
            last_status[row["id"]] = row["status"]
            if not first_pass:
                print(fmt_row(row), flush=True)

        first_pass = False
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=default_db_path())
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--channel", default=None, help="Only show one channel")
    args = parser.parse_args()
    print(f"[watch] tailing {args.db} every {args.interval}s", flush=True)
    watch(args.db, args.interval, args.channel)


if __name__ == "__main__":
    main()
