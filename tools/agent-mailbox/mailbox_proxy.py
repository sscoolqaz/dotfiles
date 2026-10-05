#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Thin stdio<->Unix-socket bridge to the shared agent-mailbox daemon.

Claude Code and Codex CLI only know how to spawn an MCP server over stdio.
This proxy is what they actually spawn: it connects to the socket-activated
mailbox_daemon.py, sends a one-line handshake identifying which agent it is
bridging for, then relays raw bytes between stdin/stdout and the socket for
the rest of the process lifetime.
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import socket
import sys
import time
from pathlib import Path

CHUNK_SIZE = 65536


def default_socket_path() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime_dir) if runtime_dir else Path("/tmp")
    return base / "agent-mailbox" / "mcp.sock"


def connect(socket_path: Path, timeout: float) -> socket.socket:
    deadline = time.monotonic() + timeout
    delay = 0.1
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(socket_path))
            return sock
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            last_error = exc
            time.sleep(delay)
    sys.exit(
        f"agent-mailbox: daemon socket not available at {socket_path} "
        f"({last_error}) - is the agent-mailbox runit service up? "
        "(SVDIR=$HOME/service sv status agent-mailbox)"
    )


def handshake(sock: socket.socket, agent: str) -> None:
    sock.sendall(json.dumps({"agent": agent}).encode() + b"\n")
    buffer = b""
    while not buffer.endswith(b"\n"):
        chunk = sock.recv(4096)
        if not chunk:
            sys.exit("agent-mailbox: daemon closed the connection during handshake")
        buffer += chunk
    ack = json.loads(buffer)
    if not ack.get("ok"):
        sys.exit(f"agent-mailbox: handshake rejected: {ack.get('error')}")


def pump(sock: socket.socket) -> None:
    stdin_fd = sys.stdin.buffer.fileno()
    stdout = sys.stdout.buffer
    selector = selectors.DefaultSelector()
    selector.register(stdin_fd, selectors.EVENT_READ, "stdin")
    selector.register(sock, selectors.EVENT_READ, "socket")
    stdin_open = True
    socket_open = True
    while stdin_open or socket_open:
        for key, _ in selector.select():
            if key.data == "stdin":
                data = os.read(stdin_fd, CHUNK_SIZE)
                if not data:
                    selector.unregister(stdin_fd)
                    stdin_open = False
                    try:
                        sock.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                sock.sendall(data)
            elif key.data == "socket":
                try:
                    data = sock.recv(CHUNK_SIZE)
                except OSError:
                    data = b""
                if not data:
                    selector.unregister(sock)
                    socket_open = False
                    continue
                stdout.write(data)
                stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--socket", type=Path, default=default_socket_path())
    parser.add_argument("--connect-timeout", type=float, default=5.0)
    args = parser.parse_args()

    sock = connect(args.socket, args.connect_timeout)
    handshake(sock, args.agent)
    pump(sock)


if __name__ == "__main__":
    main()
