from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import OpenAI

from .config import Settings

logger = logging.getLogger(__name__)


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

    def plan_turn(
        self,
        user_message: str,
        recent_messages: list[dict[str, Any]],
        state_snapshot: dict[str, Any],
        retrieval_context: list[str],
        constraints: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self.enabled or not self.client:
            return None

        conversation_text = "\n".join(
            f"{m['role']}: {m['content']}" for m in recent_messages[-8:]
        )
        context_text = "\n---\n".join(retrieval_context[:3])

        system_prompt = (
            "You are a conversation planner for a sales concierge bot.\n"
            "Return JSON only.\n"
            "Focus on natural pacing. Helpful first. Ask at most one question.\n"
            "Do not ask for slots already answered.\n"
            "Do not use robotic lead-ins.\n"
            "Use progressive qualification naturally.\n"
        )

        user_prompt = f"""
User message:
{user_message}

Recent turns:
{conversation_text}

Running state:
{json.dumps(state_snapshot, ensure_ascii=False)}

Retrieved context:
{context_text}

Constraints:
{json.dumps(constraints, ensure_ascii=False)}

Return strict JSON:
{{
  "intent": "greeting|faq|qualify|objection|booking|other",
  "ack": "short acknowledgement",
  "value": ["1-3 helpful points"],
  "next_question": {{
    "key": "country|goal|timeline|assets_context|email|name|null",
    "text": "natural question text or null"
  }},
  "cta": {{
    "type": "none|soft|hard",
    "reason": "short reason",
    "include_link": false
  }},
  "slot_updates": {{
    "country": null,
    "goal": null,
    "timeline": null,
    "assets_context": null,
    "uk_pension_flag": null,
    "email": null,
    "name": null
  }},
  "state_update": {{
    "stage": "greeting|discovery|qualifying|faq|objection|closing",
    "lead_score": 0,
    "lead_fit": "low|med|high",
    "running_summary": "1-3 sentences",
    "active_topic": "fees|retirement|insurance|investing|booking|general",
    "topic_turns_remaining": 0
  }},
  "notes_for_ui": {{
    "email_send": false,
    "email_subject": "",
    "email_body": ""
  }},
  "verbosity": "short|expanded"
}}
"""
        try:
            logger.info("planner_model=%s", self.settings.openai_model)
            response = self.client.responses.create(
                model=self.settings.openai_model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_output_tokens=700,
            )
            payload = self._extract_json(response.output_text)
            if not payload:
                return None
            return self._normalize_plan(payload)
        except Exception:
            return None

    def write_turn(
        self,
        approved_plan: dict[str, Any],
        user_message: str,
        recent_messages: list[dict[str, Any]],
    ) -> str | None:
        if not self.enabled or not self.client:
            return None

        conversation_text = "\n".join(
            f"{m['role']}: {m['content']}" for m in recent_messages[-6:]
        )
        system_prompt = (
            "Write the final assistant reply for chat.\n"
            "Natural, friendly, calm. 2-6 short sentences by default.\n"
            "Use line breaks. No corporate dump. No robotic framing.\n"
            "If plan says include link, include it exactly once.\n"
            "If plan has a question, include only that one question.\n"
        )
        user_prompt = f"""
User message:
{user_message}

Recent turns:
{conversation_text}

Approved plan JSON:
{json.dumps(approved_plan, ensure_ascii=False)}
"""
        try:
            logger.info("writer_model=%s", self.settings.openai_model)
            response = self.client.responses.create(
                model=self.settings.openai_model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_output_tokens=500,
            )
            text = (response.output_text or "").strip()
            return re.sub(r"\n{3,}", "\n\n", text).strip() if text else None
        except Exception:
            return None

    @staticmethod
    def _normalize_plan(payload: dict[str, Any]) -> dict[str, Any]:
        out = dict(payload)
        out.setdefault("intent", "other")
        out.setdefault("ack", "")
        out.setdefault("value", [])
        out.setdefault("next_question", {"key": None, "text": None})
        out.setdefault("cta", {"type": "none", "reason": "", "include_link": False})
        out.setdefault("slot_updates", {})
        out.setdefault("state_update", {})
        out.setdefault("notes_for_ui", {"email_send": False, "email_subject": "", "email_body": ""})
        out.setdefault("verbosity", "short")

        if not isinstance(out["value"], list):
            out["value"] = []
        out["value"] = [str(v).strip() for v in out["value"] if str(v).strip()][:3]

        nq = out.get("next_question")
        if not isinstance(nq, dict):
            nq = {"key": None, "text": None}
        key = nq.get("key")
        text = nq.get("text")
        if key in {"null", "", None}:
            key = None
        nq["key"] = key
        nq["text"] = str(text).strip() if text else None
        out["next_question"] = nq

        cta = out.get("cta")
        if not isinstance(cta, dict):
            cta = {"type": "none", "reason": "", "include_link": False}
        cta_type = str(cta.get("type", "none")).lower().strip()
        cta["type"] = cta_type if cta_type in {"none", "soft", "hard"} else "none"
        cta["reason"] = str(cta.get("reason", "")).strip()
        cta["include_link"] = bool(cta.get("include_link", False))
        out["cta"] = cta

        verbosity = str(out.get("verbosity", "short")).lower().strip()
        out["verbosity"] = verbosity if verbosity in {"short", "expanded"} else "short"
        return out

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
