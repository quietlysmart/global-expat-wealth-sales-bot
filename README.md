# Sales Concierge MVP (Global Expat Wealth)

Channel-agnostic backend chatbot + demo web page for lead capture, objection handling, and demo-mode follow-up workflows.

## What this MVP includes

- `FastAPI` backend with `POST /api/chat` for web, WhatsApp, or email channels.
- LLM-first conversation architecture with backend guardrails:
  - Model returns structured JSON turn output (`state_update`, `reply`, optional `ask`, `cta`, `include_booking_link`)
  - Backend persists state JSON per conversation and uses it on the next turn
  - Backend acts as referee: compliance, CTA gate/cooldown, max one question, no repeated answered-slot questions
  - Lightweight output repair strips banned robotic prefixes and extra questions without replacing the full response
- Local persistence via SQLite (`data/sales_concierge.db`).
- Local vector-style retrieval store (hashed sparse embeddings in SQLite).
- RAG ingestion pipeline for:
  - PDF files (from `data/raw` or explicit paths)
  - Website crawl (same-domain, robots aware, page-capped)
  - Raw text input
- Structured citations in API response.
- CTA timing guardrails enforced in backend state machine:
  - Value-first replies before booking links
  - Hard CTA link only when gate conditions are satisfied
  - Hard CTA cooldown and frequency cap
  - Boundary/disclaimer repetition control
- Dynamic move planner per turn:
  - `help_only`, `confirm_and_help`, `help_then_question`, `help_then_soft_cta`, `handoff`
  - Question cooldown to avoid form-like questioning every turn
  - No re-asking already answered slots
  - Slot locking with `answered_keys` + `goal_unclear` branch for "not sure" responses
  - Goal binding supports plain-language variants (e.g., "save more") and typo tolerance (e.g., "reirement")
  - Call-intent handling: phrases like "I thought I had a call" trigger immediate booking-link response with no qualifier question
- Human-style response guardrails enforced in backend:
  - One follow-up question only
  - Plain-language replacements for finance buzzwords
  - Anti-brochure relevance filter and service-dump trimming
  - Word-count cap and chat-style line breaks
- Sales playbook policy pack (`playbooks/sales_playbook.json`) with:
  - Qualification flow
  - Objection handling
  - Compliance guardrails (Posture A)
  - Conversation goals and stage tagging
- Demo-mode action tools:
  - `capture_lead`
  - `draft_followup_email`
  - `create_booking_request`
  - `handoff_to_human`
  - `tag_stage`
  - `log_interaction`
- Demo action logging to:
  - SQLite table `action_logs`
  - JSONL file `logs/actions.jsonl`
- Minimal single-page UI:
  - Logo
  - Title: "Global Expat Wealth Sales Assistant"
  - Chat window only
- Seed conversation test coverage for the 5 must-pass scenarios.

## Repo layout

- `backend/app/main.py` API app and routes
- `backend/app/orchestrator.py` chatbot policy logic and action generation
- `backend/app/dialog_manager.py` session state + question selection logic
- `backend/app/kb.py` ingestion + retrieval
- `backend/app/db.py` SQLite schema and data access
- `playbooks/sales_playbook.json` internal sales policy pack
- `scripts/ingest.py` ingestion CLI
- `scripts/run_seed_conversations.py` demo scenario runner
- `scripts/research_mode.py` optional FAQ/playbook draft helper
- `frontend/index.html` demo page
- `frontend/styles.css` page/chat styling
- `frontend/app.js` frontend chat client
- `tests/test_seed_conversations.py` must-pass behavior tests

## Quick start

1. Create environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Add your OpenAI key in `.env` for live AI inference:

```bash
OPENAI_API_KEY=your_key_here
USE_OPENAI_CHAT=true
```

2. Ingest the provided services PDF:

```bash
python scripts/ingest.py --pdf "List of Services - Dan Whiting PDF-2.pdf"
```

Optional website crawl:

```bash
python scripts/ingest.py --web https://www.globalexpatwealth.com --max-pages 25
```

3. Run backend + demo UI:

```bash
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
```

4. Open demo:

- http://localhost:8000/

Check AI runtime status:

```bash
curl -s http://localhost:8000/health
```

5. Try API directly:

```bash
curl -s http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "I\"m a UK expat in Thailand, can you help me retire in 10 years?",
    "channel_hint": "demo_web",
    "user_metadata": {"timezone": "Asia/Bangkok", "locale": "en-GB"},
    "demo_mode": true
  }' | jq
```

## API contract

### `POST /api/chat`

Request:

```json
{
  "message": "user text",
  "conversation_id": "optional-id",
  "channel_hint": "demo_web",
  "user_metadata": {"timezone": "Asia/Bangkok", "locale": "en-GB"},
  "demo_mode": true
}
```

Response:

```json
{
  "conversation_id": "...",
  "assistant_reply": "...",
  "actions": [
    {"type": "capture_lead", "payload": {}},
    {"type": "draft_followup_email", "payload": {}},
    {"type": "create_booking_request", "payload": {}},
    {"type": "handoff_to_human", "payload": {}},
    {"type": "tag_stage", "payload": {}},
    {"type": "log_interaction", "payload": {}}
  ],
  "prospect_profile": {...},
  "citations": [
    {"source_type": "pdf", "source": "...", "locator": "page 2", "excerpt": "...", "score": 0.31}
  ]
}
```

### `POST /api/ingest`
Ingest PDF paths, website URL, and/or raw text via API.

### `GET /api/conversations/{id}/actions`
Returns logged actions for the conversation.

### `GET /api/conversations/{id}/profile`
Returns current structured prospect profile.

## Compliance posture

This MVP enforces posture A:

- General educational guidance only in chat.
- No personalized financial advice or fund/product recommendations.
- No performance guarantees.
- Recommendation-like requests trigger qualification + handoff.
- Clear next-step language always points to a call with Dan.

## Seed scenarios

Run tests:

```bash
pytest -q
```

Run CTA regression tests only:

```bash
pytest -q tests/test_cta_policy.py
```

Run response-style policy tests only:

```bash
pytest -q tests/test_human_style_policy.py
```

Run dialog architecture regression tests:

```bash
pytest -q tests/test_dialog_refactor_policy.py
```

Run dynamic transcript regression test:

```bash
pytest -q tests/test_dynamic_transcript_flow.py
```

Run goal-locking + soft CTA regression test:

```bash
pytest -q tests/test_goal_locking_and_soft_cta.py
```

Run LLM-driven conversation architecture regression tests:

```bash
pytest -q tests/test_llm_driven_conversation_architecture.py
```

Run interactive scripted demo conversation against live API:

```bash
python scripts/run_seed_conversations.py
```

Covers:
1. UK expat in Thailand retiring in 10 years
2. Fees objection
3. Best fund request (safe response + handoff)
4. Trust objection
5. UK pension move yes/no request (safe response + handoff)

## Demo-mode behavior

All integrations are simulated:

- No emails are sent.
- No meetings are booked.
- No WhatsApp messages are sent.

The assistant language and action payloads explicitly indicate **prepared/drafted, not sent**.

## Optional: research mode helper

Build a starter FAQ/playbook draft from ingested KB:

```bash
python scripts/research_mode.py
```

Output:
- `data/research_mode_faq_draft.md`

## Docker

```bash
docker compose up --build
```

Then open:
- http://localhost:8000/
