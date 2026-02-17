from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import Database
from .kb import IngestionService, RetrievalService
from .models import (
    ChatRequest,
    ChatResponse,
    CurrentUserResponse,
    IngestRequest,
    IngestResponse,
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    ProspectProfile,
    TranscribeResponse,
)
from .orchestrator import ChatOrchestrator

settings = get_settings()
db = Database(settings.database_file)
db.init()
if settings.initial_user_email and settings.initial_user_password:
    db.ensure_user(settings.initial_user_email, settings.initial_user_password)
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


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    auth = authorization.strip()
    if not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    return token or None


def require_authenticated_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not settings.auth_required:
        return {"id": 0, "email": "demo@local"}
    token = _extract_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    user = db.get_user_by_session_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return user


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "ai_enabled": chat.llm.enabled,
        "ai_reason": None if chat.llm.enabled else chat.llm.disabled_reason,
        "model": settings.openai_model,
        "auth_required": settings.auth_required,
    }


@app.post("/api/login", response_model=LoginResponse)
def login(payload: LoginRequest) -> LoginResponse:
    user = db.verify_user_credentials(payload.email, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = db.create_session(int(user["id"]), ttl_hours=settings.auth_session_ttl_hours)
    return LoginResponse(access_token=token, user_email=str(user["email"]))


@app.post("/api/logout", response_model=LogoutResponse)
def logout(
    authorization: str | None = Header(default=None),
    _: dict[str, Any] = Depends(require_authenticated_user),
) -> LogoutResponse:
    token = _extract_bearer_token(authorization)
    if token:
        db.revoke_session(token)
    return LogoutResponse(ok=True)


@app.get("/api/me", response_model=CurrentUserResponse)
def me(user: dict[str, Any] = Depends(require_authenticated_user)) -> CurrentUserResponse:
    return CurrentUserResponse(email=str(user["email"]))


@app.post("/api/chat", response_model=ChatResponse)
def chat_endpoint(
    payload: ChatRequest,
    _: dict[str, Any] = Depends(require_authenticated_user),
) -> ChatResponse:
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="message is required")
    return chat.handle_chat(payload)


@app.post("/api/transcribe", response_model=TranscribeResponse)
async def transcribe_endpoint(
    audio: UploadFile = File(...),
    _: dict[str, Any] = Depends(require_authenticated_user),
) -> TranscribeResponse:
    if not chat.llm.enabled:
        raise HTTPException(status_code=503, detail="voice transcription unavailable")
    raw = await audio.read()
    if not raw:
        raise HTTPException(status_code=400, detail="audio is empty")
    text = chat.llm.transcribe_audio(
        raw,
        filename=audio.filename or "voice_input.webm",
        content_type=audio.content_type,
    )
    if not text:
        raise HTTPException(status_code=502, detail="could not transcribe audio")
    return TranscribeResponse(text=text)


@app.post("/api/ingest", response_model=IngestResponse)
def ingest_endpoint(
    payload: IngestRequest,
    _: dict[str, Any] = Depends(require_authenticated_user),
) -> IngestResponse:
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
def actions_endpoint(
    conversation_id: str,
    _: dict[str, Any] = Depends(require_authenticated_user),
) -> dict[str, object]:
    actions = db.get_actions(conversation_id)
    return {"conversation_id": conversation_id, "actions": [a.model_dump() for a in actions]}


@app.get("/api/conversations/{conversation_id}/profile", response_model=ProspectProfile)
def profile_endpoint(
    conversation_id: str,
    _: dict[str, Any] = Depends(require_authenticated_user),
) -> ProspectProfile:
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
