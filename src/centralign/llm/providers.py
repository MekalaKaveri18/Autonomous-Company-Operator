"""Concrete LLM providers, all speaking HTTP directly via httpx.

We deliberately avoid vendor SDKs. Each one brings its own retry policy,
timeout defaults and exception hierarchy, which would mean four different
failure modes leaking into the kernel. One HTTP client and one retry policy in
:mod:`centralign.llm.base` is easier to reason about and to test.
"""

from __future__ import annotations

import json
import os
from typing import Any, Sequence

import httpx

from .base import (
    LLMError,
    LLMProvider,
    LLMResponse,
    Message,
    RateLimited,
    TransientError,
    Usage,
)

_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)

#: Retry with the short backoff: a one-off server-side hiccup.
_RETRYABLE_STATUS = {408, 409, 500, 502, 504}

#: Retry with the long backoff. 503/529 from a hosted model means "capacity",
#: not "fault" -- it clears in tens of seconds, not hundreds of milliseconds, so
#: an impatient retry just burns the attempt budget before the queue drains.
_CAPACITY_STATUS = {503, 529}


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _quota_detail(response: httpx.Response) -> tuple[str, float | None, bool]:
    """Pull the useful parts out of a quota error body.

    Google returns the limit, the quota id and a RetryInfo delay. Surfacing them
    verbatim matters: "429" alone leaves you guessing whether to wait a second or
    give up for the day, and the distinction decides what the chain should do.
    """
    try:
        error = (response.json() or {}).get("error") or {}
    except (json.JSONDecodeError, ValueError):
        return response.text[:200], None, False

    delay: float | None = None
    quota_id = ""
    limit = ""
    for detail in error.get("details") or []:
        kind = str(detail.get("@type", ""))
        if kind.endswith("RetryInfo"):
            raw = str(detail.get("retryDelay", "")).rstrip("s")
            try:
                delay = float(raw)
            except ValueError:
                pass
        elif kind.endswith("QuotaFailure"):
            for violation in detail.get("violations") or []:
                quota_id = str(violation.get("quotaId", "")) or quota_id
                limit = str(violation.get("quotaValue", "")) or limit

    # A per-day quota, or a delay measured in minutes, means this model is done
    # for now -- the chain should move on rather than wait.
    exhausted = "PerDay" in quota_id or (delay is not None and delay > 300)
    summary = str(error.get("message", ""))[:220]
    if quota_id:
        summary = f"{summary} [quota={quota_id} limit={limit or '?'}]"
    return summary, delay, exhausted


def _classify(response: httpx.Response) -> None:
    """Map an HTTP error onto our retryable/non-retryable split."""
    if response.status_code == 429:
        summary, delay, exhausted = _quota_detail(response)
        raise RateLimited(
            f"quota (HTTP 429): {summary}",
            retry_after=_retry_after(response) or delay,
            exhausted=exhausted,
        )
    if response.status_code in _CAPACITY_STATUS:
        raise RateLimited(
            f"capacity (HTTP {response.status_code}): {response.text[:200]}",
            retry_after=_retry_after(response),
        )
    if response.status_code in _RETRYABLE_STATUS:
        raise TransientError(f"HTTP {response.status_code}: {response.text[:300]}")
    if response.status_code >= 400:
        raise LLMError(f"HTTP {response.status_code}: {response.text[:600]}")


