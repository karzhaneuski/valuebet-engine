"""LLM Provider abstraction and implementations (SPEC §6, decisions S5).

Provides LLMProvider interface, GeminiProvider (REST API via safe HTTP),
and MockLLMProvider for offline deterministic testing without network calls.
Logs every call to the llm_calls database table.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from core.config import LlmModelsConfig, Secrets, load_config
from core.http import HttpError, safe_request
from core.orm import LlmCall

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Base error for LLM generation failures."""


class LLMProvider(ABC):
    """Abstract interface for LLM operations."""

    @abstractmethod
    def generate_text(
        self,
        prompt: str,
        model: str | None = None,
        purpose: str = "reasoning",
    ) -> str:
        """Generate unstructured text from prompt."""

    @abstractmethod
    def generate_structured(
        self,
        schema: type[T],
        prompt: str,
        model: str | None = None,
        purpose: str = "extraction",
    ) -> T:
        """Generate structured JSON response conforming to pydantic schema T."""


class MockLLMProvider(LLMProvider):
    """Deterministic mock provider for testing without external network calls."""

    def __init__(
        self,
        default_text: str = "Mock reasoning summary.",
        default_structured: BaseModel | None = None,
    ) -> None:
        self.default_text = default_text
        self.default_structured = default_structured
        self.calls: list[dict[str, Any]] = []
        self.text_responses: list[str] = []
        self.structured_responses: list[Any] = []
        self.raise_error: Exception | None = None

    def generate_text(
        self,
        prompt: str,
        model: str | None = None,
        purpose: str = "reasoning",
    ) -> str:
        if self.raise_error is not None:
            raise self.raise_error

        resp = self.text_responses.pop(0) if self.text_responses else self.default_text
        self.calls.append({
            "type": "text",
            "prompt": prompt,
            "model": model or "mock-model",
            "purpose": purpose,
            "response": resp,
        })
        return resp

    def generate_structured(
        self,
        schema: type[T],
        prompt: str,
        model: str | None = None,
        purpose: str = "extraction",
    ) -> T:
        if self.raise_error is not None:
            raise self.raise_error

        if self.structured_responses:
            item = self.structured_responses.pop(0)
            if isinstance(item, schema):
                result = item
            elif isinstance(item, (dict, str)):
                result = schema.model_validate(item if isinstance(item, dict) else json.loads(item))
            elif isinstance(item, Exception):
                raise item
            else:
                result = schema.model_validate(item)
        elif self.default_structured is not None and isinstance(self.default_structured, schema):
            result = self.default_structured
        else:
            msg = f"MockLLMProvider has no structured response queued for {schema.__name__}"
            raise LLMError(msg)

        self.calls.append({
            "type": "structured",
            "prompt": prompt,
            "model": model or "mock-model",
            "purpose": purpose,
            "response": result.model_dump(),
        })
        return result


