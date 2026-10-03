"""Deterministic replay provider.

Two jobs:

* **Zero-cost demos.** A recorded run replays exactly, so a reviewer can watch
  the system work without holding an API key or spending quota.
* **Hermetic tests.** The suite asserts on kernel behaviour -- planning,
  recovery, verification -- without any network call, and without the flakiness
  that live sampling would introduce.

Cassettes are just the on-disk LLM cache from a live run, so recording is a
side effect of running normally rather than a separate ceremony.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Sequence

from .base import CallCache, LLMError, LLMProvider, LLMResponse, Message, fingerprint


class CassetteMiss(LLMError):
    """No recorded response for this exact call."""


class ReplayProvider(LLMProvider):
    """Serves recorded responses, keyed by the same content address as the cache.

    The cassettes were recorded under a live provider's name, so replay
    *impersonates* that provider when computing fingerprints. The identity is
    inferred from the cassettes themselves, which keeps the demo free of extra
    configuration.
    """

    def __init__(
        self,
        model: str = "replay",
        cache: CallCache | None = None,
        *,
        cassette_dir: Path,
        impersonate: str | None = None,
    ) -> None:
        super().__init__(model, cache=None)  # a cache in front of a cache is noise
        self.cassette_dir = cassette_dir
        self._index: dict[str, LLMResponse] = {}
        self._load()
        inferred_provider, inferred_model = self._infer_identity()
        self.name = impersonate or inferred_provider
        if model == "replay":
            self.model = inferred_model

    def _load(self) -> None:
        if not self.cassette_dir.exists():
            return
        for path in sorted(self.cassette_dir.glob("*.json")):
            try:
                blob = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if "text" not in blob:
                continue
            self._index[path.stem] = LLMResponse(
                text=blob["text"],
                model=blob.get("model", "replay"),
                cached=True,
            )
            self._meta(path.stem, blob.get("meta") or {}, blob.get("model", ""))

    def _metas(self) -> dict[str, tuple[str, str]]:
        if not hasattr(self, "_meta_store"):
            self._meta_store: dict[str, tuple[str, str]] = {}
        return self._meta_store

    def _meta(self, key: str, meta: dict, model: str) -> None:
        self._metas()[key] = (meta.get("provider", ""), model)

    def _infer_identity(self) -> tuple[str, str]:
        providers = Counter(p for p, _ in self._metas().values() if p)
        models = Counter(m for _, m in self._metas().values() if m)
        provider = providers.most_common(1)[0][0] if providers else "gemini"
        model = models.most_common(1)[0][0] if models else "gemini-3.8-flash"
        return provider, model

    @property
    def cassette_count(self) -> int:
        return len(self._index)

    async def _invoke(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        schema: dict | None,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        key = fingerprint(self.name, self.model, system, messages, schema, temperature)
        hit = self._index.get(key)
        if hit is None:
            raise CassetteMiss(
                f"no cassette for {key} ({self.name}/{self.model}, "
                f"{len(self._index)} cassettes loaded). The prompt has drifted from "
                "what was recorded. Re-record with a live provider, or point "
                "OPERATOR_LLM_PROVIDER at one."
            )
        return hit


class ScriptedProvider(LLMProvider):
    """Queue of canned responses, for unit tests that drive one path on purpose."""

    name = "scripted"

    def __init__(self, responses: Sequence[str | Exception], model: str = "scripted") -> None:
        super().__init__(model, cache=None)
        self._queue: list[str | Exception] = list(responses)
        self.prompts: list[tuple[str, list[Message]]] = []

    async def _invoke(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        schema: dict | None,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        self.prompts.append((system, list(messages)))
        if not self._queue:
            raise LLMError("ScriptedProvider exhausted: the run made more calls than scripted")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResponse(text=item, model=self.model)
