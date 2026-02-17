from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import main as main_module


client = TestClient(main_module.app)


def test_transcribe_requires_auth_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(main_module.settings, "auth_required", True)

    response = client.post(
        "/api/transcribe",
        files={"audio": ("voice.webm", b"abc", "audio/webm")},
    )

    assert response.status_code == 401


def test_transcribe_rejects_empty_audio_in_public_mode(monkeypatch) -> None:
    monkeypatch.setattr(main_module.settings, "auth_required", False)
    monkeypatch.setattr(main_module.chat.llm, "enabled", True)

    response = client.post(
        "/api/transcribe",
        files={"audio": ("voice.webm", b"", "audio/webm")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "audio is empty"


def test_transcribe_returns_text(monkeypatch) -> None:
    monkeypatch.setattr(main_module.settings, "auth_required", False)
    monkeypatch.setattr(main_module.chat.llm, "enabled", True)

    def _fake_transcribe(raw: bytes, filename: str, content_type: str | None) -> str:
        assert raw
        assert filename == "voice.webm"
        assert content_type == "audio/webm"
        return "hello from voice"

    monkeypatch.setattr(main_module.chat.llm, "transcribe_audio", _fake_transcribe)

    response = client.post(
        "/api/transcribe",
        files={"audio": ("voice.webm", b"abc", "audio/webm")},
    )

    assert response.status_code == 200
    assert response.json()["text"] == "hello from voice"


def test_transcribe_allows_empty_text_when_audio_is_unclear(monkeypatch) -> None:
    monkeypatch.setattr(main_module.settings, "auth_required", False)
    monkeypatch.setattr(main_module.chat.llm, "enabled", True)

    def _fake_transcribe(raw: bytes, filename: str, content_type: str | None) -> str:
        assert raw
        return ""

    monkeypatch.setattr(main_module.chat.llm, "transcribe_audio", _fake_transcribe)

    response = client.post(
        "/api/transcribe",
        files={"audio": ("voice.webm", b"abc", "audio/webm")},
    )

    assert response.status_code == 200
    assert response.json()["text"] == ""
