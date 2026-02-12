from __future__ import annotations

from pathlib import Path
import sys

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
        DATABASE_PATH=str(tmp_path / "test.db"),
        ACTION_LOG_PATH=str(tmp_path / "actions.jsonl"),
        USE_OPENAI_CHAT=False,
    )
    db = Database(Path(settings.database_path))
    db.init()
    ingestion = IngestionService(db, settings)
    ingestion.ingest_raw_text(
        "Independent Financial Advice for Expats in Asia. "
        "Plan, invest, protect and pass on your wealth with objective, cross-border guidance tailored to life abroad. "
        "Global Expat Wealth has 27 years' cross-border experience and independent access to funds and ETFs. "
        "Services include retirement planning, UK pension consolidation, protection, tax planning, wealth preservation, and beneficiary structures.",
        title="seed_services",
    )
    retrieval = RetrievalService(db, settings)
    return ChatOrchestrator(db, retrieval, settings)


def test_seed_conversations(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)

    seeds = [
        "I'm a UK expat in Thailand, can you help me retire in 10 years?",
        "What are your fees?",
        "Can you recommend the best fund?",
        "I don't trust advisors, I've been burned before.",
        "I want to move my UK pension. Is that a good idea?",
    ]

    conversation_id = None
    replies: list[str] = []
    action_sets: list[list[str]] = []

    for seed in seeds:
        response = orchestrator.handle_chat(
            ChatRequest(
                message=seed,
                conversation_id=conversation_id,
                channel_hint="demo_web",
                user_metadata={"timezone": "Asia/Bangkok", "locale": "en-GB"},
                demo_mode=True,
            )
        )
        conversation_id = response.conversation_id
        replies.append(response.assistant_reply.lower())
        action_sets.append([action.type for action in response.actions])

    # General-safe + qualification checks across all scripts
    assert all(reply.count("?") <= 1 for reply in replies)

    # Personalized recommendation should trigger guardrails
    assert "can't provide personalized" in replies[2] or "general guidance" in replies[2]

    # Pension yes/no should trigger handoff and disclaimer
    assert "can't provide personalized" in replies[4]

    # Actions should always include stage tagging and logging
    for actions in action_sets:
        assert "tag_stage" in actions
        assert "log_interaction" in actions

    # Fund and pension prompts should trigger handoff action
    assert "handoff_to_human" in action_sets[2]
    assert "handoff_to_human" in action_sets[4]
