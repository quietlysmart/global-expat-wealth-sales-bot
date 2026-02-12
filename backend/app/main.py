from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import Database
from .kb import IngestionService, RetrievalService
from .models import ChatRequest, ChatResponse, IngestRequest, IngestResponse, ProspectProfile
from .orchestrator import ChatOrchestrator

settings = get_settings()
db = Database(settings.database_file)
db.init()
retrieval = RetrievalService(db, settings)
ingestion = IngestionService(db, settings)
chat = ChatOrchestrator(db, retrieval, settings)

app = FastAPI(title="Sales Concierge MVP", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "ai_enabled": chat.llm.enabled,
        "ai_reason": None if chat.llm.enabled else chat.llm.disabled_reason,
        "model": settings.openai_model,
    }


@app.post("/api/chat", response_model=ChatResponse)
def chat_endpoint(payload: ChatRequest) -> ChatResponse:
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="message is required")
    return chat.handle_chat(payload)


@app.post("/api/ingest", response_model=IngestResponse)
def ingest_endpoint(payload: IngestRequest) -> IngestResponse:
    chunks_added = 0
    sources: list[str] = []

    for pdf in payload.pdf_paths:
        chunks = ingestion.ingest_pdf(pdf)
        chunks_added += chunks
        sources.append(f"pdf:{pdf}")

    if payload.website_start_url:
        chunks = ingestion.ingest_website(payload.website_start_url, max_pages=payload.website_max_pages)
        chunks_added += chunks
        sources.append(f"web:{payload.website_start_url}")

    if payload.raw_text:
        chunks = ingestion.ingest_raw_text(payload.raw_text, title=payload.raw_text_title)
        chunks_added += chunks
        sources.append(f"text:{payload.raw_text_title}")

    return IngestResponse(chunks_added=chunks_added, sources_processed=sources)


@app.get("/api/conversations/{conversation_id}/actions")
def actions_endpoint(conversation_id: str) -> dict[str, object]:
    actions = db.get_actions(conversation_id)
    return {"conversation_id": conversation_id, "actions": [a.model_dump() for a in actions]}


@app.get("/api/conversations/{conversation_id}/profile", response_model=ProspectProfile)
def profile_endpoint(conversation_id: str) -> ProspectProfile:
    return db.get_profile(conversation_id)


frontend_dir = Path("frontend")
if frontend_dir.exists():
    app.mount("/frontend", StaticFiles(directory=frontend_dir), name="frontend")


@app.get("/")
def root() -> FileResponse:
    index_path = Path("frontend/index.html")
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Demo UI not found")
    return FileResponse(index_path)
