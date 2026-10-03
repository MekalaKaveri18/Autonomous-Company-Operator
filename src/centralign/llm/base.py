"""Provider-agnostic LLM access.

Design decision: the runtime never uses a vendor's *tool-calling* API. Every
model interaction is a **JSON-schema-constrained completion**, and the runtime
itself decides what to do with the returned object. Two reasons:

1. Portability. Schema-constrained JSON is the one capability that behaves
   consistently across Gemini, Anthropic, OpenAI-compatible endpoints and local
   models. Vendor tool-calling semantics differ enough that a swap would ripple
   into the kernel. Here, swapping providers changes one environment variable.
2. Control. We want the plan, the observation summary and the verdict to be
   first-class typed objects we can log, diff and replay -- not opaque
   provider-side state.

Every call is content-addressed and cached on disk. That cache doubles as the
test suite's cassette store, so a recorded run replays deterministically and at
zero cost.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: Usage = field(default_factory=Usage)
    cached: bool = False
    attempts: int = 1

    def json(self) -> Any:
        return extract_json(self.text)


class LLMError(RuntimeError):
    """Non-retryable provider failure."""


class RateLimited(RuntimeError):
    """Retryable: the provider asked us to slow down.

    ``exhausted`` distinguishes "wait a moment" from "this model is finished for
    today". Free tiers meter per model per day, so an exhausted model's
    ``retry_after`` can be eleven hours -- sleeping on that would hang the run
    until tomorrow. An exhausted model is abandoned immediately so the fallback
    chain can try the next one, which has its own separate quota.
    """

    def __init__(
        self,
        message: str,
        retry_after: float | None = None,
        *,
        exhausted: bool = False,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.exhausted = exhausted


class TransientError(RuntimeError):
    """Retryable: timeout, 5xx, connection reset."""


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def extract_json(text: str) -> Any:
    """Parse JSON out of a model response, tolerating the usual contamination.

    Free-tier and local models wrap JSON in prose or markdown fences far more
    often than frontier models do. Rather than retry-and-pray on every such
    response, we repair what is unambiguously repairable and escalate only when
    the payload is genuinely not JSON.
    """
    candidate = text.strip()
    if not candidate:
        raise LLMError("model returned an empty response")

    attempts: list[str] = []

    fenced = _FENCE.search(candidate)
    if fenced:
        attempts.append(fenced.group(1).strip())

    attempts.append(candidate)

    span = _outermost_span(candidate)
    if span:
        attempts.append(span)

    for attempt in attempts:
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            repaired = _TRAILING_COMMA.sub(r"\1", attempt)
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                continue

    preview = candidate[:400].replace("\n", " ")
    raise LLMError(f"response was not JSON: {preview}")


def _outermost_span(text: str) -> str | None:
    """Return the outermost object/array span, aware of string literals."""
    start = None
    opener = ""
    closer = ""
    for index, char in enumerate(text):
        if char in "{[":
            start = index
            opener = char
            closer = "}" if char == "{" else "]"
            break
    if start is None:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def fingerprint(
    provider: str,
    model: str,
    system: str,
    messages: Sequence[Message],
    schema: dict | None,
    temperature: float,
) -> str:
    """Content address for a call -- the cache key and the cassette filename."""
    payload = json.dumps(
        {
            "provider": provider,
            "model": model,
            "system": system,
            "messages": [m.as_dict() for m in messages],
            "schema": schema,
            "temperature": temperature,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


class CallCache:
    """On-disk, content-addressed LLM cache that doubles as a cassette store."""

    def __init__(self, directory: Path, *, enabled: bool = True) -> None:
        self.directory = directory
        self.enabled = enabled
        if enabled:
            directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> LLMResponse | None:
        if not self.enabled:
            return None
        path = self._path(key)
        if not path.exists():
            return None
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return LLMResponse(
            text=blob["text"],
            model=blob.get("model", "cached"),
            usage=Usage(**blob.get("usage", {})),
            cached=True,
        )

    def put(self, key: str, response: LLMResponse, *, meta: dict | None = None) -> None:
        if not self.enabled:
            return
        blob = {
            "text": response.text,
            "model": response.model,
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
            },
            "meta": meta or {},
        }
        self._path(key).write_text(
            json.dumps(blob, indent=2, ensure_ascii=False), encoding="utf-8"
        )


class LLMProvider:
    """Base class handling cache, retry/backoff and schema coercion.

    Subclasses implement only :meth:`_invoke`.
    """

    name: str = "base"
    #: Providers that cannot enforce a schema server-side get it in the prompt.
    supports_schema: bool = True
    #: Never sleep longer than this on a provider's say-so. Beyond it, the model
    #: is treated as unavailable and we move on rather than stall the run.
    MAX_BACKOFF_SLEEP: float = 45.0

    def __init__(self, model: str, cache: CallCache | None = None) -> None:
        self.model = model
        self.cache = cache
        self.calls_made = 0
        self.cache_hits = 0
        self.usage = Usage()

    # -- subclass contract --------------------------------------------------
    async def _invoke(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        schema: dict | None,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release any network resources."""

    # -- public API ---------------------------------------------------------
    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        schema: dict | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        max_attempts: int = 4,
    ) -> LLMResponse:
        if not self.supports_schema and schema is not None:
            system = f"{system}\n\n{schema_instruction(schema)}"

        key = fingerprint(self.name, self.model, system, messages, schema, temperature)
        if self.cache:
            hit = self.cache.get(key)
            if hit is not None:
                self.cache_hits += 1
                return hit

        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = await self._invoke(
                    system=system,
                    messages=messages,
                    schema=schema,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except RateLimited as exc:
                last_error = exc
                if exc.exhausted:
                    # Out of quota for the day. Retrying this model cannot
                    # succeed, so stop spending attempts on it immediately.
                    break
                if attempt == max_attempts:
                    break
                delay = exc.retry_after or backoff(attempt, base=4.0)
                if delay > self.MAX_BACKOFF_SLEEP:
                    break
                await asyncio.sleep(delay)
                continue
            except TransientError as exc:
                last_error = exc
                if attempt == max_attempts:
                    break
                await asyncio.sleep(backoff(attempt))
                continue

            response.attempts = attempt
            self.calls_made += 1
            self.usage.prompt_tokens += response.usage.prompt_tokens
            self.usage.completion_tokens += response.usage.completion_tokens
            if self.cache:
                self.cache.put(key, response, meta={"provider": self.name})
            return response

        raise LLMError(
            f"{self.name}/{self.model} failed after {max_attempts} attempts: {last_error}"
        ) from last_error

    async def complete_json(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        schema: dict,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> Any:
        """Schema-constrained call with one *corrective* retry.

        If the model returns something unparseable we hand the failure back to
        it verbatim. That recovers far more often than a blind retry, because
        the model can see exactly what it got wrong.
        """
        response = await self.complete(
            system=system,
            messages=messages,
            schema=schema,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        try:
            return response.json()
        except LLMError as exc:
            corrective = list(messages) + [
                Message("assistant", response.text[:2000]),
                Message(
                    "user",
                    f"That was not valid JSON matching the required schema ({exc}). "
                    "Reply with the JSON value only -- no prose, no markdown fence.",
                ),
            ]
            retry = await self.complete(
                system=system,
                messages=corrective,
                schema=schema,
                temperature=0.0,
                max_tokens=max_tokens,
            )
            return retry.json()


def schema_instruction(schema: dict) -> str:
    return (
        "You must reply with a single JSON value and nothing else. It must "
        "validate against this JSON Schema:\n"
        f"{json.dumps(schema, indent=2)}"
    )


def backoff(attempt: int, *, base: float = 1.0, cap: float = 30.0) -> float:
    """Exponential backoff with full jitter."""
    return min(cap, base * (2 ** (attempt - 1))) * (0.5 + random.random() / 2)