class GeminiProvider(LLMProvider):
    """Google Gemini LLM provider calling the REST API with retries and logging."""

    def __init__(
        self,
        api_key: str | None = None,
        config: LlmModelsConfig | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        db_session_factory: Any | None = None,
    ) -> None:
        secrets = Secrets()
        self.api_key = api_key if api_key is not None else secrets.gemini_api_key
        self.config = config or load_config().llm
        self.timeout = timeout
        self.max_retries = max_retries
        self.db_session_factory = db_session_factory

    def _log_call(
        self,
        purpose: str,
        prompt: str,
        response: str | None,
        model: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cost_usd: Decimal | None = None,
    ) -> None:
        """Persist LLM call metadata to llm_calls table if database session is configured."""
        if self.db_session_factory is None:
            return

        try:
            with self.db_session_factory() as session:
                rec = LlmCall(
                    purpose=purpose,
                    prompt=prompt,
                    response=response,
                    model=model,
                    model_version=None,
                    thinking_config=None,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    thoughts_tokens=None,
                    cost_usd=cost_usd,
                    created_at_utc=dt.datetime.now(dt.UTC),
                )
                session.add(rec)
                session.commit()
        except Exception as exc:
            logger.warning("Failed to log LLM call to database: %s", exc)

    def _call_api_with_retry(
        self,
        model: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set. Cannot call Gemini API.")

        url = f"{self.config.api_base}/models/{model}:generateContent"
        attempts = 0
        backoff = 1.0

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

        with httpx.Client(timeout=self.timeout) as client:
            while attempts < self.max_retries:
                attempts += 1
                try:
                    resp = safe_request(
                        client=client,
                        method="POST",
                        url=url,
                        json=payload,
                        headers=headers,
                    )
                    data: dict[str, Any] = resp.json()
                    return data
                except HttpError as exc:
                    if exc.status in (429, 500, 502, 503, 504) and attempts < self.max_retries:
                        logger.warning(
                            "Gemini API returned status %s (attempt %s/%s). Retrying in %ss...",
                            exc.status,
                            attempts,
                            self.max_retries,
                            backoff,
                        )
                        time.sleep(backoff)
                        backoff *= 2.0
                        continue
                    raise LLMError(f"Gemini API error on {model}: {exc}") from exc
                except Exception as exc:
                    if attempts < self.max_retries:
                        time.sleep(backoff)
                        backoff *= 2.0
                        continue
                    raise LLMError(f"Gemini connection error: {exc}") from exc

        raise LLMError(f"Exceeded max retries ({self.max_retries}) calling Gemini API")

    def generate_text(
        self,
        prompt: str,
        model: str | None = None,
        purpose: str = "reasoning",
    ) -> str:
        chosen_model = model or self.config.reasoning_model
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
            },
        }

        try:
            data = self._call_api_with_retry(chosen_model, payload)

            candidates = data.get("candidates", [])
            if not candidates:
                raise LLMError("Gemini returned no candidates in response")
            text = str(candidates[0]["content"]["parts"][0]["text"])

            # Extract token usage if available
            usage = data.get("usageMetadata", {})
            p_tok = usage.get("promptTokenCount")
            c_tok = usage.get("candidatesTokenCount")
            cost = Decimal("0.0000")
            if p_tok and c_tok:
                # Estimate Flash cost: ~$0.10/1M in, $0.40/1M out
                cost = Decimal(f"{(p_tok * 0.00000010 + c_tok * 0.00000040):.6f}")

            self._log_call(
                purpose=purpose,
                prompt=prompt,
                response=text,
                model=chosen_model,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                cost_usd=cost,
            )
            return text
        except Exception as exc:
            self._log_call(
                purpose=purpose,
                prompt=prompt,
                response=f"ERROR: {exc}",
                model=chosen_model,
            )
            raise

    def generate_structured(
        self,
        schema: type[T],
        prompt: str,
        model: str | None = None,
        purpose: str = "extraction",
    ) -> T:
        chosen_model = model or self.config.extraction_model

        # Instruct model to return pure JSON conforming to schema
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        augmented_prompt = (
            f"{prompt}\n\n"
            f"IMPORTANT: Output MUST be strictly valid JSON conforming to this JSON schema:\n"
            f"{schema_json}\n"
            f"Do NOT include markdown formatting, backticks (```json), or explanatory text."
        )

        prompt_text = augmented_prompt
        last_error: Exception | None = None
        for attempt in range(2):
            payload: dict[str, Any] = {
                "contents": [{"parts": [{"text": prompt_text}]}],
                "generationConfig": {
                    "response_mime_type": "application/json",
                    "temperature": 0.0,
                },
            }
            data = self._call_api_with_retry(chosen_model, payload)
            candidates = data.get("candidates", [])
            if not candidates:
                raise LLMError("Gemini returned no candidates in structured response")
            raw_text = candidates[0]["content"]["parts"][0]["text"].strip()

            # Clean markdown code blocks if present
            if raw_text.startswith("```"):
                lines = raw_text.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                raw_text = "\n".join(lines).strip()

            try:
                result = schema.model_validate_json(raw_text)
                usage = data.get("usageMetadata", {})
                p_tok = usage.get("promptTokenCount")
                c_tok = usage.get("candidatesTokenCount")

                self._log_call(
                    purpose=purpose,
                    prompt=augmented_prompt,
                    response=raw_text,
                    model=chosen_model,
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                )
                return result
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Structured validation error on attempt %s: %s. Raw: %s",
                    attempt + 1,
                    exc,
                    raw_text[:200],
                )
                # On retry, emphasize strictly raw JSON
                prompt_text = (
                    f"{augmented_prompt}\n"
                    f"Your previous response failed validation: {exc}. "
                    "Fix errors and return strictly valid raw JSON only."
                )

        err_msg = f"Failed to generate structured data for {schema.__name__}: {last_error}"
        self._log_call(
            purpose=purpose,
            prompt=prompt,
            response=f"ERROR: {err_msg}",
            model=chosen_model,
        )
        raise LLMError(err_msg)
