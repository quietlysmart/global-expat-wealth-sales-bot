from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class Playbook:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        with self.path.open("r", encoding="utf-8") as f:
            return json.load(f)

    @property
    def disclaimer(self) -> str:
        return self.data["compliance_guardrails"]["safe_disclaimer"]

    @property
    def handoff_triggers(self) -> list[str]:
        return self.data["compliance_guardrails"]["handoff_triggers"]

    def objection_match(self, message_lower: str) -> dict[str, Any] | None:
        for item in self.data.get("objection_handling", []):
            for pattern in item.get("patterns", []):
                if pattern in message_lower:
                    return item
        return None

    def next_question_set(self, asked_count: int = 0) -> list[str]:
        sets = self.data.get("qualification_flow", {}).get("question_sets", [])
        if not sets:
            return []
        idx = min(asked_count, len(sets) - 1)
        return sets[idx].get("questions", [])[:2]

    def stage_for_message(self, message_lower: str) -> str:
        rules = self.data.get("stage_rules", {})
        for kw in rules.get("intent_keywords", []):
            if kw in message_lower:
                return "intent"
        for kw in rules.get("consideration_keywords", []):
            if kw in message_lower:
                return "consideration"
        return "awareness"
