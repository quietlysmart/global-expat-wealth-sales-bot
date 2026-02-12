from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .models import ConversationState, ProspectProfile

QuestionKey = Literal["country", "goal", "timeline", "assets_context", "uk_pension"]
MoveType = Literal[
    "help_only",
    "confirm_and_help",
    "help_then_question",
    "help_then_soft_cta",
    "handoff",
]
PrefixStyle = Literal["none", "one_thing_first", "just_so", "to_make_this_useful"]

QUESTION_MAP: dict[QuestionKey, str] = {
    "country": "Where do you live right now?",
    "goal": "What are you mainly trying to do - retirement, investing a lump sum, or something else?",
    "timeline": "When do you want to get this sorted - soon, this year, or later?",
    "assets_context": "Is this about a UK pension, savings you already have, or starting from scratch?",
    "uk_pension": "Do you have any UK pensions you want to review?",
}

QUESTION_ORDER: list[QuestionKey] = ["country", "goal", "timeline", "assets_context", "uk_pension"]
PREFIX_ORDER: list[PrefixStyle] = ["none", "one_thing_first", "just_so", "to_make_this_useful"]


@dataclass
class DialogDecision:
    move: MoveType
    question_key: QuestionKey | None
    ask_question: bool
    prefix_style: PrefixStyle


class DialogManager:
    def sync_qualifiers_from_profile(self, state: ConversationState, profile: ProspectProfile) -> None:
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
    def qualifier_count(state: ConversationState) -> int:
        q = state.qualifiers_collected
        fields = [q.location_country, q.primary_goal, q.timeline]
        return sum(1 for value in fields if bool(value))

    def choose_dialog_decision(
        self,
        state: ConversationState,
        profile: ProspectProfile,
        user_message: str,
        just_bound_key: QuestionKey | None,
        personal_advice_trigger: bool,
        next_step_or_friction: bool,
        short_user_answer: bool,
        hard_cta_allowed: bool,
        soft_cta_allowed: bool,
    ) -> DialogDecision:
        eligible = self.eligible_question_keys(state, profile)
        preferred_key = self.choose_question_key(state, profile, user_message, eligible)

        next_turn = state.counters.assistant_turn_count + 1
        question_recent = (
            state.counters.question_last_shown_turn is not None
            and (next_turn - state.counters.question_last_shown_turn) < 2
        )

        ask_question = False
        move: MoveType = "help_only"

        if personal_advice_trigger:
            move = "handoff"
            ask_question = bool(preferred_key)
        elif state.goal_unclear and question_recent:
            move = "confirm_and_help"
            ask_question = False
        elif question_recent and not next_step_or_friction:
            # Avoid questionnaire cadence: at most one question every two assistant turns.
            move = "confirm_and_help" if just_bound_key else "help_only"
            ask_question = False
        elif just_bound_key:
            # User just answered. Confirm and build value before asking again.
            move = "confirm_and_help"
            ask_question = False
        elif next_step_or_friction and hard_cta_allowed:
            move = "help_then_soft_cta" if soft_cta_allowed else "help_only"
            ask_question = False
        elif preferred_key and (short_user_answer or self.qualifier_count(state) < 3):
            move = "help_then_question"
            ask_question = True
        elif soft_cta_allowed:
            move = "help_then_soft_cta"
            ask_question = False
        else:
            move = "help_only"
            ask_question = False

        if not preferred_key:
            ask_question = False

        prefix_style = self.choose_prefix_style(state, ask_question)
        return DialogDecision(
            move=move,
            question_key=preferred_key if ask_question else None,
            ask_question=ask_question,
            prefix_style=prefix_style,
        )

    def choose_question_key(
        self,
        state: ConversationState,
        profile: ProspectProfile,
        user_message: str,
        eligible: list[QuestionKey],
    ) -> QuestionKey | None:
        if not eligible:
            return None

        user_lower = user_message.lower()
        q = state.qualifiers_collected

        if state.goal_unclear and "goal" in eligible:
            return "goal"

        # Context-driven priority from what user just said.
        if any(token in user_lower for token in ["uk pension", "pension"]):
            for key in ["uk_pension", "assets_context", "timeline"]:
                if key in eligible:
                    return key

        if any(token in user_lower for token in ["retire", "retirement"]):
            if "country" in eligible:
                return "country"
            if "timeline" in eligible:
                return "timeline"

        if "help" in user_lower and "goal" in eligible:
            return "goal"

        if q.location_country and q.primary_goal == "retirement":
            for key in ["timeline", "uk_pension", "assets_context"]:
                if key in eligible:
                    return key

        if "country" in eligible and not q.location_country:
            return "country"

        for key in eligible:
            if key != state.last_question_key:
                return key

        return eligible[0]

    def eligible_question_keys(self, state: ConversationState, profile: ProspectProfile) -> list[QuestionKey]:
        q = state.qualifiers_collected
        answered = set(state.answered_keys)
        known = {
            "country": bool(profile.country_of_residence or q.location_country),
            "goal": bool(profile.goals or q.primary_goal),
            "timeline": bool(profile.timeframe or q.timeline),
            "assets_context": bool(q.assets_context),
            "uk_pension": q.uk_pension is True,
        }

        out: list[QuestionKey] = []
        for key in QUESTION_ORDER:
            if key in answered:
                continue
            if known.get(key):
                continue
            out.append(key)
        return out

    @staticmethod
    def mark_question_asked(state: ConversationState, question_key: QuestionKey | None, prefix_style: PrefixStyle) -> None:
        if not question_key:
            return
        state.last_question_prefix_style = prefix_style
        state.last_question_key = question_key
        state.question_history = (state.question_history + [question_key])[-20:]

    @staticmethod
    def mark_question_answered(state: ConversationState, question_key: QuestionKey | None) -> None:
        if not question_key:
            return
        state.answered_keys.add(question_key)
        if question_key == "goal":
            state.goal_unclear = False
        # Clear pending key once answered so we do not ask it again.
        if state.last_question_key == question_key:
            state.last_question_key = None

    def choose_prefix_style(self, state: ConversationState, ask_question: bool) -> PrefixStyle:
        if not ask_question:
            return "none"
        last = state.last_question_prefix_style
        if last not in PREFIX_ORDER:
            return "none"
        idx = PREFIX_ORDER.index(last)
        return PREFIX_ORDER[(idx + 1) % len(PREFIX_ORDER)]

    @staticmethod
    def render_question(question_key: QuestionKey | None, prefix_style: PrefixStyle) -> str | None:
        if not question_key:
            return None
        question = QUESTION_MAP.get(question_key)
        if not question:
            return None

        core = question.rstrip("?")
        if prefix_style == "none":
            return f"{core}?"
        if prefix_style == "one_thing_first":
            return f"One thing first: {core}?"
        if prefix_style == "just_so":
            return f"Just so I don't assume: {core}?"
        if prefix_style == "to_make_this_useful":
            return f"To make this useful: {core}?"
        return f"{core}?"

    @staticmethod
    def normalize_for_match(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", text.lower())).strip()
