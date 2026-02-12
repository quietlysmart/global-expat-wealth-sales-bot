from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.config import Settings
from backend.app.db import Database
from backend.app.dialog_manager import QUESTION_MAP
from backend.app.kb import IngestionService, RetrievalService
from backend.app.models import ChatRequest
from backend.app.orchestrator import BUZZWORD_REPLACEMENTS, ChatOrchestrator


def _build_stack(tmp_path: Path) -> ChatOrchestrator:
    settings = Settings(
        APP_ENV="test",
        DATABASE_PATH=str(tmp_path / "dialog.db"),
        ACTION_LOG_PATH=str(tmp_path / "actions.jsonl"),
        USE_OPENAI_CHAT=False,
    )
    db = Database(Path(settings.database_path))
    db.init()
    ingestion = IngestionService(db, settings)
    ingestion.ingest_raw_text(
        "Global Expat Wealth helps expats with retirement and pensions.",
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


def test_greeting_not_sales_assistant() -> None:
    app_js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8").lower()
    assert "hi. what can i help you figure out?" in app_js
    assert "sales assistant" not in app_js


def test_one_question_mark_max(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    response = _chat(orchestrator, "I am confused about retirement choices.")
    assert response.assistant_reply.count("?") <= 1


def test_no_repeat_question_key_after_answer(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    r1 = _chat(orchestrator, "Can you help me retire in 10 years?")
    c_id = r1.conversation_id
    r2 = _chat(orchestrator, "Thailand", c_id)

    country_q = QUESTION_MAP["country"].lower()
    assert country_q in r1.assistant_reply.lower() or "where do you live right now" in r1.assistant_reply.lower()
    assert country_q not in r2.assistant_reply.lower()


def test_no_banned_buzzwords_in_reply(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    response = _chat(orchestrator, "How should I think about retirement planning?")
    lower = response.assistant_reply.lower()
    for term in BUZZWORD_REPLACEMENTS:
        assert term not in lower


def test_not_single_paragraph_if_long(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    response = _chat(orchestrator, "Can you explain retirement planning for expats in detail with all options and steps?")
    text = response.assistant_reply
    if len(text) > 280:
        assert "\n\n" in text


def test_no_booking_link_when_cta_gate_false(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    response = _chat(orchestrator, "Hi, I am a UK expat in Thailand. Can you help me retire?")
    assert "calendly.com" not in response.assistant_reply.lower()
