from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.config import Settings
from backend.app.db import Database
from backend.app.kb import IngestionService, RetrievalService
from backend.app.models import ChatRequest
from backend.app.orchestrator import ChatOrchestrator


def _build_stack(tmp_path: Path) -> ChatOrchestrator:
    settings = Settings(
        APP_ENV="test",
        DATABASE_PATH=str(tmp_path / "humanization.db"),
        ACTION_LOG_PATH=str(tmp_path / "actions.jsonl"),
        USE_OPENAI_CHAT=False,
    )
    db = Database(Path(settings.database_path))
    db.init()
    ingestion = IngestionService(db, settings)
    ingestion.ingest_raw_text(
        "Global Expat Wealth helps expats with retirement planning, pensions, and independent guidance.",
        title="seed_services",
    )
    retrieval = RetrievalService(db, settings)
    return ChatOrchestrator(db, retrieval, settings)


def _chat(orchestrator: ChatOrchestrator, message: str, conversation_id: str | None = None):
    return orchestrator.handle_chat(
        ChatRequest(
            message=message,
            conversation_id=conversation_id,
            channel_hint="demo_web",
            user_metadata={"timezone": "Asia/Bangkok", "locale": "en-GB"},
            demo_mode=True,
        )
    )


def test_demo_mode_email_copy_never_mentions_draft_or_not_sent(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    r1 = _chat(orchestrator, "My email is test@example.com")
    lower = r1.assistant_reply.lower()
    assert "draft" not in lower
    assert "not sent" not in lower
    assert "sent a confirmation email" in lower or "email you the call details now" in lower


def test_small_talk_opening_uses_open_question_not_country(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    r1 = _chat(orchestrator, "whats up?")
    lower = r1.assistant_reply.lower()
    assert "where do you live right now?" not in lower
    assert "saving, retirement, or just getting organised" in lower


def test_topic_override_fees_answer_before_timeline(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    r1 = _chat(orchestrator, "how much does it cost?")
    lower = r1.assistant_reply.lower()
    assert "fee" in lower or "cost" in lower or "pricing" in lower
    assert "when do you want to get this sorted" not in lower


def test_cadence_has_multiple_non_question_turns(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    flow = [
        "hi",
        "ok",
        "great",
        "i live in thailand",
        "retirement",
        "10 years",
        "uk pension",
        "thanks",
    ]
    conversation_id = None
    replies: list[str] = []
    for msg in flow:
        resp = _chat(orchestrator, msg, conversation_id)
        conversation_id = resp.conversation_id
        replies.append(resp.assistant_reply)

    no_question_turns = sum(1 for reply in replies if "?" not in reply)
    assert no_question_turns >= 2


def test_no_exact_repeat_qualifier_after_answered(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    r1 = _chat(orchestrator, "Can you help me retire?")
    c_id = r1.conversation_id
    r2 = _chat(orchestrator, "Thailand", c_id)
    r3 = _chat(orchestrator, "10 years", c_id)
    joined = " ".join([r2.assistant_reply.lower(), r3.assistant_reply.lower()])
    assert joined.count("where do you live right now?") == 0

