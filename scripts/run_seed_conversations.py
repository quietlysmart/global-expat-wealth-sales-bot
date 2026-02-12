#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_URL = "http://localhost:8000"

SEEDS = [
    "I'm a UK expat in Thailand, can you help me retire in 10 years?",
    "What are your fees?",
    "Can you recommend the best fund?",
    "I don't trust advisors, I've been burned before.",
    "I want to move my UK pension. Is that a good idea?",
]


def main() -> None:
    conversation_id = None
    for i, message in enumerate(SEEDS, start=1):
        payload = {
            "message": message,
            "conversation_id": conversation_id,
            "channel_hint": "demo_web",
            "user_metadata": {"timezone": "Asia/Bangkok", "locale": "en-GB"},
            "demo_mode": True,
        }
        response = requests.post(f"{BASE_URL}/api/chat", json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        conversation_id = data["conversation_id"]
        print(f"\n--- Seed {i} ---")
        print("User:", message)
        print("Assistant:", data["assistant_reply"])
        print("Actions:", json.dumps(data["actions"], indent=2))

    print(f"\n[done] conversation_id={conversation_id}")


if __name__ == "__main__":
    main()
