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
from backend.app.orchestrator import BOUNDARY_LINE, ChatOrchestrator


def _build_stack(tmp_path: Path) -> ChatOrchestrator:
    settings = Settings(
        APP_ENV="test",
        DATABASE_PATH=str(tmp_path / "cta.db"),
        ACTION_LOG_PATH=str(tmp_path / "actions.jsonl"),
        USE_OPENAI_CHAT=False,
    )
    db = Database(Path(settings.database_path))
    db.init()
    ingestion = IngestionService(db, settings)
    ingestion.ingest_raw_text(
        "Global Expat Wealth provides independent financial advice for expats in Asia. "
        "Services include retirement planning, pension strategy, wealth preservation, protection, and cross-border planning.",
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


def _has_link(text: str) -> bool:
    return "calendly.com" in text.lower()


def test_case_1_no_intent_no_link_on_first_message(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    resp = _chat(orchestrator, "Hi, I'm a UK expat in Thailand. Can you help me retire?")

    assert not _has_link(resp.assistant_reply)
    assert resp.assistant_reply.count("?") <= 1


def test_case_2_explicit_intent_allows_link_on_first_message(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    resp = _chat(orchestrator, "How do I get started?")

    assert _has_link(resp.assistant_reply)


def test_case_3_qualifiers_only_no_hard_cta_until_momentum_trigger(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    c_id = None

    r1 = _chat(orchestrator, "I live in Thailand.", c_id)
    c_id = r1.conversation_id
    r2 = _chat(orchestrator, "My main goal is retirement.", c_id)
    r3 = _chat(orchestrator, "My timeline is 10 years.", c_id)

    assert not _has_link(r1.assistant_reply)
    assert not _has_link(r2.assistant_reply)
    assert not _has_link(r3.assistant_reply)

    r4 = _chat(orchestrator, "I need clarity on what to do next.", c_id)
    assert _has_link(r4.assistant_reply)


def test_case_4_personal_advice_handoff_requires_qualifiers_before_link(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    c_id = None

    r1 = _chat(orchestrator, "Should I move my UK pension?", c_id)
    c_id = r1.conversation_id

    assert BOUNDARY_LINE.lower() in r1.assistant_reply.lower()
    assert not _has_link(r1.assistant_reply)

    r2 = _chat(orchestrator, "I live in Thailand and want to retire in 10 years.", c_id)
    assert not _has_link(r2.assistant_reply)

    r3 = _chat(orchestrator, "Given that, should I move it?", c_id)
    assert _has_link(r3.assistant_reply)


def test_case_5_hard_cta_cooldown(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    c_id = None

    r1 = _chat(orchestrator, "How do I get started?", c_id)
    c_id = r1.conversation_id
    assert _has_link(r1.assistant_reply)

    r2 = _chat(orchestrator, "I live in Thailand and want to retire in 8 years.", c_id)
    r3 = _chat(orchestrator, "What next?", c_id)

    assert not _has_link(r2.assistant_reply)
    assert not _has_link(r3.assistant_reply)

    r4 = _chat(orchestrator, "Can you send the booking link again?", c_id)
    assert _has_link(r4.assistant_reply)
