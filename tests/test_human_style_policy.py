from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.config import Settings
from backend.app.db import Database
from backend.app.kb import IngestionService, RetrievalService
from backend.app.models import ChatRequest
from backend.app.orchestrator import BUZZWORD_REPLACEMENTS, ChatOrchestrator


def _build_stack(tmp_path: Path) -> ChatOrchestrator:
    settings = Settings(
        APP_ENV="test",
        DATABASE_PATH=str(tmp_path / "style.db"),
        ACTION_LOG_PATH=str(tmp_path / "actions.jsonl"),
        USE_OPENAI_CHAT=False,
    )
    db = Database(Path(settings.database_path))
    db.init()
    ingestion = IngestionService(db, settings)
    ingestion.ingest_raw_text(
        "Global Expat Wealth helps expats with retirement and cross-border planning.",
        title="seed_services",
    )
    retrieval = RetrievalService(db, settings)
    return ChatOrchestrator(db, retrieval, settings)


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", text))


def test_validator_enforces_single_question_and_no_duplicates(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    bad = (
        "We can help with tax-efficient planning and a diversified portfolio. "
        "Which country are you in? Which country are you in? "
        "We also support income strategies and wealth preservation."
    )
    out = orchestrator._enforce_human_response_policy(
        answer_text=bad,
        user_message="Can you help me retire abroad?",
        question_line="Which country are you living in now?",
        soft_cta_line=None,
        link_line=None,
        hard_cta_shown=False,
        soft_cta_shown=False,
        boundary_allowed=False,
    )

    assert out.count("?") == 1
    assert out.lower().count("which country") == 1


def test_validator_removes_buzzwords_and_service_dump(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    bad = (
        "We do retirement planning, tax planning, trusts, golden visas, protection, and pension consolidation. "
        "This includes tax-efficient ideas, bespoke plans, and decumulation models. "
        "What timeline are you aiming for?"
    )
    out = orchestrator._enforce_human_response_policy(
        answer_text=bad,
        user_message="Can you help me with retirement?",
        question_line="What timeline are you aiming for?",
        soft_cta_line=None,
        link_line=None,
        hard_cta_shown=False,
        soft_cta_shown=False,
        boundary_allowed=False,
    )

    lower = out.lower()
    for term in BUZZWORD_REPLACEMENTS:
        assert term not in lower

    assert "golden visa" not in lower
    assert "second citizenship" not in lower


def test_validator_limits_length_and_formats_chat_blocks(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    long_blob = (
        "This is a very long response that keeps going with too much detail about every possible service and feature "
        "and it keeps listing many things without stopping so it feels like a brochure and not a chat response for a user "
        "who asked a simple question and it also includes several extra lines of context that are not needed right now "
        "because we should keep this short and focused and easy to read in chat while staying helpful and direct. "
        "What country are you in?"
    )

    out = orchestrator._enforce_human_response_policy(
        answer_text=long_blob,
        user_message="Can you help me retire?",
        question_line="Where do you live right now?",
        soft_cta_line=None,
        link_line=None,
        hard_cta_shown=False,
        soft_cta_shown=False,
        boundary_allowed=False,
    )

    assert _word_count(out) <= 130
    assert "\n\n" in out
    assert out.count("?") == 1


def test_live_reply_policy_no_more_than_one_question(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    resp = orchestrator.handle_chat(
        ChatRequest(
            message="I am a UK expat in Thailand and I am confused about retirement options.",
            channel_hint="demo_web",
            user_metadata={"timezone": "Asia/Bangkok", "locale": "en-GB"},
            demo_mode=True,
        )
    )

    assert resp.assistant_reply.count("?") <= 1
    assert "\n\n" in resp.assistant_reply


def test_finalize_repairs_clipped_tail_fragments(tmp_path: Path) -> None:
    orchestrator = _build_stack(tmp_path)
    out = orchestrator._finalize_writer_message(
        text="Having around $50k gives you a solid base to start investing. We can look at what mix.",
        include_booking_link=False,
        allow_expanded=False,
        expected_question=None,
        user_message="around 50k us",
        soft_cta_line=None,
    )
    lower = out.lower()
    assert "what mix." not in lower
    assert not lower.endswith(" we.")
