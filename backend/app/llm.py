from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests
from openai import OpenAI

from .config import Settings

logger = logging.getLogger(__name__)


class OptionalLLM:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.enabled = False
        self.disabled_reason = ""
        self.client = None
        self.last_transcription_error = ""

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
            "You represent Dan Whiting at Global Expat Wealth.\n"
            "Dan has 27 years of cross-border experience helping expats in Asia.\n"
            "Never claim you are Dan. Speak as his assistant and refer to Dan in third person.\n"
            "If asked who you work for, answer: Global Expat Wealth, led by Dan Whiting.\n"
            "Return JSON only.\n"
            "Focus on natural pacing. Helpful first. Ask at most one question.\n"
            "If the user just answered the previous question, usually advance to the next best question.\n"
            "Do not use repetitive acknowledgements like 'Yes, we can help' every turn.\n"
            "Treat the user's latest message as the top priority and answer it directly.\n"
            "Avoid brochure language and avoid listing multiple services unless asked.\n"
            "Write simple plain English suitable for a smart 16-year-old.\n"
            "Most turns should be a few sentences, not a long block.\n"
            "After giving real help, guide naturally toward a call with Dan when user asks for next steps or seems stuck.\n"
            "Prefer soft CTA first; use hard CTA only when constraints allow.\n"
            "Do not ask for slots already answered.\n"
            "Do not use robotic lead-ins.\n"
            "Use progressive qualification naturally.\n"
            "Question text must be exactly one sentence.\n"
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
  "answer": "2-5 natural sentences answering the latest user message (no question marks)",
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
        for model in self._candidate_models():
            try:
                logger.info("planner_model=%s", model)
                response = self.client.responses.create(
                    model=model,
                    input=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_output_tokens=700,
                )
                payload = self._extract_json(response.output_text)
                if not payload:
                    continue
                return self._normalize_plan(payload)
            except Exception as exc:
                logger.warning("planner_call_failed model=%s error=%s", model, exc)
                continue
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
            "You are Dan Whiting's assistant at Global Expat Wealth.\n"
            "Never say you personally are Dan.\n"
            "If asked who you work for, clearly state Global Expat Wealth and Dan Whiting.\n"
            "Natural, friendly, calm. 2-6 short sentences by default.\n"
            "Use line breaks. No corporate dump. No robotic framing.\n"
            "Avoid repeating sentence starters from the previous assistant turn.\n"
            "Respond to what the user just said before anything else.\n"
            "Do not use generic filler like 'we can help' or capability lists.\n"
            "Use concrete, practical wording tied to known user details from plan.\n"
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
        for model in self._candidate_models():
            try:
                logger.info("writer_model=%s", model)
                response = self.client.responses.create(
                    model=model,
                    input=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_output_tokens=500,
                )
                text = (response.output_text or "").strip()
                if text:
                    return re.sub(r"\n{3,}", "\n\n", text).strip()
            except Exception as exc:
                logger.warning("writer_call_failed model=%s error=%s", model, exc)
                continue
        return None

    def transcribe_audio(
        self,
        audio_bytes: bytes,
        filename: str = "voice_input.webm",
        content_type: str | None = None,
    ) -> str | None:
        if not self.enabled or not self.client or not audio_bytes:
            self.last_transcription_error = "llm_not_enabled_or_empty_audio"
            return None
        self.last_transcription_error = ""
        tried: list[str] = []
        had_success_response = False
        for model in [
            self.settings.openai_transcription_model,
            "gpt-4o-mini-transcribe",
            "whisper-1",
        ]:
            if model in tried:
                continue
            tried.append(model)
            try:
                response = self.client.audio.transcriptions.create(
                    model=model,
                    # Let OpenAI infer file metadata from filename when possible.
                    file=(filename, audio_bytes),
                )
                had_success_response = True
                text = (getattr(response, "text", "") or "").strip()
                if text:
                    self.last_transcription_error = ""
                    return text
            except Exception as exc:
                self.last_transcription_error = f"sdk:{type(exc).__name__}"
                logger.warning(
                    "transcription_call_failed model=%s content_type=%s bytes=%s error_type=%s",
                    model,
                    content_type,
                    len(audio_bytes),
                    type(exc).__name__,
                )
                continue

        # Fallback path using raw HTTP multipart request if SDK transport fails.
        for model in tried:
            try:
                text = self._transcribe_with_http(
                    model=model,
                    audio_bytes=audio_bytes,
                    filename=filename,
                    content_type=content_type,
                )
                if text is not None:
                    self.last_transcription_error = ""
                    return text
            except Exception as exc:
                self.last_transcription_error = f"http:{type(exc).__name__}"
                logger.warning(
                    "transcription_http_fallback_failed model=%s content_type=%s bytes=%s error_type=%s",
                    model,
                    content_type,
                    len(audio_bytes),
                    type(exc).__name__,
                )
                continue
        if not self.last_transcription_error:
            self.last_transcription_error = "no_transcription_result"
        return "" if had_success_response else None

    def _candidate_models(self) -> list[str]:
        models = [self.settings.openai_model]
        for fallback in ["gpt-5-mini", "gpt-4.1-mini"]:
            if fallback not in models:
                models.append(fallback)
        return models

    @staticmethod
    def _normalize_plan(payload: dict[str, Any]) -> dict[str, Any]:
        out = dict(payload)
        out.setdefault("answer", "")
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
        out["answer"] = str(out.get("answer", "")).strip()

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

    def _transcribe_with_http(
        self,
        model: str,
        audio_bytes: bytes,
        filename: str,
        content_type: str | None,
    ) -> str | None:
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
        }
        files = {
            "file": (filename, audio_bytes, content_type or "application/octet-stream"),
        }
        data = {
            "model": model,
        }
        resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers=headers,
            files=files,
            data=data,
            timeout=45,
        )
        logger.info(
            "transcription_http_response model=%s status=%s bytes=%s content_type=%s",
            model,
            resp.status_code,
            len(audio_bytes),
            content_type,
        )
        if resp.status_code >= 500:
            return None
        if resp.status_code >= 400:
            logger.warning(
                "transcription_http_bad_status model=%s status=%s body=%s",
                model,
                resp.status_code,
                resp.text[:400],
            )
            return None
        payload = resp.json()
        text = str(payload.get("text", "")).strip()
        return text
