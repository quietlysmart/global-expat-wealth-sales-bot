from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


ChannelHint = Literal["demo_web", "whatsapp", "email"]
ActionType = Literal[
    "capture_lead",
    "draft_followup_email",
    "create_booking_request",
    "handoff_to_human",
    "tag_stage",
    "log_interaction",
]
StageType = Literal["awareness", "consideration", "intent"]
ConversationFlowStage = Literal[
    "greeting",
    "discovery",
    "helping",
    "qualifying",
    "faq",
    "objection",
    "handoff",
    "closing",
    "ready_for_cta",
]


class UserMetadata(BaseModel):
    timezone: str | None = None
    locale: str | None = None


class Citation(BaseModel):
    source_type: Literal["pdf", "web", "text"]
    source: str
    locator: str | None = None
    excerpt: str | None = None
    score: float


class Action(BaseModel):
    type: ActionType
    payload: dict[str, Any]


class ProspectProfile(BaseModel):
    name: str | None = None
    email: str | None = None
    country_of_residence: str | None = None
    nationality: str | None = None
    goals: list[str] = Field(default_factory=list)
    timeframe: str | None = None
    asset_band: str | None = None
    pension_interest: str | None = None
    risk_comfort: str | None = None
    timezone: str | None = None
    locale: str | None = None
    stage: StageType = "awareness"
    notes: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class QualifiersCollected(BaseModel):
    location_country: str | None = None
    nationality: str | None = None
    primary_goal: str | None = None
    timeline: str | None = None
    assets_context: str | None = None
    uk_pension: bool | None = None


class ConversationCounters(BaseModel):
    assistant_turn_count: int = 0
    hard_cta_last_shown_turn: int | None = None
    hard_cta_shown_count: int = 0
    soft_cta_last_shown_turn: int | None = None
    question_last_shown_turn: int | None = None


class ConversationState(BaseModel):
    stage: ConversationFlowStage = "greeting"
    qualifiers_collected: QualifiersCollected = Field(default_factory=QualifiersCollected)
    counters: ConversationCounters = Field(default_factory=ConversationCounters)
    boundary_shown: bool = False
    first_meaningful_value_delivered: bool = False
    helpful_turn_count: int = 0
    last_question_key: str | None = None
    question_history: list[str] = Field(default_factory=list)
    last_question_prefix_style: str | None = None
    recent_assistant_sentences: list[str] = Field(default_factory=list)
    answered_keys: set[str] = Field(default_factory=set)
    goal_unclear: bool = False
    active_topic: str = "general"
    topic_turns_remaining: int = 0
    slots: dict[str, Any] = Field(default_factory=dict)
    asked_questions: list[str] = Field(default_factory=list)
    asked: list[str] = Field(default_factory=list)
    answered: list[str] = Field(default_factory=list)
    lead_score: int = 0
    lead_fit: Literal["low", "med", "high"] = "low"
    running_summary: str = ""
    cta_last_shown_turn: int | None = None
    cta_cooldown: int = 4
    cta: dict[str, Any] = Field(default_factory=dict)
    flags: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    channel_hint: ChannelHint = "demo_web"
    user_metadata: UserMetadata = Field(default_factory=UserMetadata)
    demo_mode: bool = True


class ChatResponse(BaseModel):
    conversation_id: str
    assistant_reply: str
    actions: list[Action]
    prospect_profile: ProspectProfile
    citations: list[Citation]


class IngestRequest(BaseModel):
    pdf_paths: list[str] = Field(default_factory=list)
    website_start_url: str | None = None
    website_max_pages: int = 25
    raw_text: str | None = None
    raw_text_title: str = "manual_text"


class IngestResponse(BaseModel):
    chunks_added: int
    sources_processed: list[str]


class ActionLogRecord(BaseModel):
    id: int
    conversation_id: str
    action_type: str
    payload: dict[str, Any]
    demo_mode: bool
    created_at: str


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    user_email: str


class LogoutResponse(BaseModel):
    ok: bool = True


class CurrentUserResponse(BaseModel):
    email: str
