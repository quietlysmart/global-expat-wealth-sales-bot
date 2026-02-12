from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .dialog_manager import DialogManager
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

FRICTION_PATTERNS = [
    "i'm not sure",
    "i am not sure",
    "this is complicated",
    "i keep going in circles",
    "i need clarity",
    "confused",
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
        self.dialog = DialogManager()

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

        self._update_profile_from_message(profile_after, req.message)
        self.dialog.sync_qualifiers_from_profile(state, profile_after)
        self._update_extra_qualifiers(state, req.message)
        self._sync_answered_keys(state, profile_after)
        self._sync_state_slots_from_profile(state, profile_after)
        if user_wants_call_signal:
            state.flags["user_wants_call"] = True

        qualifier_count = self.dialog.qualifier_count(state)
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
        )
        llm_state = self._state_for_llm(state, profile_after)
        plan = self.llm.generate_structured_turn(
            message=req.message,
            recent_messages=self.db.get_recent_messages(conversation_id),
            retrieval_context=[r.text[:500] for r in retrieved],
            state_json=llm_state,
            constraints=constraints,
        )

        if not plan:
            if self.settings.use_openai_chat and not self.llm.enabled:
                return self._missing_ai_config_response(
                    conversation_id=conversation_id,
                    req=req,
                    profile=profile_after,
                    state=state,
                    citations=citations,
                )
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
            )

        self._merge_model_state_update(state, profile_after, plan.get("state_update", {}))
        self._sync_answered_keys(state, profile_after)
        self._sync_state_slots_from_profile(state, profile_after)

        repaired = self._repair_plan_output(
            plan=plan,
            state=state,
            req_message=req.message,
            explicit_intent=explicit_intent,
            explicit_link_request=explicit_link_request,
            user_wants_call_signal=user_wants_call_signal,
            hard_cta_allowed=hard_cta_allowed,
            soft_cta_allowed=soft_cta_allowed,
        )

        answer = repaired["reply"]
        question_line = repaired["question_line"]
        question_slot = repaired["question_slot"]
        include_soft_cta = repaired["soft_cta"]
        include_booking_link = repaired["include_booking_link"]

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

        if just_bound_key:
            confirmation = self._binding_confirmation_line(just_bound_key, profile_after)
            if confirmation and confirmation.lower() not in answer.lower():
                answer = f"{confirmation} {answer}"

        if question_line and include_booking_link:
            question_line = None
        if question_line and include_soft_cta and not (explicit_intent or explicit_link_request):
            include_soft_cta = False

        reply = self._render_reply(
            answer=answer,
            question_line=question_line,
            include_soft_cta=include_soft_cta,
            include_booking_link=include_booking_link,
            user_message=req.message,
            detail_requested=detail_requested,
            state=state,
        )

        state.first_meaningful_value_delivered = (
            state.first_meaningful_value_delivered or self._is_meaningful_value(reply)
        )
        if self._is_meaningful_value(reply) and not self._is_generic_reassurance(reply):
            state.helpful_turn_count += 1

        state.counters.assistant_turn_count = next_turn
        if include_booking_link:
            state.counters.hard_cta_last_shown_turn = next_turn
            state.counters.hard_cta_shown_count += 1
        if include_soft_cta:
            state.counters.soft_cta_last_shown_turn = next_turn
        if question_line:
            state.counters.question_last_shown_turn = next_turn
            if question_slot:
                state.last_question_key = question_slot
                state.asked = (state.asked + [question_slot])[-25:]
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
            if "prepared a draft email" not in reply.lower():
                reply += "\n\nI've prepared a draft email for review (not sent)."

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
        state.asked = list(state.asked or [])
        state.answered = list(state.answered or [])

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
            "asked": state.asked[-20:],
            "answered": sorted(state.answered_keys),
            "lead_score": max(0, min(100, int(state.lead_score))),
            "cta": state.cta,
            "flags": state.flags,
        }

    def _build_generation_constraints(
        self,
        state: ConversationState,
        hard_cta_allowed: bool,
        soft_cta_allowed: bool,
        boundary_needed: bool,
    ) -> dict[str, Any]:
        return {
            "hard_cta_allowed": hard_cta_allowed,
            "soft_cta_allowed": soft_cta_allowed,
            "boundary_needed": boundary_needed,
            "answered_slots": sorted(state.answered_keys),
            "banned_prefixes": BANNED_PREFIX_PHRASES,
            "one_question_max": True,
        }

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
        if message_lower in {"hi", "hello", "whats up?", "what's up?", "hey"}:
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
    ) -> dict[str, Any]:
        message_lower = message.lower().strip()
        missing_slots = [s for s in ["country", "goal", "timeline", "assets_context", "uk_pension"] if s not in state.answered_keys]

        reply = "Thanks for sharing."
        ask_obj: dict[str, str] | None = None
        cta = "none"
        include_booking_link = False

        if user_wants_call_signal:
            reply = "Yes, absolutely. We can set that up now."
            cta = "hard"
            include_booking_link = hard_cta_allowed
        elif objection:
            reply = str(objection.get("response", "Fair point. We can work through this step by step."))
        elif personal_advice_trigger:
            reply = "I can share general guidance here. The key checks are fees, rules, and flexibility."
            cta = "soft" if soft_cta_allowed else "none"
        elif just_bound_key:
            reply = self._binding_confirmation_line(just_bound_key, profile) or "Thanks, that helps."
        elif retrieval_results:
            reply = "Yes, we can help. We can break this into a few clear steps."
        else:
            reply = "Yes, we can help with that."

        low_signal = self._is_low_signal_reply(message_lower)
        can_ask = (
            bool(missing_slots)
            and not user_wants_call_signal
            and not include_booking_link
            and not (state.counters.question_last_shown_turn and (state.counters.assistant_turn_count + 1 - state.counters.question_last_shown_turn) < 2)
        )
        if can_ask and not (low_signal and state.last_question_key in missing_slots):
            slot = missing_slots[0]
            if state.goal_unclear and "goal" in missing_slots:
                slot = "goal"
            ask_obj = {"slot": slot, "question": self._question_for_slot(slot, goal_unclear=state.goal_unclear)}
        elif low_signal and not ask_obj and missing_slots and state.last_question_key in missing_slots:
            alternatives = [slot for slot in missing_slots if slot != state.last_question_key]
            if alternatives:
                slot = alternatives[0]
                ask_obj = {"slot": slot, "question": self._question_for_slot(slot, goal_unclear=state.goal_unclear)}

        if next_step_or_friction and soft_cta_allowed and not ask_obj:
            cta = "soft"
        elif state.flags.get("user_wants_call") and hard_cta_allowed:
            cta = "hard"
            include_booking_link = True

        return {
            "state_update": {
                "stage": state.stage,
                "slots": {},
                "asked": [],
                "answered": sorted(state.answered_keys),
                "lead_score": state.lead_score,
                "flags": state.flags,
            },
            "reply": reply,
            "ask": ask_obj,
            "cta": cta,
            "include_booking_link": include_booking_link,
        }

    def _merge_model_state_update(self, state: ConversationState, profile: ProspectProfile, state_update: dict[str, Any]) -> None:
        if not isinstance(state_update, dict):
            return
        stage = state_update.get("stage")
        if isinstance(stage, str) and stage in {
            "greeting",
            "helping",
            "qualifying",
            "faq",
            "objection",
            "handoff",
            "closing",
        }:
            state.stage = stage

        flags = state_update.get("flags")
        if isinstance(flags, dict):
            for key in ["goal_unclear", "user_wants_call", "compliance_boundary_needed"]:
                if key in flags:
                    state.flags[key] = bool(flags[key])
            state.goal_unclear = bool(state.flags.get("goal_unclear", state.goal_unclear))

        lead_score = state_update.get("lead_score")
        if isinstance(lead_score, (int, float)):
            state.lead_score = max(0, min(100, int(lead_score)))

        slots = state_update.get("slots")
        if isinstance(slots, dict):
            state.slots.update(slots)
            self._merge_slots_into_profile(state, profile, slots)

        answered = state_update.get("answered")
        if isinstance(answered, list):
            for key in answered:
                if isinstance(key, str):
                    state.answered_keys.add(key)
        asked = state_update.get("asked")
        if isinstance(asked, list):
            state.asked = [str(item) for item in asked if isinstance(item, str)][-25:]

    def _merge_slots_into_profile(self, state: ConversationState, profile: ProspectProfile, slots: dict[str, Any]) -> None:
        if slots.get("country"):
            profile.country_of_residence = str(slots["country"]).title()
            state.qualifiers_collected.location_country = profile.country_of_residence
            state.answered_keys.add("country")
        if slots.get("goal"):
            mapped_goal = self._extract_goal_from_text(self._normalize_user_text(str(slots["goal"])))
            if mapped_goal:
                self._set_goal(profile, mapped_goal)
                state.qualifiers_collected.primary_goal = mapped_goal
                state.answered_keys.add("goal")
                state.goal_unclear = False
                state.flags["goal_unclear"] = False
        if slots.get("timeline"):
            profile.timeframe = str(slots["timeline"])
            state.qualifiers_collected.timeline = profile.timeframe
            state.answered_keys.add("timeline")
        if slots.get("assets_context"):
            state.qualifiers_collected.assets_context = str(slots["assets_context"])
            state.answered_keys.add("assets_context")
        if "uk_pension" in slots and slots.get("uk_pension") is not None:
            state.qualifiers_collected.uk_pension = bool(slots.get("uk_pension"))
            state.answered_keys.add("uk_pension")
        if slots.get("email"):
            profile.email = str(slots["email"]).strip()
        if slots.get("name"):
            profile.name = str(slots["name"]).strip().title()

    def _repair_plan_output(
        self,
        plan: dict[str, Any],
        state: ConversationState,
        req_message: str,
        explicit_intent: bool,
        explicit_link_request: bool,
        user_wants_call_signal: bool,
        hard_cta_allowed: bool,
        soft_cta_allowed: bool,
    ) -> dict[str, Any]:
        reply = self._strip_banned_prefix_lines(str(plan.get("reply", "")).strip())
        if not reply:
            reply = "Thanks for your message. We can work through this step by step."

        ask_slot = None
        question_line = None
        ask_obj = plan.get("ask")
        if isinstance(ask_obj, dict):
            slot_raw = ask_obj.get("slot")
            if isinstance(slot_raw, str):
                ask_slot = slot_raw.strip().lower()
            q_raw = ask_obj.get("question")
            if isinstance(q_raw, str):
                question_line = self._strip_banned_prefix_lines(q_raw.strip())

        if ask_slot and ask_slot in state.answered_keys and not self._has_correction_signal(req_message):
            ask_slot, question_line = None, None
        if ask_slot and ask_slot == state.last_question_key and not self._has_correction_signal(req_message):
            ask_slot, question_line = None, None
        if ask_slot and not question_line:
            question_line = self._question_for_slot(ask_slot, goal_unclear=state.goal_unclear)

        cta = str(plan.get("cta", "none")).lower().strip()
        if cta not in {"none", "soft", "hard"}:
            cta = "none"
        include_booking_link = bool(plan.get("include_booking_link", False))

        if user_wants_call_signal or state.flags.get("user_wants_call"):
            cta = "hard"
            include_booking_link = True
            ask_slot, question_line = None, None

        if cta == "hard":
            include_booking_link = include_booking_link or explicit_intent or explicit_link_request
        if include_booking_link and not hard_cta_allowed:
            include_booking_link = False
            if cta == "hard":
                cta = "soft" if soft_cta_allowed else "none"

        if cta == "soft" and not soft_cta_allowed:
            cta = "none"
        soft_cta = cta == "soft"
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
        }

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

        if self._word_count(reply) > 120 and not detail_requested:
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
                profile.country_of_residence = _clean(country_match.group(1)).title()

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
                profile.country_of_residence = _clean(text).title()
                state.qualifiers_collected.location_country = profile.country_of_residence
                self.dialog.mark_question_answered(state, "country")
                return "country"

        if key == "timeline":
            years_match = YEARS_RE.search(text)
            if years_match:
                years = years_match.group(1)
                value = f"{years} years"
                profile.timeframe = value
                state.qualifiers_collected.timeline = value
                self.dialog.mark_question_answered(state, "timeline")
                return "timeline"
            for hint, value in TIMELINE_HINTS.items():
                if hint in lower or hint in normalized:
                    profile.timeframe = value
                    state.qualifiers_collected.timeline = value
                    self.dialog.mark_question_answered(state, "timeline")
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
                self.dialog.mark_question_answered(state, "goal")
                return "goal"

        if key == "assets_context":
            if "uk pension" in lower or "pension" in lower:
                state.qualifiers_collected.assets_context = "UK pension"
                state.qualifiers_collected.uk_pension = True
                profile.pension_interest = profile.pension_interest or "Has pension questions"
                self.dialog.mark_question_answered(state, "assets_context")
                return "assets_context"
            if "from scratch" in lower:
                state.qualifiers_collected.assets_context = "Starting from scratch"
                self.dialog.mark_question_answered(state, "assets_context")
                return "assets_context"
            if "lump sum" in lower:
                state.qualifiers_collected.assets_context = "Lump sum"
                self.dialog.mark_question_answered(state, "assets_context")
                return "assets_context"
            if "savings" in lower or "portfolio" in lower:
                state.qualifiers_collected.assets_context = "Existing savings/portfolio"
                self.dialog.mark_question_answered(state, "assets_context")
                return "assets_context"

        if key == "uk_pension":
            if any(token in lower for token in ["yes", "i do", "uk pension", "pension"]):
                state.qualifiers_collected.uk_pension = True
                state.qualifiers_collected.assets_context = state.qualifiers_collected.assets_context or "UK pension"
                profile.pension_interest = profile.pension_interest or "Has pension questions"
                self.dialog.mark_question_answered(state, "uk_pension")
                return "uk_pension"
            if any(token in lower for token in ["no", "none", "not really"]):
                state.qualifiers_collected.uk_pension = False
                self.dialog.mark_question_answered(state, "uk_pension")
                return "uk_pension"

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
            self.dialog.mark_question_answered(state, "goal")
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

        last = state.counters.hard_cta_last_shown_turn
        if last is not None and (next_turn - last) <= 4:
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
        if qualifier_count >= 2 and state.helpful_turn_count >= 2:
            return True
        return False

    def _render_question_line(self, question_key: str | None, prefix_style: str, state: ConversationState) -> str | None:
        if not question_key:
            return None
        if question_key != "goal" or not state.goal_unclear:
            return self.dialog.render_question(question_key, prefix_style)

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
            "we can break this down",
            "we can work through this",
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
                        "status": "prepared_not_sent",
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
                        "status": "prepared_not_sent",
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
    ) -> str:
        text = self._strip_booking_links(answer_text)
        text = self._apply_buzzword_replacements(text)
        text = self._remove_irrelevant_topic_sentences(text, user_message)
        text = self._remove_service_dump_sentences(text)

        answer_source = " ".join(block.strip() for block in re.split(r"\n\n+", text) if block.strip())
        answer_sentences = [s for s in self._split_sentences(answer_source) if "?" not in s]
        if not answer_sentences:
            answer_sentences = ["Thanks for your question.", "We can work through this step by step."]
        answer = " ".join(self._ensure_sentence_case(s.replace("?", ".")) for s in answer_sentences[:4])
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
        prior = {self.dialog.normalize_for_match(s) for s in state.recent_assistant_sentences}
        sentences = self._split_sentences(answer)
        kept: list[str] = []
        for sentence in sentences:
            norm = self.dialog.normalize_for_match(sentence)
            if norm and norm in prior:
                continue
            kept.append(sentence)
        if not kept:
            fallbacks = [
                "Thanks, that helps. We can build from that.",
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
            norm = self.dialog.normalize_for_match(sentence)
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
