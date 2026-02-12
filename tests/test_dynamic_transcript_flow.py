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
        DATABASE_PATH=str(tmp_path / "dynamic.db"),
        ACTION_LOG_PATH=str(tmp_path / "actions.jsonl"),
        USE_OPENAI_CHAT=False,
    )
    db = Database(Path(settings.database_path))
    db.init()
    ingestion = IngestionService(db, settings)
    ingestion.ingest_raw_text(
        "Global Expat Wealth helps expats with retirement and UK pension questions.",
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


def test_dynamic_transcript_does_not_repeat_or_over_question(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    messages = [
        "how can you help?",
        "retirement",
        "thailand",
        "soon",
        "uk pension",
    ]

    conversation_id = None
    replies: list[str] = []
    for message in messages:
        resp = _chat(orchestrator, message, conversation_id)
        conversation_id = resp.conversation_id
        replies.append(resp.assistant_reply)

    lower_replies = [reply.lower() for reply in replies]
    assert "sales assistant" not in lower_replies[0]
    assert all("quick question:" not in reply for reply in lower_replies)

    # Should not ask a question on every assistant turn.
    question_turns = [reply for reply in replies if "?" in reply]
    assert len(question_turns) < len(replies)

    # Once country is provided, the country question should not appear again.
    assert all("where do you live right now?" not in reply for reply in lower_replies[3:])

    # Once timeline is provided, timeline question should not appear again.
    timeline_q = "when do you want to get this sorted - soon, this year, or later?"
    assert all(timeline_q not in reply for reply in lower_replies[4:])

    soft_cta_markers = [
        "walk through the personal trade-offs on a quick call",
        "do this faster on a call when you're ready",
    ]
    soft_cta_count = sum(1 for reply in lower_replies if any(marker in reply for marker in soft_cta_markers))
    assert soft_cta_count <= 1

