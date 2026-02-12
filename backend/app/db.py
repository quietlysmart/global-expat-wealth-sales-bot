from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from .models import ActionLogRecord, ConversationState, ProspectProfile


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                );

                CREATE TABLE IF NOT EXISTS prospect_profiles (
                    conversation_id TEXT PRIMARY KEY,
                    profile_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                );

                CREATE TABLE IF NOT EXISTS kb_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    section TEXT,
                    page INTEGER,
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    vector_json TEXT NOT NULL,
                    ingestion_ts TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS action_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    demo_mode INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                );

                CREATE TABLE IF NOT EXISTS conversation_states (
                    conversation_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                );
                """
            )
            conn.commit()

    def ensure_conversation(self, conversation_id: str) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations(id, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (conversation_id, now, now),
            )
            conn.commit()

    def add_message(self, conversation_id: str, role: str, channel: str, content: str) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO messages(conversation_id, role, channel, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_id, role, channel, content, now),
            )
            conn.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?",
                (now, conversation_id),
            )
            conn.commit()

    def get_recent_messages(self, conversation_id: str, limit: int = 12) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT role, channel, content, created_at
                FROM messages
                WHERE conversation_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_profile(self, conversation_id: str) -> ProspectProfile:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT profile_json FROM prospect_profiles WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if not row:
            return ProspectProfile()
        data = json.loads(row["profile_json"])
        return ProspectProfile.model_validate(data)

    def save_profile(self, conversation_id: str, profile: ProspectProfile) -> None:
        now = utc_now_iso()
        profile.updated_at = datetime.now(timezone.utc)
        payload = profile.model_dump(mode="json")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO prospect_profiles(conversation_id, profile_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET profile_json=excluded.profile_json, updated_at=excluded.updated_at
                """,
                (conversation_id, json.dumps(payload), now),
            )
            conn.commit()

    def get_conversation_state(self, conversation_id: str) -> ConversationState:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM conversation_states WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if not row:
            return ConversationState()
        data = json.loads(row["state_json"])
        return ConversationState.model_validate(data)

    def save_conversation_state(self, conversation_id: str, state: ConversationState) -> None:
        now = utc_now_iso()
        payload = state.model_dump(mode="json")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_states(conversation_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at
                """,
                (conversation_id, json.dumps(payload), now),
            )
            conn.commit()

    def insert_kb_chunks(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO kb_chunks(
                    source_type, source_name, source_ref, section, page, chunk_index, text,
                    metadata_json, vector_json, ingestion_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["source_type"],
                        row["source_name"],
                        row["source_ref"],
                        row.get("section"),
                        row.get("page"),
                        row["chunk_index"],
                        row["text"],
                        json.dumps(row["metadata"]),
                        json.dumps(row["vector"]),
                        row["ingestion_ts"],
                    )
                    for row in rows
                ],
            )
            conn.commit()
        return len(rows)

    def fetch_kb_chunks(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source_type, source_name, source_ref, section, page, chunk_index,
                       text, metadata_json, vector_json, ingestion_ts
                FROM kb_chunks
                ORDER BY id ASC
                """
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            item["vector"] = json.loads(item.pop("vector_json"))
            out.append(item)
        return out

    def log_action(self, conversation_id: str, action_type: str, payload: dict[str, Any], demo_mode: bool) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO action_logs(conversation_id, action_type, payload_json, demo_mode, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_id, action_type, json.dumps(payload), int(demo_mode), now),
            )
            conn.commit()

    def get_actions(self, conversation_id: str, limit: int = 100) -> list[ActionLogRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, conversation_id, action_type, payload_json, demo_mode, created_at
                FROM action_logs
                WHERE conversation_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [
            ActionLogRecord(
                id=row["id"],
                conversation_id=row["conversation_id"],
                action_type=row["action_type"],
                payload=json.loads(row["payload_json"]),
                demo_mode=bool(row["demo_mode"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]
