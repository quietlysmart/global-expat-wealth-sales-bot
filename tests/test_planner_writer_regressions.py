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
        DATABASE_PATH=str(tmp_path / "planner_writer.db"),
        ACTION_LOG_PATH=str(tmp_path / "actions.jsonl"),
        USE_OPENAI_CHAT=False,
    )
    db = Database(Path(settings.database_path))
    db.init()
    ingestion = IngestionService(db, settings)
    ingestion.ingest_raw_text(
        "Global Expat Wealth helps expats with retirement, UK pension reviews, and cross-border financial planning.",
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


def test_question_marks_present_when_question_is_asked(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    r1 = _chat(orchestrator, "Can you help me retire?")
    if any(q in r1.assistant_reply.lower() for q in ["where do you", "what are you mainly", "when do you want"]):
        assert "?" in r1.assistant_reply


def test_cta_not_shown_before_gating(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    r1 = _chat(orchestrator, "Hi, I am exploring retirement options.")
    assert "calendly.com" not in r1.assistant_reply.lower()


def test_no_draft_prepared_phrase_in_chat(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    r1 = _chat(orchestrator, "My email is person@example.com")
    lower = r1.assistant_reply.lower()
    assert "draft prepared" not in lower
    assert "prepared a draft" not in lower
    assert "not sent" not in lower


def test_natural_cadence_with_money_lol_and_whats_up(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    flow = ["whats up?", "money lol", "ok", "great", "retirement maybe", "thailand"]
    conversation_id = None
    replies: list[str] = []
    for msg in flow:
        r = _chat(orchestrator, msg, conversation_id)
        conversation_id = r.conversation_id
        replies.append(r.assistant_reply.lower())

    assert "where do you live right now?" not in replies[0]
    assert sum(reply.count("?") for reply in replies) <= len(replies)
    assert any("?" not in reply for reply in replies)

