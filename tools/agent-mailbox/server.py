#!/usr/bin/env python3
"""Dependency-free MCP server for a durable local peer-agent mailbox."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-06-18"
AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_BODY_CHARS = 262_144


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def require_agent_name(value: Any, field: str = "agent") -> str:
    if not isinstance(value, str) or not AGENT_RE.fullmatch(value):
        raise ValueError(
            f"{field} must match {AGENT_RE.pattern} (1-64 characters)"
        )
    return value


def require_text(value: Any, field: str, *, maximum: int = MAX_BODY_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds the {maximum}-character limit")
    return value


class Mailbox:
    def __init__(self, db_path: Path, agent: str, *, register: bool = True):
        self.db_path = db_path
        self.agent = require_agent_name(agent)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        if register:
            self.register_agent({"client": "mcp", "server_version": SERVER_VERSION})

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    name TEXT PRIMARY KEY,
                    registered_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'global',
                    subject TEXT,
                    body TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'message',
                    thread_id TEXT NOT NULL,
                    reply_to TEXT,
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'delivered', 'processed', 'failed')),
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    acknowledged_at TEXT,
                    acknowledgement_note TEXT,
                    idempotency_key TEXT,
                    UNIQUE(sender, idempotency_key)
                );

                CREATE INDEX IF NOT EXISTS messages_recipient_status_created
                    ON messages(recipient, channel, status, created_at);
                CREATE INDEX IF NOT EXISTS messages_thread_created
                    ON messages(thread_id, created_at);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def _touch(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE agents SET last_seen_at = ? WHERE name = ?",
            (utc_now(), self.agent),
        )

    def register_agent(self, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        timestamp = utc_now()
        metadata_json = json.dumps(metadata or {}, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agents(name, registered_at, last_seen_at, metadata_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    metadata_json = excluded.metadata_json
                """,
                (self.agent, timestamp, timestamp, metadata_json),
            )
            row = connection.execute(
                "SELECT * FROM agents WHERE name = ?", (self.agent,)
            ).fetchone()
        result = self._row(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        return result

    def list_agents(self) -> dict[str, Any]:
        with self._connect() as connection:
            self._touch(connection)
            rows = connection.execute(
                "SELECT * FROM agents ORDER BY name"
            ).fetchall()
        agents = []
        for row in rows:
            agent = self._row(row)
            agent["metadata"] = json.loads(agent.pop("metadata_json"))
            agents.append(agent)
        return {"agents": agents}

    def send_message(
        self,
        *,
        to: Any,
        body: Any,
        channel: Any = "global",
        subject: Any = None,
        kind: Any = "message",
        thread_id: Any = None,
        reply_to: Any = None,
        idempotency_key: Any = None,
    ) -> dict[str, Any]:
        recipient = require_agent_name(to, "to")
        message_body = require_text(body, "body")
        channel = require_text(channel, "channel", maximum=128)
        if subject is not None:
            subject = require_text(subject, "subject", maximum=256)
        kind = require_text(kind, "kind", maximum=64)
        if thread_id is not None:
            thread_id = require_text(thread_id, "thread_id", maximum=128)
        if reply_to is not None:
            reply_to = require_text(reply_to, "reply_to", maximum=64)
        if idempotency_key is not None:
            idempotency_key = require_text(
                idempotency_key, "idempotency_key", maximum=128
            )

        message_id = str(uuid.uuid4())
        thread_id = thread_id or message_id
        timestamp = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._touch(connection)
            if idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM messages WHERE sender = ? AND idempotency_key = ?",
                    (self.agent, idempotency_key),
                ).fetchone()
                if existing:
                    result = self._row(existing)
                    result["deduplicated"] = True
                    return result
            if reply_to:
                parent = connection.execute(
                    "SELECT thread_id FROM messages WHERE id = ?", (reply_to,)
                ).fetchone()
                if not parent:
                    raise ValueError(f"reply_to message does not exist: {reply_to}")
                if thread_id == message_id:
                    thread_id = parent["thread_id"]
            connection.execute(
                """
                INSERT INTO messages(
                    id, sender, recipient, channel, subject, body, kind, thread_id,
                    reply_to, status, created_at, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    message_id,
                    self.agent,
                    recipient,
                    channel,
                    subject,
                    message_body,
                    kind,
                    thread_id,
                    reply_to,
                    timestamp,
                    idempotency_key,
                ),
            )
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        result = self._row(row)
        result["deduplicated"] = False
        return result

    def receive_messages(
        self,
        *,
        limit: Any = 20,
        wait_seconds: Any = 0,
        channel: Any = "global",
        include_delivered: Any = False,
        mark_delivered: Any = True,
    ) -> dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        channel = require_text(channel, "channel", maximum=128)
        if not isinstance(wait_seconds, (int, float)) or isinstance(wait_seconds, bool):
            raise ValueError("wait_seconds must be a number")
        wait_seconds = max(0.0, min(float(wait_seconds), 30.0))
        if not isinstance(include_delivered, bool) or not isinstance(mark_delivered, bool):
            raise ValueError("include_delivered and mark_delivered must be booleans")

        deadline = time.monotonic() + wait_seconds
        while True:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._touch(connection)
                statuses = ("queued", "delivered") if include_delivered else ("queued",)
                placeholders = ",".join("?" for _ in statuses)
                rows = connection.execute(
                    f"""
                    SELECT * FROM messages
                    WHERE recipient = ? AND channel = ? AND status IN ({placeholders})
                    ORDER BY created_at, id
                    LIMIT ?
                    """,
                    (self.agent, channel, *statuses, limit),
                ).fetchall()
                if rows:
                    if mark_delivered:
                        timestamp = utc_now()
                        queued_ids = [row["id"] for row in rows if row["status"] == "queued"]
                        if queued_ids:
                            marks = ",".join("?" for _ in queued_ids)
                            connection.execute(
                                f"""
                                UPDATE messages
                                SET status = 'delivered', delivered_at = ?
                                WHERE id IN ({marks})
                                """,
                                (timestamp, *queued_ids),
                            )
                            rows = connection.execute(
                                f"SELECT * FROM messages WHERE id IN ({marks}) ORDER BY created_at, id",
                                queued_ids,
                            ).fetchall()
                    return {"messages": [self._row(row) for row in rows]}
            if time.monotonic() >= deadline:
                return {"messages": []}
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    def acknowledge_message(
        self, *, message_id: Any, status: Any = "processed", note: Any = None
    ) -> dict[str, Any]:
        message_id = require_text(message_id, "message_id", maximum=64)
        if status not in {"processed", "failed"}:
            raise ValueError("status must be processed or failed")
        if note is not None:
            note = require_text(note, "note", maximum=4096)
        timestamp = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._touch(connection)
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"message does not exist: {message_id}")
            if row["recipient"] != self.agent:
                raise ValueError("only the recipient may acknowledge a message")
            connection.execute(
                """
                UPDATE messages
                SET status = ?, acknowledged_at = ?, acknowledgement_note = ?,
                    delivered_at = COALESCE(delivered_at, ?)
                WHERE id = ?
                """,
                (status, timestamp, note, timestamp, message_id),
            )
            result = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return self._row(result)

    def reply(self, *, message_id: Any, body: Any, subject: Any = None) -> dict[str, Any]:
        message_id = require_text(message_id, "message_id", maximum=64)
        with self._connect() as connection:
            parent = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        if not parent:
            raise ValueError(f"message does not exist: {message_id}")
        if self.agent not in {parent["sender"], parent["recipient"]}:
            raise ValueError("agent is not a participant in that message")
        recipient = parent["sender"] if parent["sender"] != self.agent else parent["recipient"]
        return self.send_message(
            to=recipient,
            body=body,
            subject=subject,
            kind="reply",
            channel=parent["channel"],
            thread_id=parent["thread_id"],
            reply_to=message_id,
        )

    def get_thread(self, *, thread_id: Any, limit: Any = 100) -> dict[str, Any]:
        thread_id = require_text(thread_id, "thread_id", maximum=128)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer from 1 to 500")
        with self._connect() as connection:
            self._touch(connection)
            rows = connection.execute(
                """
                SELECT * FROM messages
                WHERE thread_id = ? AND (sender = ? OR recipient = ?)
                ORDER BY created_at, id LIMIT ?
                """,
                (thread_id, self.agent, self.agent, limit),
            ).fetchall()
        return {"messages": [self._row(row) for row in rows]}

    def mailbox_status(self, *, channel: Any = "global") -> dict[str, Any]:
        channel = require_text(channel, "channel", maximum=128)
        with self._connect() as connection:
            self._touch(connection)
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count FROM messages
                WHERE recipient = ? AND channel = ? GROUP BY status ORDER BY status
                """,
                (self.agent, channel),
            ).fetchall()
        counts = {status: 0 for status in ("queued", "delivered", "processed", "failed")}
        counts.update({row["status"]: row["count"] for row in rows})
        return {"agent": self.agent, "channel": channel, "counts": counts}


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "register_agent",
            "description": "Register or refresh this configured peer-agent identity.",
            "inputSchema": {
                "type": "object",
                "properties": {"metadata": {"type": "object"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "list_agents",
            "description": "List peer agents that have connected to this mailbox.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "send_message",
            "description": "Queue a durable message for another agent. Use idempotency_key on retried sends.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "body": {"type": "string"},
                    "channel": {"type": "string", "default": "global"},
                    "subject": {"type": "string"},
                    "kind": {"type": "string", "default": "message"},
                    "thread_id": {"type": "string"},
                    "reply_to": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["to", "body"],
                "additionalProperties": False,
            },
        },
        {
            "name": "receive_messages",
            "description": "Receive queued messages for this agent, optionally long-polling for up to 30 seconds.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                    "wait_seconds": {"type": "number", "minimum": 0, "maximum": 30, "default": 0},
                    "channel": {"type": "string", "default": "global"},
                    "include_delivered": {"type": "boolean", "default": False},
                    "mark_delivered": {"type": "boolean", "default": True},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "acknowledge_message",
            "description": "Mark a received message processed or failed, with an optional note.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["processed", "failed"], "default": "processed"},
                    "note": {"type": "string"},
                },
                "required": ["message_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "reply",
            "description": "Reply to a mailbox message while preserving its thread correlation.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "body": {"type": "string"},
                    "subject": {"type": "string"},
                },
                "required": ["message_id", "body"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_thread",
            "description": "Read the messages in one thread that involve this agent.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                },
                "required": ["thread_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "mailbox_status",
            "description": "Count this agent's queued, delivered, processed, and failed messages.",
            "inputSchema": {
                "type": "object",
                "properties": {"channel": {"type": "string", "default": "global"}},
                "additionalProperties": False,
            },
        },
    ]


def call_tool(mailbox: Mailbox, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handlers = {
        "register_agent": mailbox.register_agent,
        "list_agents": mailbox.list_agents,
        "send_message": mailbox.send_message,
        "receive_messages": mailbox.receive_messages,
        "acknowledge_message": mailbox.acknowledge_message,
        "reply": mailbox.reply,
        "get_thread": mailbox.get_thread,
        "mailbox_status": mailbox.mailbox_status,
    }
    if name not in handlers:
        raise ValueError(f"unknown tool: {name}")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    result = handlers[name](**arguments)
    return {
        "content": [
            {"type": "text", "text": json.dumps(result, sort_keys=True, indent=2)}
        ]
    }


def success(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def handle_request(mailbox: Mailbox, request: dict[str, Any]) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = request.get("method")
    if method == "initialize":
        requested_version = request.get("params", {}).get("protocolVersion")
        return success(
            request_id,
            {
                "protocolVersion": requested_version or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agent-mailbox", "version": SERVER_VERSION},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return success(request_id, {})
    if method == "tools/list":
        return success(request_id, {"tools": tool_definitions()})
    if method == "tools/call":
        params = request.get("params") or {}
        try:
            result = call_tool(mailbox, params.get("name"), params.get("arguments") or {})
            return success(request_id, result)
        except (ValueError, TypeError, sqlite3.Error) as exc:
            return success(
                request_id,
                {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            )
    if request_id is None:
        return None
    return error(request_id, -32601, f"method not found: {method}")


def serve(mailbox: Mailbox) -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id") if isinstance(request, dict) else None
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            response = handle_request(mailbox, request)
        except (json.JSONDecodeError, ValueError) as exc:
            response = error(request_id, -32700, str(exc))
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()


def parse_args() -> argparse.Namespace:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True, help="Fixed identity for this MCP process")
    parser.add_argument(
        "--db",
        type=Path,
        default=data_home / "agent-mailbox" / "mailbox.sqlite3",
        help="Shared SQLite mailbox path",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    serve(Mailbox(args.db.resolve(), args.agent))


if __name__ == "__main__":
    main()
