#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Socket-activated MCP daemon that serves the shared agent mailbox.

Reuses server.py's protocol implementation (Mailbox, tool_definitions,
call_tool, handle_request) over a Unix domain socket instead of stdio, so
multiple clients (Claude, Codex) share one long-lived process. Each
connection identifies its agent via a one-line JSON handshake before
entering the same newline-delimited JSON-RPC loop server.py speaks on
stdin/stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from server import Mailbox, error, handle_request, require_agent_name

LISTEN_FDS_START = 3
HANDSHAKE_TIMEOUT = 10.0


def default_db_path() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return data_home / "agent-mailbox" / "mailbox.sqlite3"


def default_socket_path() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime_dir) if runtime_dir else Path("/tmp")
    return base / "agent-mailbox" / "mcp.sock"


def get_listen_socket(socket_path: Path) -> socket.socket:
    listen_pid = os.environ.get("LISTEN_PID")
    if listen_pid and int(listen_pid) == os.getpid():
        listen_fds = int(os.environ.get("LISTEN_FDS", "0"))
        if listen_fds != 1:
            sys.exit(
                f"[daemon] expected exactly 1 socket-activated fd, got {listen_fds} "
                "(check agent-mailbox.socket for extra ListenStream= lines)"
            )
        sock = socket.socket(fileno=LISTEN_FDS_START)
        sock.setblocking(False)
        return sock

    socket_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(socket_path.parent, 0o700)
    if socket_path.exists():
        socket_path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    sock.listen(64)
    sock.setblocking(False)
    return sock


async def handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    db_path: Path,
    executor: ThreadPoolExecutor,
) -> None:
    loop = asyncio.get_running_loop()
    agent_name: str | None = None
    try:
        try:
            handshake_line = await asyncio.wait_for(reader.readline(), HANDSHAKE_TIMEOUT)
        except asyncio.TimeoutError:
            writer.write(json.dumps({"ok": False, "error": "handshake timed out"}).encode() + b"\n")
            await writer.drain()
            return
        if not handshake_line:
            return
        try:
            handshake = json.loads(handshake_line)
            if not isinstance(handshake, dict):
                raise ValueError("handshake must be a JSON object")
            agent_name = require_agent_name(handshake.get("agent"), "agent")
        except (json.JSONDecodeError, ValueError) as exc:
            writer.write(json.dumps({"ok": False, "error": str(exc)}).encode() + b"\n")
            await writer.drain()
            return

        mailbox = await loop.run_in_executor(executor, Mailbox, db_path, agent_name)
        writer.write(json.dumps({"ok": True}).encode() + b"\n")
        await writer.drain()

        while True:
            line = await reader.readline()
            if not line:
                break
            if not line.strip():
                continue
            request_id = None
            try:
                request = json.loads(line)
                request_id = request.get("id") if isinstance(request, dict) else None
                if not isinstance(request, dict):
                    raise ValueError("request must be a JSON object")
                response = await loop.run_in_executor(
                    executor, handle_request, mailbox, request
                )
            except (json.JSONDecodeError, ValueError) as exc:
                response = error(request_id, -32700, str(exc))
            if response is not None:
                writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
                await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        print(f"[daemon] {agent_name or '?'} disconnected", file=sys.stderr, flush=True)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def run(db_path: Path, socket_path: Path) -> None:
    listen_socket = get_listen_socket(socket_path)
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mailbox-db")
    server = await asyncio.start_unix_server(
        lambda r, w: handle_connection(r, w, db_path, executor), sock=listen_socket
    )

    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: stop.done() or stop.set_result(None))

    print(f"[daemon] listening, db={db_path}", file=sys.stderr, flush=True)
    async with server:
        await stop
    executor.shutdown(wait=False, cancel_futures=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=default_db_path())
    parser.add_argument("--socket", type=Path, default=default_socket_path())
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run(args.db.resolve(), args.socket))


if __name__ == "__main__":
    main()
