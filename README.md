# Sales Concierge MVP (Global Expat Wealth)

Channel-agnostic backend chatbot + demo web page for lead capture, objection handling, and demo-mode follow-up workflows.

## What this MVP includes

- `FastAPI` backend with `POST /api/chat` for web, WhatsApp, or email channels.
- Planner -> Referee -> Writer architecture:
  - **Planner (LLM step 1)** returns strict JSON (`intent`, `ack`, `value`, `next_question`, `cta`, `slot_updates`, `state_update`, `verbosity`)
  - **Referee (backend)** validates guardrails only (max one question, no re-asking filled slots, CTA gating/cooldown, compliance boundary)
  - **Writer (LLM step 2)** generates final natural chat message from the approved plan
  - Backend stores state server-side and feeds it back each turn (`slots`, `stage`, `lead_score`, `lead_fit`, `running_summary`, topic state)
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
- `backend/app/dialog_manager.py` legacy helpers (no longer primary flow controller)
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
OPENAI_MODEL=gpt-5-mini
AUTH_REQUIRED=true
```

Create a login user (stored securely with password hashing in SQLite):

```bash
python scripts/create_user.py --email dan.whiting@globalexpatwealth.com --password "your_password_here"
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

Log in with your created user on the login screen, then use the chat.

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

### `POST /api/login`
Creates an authenticated session token.

### `POST /api/logout`
Revokes current session token.

### `GET /api/me`
Returns current authenticated user email.

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

Run planner/writer transcript regressions (includes `whats up?` and `money lol` paths):

```bash
pytest -q tests/test_planner_writer_regressions.py
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

Demo mode simulates follow-up actions as completed so the chat feels realistic:

- Email follow-up is phrased as sent in chat.
- Booking actions are phrased as scheduled/arranged in chat.
- Internal action objects still capture status for debugging.

## Conversation Tuning

Key tuning points:

- CTA gating + cooldown: `/Users/unclematty/Library/Mobile Documents/com~apple~CloudDocs/A.I./Dan Whiting Wealth/sales bot/backend/app/orchestrator.py` in `_hard_cta_reason`, `_passes_hard_cta_limits`, `_should_show_soft_cta`
- Question strategy / no-repeat checks: `/Users/unclematty/Library/Mobile Documents/com~apple~CloudDocs/A.I./Dan Whiting Wealth/sales bot/backend/app/orchestrator.py` in `_repair_plan_output`
- Topic override (fees, retirement, etc.): `/Users/unclematty/Library/Mobile Documents/com~apple~CloudDocs/A.I./Dan Whiting Wealth/sales bot/backend/app/orchestrator.py` in `_detect_topic`, `_update_active_topic`
- Planner and writer prompts / model: `/Users/unclematty/Library/Mobile Documents/com~apple~CloudDocs/A.I./Dan Whiting Wealth/sales bot/backend/app/llm.py`

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
