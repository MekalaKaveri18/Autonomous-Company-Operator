"""The fallback chain and quota handling.

The regression these guard against was severe and silent. Google returns
``retryDelay: 40557s`` when a model's *daily* free-tier quota is gone, and the
retry loop honoured it literally -- so a run would have slept for eleven hours
instead of switching to one of the eight other models that each have their own
separate quota. Nothing would have crashed; the run would simply never finish.
"""

from __future__ import annotations

import json

import httpx
import pytest

from centralign.llm.base import (
    LLMError,
    LLMProvider,
    LLMResponse,
    Message,
    RateLimited,
    TransientError,
)
from centralign.llm.fallback import FallbackProvider
from centralign.llm.providers import _classify, _quota_detail


def _response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://x/y"))


DAILY_QUOTA_BODY = {
    "error": {
        "code": 429,
        "message": "You exceeded your current quota.",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {
                        "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                        "quotaValue": "20",
                    }
                ],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "40557s"},
        ],
    }
}

PER_MINUTE_BODY = {
    "error": {
        "code": 429,
        "message": "Too many requests.",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {"quotaId": "GenerateRequestsPerMinutePerProject-FreeTier", "quotaValue": "15"}
                ],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "6s"},
        ],
    }
}


class TestQuotaParsing:
    def test_a_daily_quota_is_marked_exhausted(self):
        summary, delay, exhausted = _quota_detail(_response(429, DAILY_QUOTA_BODY))
        assert exhausted is True
        assert delay == 40557.0
        assert "PerDay" in summary and "20" in summary

    def test_a_per_minute_quota_is_not_exhausted(self):
        _, delay, exhausted = _quota_detail(_response(429, PER_MINUTE_BODY))
        assert exhausted is False
        assert delay == 6.0

    def test_a_long_delay_counts_as_exhausted_even_without_a_quota_id(self):
        body = {"error": {"message": "slow down", "details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "900s"}]}}
        _, _, exhausted = _quota_detail(_response(429, body))
        assert exhausted is True

    def test_a_non_json_body_does_not_crash_the_parser(self):
        response = httpx.Response(429, text="<html>429</html>", request=httpx.Request("POST", "https://x"))
        summary, delay, exhausted = _quota_detail(response)
        assert delay is None and exhausted is False
        assert summary

    def test_classify_raises_rate_limited_with_the_exhausted_flag(self):
        with pytest.raises(RateLimited) as caught:
            _classify(_response(429, DAILY_QUOTA_BODY))
        assert caught.value.exhausted is True

    def test_a_503_is_capacity_not_quota_exhaustion(self):
        with pytest.raises(RateLimited) as caught:
            _classify(_response(503, {"error": {"message": "high demand"}}))
        assert caught.value.exhausted is False

    def test_a_400_is_not_retryable_at_all(self):
        with pytest.raises(LLMError):
            _classify(_response(400, {"error": {"message": "bad schema"}}))


class _Stub(LLMProvider):
    """A provider that fails a fixed number of times, then succeeds."""

    name = "stub"

    def __init__(self, model: str, *, failures: list[Exception] | None = None, answer: str = '{"ok":1}'):
        super().__init__(model, cache=None)
        self.queue = list(failures or [])
        self.answer = answer
        self.attempts = 0

    async def _invoke(self, **_: object) -> LLMResponse:
        self.attempts += 1
        if self.queue:
            raise self.queue.pop(0)
        return LLMResponse(text=self.answer, model=self.model)


def _exhausted() -> RateLimited:
    return RateLimited("daily quota gone", retry_after=40557.0, exhausted=True)


class TestNoSleepOnExhaustion:
    async def test_an_exhausted_model_is_abandoned_without_sleeping(self, monkeypatch):
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr("centralign.llm.base.asyncio.sleep", fake_sleep)

        provider = _Stub("m", failures=[_exhausted()])
        with pytest.raises(LLMError):
            await provider.complete(system="s", messages=[Message("user", "x")])

        assert slept == [], "an eleven-hour retry delay must never be slept on"
        assert provider.attempts == 1, "an exhausted model must not be retried"

    async def test_a_short_delay_is_still_honoured(self, monkeypatch):
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr("centralign.llm.base.asyncio.sleep", fake_sleep)

        provider = _Stub("m", failures=[RateLimited("slow down", retry_after=6.0)])
        await provider.complete(system="s", messages=[Message("user", "x")])
        assert slept == [6.0]

    async def test_an_absurd_delay_is_capped_rather_than_slept(self, monkeypatch):
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr("centralign.llm.base.asyncio.sleep", fake_sleep)

        # Not flagged exhausted, but still far beyond anything worth waiting for.
        provider = _Stub("m", failures=[RateLimited("busy", retry_after=3600.0)])
        with pytest.raises(LLMError):
            await provider.complete(system="s", messages=[Message("user", "x")])
        assert slept == []


class TestChainBehaviour:
    async def test_it_moves_to_the_next_model_when_one_is_exhausted(self):
        first = _Stub("model-a", failures=[_exhausted()])
        second = _Stub("model-b", answer='{"ok":2}')
        chain = FallbackProvider([first, second], name="gemini")

        result = await chain.complete_json(
            system="s", messages=[Message("user", "x")], schema={"type": "object"}
        )
        assert result == {"ok": 2}
        assert "model-a" in chain.exhausted
        assert chain.active_model == "model-b"

    async def test_an_exhausted_model_is_skipped_on_later_calls(self):
        first = _Stub("model-a", failures=[_exhausted()])
        second = _Stub("model-b")
        chain = FallbackProvider([first, second], name="gemini")

        await chain.complete(system="s", messages=[Message("user", "1")])
        attempts_after_first = first.attempts
        await chain.complete(system="s", messages=[Message("user", "2")])

        assert first.attempts == attempts_after_first, "must not re-probe an exhausted model"
        assert second.attempts == 2

    async def test_a_genuine_bad_request_is_not_retried_on_other_models(self):
        """A schema error fails identically everywhere; falling back hides the bug."""
        first = _Stub("model-a", failures=[LLMError("HTTP 400: invalid schema")])
        second = _Stub("model-b")
        chain = FallbackProvider([first, second], name="gemini")

        with pytest.raises(LLMError, match="invalid schema"):
            await chain.complete(system="s", messages=[Message("user", "x")])
        assert second.attempts == 0, "a bad request must not be replayed on the next model"

    async def test_transient_faults_fall_through_to_the_next_model(self):
        first = _Stub("model-a", failures=[TransientError("boom")] * 5)
        second = _Stub("model-b")
        chain = FallbackProvider([first, second], name="gemini")

        await chain.complete(system="s", messages=[Message("user", "x")])
        assert chain.active_model == "model-b"
        assert chain.fallbacks_used

    async def test_it_reports_clearly_when_the_whole_chain_is_down(self):
        members = [_Stub(f"m{i}", failures=[_exhausted()]) for i in range(3)]
        chain = FallbackProvider(members, name="gemini")

        with pytest.raises(LLMError, match="every model in the chain is unavailable"):
            await chain.complete(system="s", messages=[Message("user", "x")])

    async def test_the_chain_keeps_one_stable_cache_identity(self, tmp_path):
        """Cassettes must not be split across whichever model happened to answer."""
        from centralign.llm.base import CallCache, fingerprint

        cache = CallCache(tmp_path)
        first = _Stub("model-a", failures=[_exhausted()])
        second = _Stub("model-b", answer='{"ok":9}')
        chain = FallbackProvider([first, second], name="gemini", cache=cache)

        await chain.complete(system="s", messages=[Message("user", "x")], temperature=0.1)

        # Keyed on the chain's primary identity, not on model-b which served it.
        key = fingerprint("gemini", "model-a", "s", [Message("user", "x")], None, 0.1)
        assert cache.get(key) is not None

    async def test_an_empty_chain_is_rejected(self):
        with pytest.raises(LLMError):
            FallbackProvider([], name="gemini")

    async def test_schema_support_is_the_weakest_members(self):
        strong = _Stub("a")
        weak = _Stub("b")
        weak.supports_schema = False
        assert FallbackProvider([strong, weak], name="x").supports_schema is False
        assert FallbackProvider([strong], name="x").supports_schema is True


def test_a_comma_separated_model_builds_a_chain(monkeypatch, tmp_path):
    import dataclasses

    from centralign.config import SETTINGS
    from centralign.llm.registry import build_provider

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    settings = dataclasses.replace(
        SETTINGS, provider="gemini", model="gemini-3.8-flash, gemini-3.5-flash", llm_cache=False
    )
    provider = build_provider(settings)
    assert isinstance(provider, FallbackProvider)
    assert [m.model for m in provider.members] == ["gemini-3.8-flash", "gemini-3.5-flash"]


def test_a_single_model_does_not_build_a_chain(monkeypatch):
    import dataclasses

    from centralign.config import SETTINGS
    from centralign.llm.registry import build_provider

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    settings = dataclasses.replace(
        SETTINGS, provider="gemini", model="gemini-3.8-flash", llm_cache=False
    )
    assert not isinstance(build_provider(settings), FallbackProvider)
