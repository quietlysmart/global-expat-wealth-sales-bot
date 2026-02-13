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


def test_move_transcript_advances_after_short_country_answer(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    conversation_id = None

    r1 = _chat(orchestrator, "im planning to move to thailand", conversation_id)
    conversation_id = r1.conversation_id
    r2 = _chat(orchestrator, "uk", conversation_id)
    r3 = _chat(orchestrator, "cool.", conversation_id)
    r4 = _chat(orchestrator, "retirement", conversation_id)

    replies = [r1.assistant_reply.lower(), r2.assistant_reply.lower(), r3.assistant_reply.lower(), r4.assistant_reply.lower()]

    # After a short country answer, the bot should move forward to the next qualifier.
    assert "what are you mainly trying to do - retirement, investing a lump sum, or something else?" in replies[1]
    assert "where do you live right now?" not in replies[1]

    # Avoid repetitive canned filler.
    assert sum("yes, we can help" in reply for reply in replies) <= 1
    assert sum("we can build from that" in reply for reply in replies) == 0

    # Conversation should continue progressing.
    assert "when do you want to get this sorted - soon, this year, or later?" in replies[3]


def test_question_sentence_is_clean_single_sentence(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    out = orchestrator._finalize_writer_message(
        text=(
            "Here are a few practical steps to start with retirement saving.\n\n"
            "Which country are you based in. This helps me provide advice tailored to your location?"
        ),
        include_booking_link=False,
        allow_expanded=False,
        expected_question="Which country are you based in. This helps me provide advice tailored to your location?",
        user_message="how to save for retirement",
    )
    assert out.count("?") == 1
    assert "which country are you based in?" in out.lower()
    assert "this helps me provide advice tailored to your location?" not in out.lower()
