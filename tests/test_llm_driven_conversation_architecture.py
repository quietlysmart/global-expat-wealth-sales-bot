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
        DATABASE_PATH=str(tmp_path / "llm_arch.db"),
        ACTION_LOG_PATH=str(tmp_path / "actions.jsonl"),
        USE_OPENAI_CHAT=False,
    )
    db = Database(Path(settings.database_path))
    db.init()
    ingestion = IngestionService(db, settings)
    ingestion.ingest_raw_text(
        "Global Expat Wealth helps expats with retirement, pensions, and general planning support.",
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


def _extract_slot_from_reply(reply: str) -> str | None:
    lower = reply.lower()
    if "where do you live right now?" in lower:
        return "country"
    if "what are you mainly trying to do" in lower or "what's the main thing right now" in lower:
        return "goal"
    if "when do you want to get this sorted" in lower:
        return "timeline"
    if "is this about a uk pension, savings you already have, or starting from scratch?" in lower:
        return "assets_context"
    if "do you have any uk pensions you want to review?" in lower:
        return "uk_pension"
    return None


def test_call_intent_message_gets_booking_link_without_qualifier_question(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    r1 = _chat(orchestrator, "I thought I was going to have a call")

    lower = r1.assistant_reply.lower()
    assert "calendly.com" in lower
    assert "?" not in r1.assistant_reply


def test_session_density_prefix_and_low_signal_progression(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    flow = [
        "whats up?",
        "ok",
        "great",
        "i live in thailand",
        "retirement",
        "ok",
        "10 years",
        "great",
        "i thought i was going to have a call",
        "thanks",
    ]

    conversation_id = None
    replies: list[str] = []
    asked_slots: list[str | None] = []
    for message in flow:
        resp = _chat(orchestrator, message, conversation_id)
        conversation_id = resp.conversation_id
        replies.append(resp.assistant_reply)
        asked_slots.append(_extract_slot_from_reply(resp.assistant_reply))

    lower_replies = [reply.lower() for reply in replies]
    assert all("one thing first" not in reply for reply in lower_replies)
    assert all("just so i don't assume" not in reply for reply in lower_replies)
    assert all("to make this useful" not in reply for reply in lower_replies)

    avg_questions = sum(reply.count("?") for reply in replies) / len(replies)
    assert avg_questions < 0.9

    # "ok/great" should not trigger re-asking the exact previous slot question.
    low_signal_indexes = [1, 2, 5, 7]  # indexes in user flow for ok/great
    for idx in low_signal_indexes:
        if idx == 0:
            continue
        prev_slot = asked_slots[idx - 1]
        next_slot = asked_slots[idx]
        if prev_slot and next_slot:
            assert next_slot != prev_slot

    # No repeated slot question once answered in this progression.
    # Goal answered at "retirement", country at "i live in thailand", timeline at "10 years".
    later = " ".join(lower_replies[6:])
    assert "what are you mainly trying to do" not in later
    assert "where do you live right now?" not in later
    assert "when do you want to get this sorted" not in later

