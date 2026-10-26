"""Tests for LLM abstractions, MockLLMProvider, and GeminiProvider (SPEC §6, Phase 6)."""

from __future__ import annotations

import httpx
import pytest
import respx
from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from core.config import LlmModelsConfig
from core.llm.provider import GeminiProvider, LLMError, MockLLMProvider
from core.orm import Base, LlmCall


class SimpleExtractionModel(BaseModel):
    name: str
    status: str
    confidence: float


@pytest.fixture
def in_memory_db() -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_mock_llm_provider_text() -> None:
    provider = MockLLMProvider(default_text="Deterministic test text.")
    resp = provider.generate_text("Explain the game")
    assert resp == "Deterministic test text."
    assert len(provider.calls) == 1
    assert provider.calls[0]["purpose"] == "reasoning"
    assert provider.calls[0]["prompt"] == "Explain the game"


def test_mock_llm_provider_structured() -> None:
    expected = SimpleExtractionModel(name="Bukayo Saka", status="out", confidence=0.95)
    provider = MockLLMProvider(default_structured=expected)

    result = provider.generate_structured(SimpleExtractionModel, "Extract player status")
    assert result.name == "Bukayo Saka"
    assert result.status == "out"
    assert result.confidence == 0.95
    assert len(provider.calls) == 1
    assert provider.calls[0]["type"] == "structured"


def test_mock_llm_provider_error_injection() -> None:
    provider = MockLLMProvider()
    provider.raise_error = RuntimeError("Provider down")

    with pytest.raises(RuntimeError, match="Provider down"):
        provider.generate_text("Hi")


@respx.mock
def test_gemini_provider_generate_text(in_memory_db: sessionmaker[Session]) -> None:
    cfg = LlmModelsConfig(
        reasoning_model="gemini-3.8-flash",
        api_base="https://generativelanguage.googleapis.com/v1beta",
    )
    provider = GeminiProvider(
        api_key="test-gemini-key",
        config=cfg,
        db_session_factory=in_memory_db,
    )

    expected_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash"
        ":generateContent?key=test-gemini-key"
    )
    mock_response = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "Analytic briefing text."}]
                }
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 50000,
            "candidatesTokenCount": 20000,
        },
    }

    respx.post(expected_url).mock(
        return_value=httpx.Response(200, json=mock_response)
    )

    text = provider.generate_text("Provide brief", purpose="user_brief")
    assert text == "Analytic briefing text."

    # Verify call was logged into llm_calls table
    with in_memory_db() as session:
        calls = session.scalars(select(LlmCall)).all()
        assert len(calls) == 1
        assert calls[0].purpose == "user_brief"
        assert calls[0].model == "gemini-3.8-flash"
        assert calls[0].prompt_tokens == 50000
        assert calls[0].completion_tokens == 20000
        assert calls[0].cost_usd is not None
        assert calls[0].cost_usd > 0


@respx.mock
def test_gemini_provider_generate_structured_with_markdown_strip(
    in_memory_db: sessionmaker[Session],
) -> None:
    cfg = LlmModelsConfig(
        extraction_model="gemini-3.8-flash",
        api_base="https://generativelanguage.googleapis.com/v1beta",
    )
    provider = GeminiProvider(
        api_key="test-gemini-key",
        config=cfg,
        db_session_factory=in_memory_db,
    )

    expected_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash"
        ":generateContent?key=test-gemini-key"
    )

    # Return valid JSON wrapped in markdown fences
    fenced_json = (
        "```json\n"
        '{"name": "Kevin De Bruyne", "status": "doubtful", "confidence": 0.8}\n'
        "```"
    )
    mock_response = {
        "candidates": [{"content": {"parts": [{"text": fenced_json}]}}],
        "usageMetadata": {"promptTokenCount": 150, "candidatesTokenCount": 40},
    }

    respx.post(expected_url).mock(
        return_value=httpx.Response(200, json=mock_response)
    )

    res = provider.generate_structured(SimpleExtractionModel, "Extract KDB status")
    assert res.name == "Kevin De Bruyne"
    assert res.status == "doubtful"
    assert res.confidence == 0.8


@respx.mock
def test_gemini_provider_retry_on_invalid_json(in_memory_db: sessionmaker[Session]) -> None:
    cfg = LlmModelsConfig(
        extraction_model="gemini-3.8-flash",
        api_base="https://generativelanguage.googleapis.com/v1beta",
    )
    provider = GeminiProvider(
        api_key="test-gemini-key",
        config=cfg,
        db_session_factory=in_memory_db,
    )

    expected_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash"
        ":generateContent?key=test-gemini-key"
    )

    route = respx.post(expected_url)
    # First response: invalid JSON
    route.side_effect = [
        httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}),
        # Second response: valid JSON
        httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": (
                                        '{"name": "Rodri", "status": "out", "confidence": 1.0}'
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        ),
    ]

    res = provider.generate_structured(SimpleExtractionModel, "Extract Rodri status")
    assert res.name == "Rodri"
    assert res.status == "out"
    assert route.call_count == 2


@respx.mock
def test_gemini_provider_structured_exhausted_raises(in_memory_db: sessionmaker[Session]) -> None:
    cfg = LlmModelsConfig(
        extraction_model="gemini-3.8-flash",
        api_base="https://generativelanguage.googleapis.com/v1beta",
    )
    provider = GeminiProvider(
        api_key="test-gemini-key",
        config=cfg,
        db_session_factory=in_memory_db,
    )

    expected_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash"
        ":generateContent?key=test-gemini-key"
    )

    route = respx.post(expected_url)
    route.side_effect = [
        httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "bad 1"}]}}]}),
        httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "bad 2"}]}}]}),
    ]

    with pytest.raises(LLMError, match="Failed to generate structured data"):
        provider.generate_structured(SimpleExtractionModel, "Prompt")
