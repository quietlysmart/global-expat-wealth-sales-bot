from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .kb import RetrievalResult, RetrievalService
from .llm import OptionalLLM
from .models import Action, ChatRequest, ChatResponse, Citation, ConversationState, ProspectProfile
from .playbook import Playbook

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
NAME_RE = re.compile(r"(?:my name is)\s+([A-Za-z][A-Za-z\-\s]{1,40})", re.IGNORECASE)
YEARS_RE = re.compile(r"(\d{1,2})\s*(?:years?|yrs?)", re.IGNORECASE)
COUNTRY_RE = re.compile(
    r"(?:living in|based in|in)\s+([A-Za-z]+(?:\s+[A-Za-z]+){0,2})(?=[,.;]|\s+(?:and|with|for|to)\b|$)",
    re.IGNORECASE,
)
NATIONALITY_RE = re.compile(
    r"(?:i am|i'm|as a)\s+(?:a\s+)?(uk|british|american|australian|canadian|singaporean|indian|irish|new zealand|south african)\b",
    re.IGNORECASE,
)

ASSET_BANDS = [
    ("under 100k", ["under 100", "below 100", "under $100", "under £100"]),
    ("100k-250k", ["100k", "150k", "200k", "250k"]),
    ("250k-500k", ["300k", "400k", "500k"]),
    ("500k-1m", ["600k", "700k", "800k", "900k", "1m"]),
    ("1m+", ["1.2m", "2m", "3m", "million+"]),
]

GOAL_KEYWORDS = {
    "retirement": ["retire", "retirement", "pension"],
    "education": ["education", "school", "university", "college"],
    "protection": ["insurance", "critical illness", "life cover", "life assurance", "protect"],
    "investing": ["invest", "lump sum", "portfolio"],
}

NON_NAME_MARKERS = {
    "expat",
    "retire",
    "retirement",
    "thailand",
    "singapore",
    "hong kong",
    "uk",
    "british",
    "advisor",
    "adviser",
    "pension",
}

EXPLICIT_INTENT_PATTERNS = [
    "book a call",
    "book a consultation",
    "schedule",
    "set up a call",
    "talk to dan",
    "how do i get started",
    "next step",
    "can we work together",
    "book a free 30",
]

LINK_REQUEST_PATTERNS = [
    "booking link",
    "calendly",
    "send me the link",
    "where is the link",
]

CALL_INTENT_PATTERNS = [
    "i thought i was going to have a call",
    "i thought i had a call",
    "can dan call me",
    "i want a call",
    "want a call",
    "speak to dan",
    "talk to dan",
]

SMALL_TALK_PATTERNS = [
    "hi",
    "hello",
    "hey",
    "whats up",
    "what's up",
    "yo",
]

TOPIC_KEYWORDS = {
    "fees": ["fee", "fees", "cost", "price", "pricing", "how much"],
    "retirement": ["retire", "retirement", "pension"],
    "insurance": ["insurance", "critical illness", "life cover", "life insurance", "protect"],
    "investing": ["invest", "investing", "lump sum", "portfolio", "save more", "savings"],
    "booking": ["book", "booking", "call", "calendly", "speak to dan", "talk to dan"],
}

FRICTION_PATTERNS = [
    "i'm not sure",
    "i am not sure",
    "this is complicated",
    "i keep going in circles",
    "i need clarity",
    "confused",
    "what do i do",
    "what should i do",
    "what do i do next",
    "not sure what to do",
]

DETAIL_REQUEST_PATTERNS = [
    "in detail",
    "step by step",
    "full breakdown",
    "give me details",
    "long answer",
    "deep dive",
]

BOUNDARY_LINE = "I can share general guidance here, but I can't provide personalized financial advice in chat."
CALENDLY_RE = re.compile(r"https?://[^\s]*calendly\.com/[^\s]*", re.IGNORECASE)

BUZZWORD_REPLACEMENTS = {
    "tax-efficient": "set up to avoid paying more tax than needed",
    "diversified portfolio": "a spread of investments across different areas",
    "income strategies": "how to turn savings into monthly income",
    "access to specialists": "we can bring in a tax expert if needed",
    "fund managers": "investment managers",
    "wealth preservation": "protect what you've built",
    "bespoke": "tailored",
    "decumulation": "spending your savings in retirement",
}

TIMELINE_HINTS = {
    "soon": "soon",
    "this year": "this year",
    "later": "later",
    "3 months": "3 months",
    "6 months": "6 months",
    "12 months": "12 months",
}

GOAL_LABEL_MAP = {
    "retirement": "retirement",
    "investing": "investing",
    "lump sum": "investing",
    "insurance": "protection",
    "protection": "protection",
    "fees": "fees",
    "pension": "retirement",
}

GOAL_UNCLEAR_PATTERNS = [
    "not sure",
    "dont know",
    "don't know",
    "no idea",
    "unsure",
]

GOAL_SAVE_PATTERNS = [
    "save more",
    "build savings",
    "put money aside",
    "grow savings",
]

GOAL_PROTECTION_PATTERNS = [
    "insurance",
    "life insurance",
    "protect my family",
    "critical illness",
]

GOAL_CORRECTION_PATTERNS = [
    "actually not",
    "change that",
    "not retirement",
    "not investing",
    "not protection",
]

GOAL_TRIAGE_QUESTION = "What's the main thing right now - save more, retire sooner, or feel safer if something goes wrong?"
BANNED_PREFIX_PHRASES = [
    "one thing first",
    "just so i don't assume",
    "to make this useful",
]


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .,;:-")