class _HttpProvider(LLMProvider):
    """Shared httpx plumbing: one reusable client, uniform error classification."""

    def __init__(self, model: str, cache=None) -> None:
        super().__init__(model, cache)
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, url: str, *, headers: dict, payload: dict) -> dict:
        try:
            response = await self.client.post(url, headers=headers, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(f"transport failure: {exc}") from exc
        _classify(response)
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise TransientError(f"non-JSON provider response: {response.text[:300]}") from exc


# ---------------------------------------------------------------------------
# Gemini -- the default, because the free tier needs no credit card.
# ---------------------------------------------------------------------------

#: Gemini accepts only a restricted OpenAPI subset as ``responseSchema``.
#: Anything else is rejected with a 400, so we prune before sending.
_GEMINI_SCHEMA_KEYS = {
    "type",
    "format",
    "description",
    "nullable",
    "enum",
    "items",
    "properties",
    "required",
    "minItems",
    "maxItems",
    "anyOf",
    "propertyOrdering",
}


def sanitize_gemini_schema(schema: Any) -> Any:
    """Recursively prune a JSON Schema down to what Gemini will accept."""
    if isinstance(schema, list):
        return [sanitize_gemini_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _GEMINI_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {k: sanitize_gemini_schema(v) for k, v in value.items()}
        elif key in {"items", "anyOf"}:
            cleaned[key] = sanitize_gemini_schema(value)
        elif key == "type" and isinstance(value, list):
            # ["string", "null"] -> nullable string
            non_null = [t for t in value if t != "null"]
            cleaned["type"] = (non_null[0] if non_null else "string").upper()
            if len(non_null) != len(value):
                cleaned["nullable"] = True
        elif key == "type" and isinstance(value, str):
            cleaned["type"] = value.upper()
        else:
            cleaned[key] = value

    # Gemini rejects an OBJECT with no declared properties.
    if cleaned.get("type") == "OBJECT" and not cleaned.get("properties"):
        cleaned.pop("required", None)
        cleaned["type"] = "STRING"
        cleaned["description"] = "JSON-encoded object"
    return cleaned


class GeminiProvider(_HttpProvider):
    name = "gemini"
    supports_schema = True
    base_url = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, model: str, cache=None, api_key: str | None = None) -> None:
        super().__init__(model, cache)
        self.api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
        if not self.api_key:
            raise LLMError(
                "GEMINI_API_KEY is not set. Get a free key at "
                "https://aistudio.google.com/apikey, or run with "
                "OPERATOR_LLM_PROVIDER=replay to use recorded cassettes."
            )

    async def _invoke(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        schema: dict | None,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        contents = [
            {
                "role": "model" if m.role == "assistant" else "user",
                "parts": [{"text": m.content}],
            }
            for m in messages
        ]
        generation: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        if schema is not None:
            generation["responseMimeType"] = "application/json"
            generation["responseSchema"] = sanitize_gemini_schema(schema)

        payload: dict[str, Any] = {"contents": contents, "generationConfig": generation}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        body = await self._post(
            f"{self.base_url}/models/{self.model}:generateContent",
            headers={"x-goog-api-key": self.api_key, "content-type": "application/json"},
            payload=payload,
        )

        candidates = body.get("candidates") or []
        if not candidates:
            blocked = (body.get("promptFeedback") or {}).get("blockReason")
            raise LLMError(f"gemini returned no candidates (blockReason={blocked})")

        candidate = candidates[0]
        finish = candidate.get("finishReason")
        text = "".join(
            part.get("text", "") for part in (candidate.get("content") or {}).get("parts", [])
        )
        if finish == "MAX_TOKENS" and not text.strip():
            raise LLMError(
                "gemini hit MAX_TOKENS before emitting any content -- raise max_tokens "
                "or lower the thinking budget"
            )

        meta = body.get("usageMetadata") or {}
        return LLMResponse(
            text=text,
            model=self.model,
            usage=Usage(
                prompt_tokens=meta.get("promptTokenCount", 0),
                completion_tokens=meta.get("candidatesTokenCount", 0),
            ),
        )


# ---------------------------------------------------------------------------
# Anthropic -- schema enforced through a single forced tool call.
# ---------------------------------------------------------------------------


class AnthropicProvider(_HttpProvider):
    name = "anthropic"
    supports_schema = True
    base_url = "https://api.anthropic.com/v1"

    def __init__(self, model: str, cache=None, api_key: str | None = None) -> None:
        super().__init__(model, cache)
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY") or ""
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")

    async def _invoke(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        schema: dict | None,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "assistant" if m.role == "assistant" else "user", "content": m.content}
                for m in messages
            ],
        }
        if system:
            payload["system"] = system
        if schema is not None:
            # Forcing a tool is the only way to get a hard schema guarantee out
            # of the Messages API.
            payload["tools"] = [
                {
                    "name": "respond",
                    "description": "Return the structured result.",
                    "input_schema": schema,
                }
            ]
            payload["tool_choice"] = {"type": "tool", "name": "respond"}

        body = await self._post(
            f"{self.base_url}/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            payload=payload,
        )

        text_parts: list[str] = []
        for block in body.get("content", []):
            if block.get("type") == "tool_use":
                text_parts.append(json.dumps(block.get("input", {})))
            elif block.get("type") == "text":
                text_parts.append(block.get("text", ""))

        usage = body.get("usage") or {}
        return LLMResponse(
            text="".join(text_parts),
            model=body.get("model", self.model),
            usage=Usage(
                prompt_tokens=usage.get("input_tokens", 0),
                completion_tokens=usage.get("output_tokens", 0),
            ),
        )


# ---------------------------------------------------------------------------
# OpenAI-compatible: Groq, OpenRouter, OpenAI, Ollama, vLLM, LM Studio...
# ---------------------------------------------------------------------------


class OpenAICompatProvider(_HttpProvider):
    """Any ``/chat/completions`` endpoint.

    ``json_mode`` controls how hard we can constrain the output:
    ``schema`` for endpoints with real ``json_schema`` support, ``object`` for
    those with only ``json_object``, ``none`` for the rest (schema then travels
    in the system prompt, handled by the base class).
    """

    def __init__(
        self,
        model: str,
        cache=None,
        *,
        name: str,
        base_url: str,
        api_key: str = "",
        json_mode: str = "object",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(model, cache)
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.json_mode = json_mode
        self.supports_schema = json_mode == "schema"
        self.extra_headers = extra_headers or {}

    async def _invoke(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        schema: dict | None,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        chat: list[dict[str, str]] = []
        if system:
            chat.append({"role": "system", "content": system})
        chat.extend(m.as_dict() for m in messages)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": chat,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if schema is not None:
            if self.json_mode == "schema":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "result", "strict": False, "schema": schema},
                }
            elif self.json_mode == "object":
                payload["response_format"] = {"type": "json_object"}

        headers = {"content-type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"

        body = await self._post(f"{self.base_url}/chat/completions", headers=headers, payload=payload)

        choices = body.get("choices") or []
        if not choices:
            raise LLMError(f"{self.name} returned no choices: {str(body)[:300]}")
        text = (choices[0].get("message") or {}).get("content") or ""

        usage = body.get("usage") or {}
        return LLMResponse(
            text=text,
            model=body.get("model", self.model),
            usage=Usage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
            ),
        )
