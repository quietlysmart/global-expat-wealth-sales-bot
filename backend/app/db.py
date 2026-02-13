from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );

                CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_id ON auth_sessions(user_id);
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

    @staticmethod
    def _hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
        salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260000)
        return salt.hex(), derived.hex()

    @staticmethod
    def _hash_session_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def ensure_user(self, email: str, password: str) -> int:
        normalized_email = email.strip().lower()
        if not normalized_email or not password:
            raise ValueError("email and password are required")

        salt_hex, password_hash = self._hash_password(password)
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO users(email, password_salt, password_hash, is_active, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(email) DO UPDATE
                SET password_salt=excluded.password_salt,
                    password_hash=excluded.password_hash,
                    is_active=1,
                    updated_at=excluded.updated_at
                """,
                (normalized_email, salt_hex, password_hash, now, now),
            )
            row = conn.execute("SELECT id FROM users WHERE email=?", (normalized_email,)).fetchone()
            conn.commit()
        if not row:
            raise RuntimeError("failed to create or update user")
        return int(row["id"])

    def verify_user_credentials(self, email: str, password: str) -> dict[str, Any] | None:
        normalized_email = email.strip().lower()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, email, password_salt, password_hash, is_active FROM users WHERE email=?",
                (normalized_email,),
            ).fetchone()
        if not row or not bool(row["is_active"]):
            return None

        _, check_hash = self._hash_password(password, salt_hex=str(row["password_salt"]))
        if not hmac.compare_digest(check_hash, str(row["password_hash"])):
            return None

        return {"id": int(row["id"]), "email": str(row["email"])}

    def create_session(self, user_id: int, ttl_hours: int = 24) -> str:
        token = secrets.token_urlsafe(48)
        token_hash = self._hash_session_token(token)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=max(1, ttl_hours))

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO auth_sessions(token_hash, user_id, created_at, expires_at, revoked_at)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (token_hash, user_id, now.isoformat(), expires_at.isoformat()),
            )
            conn.commit()
        return token

    def get_user_by_session_token(self, token: str) -> dict[str, Any] | None:
        token_hash = self._hash_session_token(token)
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT u.id, u.email
                FROM auth_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash=?
                  AND s.revoked_at IS NULL
                  AND s.expires_at > ?
                  AND u.is_active=1
                """,
                (token_hash, now_iso),
            ).fetchone()
        if not row:
            return None
        return {"id": int(row["id"]), "email": str(row["email"])}

    def revoke_session(self, token: str) -> None:
        token_hash = self._hash_session_token(token)
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                (now, token_hash),
            )
            conn.commit()