class ChatOrchestrator:
    def __init__(self, db: Database, retrieval: RetrievalService, settings: Settings) -> None:
        self.db = db
        self.retrieval = retrieval
        self.settings = settings
        self.playbook = Playbook(Path("playbooks/sales_playbook.json"))
        self.llm = OptionalLLM(settings)

    def handle_chat(self, req: ChatRequest) -> ChatResponse:
        conversation_id = req.conversation_id or str(uuid.uuid4())
        self.db.ensure_conversation(conversation_id)

        state = self.db.get_conversation_state(conversation_id)
        self._ensure_state_shape(state)
        profile_before = self.db.get_profile(conversation_id)
        profile_after = profile_before.model_copy(deep=True)

        if req.user_metadata.timezone:
            profile_after.timezone = req.user_metadata.timezone
        if req.user_metadata.locale:
            profile_after.locale = req.user_metadata.locale

        self.db.add_message(conversation_id, "user", req.channel_hint, req.message)

        message_lower = req.message.lower().strip()
        message_topic = self._detect_topic(message_lower)
        explicit_intent = self._is_explicit_intent(message_lower)
        explicit_link_request = self._is_link_request(message_lower)
        user_wants_call_signal = self._is_call_intent_signal(message_lower)
        personal_advice_trigger = self._is_personal_advice_trigger(message_lower)
        next_step_or_friction = self._is_next_step_or_friction(message_lower)
        detail_requested = any(pattern in message_lower for pattern in DETAIL_REQUEST_PATTERNS)
        objection = self.playbook.objection_match(message_lower)

        retrieved = self.retrieval.retrieve(req.message)
        citations = self.retrieval.citations_from_results(retrieved)

        corrected_key = self._maybe_apply_goal_correction(state, profile_after, req.message)
        short_user_answer = self._is_short_answer(req.message)
        just_bound_key = corrected_key or self._bind_answer_to_last_question(state, profile_after, req.message)
        if not just_bound_key:
            just_bound_key = self._bind_short_freeform_answer(state, profile_after, req.message)

        self._update_profile_from_message(profile_after, req.message)
        self._sync_qualifiers_from_profile(state, profile_after)
        self._update_extra_qualifiers(state, req.message)
        self._sync_answered_keys(state, profile_after)
        self._sync_state_slots_from_profile(state, profile_after)
        if user_wants_call_signal:
            state.flags["user_wants_call"] = True
        self._update_active_topic(state, message_topic)

        qualifier_count = self._qualifier_count(state)
        state.stage = self._infer_stage(
            state=state,
            message_lower=message_lower,
            qualifier_count=qualifier_count,
            objection=objection,
            personal_advice_trigger=personal_advice_trigger,
        )

        next_turn = state.counters.assistant_turn_count + 1
        hard_reason = self._hard_cta_reason(
            explicit_intent=explicit_intent or user_wants_call_signal,
            explicit_link_request=explicit_link_request,
            personal_advice_trigger=personal_advice_trigger,
            qualifier_count=qualifier_count,
            first_value_done=state.first_meaningful_value_delivered,
            next_step_or_friction=next_step_or_friction,
        )

        hard_cta_allowed = bool(hard_reason) and self._passes_hard_cta_limits(
            state,
            next_turn=next_turn,
            explicit_link_request=explicit_link_request or user_wants_call_signal,
        )
        if user_wants_call_signal:
            hard_cta_allowed = True

        soft_cta_allowed = self._should_show_soft_cta(
            state=state,
            next_turn=next_turn,
            explicit_intent=explicit_intent,
            personal_advice_trigger=personal_advice_trigger,
            next_step_or_friction=next_step_or_friction,
            qualifier_count=qualifier_count,
            short_user_answer=short_user_answer,
        )

        boundary_needed = personal_advice_trigger and (
            not state.boundary_shown or self._is_personal_recommendation_request(message_lower)
        )
        if boundary_needed:
            state.flags["compliance_boundary_needed"] = True

        constraints = self._build_generation_constraints(
            state=state,
            hard_cta_allowed=hard_cta_allowed,
            soft_cta_allowed=soft_cta_allowed,
            boundary_needed=boundary_needed,
            just_bound_key=just_bound_key,
        )
        llm_state = self._state_for_llm(state, profile_after)
        recent_messages = self.db.get_recent_messages(conversation_id)
        plan = self.llm.plan_turn(
            user_message=req.message,
            recent_messages=recent_messages,
            state_snapshot=llm_state,
            retrieval_context=[r.text[:500] for r in retrieved],
            constraints=constraints,
        )

        used_fallback_plan = False
        if not plan:
            if self.settings.use_openai_chat and not self.llm.enabled:
                return self._missing_ai_config_response(
                    conversation_id=conversation_id,
                    req=req,
                    profile=profile_after,
                    state=state,
                    citations=citations,
                )
            used_fallback_plan = True
            plan = self._fallback_structured_turn(
                message=req.message,
                objection=objection,
                retrieval_results=retrieved,
                state=state,
                profile=profile_after,
                just_bound_key=just_bound_key,
                short_user_answer=short_user_answer,
                next_step_or_friction=next_step_or_friction,
                soft_cta_allowed=soft_cta_allowed,
                hard_cta_allowed=hard_cta_allowed,
                user_wants_call_signal=user_wants_call_signal,
                personal_advice_trigger=personal_advice_trigger,
                message_topic=message_topic,
            )

        self._merge_model_state_update(state, profile_after, plan, req.message)
        self._sync_answered_keys(state, profile_after)
        self._sync_state_slots_from_profile(state, profile_after)

        repaired = self._repair_plan_output(
            plan=plan,
            state=state,
            req_message=req.message,
            message_topic=message_topic,
            explicit_intent=explicit_intent,
            explicit_link_request=explicit_link_request,
            user_wants_call_signal=user_wants_call_signal,
            hard_cta_allowed=hard_cta_allowed,
            soft_cta_allowed=soft_cta_allowed,
            just_bound_key=just_bound_key,
        )

        answer = repaired["reply"]
        question_line = repaired["question_line"]
        question_slot = repaired["question_slot"]
        include_soft_cta = repaired["soft_cta"]
        include_booking_link = repaired["include_booking_link"]
        verbosity = repaired.get("verbosity", "short")
        planned_topic = repaired.get("topic", state.active_topic)
        if planned_topic in {"fees", "retirement", "insurance", "investing", "booking", "general"}:
            state.active_topic = planned_topic

        force_hard_cta = hard_cta_allowed and (
            explicit_intent
            or explicit_link_request
            or user_wants_call_signal
            or (personal_advice_trigger and qualifier_count >= 2)
            or (next_step_or_friction and qualifier_count >= 2)
        )
        if force_hard_cta:
            include_booking_link = True
            include_soft_cta = False
            question_line = None
            question_slot = None

        if include_booking_link:
            state.stage = "closing" if state.stage != "handoff" else "handoff"
            state.flags["user_wants_call"] = False

        if boundary_needed:
            state.boundary_shown = True
            if BOUNDARY_LINE.lower() not in answer.lower():
                answer = f"{BOUNDARY_LINE}\n\n{answer}"

        if personal_advice_trigger and not include_booking_link:
            considerations = self._general_considerations_line(message_lower)
            if considerations and considerations.lower() not in answer.lower():
                answer = f"{answer} {considerations}"

        if used_fallback_plan and just_bound_key:
            confirmation = self._binding_confirmation_line(just_bound_key, profile_after)
            if confirmation and confirmation.lower() not in answer.lower():
                answer = f"{confirmation} {answer}"
        if used_fallback_plan and just_bound_key and self._is_generic_reassurance(answer):
            contextual = self._contextual_progress_line(just_bound_key, profile_after)
            if contextual:
                answer = contextual
        answer = self._drop_repeated_answer_sentences(answer, state)

        if message_topic == "fees":
            answer = self._ensure_fee_answer(answer)
            if question_line and "timeline" in question_line.lower() and "retire" not in message_lower:
                question_line = None

        if (
            not question_line
            and just_bound_key
            and not include_booking_link
            and not include_soft_cta
            and used_fallback_plan
        ):
            next_slot, next_question = self._next_unfilled_slot_question(state, message_topic, just_bound_key)
            if next_slot and next_question:
                question_slot = next_slot
                question_line = next_question

        if question_line and include_booking_link:
            question_line = None
        if question_line and include_soft_cta and not (explicit_intent or explicit_link_request):
            include_soft_cta = False
        if (
            next_step_or_friction
            and soft_cta_allowed
            and not include_booking_link
            and not question_line
            and not include_soft_cta
        ):
            include_soft_cta = True

        approved_plan = self._build_approved_writer_plan(
            answer=answer,
            question_line=question_line,
            include_soft_cta=include_soft_cta,
            include_booking_link=include_booking_link,
            verbosity=verbosity,
            state=state,
            req_message=req.message,
        )
        writer_reply = self.llm.write_turn(
            approved_plan=approved_plan,
            user_message=req.message,
            recent_messages=recent_messages,
        )
        if not writer_reply:
            writer_reply = self._fallback_write_turn(approved_plan)
        reply = self._finalize_writer_message(
            text=writer_reply,
            include_booking_link=include_booking_link,
            allow_expanded=verbosity == "expanded" or detail_requested,
            expected_question=question_line,
            user_message=req.message,
            soft_cta_line=self._soft_cta_line(state.counters.assistant_turn_count + 1) if include_soft_cta else None,
        )
        if boundary_needed and BOUNDARY_LINE.lower() not in reply.lower():
            reply = f"{BOUNDARY_LINE}\n\n{reply}"
        self._remember_assistant_sentences(state, reply)

        state.first_meaningful_value_delivered = (
            state.first_meaningful_value_delivered or self._is_meaningful_value(reply)
        )
        if self._is_meaningful_value(reply) and not self._is_generic_reassurance(reply):
            state.helpful_turn_count += 1

        state.counters.assistant_turn_count = next_turn
        if state.active_topic != "general" and message_topic == "general":
            state.topic_turns_remaining = max(0, state.topic_turns_remaining - 1)
            if state.topic_turns_remaining == 0:
                state.active_topic = "general"
        if include_booking_link:
            state.counters.hard_cta_last_shown_turn = next_turn
            state.counters.hard_cta_shown_count += 1
            state.cta_last_shown_turn = next_turn
        if include_soft_cta:
            state.counters.soft_cta_last_shown_turn = next_turn
        if question_line:
            state.counters.question_last_shown_turn = next_turn
            if question_slot:
                state.last_question_key = question_slot
                state.asked = (state.asked + [question_slot])[-25:]
                state.asked_questions = (state.asked_questions + [question_slot])[-25:]
                state.question_history = (state.question_history + [question_slot])[-25:]
        else:
            state.last_question_key = None

        state.cta = {
            "soft_last_turn": state.counters.soft_cta_last_shown_turn,
            "hard_last_turn": state.counters.hard_cta_last_shown_turn,
            "hard_shown_count": state.counters.hard_cta_shown_count,
        }

        actions = self._build_actions(
            conversation_id=conversation_id,
            message=req.message,
            profile_before=profile_before,
            profile_after=profile_after,
            conversation_stage=state.stage,
            handoff_needed=personal_advice_trigger,
            hard_cta_shown=include_booking_link,
            explicit_intent=explicit_intent,
            demo_mode=req.demo_mode,
        )

        if any(action.type == "draft_followup_email" for action in actions):
            reply = self._apply_email_followup_copy(reply=reply, demo_mode=req.demo_mode)

        state.running_summary = self._update_running_summary(
            previous=state.running_summary,
            user_message=req.message,
            assistant_reply=reply,
        )

        self.db.save_profile(conversation_id, profile_after)
        self.db.save_conversation_state(conversation_id, state)
        self.db.add_message(conversation_id, "assistant", req.channel_hint, reply)

        return ChatResponse(
            conversation_id=conversation_id,
            assistant_reply=reply,
            actions=actions,
            prospect_profile=profile_after,
            citations=citations,
        )

    def _ensure_state_shape(self, state: ConversationState) -> None:
        state.flags = {
            "goal_unclear": bool(state.goal_unclear),
            "user_wants_call": bool(state.flags.get("user_wants_call", False)),
            "compliance_boundary_needed": bool(state.flags.get("compliance_boundary_needed", False)),
        }
        state.goal_unclear = bool(state.flags.get("goal_unclear", state.goal_unclear))
        state.cta = {
            "soft_last_turn": state.counters.soft_cta_last_shown_turn,
            "hard_last_turn": state.counters.hard_cta_last_shown_turn,
            "hard_shown_count": state.counters.hard_cta_shown_count,
        }
        if not isinstance(state.slots, dict):
            state.slots = {}
        if not isinstance(state.asked_questions, list):
            state.asked_questions = []
        state.asked = list(state.asked or [])
        state.answered = list(state.answered or [])
        if not state.active_topic:
            state.active_topic = "general"
        state.topic_turns_remaining = max(0, int(state.topic_turns_remaining or 0))
        state.lead_score = max(0, min(100, int(state.lead_score or 0)))
        state.lead_fit = state.lead_fit if state.lead_fit in {"low", "med", "high"} else self._derive_lead_fit(state.lead_score)
        state.running_summary = (state.running_summary or "").strip()[:500]
        if state.cta_cooldown <= 0:
            state.cta_cooldown = 4
        state.cta_last_shown_turn = state.cta_last_shown_turn or state.counters.hard_cta_last_shown_turn

    @staticmethod
    def _format_country_label(value: str) -> str:
        cleaned = _clean(value)
        if not cleaned:
            return cleaned
        tokens = cleaned.split()
        out: list[str] = []
        acronyms = {"uk", "usa", "uae"}
        for token in tokens:
            low = token.lower()
            out.append(low.upper() if low in acronyms else token.title())
        return " ".join(out)

    @staticmethod
    def _sync_qualifiers_from_profile(state: ConversationState, profile: ProspectProfile) -> None:
        q = state.qualifiers_collected
        if profile.country_of_residence:
            q.location_country = profile.country_of_residence
        if profile.nationality:
            q.nationality = profile.nationality
        if profile.goals and not q.primary_goal:
            q.primary_goal = profile.goals[0]
        if profile.timeframe:
            q.timeline = profile.timeframe
        if profile.pension_interest and not q.assets_context:
            q.assets_context = profile.pension_interest
        if q.uk_pension is None and profile.pension_interest:
            q.uk_pension = True

    @staticmethod
    def _qualifier_count(state: ConversationState) -> int:
        q = state.qualifiers_collected
        fields = [q.location_country, q.primary_goal, q.timeline]
        return sum(1 for value in fields if bool(value))

    @staticmethod
    def _mark_question_answered(state: ConversationState, key: str) -> None:
        state.answered_keys.add(key)
        state.last_question_key = None
        if key == "goal":
            state.goal_unclear = False
            state.flags["goal_unclear"] = False

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", text.lower())).strip()

    def _sync_state_slots_from_profile(self, state: ConversationState, profile: ProspectProfile) -> None:
        slots = {
            "country": profile.country_of_residence,
            "goal": profile.goals[0] if profile.goals else state.qualifiers_collected.primary_goal,
            "timeline": profile.timeframe or state.qualifiers_collected.timeline,
            "assets_context": state.qualifiers_collected.assets_context,
            "uk_pension": state.qualifiers_collected.uk_pension,
            "email": profile.email,
            "name": profile.name,
        }
        state.slots.update(slots)
        state.answered = sorted(state.answered_keys)

    def _state_for_llm(self, state: ConversationState, profile: ProspectProfile) -> dict[str, Any]:
        self._sync_state_slots_from_profile(state, profile)
        return {
            "stage": state.stage,
            "slots": state.slots,
            "asked_questions": state.asked_questions[-20:],
            "asked": state.asked[-20:],
            "answered": sorted(state.answered_keys),
            "lead_score": max(0, min(100, int(state.lead_score))),
            "lead_fit": state.lead_fit,
            "running_summary": state.running_summary,
            "cta_last_shown_turn": state.cta_last_shown_turn,
            "cta_cooldown": state.cta_cooldown,
            "cta": state.cta,
            "flags": state.flags,
            "active_topic": state.active_topic,
            "topic_turns_remaining": state.topic_turns_remaining,
        }

    def _build_generation_constraints(
        self,
        state: ConversationState,
        hard_cta_allowed: bool,
        soft_cta_allowed: bool,
        boundary_needed: bool,
        just_bound_key: str | None = None,
    ) -> dict[str, Any]:
        return {
            "hard_cta_allowed": hard_cta_allowed,
            "soft_cta_allowed": soft_cta_allowed,
            "boundary_needed": boundary_needed,
            "answered_slots": sorted(state.answered_keys),
            "just_bound_key": just_bound_key,
            "banned_prefixes": BANNED_PREFIX_PHRASES,
            "one_question_max": True,
            "active_topic": state.active_topic,
            "topic_turns_remaining": state.topic_turns_remaining,
            "lead_score": state.lead_score,
            "lead_fit": getattr(state, "lead_fit", "low"),
        }

    @staticmethod
    def _map_stage_for_external(stage: str) -> str:
        mapping = {
            "greeting": "greeting",
            "helping": "discovery",
            "qualifying": "qualifying",
            "faq": "faq",
            "objection": "objection",
            "handoff": "closing",
            "closing": "closing",
            "ready_for_cta": "closing",
        }
        return mapping.get(stage, "discovery")

    @staticmethod
    def _map_stage_from_external(stage: str) -> str | None:
        mapping = {
            "greeting": "greeting",
            "discovery": "helping",
            "qualifying": "qualifying",
            "faq": "faq",
            "objection": "objection",
            "closing": "closing",
        }
        return mapping.get(stage)

    @staticmethod
    def _derive_lead_fit(lead_score: int) -> str:
        if lead_score >= 70:
            return "high"
        if lead_score >= 40:
            return "med"
        return "low"

    @staticmethod
    def _infer_stage(
        state: ConversationState,
        message_lower: str,
        qualifier_count: int,
        objection: dict[str, Any] | None,
        personal_advice_trigger: bool,
    ) -> str:
        if personal_advice_trigger:
            return "handoff"
        if objection:
            return "objection"
        if state.flags.get("user_wants_call"):
            return "closing"
        if qualifier_count >= 2:
            return "qualifying"
        if ChatOrchestrator._is_small_talk_opening(message_lower):
            return "greeting"
        return "helping"

    def _fallback_structured_turn(
        self,
        message: str,
        objection: dict[str, Any] | None,
        retrieval_results: list[RetrievalResult],
        state: ConversationState,
        profile: ProspectProfile,
        just_bound_key: str | None,
        short_user_answer: bool,
        next_step_or_friction: bool,
        soft_cta_allowed: bool,
        hard_cta_allowed: bool,
        user_wants_call_signal: bool,
        personal_advice_trigger: bool,
        message_topic: str,
    ) -> dict[str, Any]:
        message_lower = message.lower().strip()
        missing_slots = [s for s in ["country", "goal", "timeline", "assets_context", "uk_pension"] if s not in state.answered_keys]

        ack = "Thanks for sharing."
        value_points: list[str] = []
        ask_obj: dict[str, str] | None = None
        cta_type = "none"
        cta_reason = "not_ready"
        include_booking_link = False
        verbosity = "short"
        topic = message_topic if message_topic != "general" else state.active_topic
        intent = "other"
        slot_updates: dict[str, Any] = {}
        notes_for_ui = {"email_send": False, "email_subject": "", "email_body": ""}

        if user_wants_call_signal:
            ack = "Yes, absolutely."
            value_points = ["We can set that up now."]
            cta_type = "hard"
            cta_reason = "user_requested_call"
            include_booking_link = hard_cta_allowed
            topic = "booking"
            intent = "booking"
        elif self._is_small_talk_opening(message_lower):
            ack = "Hey, good to meet you."
            value_points = ["Happy to help with this."]
            ask_obj = {
                "slot": "goal",
                "question": "What are you thinking about - saving, retirement, or just getting organised?",
            }
            topic = "general"
            intent = "greeting"
        elif objection:
            ack = "Fair point."
            value_points = [str(objection.get("response", "We can work through this step by step."))]
            intent = "objection"
        elif personal_advice_trigger:
            ack = "I can share general guidance here."
            value_points = ["The key checks are fees, rules, and flexibility."]
            cta_type = "soft" if soft_cta_allowed else "none"
            cta_reason = "personal_recommendation_request"
            intent = "faq"
        elif message_topic == "fees" or "cost" in message_lower or "fee" in message_lower:
            ack = "Good question."
            value_points = [
                "Fees depend on the type of support and whether it's one-off or ongoing.",
                "Dan explains fees clearly before you decide anything.",
            ]
            verbosity = "expanded"
            topic = "fees"
            intent = "faq"
            if not state.last_question_key and "goal" not in state.answered_keys:
                ask_obj = {"slot": "goal", "question": "Is this for one-off help or ongoing support?"}
        elif just_bound_key:
            ack = self._contextual_progress_line(just_bound_key, profile) or "Thanks, that helps."
            value_points = []
            intent = "qualify"
        elif retrieval_results:
            ack = "That makes sense."
            value_points = ["Let’s keep this practical and focus on the next step that matters most."]
            intent = "faq"
        else:
            ack = "Got it."
            value_points = ["We can keep this simple and move one decision at a time."]

        low_signal = self._is_low_signal_reply(message_lower)
        question_recent = (
            state.counters.question_last_shown_turn is not None
            and (state.counters.assistant_turn_count + 1 - state.counters.question_last_shown_turn) < 2
        )
        can_ask = (
            bool(missing_slots)
            and not user_wants_call_signal
            and not include_booking_link
            and ask_obj is None
            and not (question_recent and not just_bound_key)
        )
        if can_ask and not (low_signal and state.last_question_key in missing_slots):
            slot = missing_slots[0]
            if state.goal_unclear and "goal" in missing_slots:
                slot = "goal"
            if topic == "fees" and slot == "timeline" and "retire" not in message_lower:
                slot = "goal" if "goal" in missing_slots else slot
            ask_obj = {"slot": slot, "question": self._question_for_slot(slot, goal_unclear=state.goal_unclear)}
        elif (
            low_signal
            and not ask_obj
            and missing_slots
            and state.last_question_key in missing_slots
            and not (
                state.counters.question_last_shown_turn
                and (state.counters.assistant_turn_count + 1 - state.counters.question_last_shown_turn) < 2
            )
        ):
            alternatives = [slot for slot in missing_slots if slot != state.last_question_key]
            if alternatives:
                slot = alternatives[0]
                ask_obj = {"slot": slot, "question": self._question_for_slot(slot, goal_unclear=state.goal_unclear)}

        if next_step_or_friction and soft_cta_allowed and not ask_obj:
            cta_type = "soft"
            cta_reason = "next_step_signal"
        elif state.flags.get("user_wants_call") and hard_cta_allowed:
            cta_type = "hard"
            cta_reason = "user_requested_call"
            include_booking_link = True

        if ask_obj and ask_obj["slot"] == "email":
            notes_for_ui["email_send"] = True
            notes_for_ui["email_subject"] = "Global Expat Wealth follow-up"
            notes_for_ui["email_body"] = "Thanks for the chat. Here are your next steps."

        answer_text = " ".join([part for part in [ack] + value_points[:3] if part]).strip()

        return {
            "answer": answer_text,
            "intent": intent,
            "ack": ack,
            "value": value_points[:3],
            "next_question": {
                "key": ask_obj["slot"] if ask_obj else None,
                "text": ask_obj["question"] if ask_obj else None,
            },
            "cta": {
                "type": cta_type,
                "reason": cta_reason,
                "include_link": include_booking_link,
            },
            "slot_updates": slot_updates,
            "state_update": {
                "stage": self._map_stage_for_external(state.stage),
                "lead_score": state.lead_score,
                "lead_fit": self._derive_lead_fit(state.lead_score),
                "running_summary": state.running_summary if hasattr(state, "running_summary") else "",
                "active_topic": topic,
                "topic_turns_remaining": state.topic_turns_remaining,
            },
            "notes_for_ui": notes_for_ui,
            "verbosity": verbosity,
            "topic": topic,
        }

    def _merge_model_state_update(
        self,
        state: ConversationState,
        profile: ProspectProfile,
        plan: dict[str, Any],
        user_message: str,
    ) -> None:
        if not isinstance(plan, dict):
            return

        slot_updates = plan.get("slot_updates")
        if isinstance(slot_updates, dict):
            applied_slots = self._merge_slots_into_profile(state, profile, slot_updates, user_message=user_message)
            state.slots.update(applied_slots)

        state_update = plan.get("state_update")
        if not isinstance(state_update, dict):
            state_update = {}

        stage = state_update.get("stage")
        if isinstance(stage, str):
            mapped_stage = self._map_stage_from_external(stage)
            if mapped_stage:
                state.stage = mapped_stage

        lead_score = state_update.get("lead_score")
        if isinstance(lead_score, (int, float)):
            state.lead_score = max(0, min(100, int(lead_score)))
        lead_fit = state_update.get("lead_fit")
        if isinstance(lead_fit, str) and lead_fit.lower() in {"low", "med", "high"}:
            state.lead_fit = lead_fit.lower()
        running_summary = state_update.get("running_summary")
        if isinstance(running_summary, str) and running_summary.strip():
            state.running_summary = running_summary.strip()[:500]

        active_topic = state_update.get("active_topic")
        if isinstance(active_topic, str) and active_topic in {"fees", "retirement", "insurance", "investing", "booking", "general"}:
            state.active_topic = active_topic
        topic_turns_remaining = state_update.get("topic_turns_remaining")
        if isinstance(topic_turns_remaining, (int, float)):
            state.topic_turns_remaining = max(0, min(3, int(topic_turns_remaining)))

        next_question = plan.get("next_question")
        if isinstance(next_question, dict):
            asked_key = next_question.get("key")
            if isinstance(asked_key, str) and asked_key and asked_key.lower() != "null":
                state.asked = (state.asked + [asked_key])[-25:]

    def _merge_slots_into_profile(
        self,
        state: ConversationState,
        profile: ProspectProfile,
        slots: dict[str, Any],
        user_message: str,
    ) -> dict[str, Any]:
        applied: dict[str, Any] = {}
        if slots.get("country"):
            if self._allow_model_country_update(user_message, state):
                profile.country_of_residence = self._format_country_label(str(slots["country"]))
                state.qualifiers_collected.location_country = profile.country_of_residence
                state.answered_keys.add("country")
                applied["country"] = profile.country_of_residence
        if slots.get("goal"):
            mapped_goal = self._extract_goal_from_text(self._normalize_user_text(str(slots["goal"])))
            if mapped_goal:
                self._set_goal(profile, mapped_goal)
                state.qualifiers_collected.primary_goal = mapped_goal
                state.answered_keys.add("goal")
                state.goal_unclear = False
                state.flags["goal_unclear"] = False
                applied["goal"] = mapped_goal
        if slots.get("timeline"):
            profile.timeframe = str(slots["timeline"])
            state.qualifiers_collected.timeline = profile.timeframe
            state.answered_keys.add("timeline")
            applied["timeline"] = profile.timeframe
        if slots.get("assets_context"):
            state.qualifiers_collected.assets_context = str(slots["assets_context"])
            state.answered_keys.add("assets_context")
            applied["assets_context"] = state.qualifiers_collected.assets_context
        pension_key = "uk_pension_flag" if "uk_pension_flag" in slots else "uk_pension"
        if pension_key in slots and slots.get(pension_key) is not None:
            state.qualifiers_collected.uk_pension = bool(slots.get(pension_key))
            state.answered_keys.add("uk_pension")
            applied["uk_pension"] = state.qualifiers_collected.uk_pension
        if slots.get("email"):
            profile.email = str(slots["email"]).strip()
            applied["email"] = profile.email
        if slots.get("name"):
            profile.name = str(slots["name"]).strip().title()
            applied["name"] = profile.name
        return applied

    @staticmethod
    def _allow_model_country_update(user_message: str, state: ConversationState) -> bool:
        lower = user_message.lower()
        if state.last_question_key == "country":
            return True
        residence_markers = [
            "i live in",
            "living in",
            "based in",
            "currently in",
            "i am in",
            "i'm in",
        ]
        if any(marker in lower for marker in residence_markers):
            return True
        if "move to " in lower or "moving to " in lower:
            return False
        return False

    def _repair_plan_output(
        self,
        plan: dict[str, Any],
        state: ConversationState,
        req_message: str,
        message_topic: str,
        explicit_intent: bool,
        explicit_link_request: bool,
        user_wants_call_signal: bool,
        hard_cta_allowed: bool,
        soft_cta_allowed: bool,
        just_bound_key: str | None = None,
    ) -> dict[str, Any]:
        raw_answer = self._strip_banned_prefix_lines(str(plan.get("answer", "")).strip())
        ack = self._strip_banned_prefix_lines(str(plan.get("ack", "")).strip())
        value_items = plan.get("value", [])
        if not isinstance(value_items, list):
            value_items = []
        cleaned_value = [self._strip_banned_prefix_lines(str(v).strip()) for v in value_items if str(v).strip()]
        body_parts = [part for part in [ack] + cleaned_value[:3] if part]
        reply = raw_answer if raw_answer else "\n\n".join(body_parts).strip()
        if not reply:
            reply = "Thanks for your message. We can work through this step by step."

        ask_slot = None
        question_line = None
        ask_question = False
        next_question = plan.get("next_question")
        if isinstance(next_question, dict):
            slot_raw = next_question.get("key")
            if isinstance(slot_raw, str):
                ask_slot = slot_raw.strip().lower()
                if ask_slot in {"null", "none", ""}:
                    ask_slot = None
            q_raw = next_question.get("text")
            if isinstance(q_raw, str):
                question_line = self._strip_banned_prefix_lines(q_raw.strip())
            ask_question = bool(ask_slot or question_line)

        if not ask_question:
            ask_slot, question_line = None, None
        if question_line and not ask_slot:
            ask_slot = self._infer_slot_from_question_text(question_line)
        if ask_slot and ask_slot in state.answered_keys and not self._has_correction_signal(req_message):
            ask_slot, question_line = None, None
        if ask_slot and ask_slot == state.last_question_key and not self._has_correction_signal(req_message):
            ask_slot, question_line = None, None
        if ask_slot and not question_line:
            question_line = self._question_for_slot(ask_slot, goal_unclear=state.goal_unclear)
        if self._is_small_talk_opening(req_message.lower().strip()) and ask_slot == "country":
            ask_slot = "goal"
            question_line = "What are you thinking about - saving, retirement, or just getting organised?"
        question_recent = (
            state.counters.question_last_shown_turn is not None
            and ((state.counters.assistant_turn_count + 1) - state.counters.question_last_shown_turn) < 2
        )
        if question_recent and question_line and not self._has_correction_signal(req_message) and not just_bound_key:
            ask_slot, question_line = None, None
        if message_topic == "fees" and question_line and "timeline" in question_line.lower() and "retire" not in req_message.lower():
            ask_slot, question_line = None, None

        if self._is_small_talk_opening(req_message.lower().strip()) and reply.count(",") >= 3:
            reply = "Hey, good to meet you.\n\nHappy to help with this."

        cta = plan.get("cta")
        cta_type = "none"
        include_booking_link = False
        if isinstance(cta, dict):
            cta_type = str(cta.get("type", "none")).lower().strip()
            include_booking_link = bool(cta.get("include_link", False))
        if cta_type not in {"none", "soft", "hard"}:
            cta_type = "none"

        verbosity = str(plan.get("verbosity", "short")).lower().strip()
        if verbosity not in {"short", "expanded"}:
            verbosity = "short"
        if self._needs_expanded_reply(req_message.lower()):
            verbosity = "expanded"
        topic = str(plan.get("topic", state.active_topic)).lower().strip()
        if topic not in {"fees", "retirement", "insurance", "investing", "booking", "general"}:
            topic = state.active_topic
        if topic == "general" and message_topic != "general":
            topic = message_topic

        if user_wants_call_signal or state.flags.get("user_wants_call"):
            cta_type = "hard"
            include_booking_link = True
            ask_slot, question_line = None, None

        if cta_type == "hard":
            include_booking_link = include_booking_link or explicit_intent or explicit_link_request
        if include_booking_link and not hard_cta_allowed:
            include_booking_link = False
            if cta_type == "hard":
                cta_type = "soft" if soft_cta_allowed else "none"

        if cta_type == "soft" and not soft_cta_allowed:
            cta_type = "none"
        soft_cta = cta_type == "soft"
        if soft_cta and question_line and not (explicit_intent or explicit_link_request):
            soft_cta = False
        if include_booking_link:
            soft_cta = False

        reply = self._strip_extra_questions(reply)
        if question_line:
            question_line = self._normalize_question_sentence(question_line)
        return {
            "reply": reply,
            "question_line": question_line,
            "question_slot": ask_slot,
            "soft_cta": soft_cta,
            "include_booking_link": include_booking_link,
            "verbosity": verbosity,
            "topic": topic,
        }

    @staticmethod
    def _infer_slot_from_question_text(question_text: str) -> str | None:
        q = question_text.lower()
        if any(k in q for k in ["where do you live", "which country", "country are you in", "live right now"]):
            return "country"
        if any(k in q for k in ["main goal", "mainly trying to do", "what are you trying to do", "retirement, investing"]):
            return "goal"
        if any(k in q for k in ["when do you", "timeline", "when are you planning", "how soon"]):
            return "timeline"
        if any(k in q for k in ["uk pension", "pensions you want to review", "do you have any uk pensions"]):
            return "uk_pension"
        if any(k in q for k in ["from scratch", "lump sum", "savings you already have"]):
            return "assets_context"
        if "email" in q:
            return "email"
        if "what should i call you" in q or "your name" in q:
            return "name"
        return None

    def _build_approved_writer_plan(
        self,
        answer: str,
        question_line: str | None,
        include_soft_cta: bool,
        include_booking_link: bool,
        verbosity: str,
        state: ConversationState,
        req_message: str,
    ) -> dict[str, Any]:
        return {
            "answer": answer.strip(),
            "question": question_line,
            "soft_cta": include_soft_cta,
            "hard_cta": include_booking_link,
            "booking_link": self.settings.default_calendly_link if include_booking_link else "",
            "verbosity": verbosity,
            "active_topic": state.active_topic,
            "user_message": req_message,
        }

    def _fallback_write_turn(self, approved_plan: dict[str, Any]) -> str:
        blocks: list[str] = []
        answer = str(approved_plan.get("answer", "")).strip()
        question = approved_plan.get("question")
        soft_cta = bool(approved_plan.get("soft_cta", False))
        hard_cta = bool(approved_plan.get("hard_cta", False))
        booking_link = str(approved_plan.get("booking_link", "")).strip()

        if answer:
            blocks.append(answer)
        if question:
            blocks.append(str(question).strip())
        if soft_cta:
            blocks.append("If helpful, Dan can walk through your options on a quick call.")
        if hard_cta and booking_link:
            blocks.append(f"Book a time with Dan: {booking_link}")
        return "\n\n".join(blocks).strip()

    def _finalize_writer_message(
        self,
        text: str,
        include_booking_link: bool,
        allow_expanded: bool,
        expected_question: str | None,
        user_message: str,
        soft_cta_line: str | None = None,
    ) -> str:
        active_question: str | None = None
        out = self._strip_banned_prefix_lines((text or "").strip())
        if not out:
            out = "Thanks for your message. We can work through this step by step."

        out = self._apply_buzzword_replacements(out)
        out = self._apply_compliance_language_guardrails(out)
        out = self._fix_sentence_casing(out)
        out = re.sub(r"\n{3,}", "\n\n", out).strip()
        if include_booking_link:
            out = self._strip_booking_links(out)
            out = re.sub(
                r"(?i)\b(connect|book|speak).*?\b(adviser|advisor|specialist).*",
                "If you'd like to take the personal side further, we can do that on a quick call with Dan.",
                out,
            ).strip()
            link_line = f"Book a time with Dan: {self.settings.default_calendly_link}"
            if link_line.lower() not in out.lower():
                out = f"{out}\n\n{link_line}".strip()
        else:
            out = self._strip_booking_links(out)

        if expected_question:
            cleaned_question = self._sanitize_question_text(expected_question)
            active_question = cleaned_question
            out = self._remove_question_sentences(out)
            out = out.strip()
            if cleaned_question:
                out = f"{out}\n\n{cleaned_question}" if out else cleaned_question
        else:
            out = self._strip_extra_questions(out)
            if soft_cta_line and not include_booking_link and "call" not in out.lower():
                out = f"{out}\n\n{soft_cta_line}".strip()

        out = re.sub(r"\n{3,}", "\n\n", out).strip()
        if "\n" in out and "\n\n" not in out:
            out = out.replace("\n", "\n\n")
        if "\n\n" not in out:
            link_line = f"Book a time with Dan: {self.settings.default_calendly_link}"
            if link_line.lower() in out.lower():
                idx = out.lower().find("book a time with dan:")
                if idx > 0:
                    out = out[:idx].strip() + "\n\n" + out[idx:].strip()
        if "\n\n" not in out and len(out) > 260:
            out = self._insert_line_breaks(out, None, None)
        if self._word_count(out) > 150 and not allow_expanded:
            out = self._trim_for_length(out)
        out = self._repair_dangling_blocks(out)
        if active_question:
            out = self._strip_question_echo_statement(out, active_question)
        return out.strip()

    @staticmethod
    def _apply_compliance_language_guardrails(text: str) -> str:
        out = text
        replacements = [
            (r"(?i)\bi recommend\b", "A common approach is"),
            (r"(?i)\byou should\b", "Many people choose to"),
            (r"(?i)\bthe best option for you\b", "an option that can fit your situation"),
            (r"(?i)\bwhat you should do\b", "what people often consider"),
        ]
        for pattern, repl in replacements:
            out = re.sub(pattern, repl, out)
        return out

    @staticmethod
    def _remove_question_sentences(text: str) -> str:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        kept: list[str] = []
        question_starts = ("what ", "which ", "when ", "where ", "who ", "why ", "how ", "can ", "do ", "are ", "is ")
        for sentence in sentences:
            if "?" not in sentence:
                kept.append(sentence)
                continue
            prefix = sentence.split("?", 1)[0].strip()
            prefix_lower = prefix.lower()
            if prefix and len(re.findall(r"[A-Za-z0-9']+", prefix)) >= 8 and not prefix_lower.startswith(question_starts):
                if prefix[-1] not in ".!":
                    prefix += "."
                kept.append(prefix)
        return " ".join(kept).strip()

    @staticmethod
    def _repair_dangling_blocks(text: str) -> str:
        blocks = [b.strip() for b in re.split(r"\n{2,}", text) if b.strip()]
        repaired: list[str] = []
        connector_tail = re.compile(
            r"(?i)\b(and|or|but|so|because|that|which|to|for|with|if|when|while|ensuring)$"
        )
        for block in blocks:
            lower = block.lower()
            if "?" in block or "calendly.com" in lower or lower.startswith("book a time with dan:"):
                repaired.append(block)
                continue
            line = block.rstrip(" ,;:-")
            # If the model stops mid-thought, finish the line cleanly instead of clipping words.
            if re.search(r"(?i)\b(we|you|i|they)\s+(can|could|would|should)\.?$", line):
                line = re.sub(
                    r"(?i)\b(we|you|i|they)\s+(can|could|would|should)\.?$",
                    "we can look at the next best step",
                    line,
                )
            elif re.search(r"(?i)\b(we|you|i|they)\.?$", line):
                line = re.sub(
                    r"(?i)\b(we|you|i|they)\.?$",
                    "we can map out the right next step",
                    line,
                )
            elif re.search(r"(?i)\bwhat mix\.?$", line):
                line = re.sub(
                    r"(?i)\bwhat mix\.?$",
                    "what mix could suit your timeline",
                    line,
                )
            elif connector_tail.search(line):
                line = connector_tail.sub("with a clear next step", line).rstrip(" ,;:-")
            if line and line[-1] not in ".!?":
                line += "."
            repaired.append(line or block)
        return "\n\n".join(repaired)

    @staticmethod
    def _strip_question_echo_statement(text: str, question: str) -> str:
        q_norm = " ".join(re.findall(r"[a-z0-9']+", question.lower()))
        if not q_norm:
            return text

        blocks = [b.strip() for b in re.split(r"\n{2,}", text) if b.strip()]
        cleaned_blocks: list[str] = []
        for block in blocks:
            if "?" in block:
                cleaned_blocks.append(block)
                continue
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", block) if s.strip()]
            kept: list[str] = []
            for sentence in sentences:
                s_norm = " ".join(re.findall(r"[a-z0-9']+", sentence.lower()))
                if not s_norm:
                    continue
                if (
                    s_norm == q_norm
                    or s_norm.startswith(q_norm)
                    or q_norm.startswith(s_norm)
                    or q_norm in s_norm
                ):
                    continue
                kept.append(sentence)
            if kept:
                cleaned_blocks.append(" ".join(kept))
        return "\n\n".join(cleaned_blocks)

    def _sanitize_question_text(self, question: str) -> str:
        raw = re.sub(r"\s+", " ", (question or "").strip())
        if not raw:
            return ""
        if "?" in raw:
            raw = raw.split("?", 1)[0].strip()
        if "." in raw:
            raw = raw.split(".", 1)[0].strip()
        raw = raw.rstrip(" .")
        return self._normalize_question_sentence(raw)

    @staticmethod
    def _fix_sentence_casing(text: str) -> str:
        cleaned = re.sub(r"\s+", " ", text).strip()
        if not cleaned:
            return cleaned
        urls = re.findall(r"https?://\S+", cleaned, flags=re.IGNORECASE)
        placeholders: dict[str, str] = {}
        for i, url in enumerate(urls):
            key = f"__URL_{i}__"
            placeholders[key] = url
            cleaned = cleaned.replace(url, key)
        chars = list(cleaned)
        cap_next = True
        for i, ch in enumerate(chars):
            if cap_next and ch.isalpha():
                chars[i] = ch.upper()
                cap_next = False
            if ch in ".!?":
                cap_next = True
        out = "".join(chars)
        for key, url in placeholders.items():
            out = out.replace(key, url)
        return out

    @staticmethod
    def _strip_extra_questions(text: str) -> str:
        if text.count("?") <= 1:
            return text
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        kept: list[str] = []
        question_seen = False
        for sentence in sentences:
            if "?" in sentence:
                if question_seen:
                    kept.append(sentence.replace("?", "."))
                    continue
                question_seen = True
            kept.append(sentence)
        return " ".join(kept).strip()

    @staticmethod
    def _is_low_signal_reply(message_lower: str) -> bool:
        msg = message_lower.strip()
        if msg in {"ok", "okay", "great", "cool", "nice", "thanks", "thx", "sure"}:
            return True
        return len([t for t in msg.split() if t]) <= 2 and msg not in {"uk pension", "retirement", "investing"}

    @staticmethod
    def _has_correction_signal(message: str) -> bool:
        lower = message.lower()
        return "actually" in lower or "change that" in lower or "not " in lower

    @staticmethod
    def _detect_topic(message_lower: str) -> str:
        for topic, keywords in TOPIC_KEYWORDS.items():
            if any(keyword in message_lower for keyword in keywords):
                return topic
        return "general"

    @staticmethod
    def _is_small_talk_opening(message_lower: str) -> bool:
        cleaned = message_lower.strip().rstrip("!?.,")
        return cleaned in SMALL_TALK_PATTERNS

    def _update_active_topic(self, state: ConversationState, message_topic: str) -> None:
        if message_topic != "general":
            state.active_topic = message_topic
            state.topic_turns_remaining = 2
            return
        if state.topic_turns_remaining > 0 and state.active_topic != "general":
            return
        state.active_topic = "general"
        state.topic_turns_remaining = 0

    @staticmethod
    def _ensure_fee_answer(answer: str) -> str:
        lower = answer.lower()
        if "fee" in lower or "cost" in lower or "pricing" in lower:
            return answer
        fee_line = "Fees depend on the type of help you need and whether it is one-off or ongoing."
        return f"{fee_line} {answer}".strip()

    @staticmethod
    def _apply_email_followup_copy(reply: str, demo_mode: bool) -> str:
        cleaned = re.sub(r"(?i)i can prepare an email for dan to send\.?\s*want me to draft it\??", "", reply).strip()
        cleaned = re.sub(r"(?i)i've prepared a draft email for review \(not sent\)\.?", "", cleaned).strip()
        if demo_mode:
            if "i've sent a confirmation email with the booking details." not in cleaned.lower():
                cleaned = f"{cleaned}\n\nOk - I've sent a confirmation email with the booking details."
            cleaned = re.sub(r"(?i)want me to send it\??", "", cleaned).strip()
            return cleaned
        if "i can prepare an email for dan to send" not in cleaned.lower():
            cleaned = f"{cleaned}\n\nI can prepare an email for Dan to send. Want me to draft it?"
        return cleaned

    @staticmethod
    def _needs_expanded_reply(message_lower: str) -> bool:
        markers = [
            "explain",
            "overview",
            "tell me more",
            "how does it work",
            "how it works",
            "what do you do",
            "cost",
            "fees",
            "what?",
            "what ?",
            "next step",
            "what next",
            "confused",
        ]
        return any(marker in message_lower for marker in markers)

    @staticmethod
    def _update_running_summary(previous: str, user_message: str, assistant_reply: str) -> str:
        user_short = re.sub(r"\s+", " ", user_message.strip())[:120]
        assistant_short = re.sub(r"\s+", " ", assistant_reply.strip())[:180]
        parts = [p for p in [previous.strip(), f"User: {user_short}", f"Assistant: {assistant_short}"] if p]
        merged = " ".join(parts)
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", merged) if s.strip()]
        return " ".join(sentences[-3:])[:500]

    @staticmethod
    def _question_for_slot(slot: str, goal_unclear: bool = False) -> str:
        if slot == "country":
            return "Where do you live right now?"
        if slot == "goal":
            if goal_unclear:
                return GOAL_TRIAGE_QUESTION
            return "What are you mainly trying to do - retirement, investing a lump sum, or something else?"
        if slot == "timeline":
            return "When do you want to get this sorted - soon, this year, or later?"
        if slot == "assets_context":
            return "Is this about a UK pension, savings you already have, or starting from scratch?"
        if slot == "uk_pension":
            return "Do you have any UK pensions you want to review?"
        if slot == "email":
            return "What's the best email for a follow-up?"
        if slot == "name":
            return "What should I call you?"
        return "What would be most helpful to focus on next?"

    def _next_unfilled_slot_question(
        self,
        state: ConversationState,
        message_topic: str,
        just_bound_key: str,
    ) -> tuple[str | None, str | None]:
        filled = set(state.answered_keys)
        order = ["goal", "country", "timeline", "assets_context", "uk_pension"]
        if message_topic == "fees":
            order = ["goal", "country", "timeline", "assets_context"]
        if just_bound_key == "goal":
            order = ["country", "timeline", "assets_context", "uk_pension"]
        if just_bound_key == "country":
            order = ["goal", "timeline", "assets_context", "uk_pension"]
        if just_bound_key == "timeline":
            order = ["assets_context", "uk_pension", "goal", "country"]
        for slot in order:
            if slot in filled:
                continue
            return slot, self._question_for_slot(slot, goal_unclear=state.goal_unclear)
        return None, None

    @staticmethod
    def _is_call_intent_signal(message_lower: str) -> bool:
        return any(pattern in message_lower for pattern in CALL_INTENT_PATTERNS)

    def _render_reply(
        self,
        answer: str,
        question_line: str | None,
        include_soft_cta: bool,
        include_booking_link: bool,
        user_message: str,
        detail_requested: bool,
        verbosity: str,
        state: ConversationState,
    ) -> str:
        cleaned_answer = self._normalize_answer_text(answer, user_message=user_message)
        cleaned_answer = self._drop_repeated_answer_sentences(cleaned_answer, state)
        cleaned_answer = self._strip_banned_prefix_lines(cleaned_answer)
        question_line = self._strip_banned_prefix_lines(question_line or "") or None
        soft_cta_line = self._soft_cta_line(state.counters.assistant_turn_count + 1) if include_soft_cta else None
        link_line = f"Book a time with Dan: {self.settings.default_calendly_link}" if include_booking_link else None

        reply = self._enforce_human_response_policy(
            answer_text=cleaned_answer,
            user_message=user_message,
            question_line=question_line,
            soft_cta_line=soft_cta_line,
            link_line=link_line,
            hard_cta_shown=include_booking_link,
            soft_cta_shown=include_soft_cta,
            boundary_allowed=BOUNDARY_LINE.lower() in cleaned_answer.lower(),
        )

        allow_expanded = verbosity == "expanded" or detail_requested
        if self._word_count(reply) > 120 and not allow_expanded:
            reply = self._trim_for_length(reply)

        self._remember_assistant_sentences(state, reply)

        return reply.strip()

    def _normalize_answer_text(self, answer: str, user_message: str) -> str:
        text = answer
        text = self._strip_booking_links(text)
        text = self._apply_buzzword_replacements(text)
        text = self._remove_irrelevant_topic_sentences(text, user_message)
        text = self._remove_service_dump_sentences(text)

        sentences = self._split_sentences(text)
        if not sentences:
            sentences = ["Thanks for the question.", "We can work through this one step at a time."]

        output_sentences: list[str] = []
        for sentence in sentences:
            sentence = sentence.replace("?", ".").strip()
            if not sentence:
                continue
            output_sentences.append(self._ensure_sentence_case(sentence))
            if len(output_sentences) >= 4:
                break

        if len(output_sentences) < 2:
            output_sentences.append("We can keep this simple and focus on what matters first.")

        return " ".join(output_sentences).strip()

    def _missing_ai_config_response(
        self,
        conversation_id: str,
        req: ChatRequest,
        profile: ProspectProfile,
        state: ConversationState,
        citations: list[Citation],
    ) -> ChatResponse:
        reply = (
            "AI mode is enabled, but `OPENAI_API_KEY` is missing on the backend, so I cannot run live model inference yet. "
            "Please add the key in `.env` and restart the server."
        )
        state.counters.assistant_turn_count += 1
        self.db.save_profile(conversation_id, profile)
        self.db.save_conversation_state(conversation_id, state)
        self.db.add_message(conversation_id, "assistant", req.channel_hint, reply)
        return ChatResponse(
            conversation_id=conversation_id,
            assistant_reply=reply,
            actions=[],
            prospect_profile=profile,
            citations=citations,
        )

    def _update_profile_from_message(self, profile: ProspectProfile, message: str) -> None:
        text = message.strip()
        lower = text.lower()
        normalized = self._normalize_user_text(text)

        email_match = EMAIL_RE.search(text)
        if email_match:
            profile.email = email_match.group(0)

        if not profile.name:
            name_match = NAME_RE.search(text)
            if name_match:
                candidate = _clean(name_match.group(1))
                candidate_lower = candidate.lower()
                if 2 <= len(candidate) <= 40 and not any(marker in candidate_lower for marker in NON_NAME_MARKERS):
                    profile.name = candidate.title()

        if not profile.country_of_residence:
            country_match = COUNTRY_RE.search(text)
            if country_match:
                profile.country_of_residence = self._format_country_label(country_match.group(1))

        if not profile.nationality:
            nat_match = NATIONALITY_RE.search(lower)
            if nat_match:
                value = nat_match.group(1).upper()
                profile.nationality = "UK" if value in {"UK", "BRITISH"} else value.title()

        timeframe_match = YEARS_RE.search(text)
        if timeframe_match:
            years = timeframe_match.group(1)
            profile.timeframe = f"{years} years"
        elif not profile.timeframe:
            for hint, value in TIMELINE_HINTS.items():
                if hint in lower:
                    profile.timeframe = value
                    break

        for goal, words in GOAL_KEYWORDS.items():
            if any(word in lower for word in words) and goal not in profile.goals:
                profile.goals.append(goal)
        extracted_goal = self._extract_goal_from_text(normalized)
        if extracted_goal:
            self._set_goal(profile, extracted_goal)

        for band, markers in ASSET_BANDS:
            if any(marker in lower for marker in markers):
                profile.asset_band = band
                break

        if "pension" in lower:
            if "consolidat" in lower:
                profile.pension_interest = "Interested in consolidation"
            elif "don" in lower and "move" in lower:
                profile.pension_interest = "Concerned about moving pension"
            else:
                profile.pension_interest = profile.pension_interest or "Has pension questions"

        note = f"{datetime.now(timezone.utc).date().isoformat()}: {text[:180]}"
        profile.notes = (profile.notes + [note])[-12:]

    def _update_extra_qualifiers(self, state: ConversationState, message: str) -> None:
        lower = message.lower()
        normalized = self._normalize_user_text(message)
        q = state.qualifiers_collected

        if q.assets_context is None:
            if "uk pension" in lower:
                q.assets_context = "UK pension"
            elif "lump sum" in lower:
                q.assets_context = "Lump sum"
            elif "portfolio" in lower:
                q.assets_context = "Existing portfolio"
            elif "from scratch" in lower:
                q.assets_context = "Starting from scratch"

        if q.uk_pension is None:
            if "uk pension" in lower or "pension" in lower:
                q.uk_pension = True

        if q.timeline is None:
            for hint, value in TIMELINE_HINTS.items():
                if hint in lower:
                    q.timeline = value
                    break
        mapped_goal = self._extract_goal_from_text(normalized)
        if mapped_goal and not state.goal_unclear:
            q.primary_goal = mapped_goal

    def _bind_answer_to_last_question(
        self,
        state: ConversationState,
        profile: ProspectProfile,
        message: str,
    ) -> str | None:
        key = state.last_question_key
        if not key:
            return None

        text = message.strip()
        lower = text.lower()
        normalized = self._normalize_user_text(text)

        if key == "country":
            if self._is_goal_unclear_message(normalized):
                return None
            if self._is_short_answer(text) and self._looks_like_country_only_reply(text):
                profile.country_of_residence = self._format_country_label(text)
                state.qualifiers_collected.location_country = profile.country_of_residence
                self._mark_question_answered(state, "country")
                return "country"

        if key == "timeline":
            years_match = YEARS_RE.search(text)
            if years_match:
                years = years_match.group(1)
                value = f"{years} years"
                profile.timeframe = value
                state.qualifiers_collected.timeline = value
                self._mark_question_answered(state, "timeline")
                return "timeline"
            for hint, value in TIMELINE_HINTS.items():
                if hint in lower or hint in normalized:
                    profile.timeframe = value
                    state.qualifiers_collected.timeline = value
                    self._mark_question_answered(state, "timeline")
                    return "timeline"

        if key == "goal":
            if self._is_goal_unclear_message(normalized):
                state.goal_unclear = True
                state.answered_keys.discard("goal")
                state.qualifiers_collected.primary_goal = None
                return None

            mapped_goal = self._extract_goal_from_text(normalized)
            if mapped_goal:
                self._set_goal(profile, mapped_goal)
                state.qualifiers_collected.primary_goal = mapped_goal
                self._mark_question_answered(state, "goal")
                return "goal"

        if key == "assets_context":
            if "uk pension" in lower or "pension" in lower:
                state.qualifiers_collected.assets_context = "UK pension"
                state.qualifiers_collected.uk_pension = True
                profile.pension_interest = profile.pension_interest or "Has pension questions"
                self._mark_question_answered(state, "assets_context")
                return "assets_context"
            if "from scratch" in lower:
                state.qualifiers_collected.assets_context = "Starting from scratch"
                self._mark_question_answered(state, "assets_context")
                return "assets_context"
            if "lump sum" in lower:
                state.qualifiers_collected.assets_context = "Lump sum"
                self._mark_question_answered(state, "assets_context")
                return "assets_context"
            if "savings" in lower or "portfolio" in lower:
                state.qualifiers_collected.assets_context = "Existing savings/portfolio"
                self._mark_question_answered(state, "assets_context")
                return "assets_context"

        if key == "uk_pension":
            if any(token in lower for token in ["yes", "i do", "uk pension", "pension"]):
                state.qualifiers_collected.uk_pension = True
                state.qualifiers_collected.assets_context = state.qualifiers_collected.assets_context or "UK pension"
                profile.pension_interest = profile.pension_interest or "Has pension questions"
                self._mark_question_answered(state, "uk_pension")
                return "uk_pension"
            if any(token in lower for token in ["no", "none", "not really"]):
                state.qualifiers_collected.uk_pension = False
                self._mark_question_answered(state, "uk_pension")
                return "uk_pension"

        return None

    def _bind_short_freeform_answer(
        self,
        state: ConversationState,
        profile: ProspectProfile,
        message: str,
    ) -> str | None:
        text = message.strip()
        lower = text.lower()
        normalized = self._normalize_user_text(text)

        if self._is_short_answer(text) and not profile.country_of_residence and self._looks_like_country_only_reply(text):
            profile.country_of_residence = self._format_country_label(text)
            state.qualifiers_collected.location_country = profile.country_of_residence
            self._mark_question_answered(state, "country")
            return "country"

        if "goal" not in state.answered_keys:
            mapped_goal = self._extract_goal_from_text(normalized)
            if mapped_goal:
                self._set_goal(profile, mapped_goal)
                state.qualifiers_collected.primary_goal = mapped_goal
                self._mark_question_answered(state, "goal")
                return "goal"

        if "timeline" not in state.answered_keys:
            years_match = YEARS_RE.search(text)
            if years_match:
                years = years_match.group(1)
                value = f"{years} years"
                profile.timeframe = value
                state.qualifiers_collected.timeline = value
                self._mark_question_answered(state, "timeline")
                return "timeline"
            for hint, value in TIMELINE_HINTS.items():
                if hint in lower or hint in normalized:
                    profile.timeframe = value
                    state.qualifiers_collected.timeline = value
                    self._mark_question_answered(state, "timeline")
                    return "timeline"

        if "assets_context" not in state.answered_keys:
            if "uk pension" in lower or "pension" in lower:
                state.qualifiers_collected.assets_context = "UK pension"
                state.qualifiers_collected.uk_pension = True
                profile.pension_interest = profile.pension_interest or "Has pension questions"
                self._mark_question_answered(state, "assets_context")
                return "assets_context"
            if "from scratch" in lower:
                state.qualifiers_collected.assets_context = "Starting from scratch"
                self._mark_question_answered(state, "assets_context")
                return "assets_context"
            if "lump sum" in lower:
                state.qualifiers_collected.assets_context = "Lump sum"
                self._mark_question_answered(state, "assets_context")
                return "assets_context"
        return None

    def _maybe_apply_goal_correction(
        self,
        state: ConversationState,
        profile: ProspectProfile,
        message: str,
    ) -> str | None:
        normalized = self._normalize_user_text(message)
        if not any(marker in normalized for marker in GOAL_CORRECTION_PATTERNS):
            return None

        state.answered_keys.discard("goal")
        state.goal_unclear = False
        state.qualifiers_collected.primary_goal = None
        profile.goals = []

        cleaned = normalized
        for phrase in ["not retirement", "not investing", "not protection", "actually not", "change that"]:
            cleaned = cleaned.replace(phrase, " ")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        mapped_goal = self._extract_goal_from_text(cleaned)
        if mapped_goal:
            self._set_goal(profile, mapped_goal)
            state.qualifiers_collected.primary_goal = mapped_goal
            self._mark_question_answered(state, "goal")
            return "goal"

        state.goal_unclear = True
        return None

    @staticmethod
    def _normalize_user_text(text: str) -> str:
        lowered = text.lower().strip()
        lowered = re.sub(r"[^a-z0-9\s']", " ", lowered)
        return re.sub(r"\s+", " ", lowered).strip()

    @staticmethod
    def _levenshtein(a: str, b: str) -> int:
        if a == b:
            return 0
        if not a:
            return len(b)
        if not b:
            return len(a)
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, start=1):
            cur = [i]
            for j, cb in enumerate(b, start=1):
                cost = 0 if ca == cb else 1
                cur.append(min(cur[-1] + 1, prev[j] + 1, prev[j - 1] + cost))
            prev = cur
        return prev[-1]

    @classmethod
    def _extract_goal_from_text(cls, normalized_text: str) -> str | None:
        if not normalized_text:
            return None

        if any(phrase in normalized_text for phrase in GOAL_SAVE_PATTERNS):
            return "investing"

        if "lump sum" in normalized_text or "investing" in normalized_text or "invest" in normalized_text:
            return "investing"

        if any(phrase in normalized_text for phrase in GOAL_PROTECTION_PATTERNS):
            return "protection"

        tokens = normalized_text.split()
        for token in tokens:
            if "retir" in token:
                return "retirement"
            if token == "pension":
                return "retirement"

        goal_vocab = {"retirement", "investing", "protection"}
        for token in tokens:
            if len(token) < 6:
                continue
            for goal in goal_vocab:
                if cls._levenshtein(token, goal) <= 2:
                    return goal

        for label, mapped in GOAL_LABEL_MAP.items():
            if label in normalized_text:
                return mapped
        return None

    @staticmethod
    def _is_goal_unclear_message(normalized_text: str) -> bool:
        return any(pattern in normalized_text for pattern in GOAL_UNCLEAR_PATTERNS)

    @staticmethod
    def _set_goal(profile: ProspectProfile, mapped_goal: str) -> None:
        if not profile.goals:
            profile.goals = [mapped_goal]
            return
        if profile.goals[0] != mapped_goal:
            profile.goals = [mapped_goal] + [g for g in profile.goals if g != mapped_goal]

    @staticmethod
    def _sync_answered_keys(state: ConversationState, profile: ProspectProfile) -> None:
        q = state.qualifiers_collected
        if profile.country_of_residence or q.location_country:
            state.answered_keys.add("country")
        if profile.timeframe or q.timeline:
            state.answered_keys.add("timeline")
        if q.assets_context:
            state.answered_keys.add("assets_context")
        if q.uk_pension is not None:
            state.answered_keys.add("uk_pension")
        if q.primary_goal and not state.goal_unclear:
            state.answered_keys.add("goal")
        state.answered = sorted(state.answered_keys)
        state.flags["goal_unclear"] = bool(state.goal_unclear)
        state.lead_fit = ChatOrchestrator._derive_lead_fit(state.lead_score)

    @staticmethod
    def _binding_confirmation_line(bound_key: str, profile: ProspectProfile) -> str:
        if bound_key == "country" and profile.country_of_residence:
            return f"Thanks, that helps. Living in {profile.country_of_residence} changes which rules matter."
        if bound_key == "timeline" and profile.timeframe:
            if profile.timeframe in {"soon", "this year", "later"}:
                return f"Great, aiming for {profile.timeframe} gives us a clear pace for next steps."
            return f"Great, a {profile.timeframe} timeline gives us room to plan this in stages."
        if bound_key == "goal":
            return "Got it. Focusing on one clear goal makes this much easier."
        if bound_key == "assets_context":
            return "Thanks, that gives useful context for the options."
        if bound_key == "uk_pension":
            return "Helpful, that clarifies what we should focus on next."
        return ""

    @staticmethod
    def _is_short_answer(message: str) -> bool:
        text = message.strip()
        if "?" in text:
            return False
        tokens = [token for token in re.findall(r"[A-Za-z0-9']+", text) if token]
        if len(tokens) > 4:
            return False
        if any(ch in text for ch in [".", "!", ",", ";", ":"]):
            return False
        return True

    @staticmethod
    def _looks_like_country_only_reply(text: str) -> bool:
        lower = text.lower().strip()
        tokens = [t for t in lower.split() if t]
        if len(tokens) > 2:
            return False
        if any(ch in lower for ch in "?.!,:"):
            return False
        if any(marker in lower for marker in ["not sure", "dont know", "don't know", "no idea", "unsure"]):
            return False
        if any(
            token in {
                "i",
                "im",
                "guess",
                "wanna",
                "want",
                "save",
                "more",
                "suppose",
                "maybe",
                "retirement",
                "reirement",
                "ok",
                "okay",
                "great",
                "cool",
                "thanks",
                "thx",
            }
            for token in tokens
        ):
            return False
        if any(keyword in lower for keyword in ["retire", "pension", "goal", "timeline", "fees"]):
            return False
        return bool(re.fullmatch(r"[a-zA-Z\s\-]{2,40}", text.strip()))

    @staticmethod
    def _is_explicit_intent(message_lower: str) -> bool:
        return any(pattern in message_lower for pattern in EXPLICIT_INTENT_PATTERNS)

    @staticmethod
    def _is_link_request(message_lower: str) -> bool:
        return any(pattern in message_lower for pattern in LINK_REQUEST_PATTERNS)

    @staticmethod
    def _is_next_step_or_friction(message_lower: str) -> bool:
        return any(pattern in message_lower for pattern in FRICTION_PATTERNS) or "what next" in message_lower or "next step" in message_lower

    def _is_personal_advice_trigger(self, message_lower: str) -> bool:
        if any(trigger in message_lower for trigger in self.playbook.handoff_triggers):
            return True
        if "pension" in message_lower and any(term in message_lower for term in ["good idea", "should", "move"]):
            return True
        extra_patterns = [
            "should i",
            "for me",
            "for my situation",
            "exactly what to do",
            "best option for me",
            "tell me what to do",
        ]
        return any(pattern in message_lower for pattern in extra_patterns)

    def _is_personal_recommendation_request(self, message_lower: str) -> bool:
        return self._is_personal_advice_trigger(message_lower)

    @staticmethod
    def _general_considerations_line(message_lower: str) -> str:
        if "pension" in message_lower:
            return "In general, the key checks are fees, rules, flexibility, and tax impact."
        if "fund" in message_lower or "etf" in message_lower:
            return "In general, compare cost, risk, and how well it fits your goal and timeline."
        return "In general, key checks are your goal, timeline, and how much risk you can handle."

    def _hard_cta_reason(
        self,
        explicit_intent: bool,
        explicit_link_request: bool,
        personal_advice_trigger: bool,
        qualifier_count: int,
        first_value_done: bool,
        next_step_or_friction: bool,
    ) -> str | None:
        if explicit_intent or explicit_link_request:
            return "explicit"
        if personal_advice_trigger and qualifier_count >= 2:
            return "handoff"
        if qualifier_count >= 2 and next_step_or_friction:
            return "earned"
        return None

    @staticmethod
    def _passes_hard_cta_limits(state: ConversationState, next_turn: int, explicit_link_request: bool) -> bool:
        if explicit_link_request:
            return True

        last = state.cta_last_shown_turn if state.cta_last_shown_turn is not None else state.counters.hard_cta_last_shown_turn
        cooldown = max(4, int(state.cta_cooldown or 4))
        if last is not None and (next_turn - last) <= cooldown:
            return False

        if next_turn < 12 and state.counters.hard_cta_shown_count >= 2:
            return False

        return True

    @staticmethod
    def _soft_cta_line(seed: int) -> str:
        options = [
            "If you want, Dan can walk through the personal trade-offs on a quick call.",
            "Happy to keep chatting here, or we can do this faster on a call when you're ready.",
        ]
        return options[seed % len(options)]

    @staticmethod
    def _should_show_soft_cta(
        state: ConversationState,
        next_turn: int,
        explicit_intent: bool,
        personal_advice_trigger: bool,
        next_step_or_friction: bool,
        qualifier_count: int,
        short_user_answer: bool,
    ) -> bool:
        last_soft = state.counters.soft_cta_last_shown_turn
        if last_soft is not None and (next_turn - last_soft) < 5:
            return False

        if state.goal_unclear and not personal_advice_trigger:
            return False

        if personal_advice_trigger:
            return True
        if next_step_or_friction:
            return True
        if explicit_intent:
            return False
        if short_user_answer:
            return False
        if qualifier_count >= 2 and state.helpful_turn_count >= 2:
            recent_q = state.counters.question_last_shown_turn
            if recent_q is not None and (next_turn - recent_q) < 2:
                return False
            return True
        return False

    @staticmethod
    def _contextual_progress_line(bound_key: str, profile: ProspectProfile) -> str:
        if bound_key == "country":
            if profile.country_of_residence:
                return f"Got it. Since you're in {profile.country_of_residence}, we can keep this focused on cross-border decisions that affect you."
            return "Got it. That location context helps us keep this practical."
        if bound_key == "goal":
            return "Great, that gives us a clear direction and makes the next steps simpler."
        if bound_key == "timeline":
            return "Perfect, that timeline helps us prioritize what to do first."
        if bound_key == "assets_context":
            return "Helpful context. That tells us which options are worth comparing first."
        if bound_key == "uk_pension":
            return "Good to know. That helps us focus on the pension rules that actually matter here."
        return "Thanks, that helps us move this forward."

    def _render_question_line(self, question_key: str | None, prefix_style: str, state: ConversationState) -> str | None:
        if not question_key:
            return None
        if question_key != "goal" or not state.goal_unclear:
            return self._question_for_slot(question_key, goal_unclear=False)

        base = GOAL_TRIAGE_QUESTION.rstrip("?")
        if prefix_style == "one_thing_first":
            return f"One thing first: {base.lower()}?"
        if prefix_style == "just_so":
            return f"Just so I don't assume: {base.lower()}?"
        if prefix_style == "to_make_this_useful":
            return f"To make this useful: {base.lower()}?"
        return f"{base}?"

    @staticmethod
    def _is_generic_reassurance(text: str) -> bool:
        normalized = text.lower()
        generic_markers = [
            "yes, we can help",
            "yes, we can help with that",
            "we can break this down",
            "we can work through this",
            "we can build from that",
            "we can take this one step at a time",
            "thanks for your question",
        ]
        return any(marker in normalized for marker in generic_markers)

    def _build_actions(
        self,
        conversation_id: str,
        message: str,
        profile_before: ProspectProfile,
        profile_after: ProspectProfile,
        conversation_stage: str,
        handoff_needed: bool,
        hard_cta_shown: bool,
        explicit_intent: bool,
        demo_mode: bool,
    ) -> list[Action]:
        actions: list[Action] = []

        actions.append(Action(type="tag_stage", payload={"stage": conversation_stage}))
        actions.append(
            Action(
                type="log_interaction",
                payload={
                    "message_preview": message[:160],
                    "stage": conversation_stage,
                    "has_email": bool(profile_after.email),
                    "has_goals": bool(profile_after.goals),
                    "demo_mode": demo_mode,
                },
            )
        )

        new_fields = self._changed_fields(profile_before, profile_after)
        if any(k in new_fields for k in ["name", "email", "country_of_residence", "goals", "timeframe", "asset_band"]):
            actions.append(
                Action(
                    type="capture_lead",
                    payload={
                        "name": profile_after.name,
                        "email": profile_after.email,
                        "country": profile_after.country_of_residence,
                        "goals": profile_after.goals,
                        "timeframe": profile_after.timeframe,
                        "asset_band": profile_after.asset_band,
                        "notes": profile_after.notes[-2:],
                    },
                )
            )

        wants_email = any(kw in message.lower() for kw in ["email", "follow up", "follow-up", "send details"])
        if profile_after.email and ("email" in new_fields or wants_email):
            actions.append(
                Action(
                    type="draft_followup_email",
                    payload={
                        "to": profile_after.email,
                        "recap": self._build_recap(profile_after),
                        "next_steps": "Book a 30-minute consultation with Dan.",
                        "booking_link_placeholder": self.settings.default_calendly_link,
                        "status": "sent_in_demo" if demo_mode else "draft_for_review",
                    },
                )
            )

        if hard_cta_shown or explicit_intent:
            actions.append(
                Action(
                    type="create_booking_request",
                    payload={
                        "preferred_times": "To be confirmed",
                        "timezone": profile_after.timezone or "Unknown",
                        "meeting_type": "30-min discovery call",
                        "status": "scheduled_in_demo" if demo_mode else "prepared_for_human_confirmation",
                    },
                )
            )

        if handoff_needed:
            actions.append(
                Action(
                    type="handoff_to_human",
                    payload={
                        "reason": "Potential regulated/personalized advice request",
                        "summary": self._handoff_summary(profile_after),
                    },
                )
            )

        for action in actions:
            self.db.log_action(conversation_id, action.type, action.payload, demo_mode)
            self._append_action_jsonl(conversation_id, action.type, action.payload, demo_mode)

        return actions

    @staticmethod
    def _changed_fields(before: ProspectProfile, after: ProspectProfile) -> set[str]:
        changed: set[str] = set()
        before_dict = before.model_dump()
        after_dict = after.model_dump()
        for key, value in after_dict.items():
            if value != before_dict.get(key):
                changed.add(key)
        return changed

    @staticmethod
    def _build_recap(profile: ProspectProfile) -> str:
        parts = []
        if profile.country_of_residence:
            parts.append(f"Country: {profile.country_of_residence}")
        if profile.nationality:
            parts.append(f"Nationality: {profile.nationality}")
        if profile.goals:
            parts.append("Goals: " + ", ".join(profile.goals))
        if profile.timeframe:
            parts.append(f"Timeframe: {profile.timeframe}")
        if profile.asset_band:
            parts.append(f"Asset band: {profile.asset_band}")
        return " | ".join(parts) if parts else "General exploratory discussion"

    @staticmethod
    def _handoff_summary(profile: ProspectProfile) -> str:
        return (
            f"Prospect summary -> name={profile.name or 'Unknown'}, email={profile.email or 'Unknown'}, "
            f"country={profile.country_of_residence or 'Unknown'}, goals={profile.goals or []}, "
            f"timeframe={profile.timeframe or 'Unknown'}, pension_interest={profile.pension_interest or 'Unknown'}"
        )

    def _append_action_jsonl(self, conversation_id: str, action_type: str, payload: dict[str, Any], demo_mode: bool) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "conversation_id": conversation_id,
            "action_type": action_type,
            "payload": payload,
            "demo_mode": demo_mode,
        }
        with self.settings.action_log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized:
            return []
        return [s.strip() for s in re.split(r"(?<=[.!?])\s+", normalized) if s.strip()]

    @staticmethod
    def _word_count(text: str) -> int:
        return len(re.findall(r"[A-Za-z0-9']+", text))

    @staticmethod
    def _ensure_sentence_case(sentence: str) -> str:
        text = sentence.strip()
        if not text:
            return ""
        text = text[0].upper() + text[1:]
        if text[-1] not in {".", "!"}:
            text += "."
        return text

    @staticmethod
    def _apply_buzzword_replacements(text: str) -> str:
        updated = text
        for term, replacement in BUZZWORD_REPLACEMENTS.items():
            updated = re.sub(rf"\b{re.escape(term)}\b", replacement, updated, flags=re.IGNORECASE)
        return updated

    def _remove_irrelevant_topic_sentences(self, text: str, user_message: str) -> str:
        user_lower = user_message.lower()
        blocked_groups = [
            {"golden visa", "second citizenship", "second passport", "citizenship"},
            {"trust", "trusts", "probate"},
            {"tax planning"},
        ]
        sentences = self._split_sentences(text)
        kept: list[str] = []
        for sentence in sentences:
            lower = sentence.lower()
            drop = False
            for group in blocked_groups:
                if not any(token in user_lower for token in group) and any(token in lower for token in group):
                    drop = True
                    break
            if not drop:
                kept.append(sentence)
        return " ".join(kept).strip()

    @staticmethod
    def _remove_service_dump_sentences(text: str) -> str:
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
        kept: list[str] = []
        for sentence in sentences:
            if sentence.count(",") >= 2 and len(re.findall(r"\b(and|,|also)\b", sentence.lower())) >= 2:
                continue
            kept.append(sentence)
        return " ".join(kept).strip()

    def _compose_blocks(
        self,
        answer: str,
        question_line: str | None,
        soft_cta_line: str,
        link_line: str,
    ) -> str:
        blocks = [answer.strip()]
        if question_line:
            blocks.append(question_line.strip())
        if soft_cta_line:
            blocks.append(soft_cta_line.strip())
        if link_line:
            blocks.append(link_line.strip())
        return "\n\n".join(block for block in blocks if block)

    def _trim_for_length(self, reply: str) -> str:
        blocks = [block.strip() for block in reply.split("\n\n") if block.strip()]
        if not blocks:
            return reply

        answer = blocks[0]
        question = next((b for b in blocks if "?" in b), "")
        cta = next(
            (
                b
                for b in blocks
                if "calendly.com" not in b.lower() and "call" in b.lower() and "?" not in b
            ),
            "",
        )
        link = next((b for b in blocks if "calendly.com" in b.lower()), "")

        answer_sentences = self._split_sentences(answer)[:2]
        answer_short = " ".join(answer_sentences)

        short_blocks = [answer_short]
        if question:
            short_blocks.append(question)
        if cta:
            short_blocks.append(cta)
        if link:
            short_blocks.append(link)
        return "\n\n".join(short_blocks)

    def _enforce_human_response_policy(
        self,
        answer_text: str,
        user_message: str,
        question_line: str | None,
        soft_cta_line: str | None,
        link_line: str | None,
        hard_cta_shown: bool,
        soft_cta_shown: bool,
        boundary_allowed: bool,
        allow_expanded: bool = False,
    ) -> str:
        text = self._strip_booking_links(answer_text)
        text = self._apply_buzzword_replacements(text)
        text = self._remove_irrelevant_topic_sentences(text, user_message)
        text = self._remove_service_dump_sentences(text)

        answer_source = " ".join(block.strip() for block in re.split(r"\n\n+", text) if block.strip())
        answer_sentences = [s for s in self._split_sentences(answer_source) if "?" not in s]
        answer_sentences = self._drop_question_echo_sentences(answer_sentences, question_line or "")
        if not answer_sentences:
            answer_sentences = ["Thanks for your question.", "We can work through this step by step."]
        max_sentences = 4 if allow_expanded else 2
        answer = " ".join(self._ensure_sentence_case(s.replace("?", ".")) for s in answer_sentences[:max_sentences])
        answer = re.sub(r"\s+", " ", answer).strip()
        answer = self._ensure_sentence_case(answer)

        question = self._normalize_question_sentence(question_line or "")
        cta = ""
        if soft_cta_shown and not hard_cta_shown and soft_cta_line:
            cta = self._ensure_sentence_case(soft_cta_line.replace("?", "."))

        output_blocks = [answer]
        if question:
            output_blocks.append(question)
        if cta:
            output_blocks.append(cta)
        if hard_cta_shown and link_line:
            output_blocks.append(link_line.strip())

        final = "\n\n".join(output_blocks)
        final = self._force_single_question(final, question)

        if "\n\n" not in final and len(final) > 280:
            final = self._insert_line_breaks(final, question, output_blocks[-1] if (hard_cta_shown and link_line) else cta)

        return final.strip()

    @classmethod
    def _drop_question_echo_sentences(cls, sentences: list[str], question_line: str) -> list[str]:
        if not sentences or not question_line:
            return sentences
        q_tokens = {t for t in re.findall(r"[a-z0-9']+", question_line.lower()) if len(t) > 3}
        if not q_tokens:
            return sentences
        kept: list[str] = []
        for sentence in sentences:
            s_tokens = {t for t in re.findall(r"[a-z0-9']+", sentence.lower()) if len(t) > 3}
            overlap = len(q_tokens.intersection(s_tokens))
            if overlap >= 4:
                continue
            kept.append(sentence)
        return kept or sentences[:1]

    @staticmethod
    def _normalize_question_sentence(question: str) -> str:
        q = re.sub(r"\s+", " ", (question or "").strip())
        if not q:
            return ""
        q = q.rstrip(".")
        if not q.endswith("?"):
            q += "?"
        return q[0].upper() + q[1:]

    @staticmethod
    def _force_single_question(text: str, best_question: str | None) -> str:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        cleaned: list[str] = []
        if not best_question:
            cleaned = [line.replace("?", ".") for line in lines]
            return "\n\n".join(cleaned).strip()

        question_used = False
        for line in lines:
            if "?" in line:
                if not question_used:
                    cleaned.append(best_question)
                    question_used = True
                continue
            cleaned.append(line.replace("?", "."))

        final = "\n\n".join(cleaned)
        if final.count("?") > 1:
            final = final.replace("?", ".", final.count("?") - 1)
            final = final.replace(best_question.replace("?", "."), best_question)
        return final

    @staticmethod
    def _insert_line_breaks(text: str, question: str | None, cta: str | None) -> str:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
        if len(sentences) <= 2:
            return text
        answer = " ".join(sentences[:2]).strip()
        remainder = " ".join(sentences[2:]).strip()
        blocks = [answer]
        if remainder and not question:
            blocks.append(remainder)
        if question:
            blocks.append(question)
        if cta:
            blocks.append(cta)
        return "\n\n".join(blocks)

    def _strip_booking_links(self, reply: str) -> str:
        cleaned = CALENDLY_RE.sub("", reply)
        cleaned = cleaned.replace(self.settings.default_calendly_link, "")
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        cleaned = re.sub(r"\n\s+", "\n", cleaned)
        return cleaned.strip()

    @staticmethod
    def _strip_banned_prefix_lines(text: str) -> str:
        if not text:
            return ""
        lines = [line.strip() for line in text.splitlines()]
        kept: list[str] = []
        for line in lines:
            lower = line.lower().lstrip()
            if any(lower.startswith(prefix) for prefix in BANNED_PREFIX_PHRASES):
                candidate = line.split(":", 1)[-1].strip() if ":" in line else ""
                if candidate:
                    kept.append(candidate[0].upper() + candidate[1:])
                continue
            kept.append(line)
        return "\n".join(line for line in kept if line).strip()

    def _drop_repeated_answer_sentences(self, answer: str, state: ConversationState) -> str:
        prior = {self._normalize_for_match(s) for s in state.recent_assistant_sentences}
        sentences = self._split_sentences(answer)
        kept: list[str] = []
        for sentence in sentences:
            norm = self._normalize_for_match(sentence)
            if norm and norm in prior:
                continue
            kept.append(sentence)
        if not kept:
            fallbacks = [
                "Thanks, that helps. We can move to the next step.",
                "Got it. That gives us a clear next step.",
            ]
            return fallbacks[len(state.recent_assistant_sentences) % len(fallbacks)]
        return " ".join(kept)

    def _remember_assistant_sentences(self, state: ConversationState, reply: str) -> None:
        sentences = [s for s in self._split_sentences(reply) if "?" not in s]
        merged = (state.recent_assistant_sentences + sentences)[-5:]
        # Keep normalized-unique in order.
        out: list[str] = []
        seen: set[str] = set()
        for sentence in merged:
            norm = self._normalize_for_match(sentence)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            out.append(sentence.strip())
        state.recent_assistant_sentences = out[-5:]

    def _is_meaningful_value(self, reply: str) -> bool:
        text = reply.lower()
        markers = ["first", "step", "consider", "compare", "plan", "focus"]
        sentence_count = len([s for s in re.split(r"[.!?]+", text) if s.strip()])
        return sentence_count >= 2 and any(marker in text for marker in markers)
