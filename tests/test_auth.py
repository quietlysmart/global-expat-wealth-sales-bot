from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.db import Database


def test_user_password_hash_and_verify(tmp_path: Path) -> None:
    db = Database(tmp_path / "auth.db")
    db.init()
    db.ensure_user("user@example.com", "TopSecret123!")

    ok_user = db.verify_user_credentials("user@example.com", "TopSecret123!")
    bad_user = db.verify_user_credentials("user@example.com", "wrong-password")

    assert ok_user is not None
    assert ok_user["email"] == "user@example.com"
    assert bad_user is None


def test_session_create_validate_and_revoke(tmp_path: Path) -> None:
    db = Database(tmp_path / "auth_sessions.db")
    db.init()
    user_id = db.ensure_user("user@example.com", "TopSecret123!")
    token = db.create_session(user_id, ttl_hours=2)

    user = db.get_user_by_session_token(token)
    assert user is not None
    assert user["email"] == "user@example.com"

    db.revoke_session(token)
    revoked_user = db.get_user_by_session_token(token)
    assert revoked_user is None
