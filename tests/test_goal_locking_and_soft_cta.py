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
        DATABASE_PATH=str(tmp_path / "goal_locking.db"),
        ACTION_LOG_PATH=str(tmp_path / "actions.jsonl"),
        USE_OPENAI_CHAT=False,
    )
    db = Database(Path(settings.database_path))
    db.init()
    ingestion = IngestionService(db, settings)
    ingestion.ingest_raw_text(
        "Global Expat Wealth helps expats with retirement and investment planning.",
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


def test_goal_locking_with_not_sure_and_typos(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    flow = [
        "how can you help me?",
        "not sure",
        "i guess i wanna save more",
        "reirement i suppose",
    ]

    replies: list[str] = []
    conversation_id = None
    for msg in flow:
        resp = _chat(orchestrator, msg, conversation_id)
        conversation_id = resp.conversation_id
        replies.append(resp.assistant_reply.lower())

    # Broad goal question should not be repeated in the flow.
    broad_goal = "what are you mainly trying to do - retirement, investing a lump sum, or something else?"
    assert sum(1 for reply in replies if broad_goal in reply) <= 1

    # Once goal is mapped ("save more" or typo'd retirement), do not ask goal again.
    triage_goal = "what's the main thing right now - save more, retire sooner, or feel safer if something goes wrong?"
    assert all(broad_goal not in reply and triage_goal not in reply for reply in replies[3:])

    # Goal should be captured in state/profile.
    state = orchestrator.db.get_conversation_state(conversation_id)
    profile = orchestrator.db.get_profile(conversation_id)
    assert bool(profile.goals) or bool(state.qualifiers_collected.primary_goal)
    assert "goal" in state.answered_keys

    soft_cta_markers = [
        "walk through the personal trade-offs on a quick call",
        "do this faster on a call when you're ready",
    ]
    soft_cta_count = sum(1 for reply in replies[:8] if any(marker in reply for marker in soft_cta_markers))
    assert soft_cta_count <= 1

    # No soft CTA in same turn as a follow-up question.
    for reply in replies[:8]:
        has_question = "?" in reply
        has_soft_cta = any(marker in reply for marker in soft_cta_markers)
        assert not (has_question and has_soft_cta)

