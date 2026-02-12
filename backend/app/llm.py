from __future__ import annotations

import json
import re
from typing import Any

from openai import OpenAI

from .config import Settings


class OptionalLLM:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.enabled = False
        self.disabled_reason = ""
        self.client = None

        if not settings.use_openai_chat:
            self.disabled_reason = "disabled_by_config"
            return

        if not settings.openai_api_key:
            self.disabled_reason = "missing_openai_api_key"
            return

        self.client = OpenAI(api_key=settings.openai_api_key)
        self.enabled = True

    def generate_dialog_plan(
        self,
        message: str,
        recent_messages: list[dict[str, Any]],
        retrieval_context: list[str],
        allowed_question_keys: list[str],
        cta_gate_open: bool,
        soft_cta_allowed: bool,
        boundary_needed: bool,
        default_ask_question: bool,
        default_prefix_style: str,
    ) -> dict[str, Any] | None:
        if not self.enabled or not self.client:
            return None

    def generate_structured_turn(
        self,
        message: str,
        recent_messages: list[dict[str, Any]],
        retrieval_context: list[str],
        state_json: dict[str, Any],
        constraints: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self.enabled or not self.client:
            return None

        conversation_text = "\n".join(f"{m['role']}: {m['content']}" for m in recent_messages[-10:])
        context_text = "\n---\n".join(retrieval_context[:3])
        state_blob = json.dumps(state_json, ensure_ascii=False)
        constraint_blob = json.dumps(constraints, ensure_ascii=False)

        instructions = (
            "You are a compliant assistant for Global Expat Wealth. Return strict JSON only.\n"
            "Voice:\n"
            "- Human, calm, simple English, short sentences.\n"
            "- Respond to the user's latest message first.\n"
            "- Avoid robotic framing and repeated templates.\n"
            "- Never use: 'One thing first', 'Just so I don't assume', 'To make this useful'.\n"
            "Format:\n"
            "- reply should be 2-6 short lines with blank lines between ideas.\n"
            "- ask.question is optional and should be one natural question at most.\n"
            "- ask_question must be true only when ask is present.\n"
            "- set verbosity=expanded for explain/overview/how-it-works/fees/confused/next-steps requests.\n"
            "- cta must be one of: none, soft, hard.\n"
            "- include_booking_link true only when hard CTA is intended.\n"
            "Compliance:\n"
            "- No personalized investment advice.\n"
            "- If user asks personal recommendation, add a short boundary sentence once.\n"
        )

        prompt = f"""
User message:
{message}

Recent conversation:
{conversation_text}

Retrieved context:
{context_text}

Current state JSON:
{state_blob}

Constraints JSON:
{constraint_blob}

Return JSON with exactly this shape:
{{
  "state_update": {{
    "stage": "greeting|helping|qualifying|faq|objection|handoff|closing",
    "slots": {{
      "country": null,
      "goal": null,
      "timeline": null,
      "assets_context": null,
      "uk_pension": null,
      "email": null,
      "name": null
    }},
    "asked": [],
    "answered": [],
    "lead_score": 0,
    "active_topic": "fees|retirement|insurance|investing|booking|general",
    "topic_turns_remaining": 0,
    "flags": {{
      "goal_unclear": false,
      "user_wants_call": false,
      "compliance_boundary_needed": false
    }}
  }},
  "reply": "...",
  "ask_question": false,
  "ask": {{"slot":"country|goal|timeline|assets_context|uk_pension|email|name","question":"..."}} or null,
  "verbosity": "short|expanded",
  "topic": "fees|retirement|insurance|investing|booking|general",
  "cta": "none|soft|hard",
  "include_booking_link": false
}}
"""
        try:
            response = self.client.responses.create(
                model=self.settings.openai_model,
                input=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": prompt},
                ],
                max_output_tokens=700,
            )
            payload = self._extract_json(response.output_text)
            if not payload:
                return None

            if not isinstance(payload.get("state_update"), dict):
                payload["state_update"] = {}
            if not isinstance(payload.get("reply"), str):
                payload["reply"] = ""

            ask = payload.get("ask")
            if ask is not None and not isinstance(ask, dict):
                payload["ask"] = None
            payload["ask_question"] = bool(payload.get("ask_question", payload.get("ask") is not None))

            cta = str(payload.get("cta", "none")).lower().strip()
            payload["cta"] = cta if cta in {"none", "soft", "hard"} else "none"
            verbosity = str(payload.get("verbosity", "short")).lower().strip()
            payload["verbosity"] = verbosity if verbosity in {"short", "expanded"} else "short"
            topic = str(payload.get("topic", "general")).lower().strip()
            payload["topic"] = topic if topic in {"fees", "retirement", "insurance", "investing", "booking", "general"} else "general"
            payload["include_booking_link"] = bool(payload.get("include_booking_link", False))
            payload["reply"] = re.sub(r"\s+\n", "\n", payload["reply"]).strip()
            return payload
        except Exception:
            return None

        conversation_text = "\n".join(
            f"{m['role']}: {m['content']}" for m in recent_messages[-8:]
        )
        context_text = "\n---\n".join(retrieval_context[:3])
        allowed_keys = allowed_question_keys + ["null"]

        instructions = (
            "You are a compliant assistant for Global Expat Wealth. Return JSON only.\n"
            "Voice Contract:\n"
            "- Write like you're texting a smart 16-year-old.\n"
            "- Use short sentences and plain words.\n"
            "- Calm, friendly, not salesy.\n"
            "- No brochure lists.\n"
            "Reply Limits:\n"
            "- answer must be 2-4 short sentences, max 120 words unless user asked for detail.\n"
            "- max 2 concepts in one answer.\n"
            "- answer must contain no question marks.\n"
            "- do not list services.\n"
            "Banned buzzwords: tax-efficient, diversified portfolio, income strategies, access to specialists, fund managers, wealth preservation, bespoke, decumulation.\n"
            "CTA Policy:\n"
            "- Only include booking link when gate is open.\n"
            "- If gate is closed, include_booking_link must be false.\n"
            "Golden examples:\n"
            "1) User: Can you help me retire in 10 years?\n"
            "{\"answer\":\"Yes. We can map where you are now and what retirement could look like in simple steps. Then we can work out what needs to change over the next decade.\",\"question_key\":\"country\",\"ask_question\":true,\"question_prefix_style\":\"none\",\"soft_cta\":true,\"include_booking_link\":false}\n"
            "2) Bad style to avoid: long service dump with repeated questions.\n"
            "3) User: Should I move my UK pension?\n"
            "{\"answer\":\"I can't give a personal recommendation in chat. I can explain the usual trade-offs, like fees, rules, and flexibility.\",\"question_key\":\"uk_pension\",\"ask_question\":true,\"question_prefix_style\":\"just_so\",\"soft_cta\":true,\"include_booking_link\":false}\n"
            "4) User: What are your fees?\n"
            "{\"answer\":\"Fair question. Fees depend on what kind of help you need and how long you want support. Dan keeps this clear and upfront.\",\"question_key\":\"goal\",\"ask_question\":true,\"question_prefix_style\":\"one_thing_first\",\"soft_cta\":true,\"include_booking_link\":false}\n"
            "5) User: How do I get started?\n"
            "{\"answer\":\"The first step is to quickly understand your goal and current setup so we can point you in the right direction.\",\"question_key\":\"country\",\"ask_question\":true,\"question_prefix_style\":\"none\",\"soft_cta\":false,\"include_booking_link\":true}\n"
            "6) User: I am confused.\n"
            "{\"answer\":\"That is normal. We can simplify this into a few clear decisions so it feels manageable.\",\"question_key\":\"goal\",\"ask_question\":false,\"question_prefix_style\":\"none\",\"soft_cta\":true,\"include_booking_link\":false}\n"
        )

        prompt = f"""
User message:
{message}

Recent conversation:
{conversation_text}

Retrieved context:
{context_text}

Allowed question keys:
{allowed_keys}

Constraints:
- cta_gate_open={cta_gate_open}
- soft_cta_allowed={soft_cta_allowed}
- boundary_needed={boundary_needed}
- default_ask_question={default_ask_question}
- default_question_prefix_style={default_prefix_style}

Return strict JSON with exactly these fields:
{{
  "answer": "...",
  "question_key": "country|goal|timeline|assets_context|uk_pension|null",
  "ask_question": true,
  "question_prefix_style": "none|one_thing_first|just_so|to_make_this_useful",
  "soft_cta": true,
  "include_booking_link": false
}}
"""

        try:
            response = self.client.responses.create(
                model=self.settings.openai_model,
                input=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": prompt},
                ],
                max_output_tokens=450,
            )
            payload = self._extract_json(response.output_text)
            if not payload:
                return None

            answer = str(payload.get("answer", "")).strip()
            question_key = payload.get("question_key")
            ask_question = bool(payload.get("ask_question", default_ask_question))
            question_prefix_style = str(payload.get("question_prefix_style", default_prefix_style))
            soft_cta = bool(payload.get("soft_cta", False))
            include_booking_link = bool(payload.get("include_booking_link", False))

            if question_key == "null":
                question_key = None

            allowed_set = set(allowed_question_keys)
            if question_key is not None and question_key not in allowed_set:
                question_key = allowed_question_keys[0] if allowed_question_keys else None

            answer = answer.replace("?", ".")
            answer = re.sub(r"\s+", " ", answer).strip()

            if not cta_gate_open:
                include_booking_link = False
            if not soft_cta_allowed:
                soft_cta = False
            if not ask_question:
                question_key = None

            if question_prefix_style not in {
                "none",
                "one_thing_first",
                "just_so",
                "to_make_this_useful",
            }:
                question_prefix_style = default_prefix_style

            if not answer:
                return None

            return {
                "answer": answer,
                "question_key": question_key,
                "ask_question": ask_question,
                "question_prefix_style": question_prefix_style,
                "soft_cta": soft_cta,
                "include_booking_link": include_booking_link,
            }
        except Exception:
            return None

    @staticmethod
    def _extract_json(text: str | None) -> dict[str, Any] | None:
        if not text:
            return None
        raw = text.strip()
        try:
            return json.loads(raw)
        except Exception:
            pass

        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except Exception:
            return None
