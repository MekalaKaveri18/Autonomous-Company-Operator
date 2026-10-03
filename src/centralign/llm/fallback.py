"""Model fallback chain.

Free tiers are congested, and congestion is not an error the agent can reason
its way out of: a 503 "this model is experiencing high demand" says nothing about
the task. Retrying the same overloaded model with a longer sleep converts a
capacity problem into a wall-clock problem, and on a free tier the sleep often
loses.

So the model is a *chain*, not a single endpoint. Set

    OPERATOR_LLM_MODEL=gemini-3.8-flash,gemini-3.5-flash,gemini-flash-latest

and the chain tries each in order, moving on only when one is genuinely
unavailable. A bad request is **not** a reason to fall back -- an invalid schema
would fail identically everywhere, and silently retrying it on three models would
hide the bug and triple the cost.

Two details that matter:

**Stickiness.** Once a model answers, the chain prefers it for the rest of the
run. Re-probing a known-overloaded primary before every call would add its
timeout to every step.

**Stable cache identity.** The chain owns the cache and keys it on the chain's
logical name, not on whichever model happened to answer. Otherwise a run that
fell back midway would produce cassettes under two different model identities and
would not replay deterministically.
"""

from __future__ import annotations

from typing import Sequence

from .base import (
    CallCache,
    LLMError,
    LLMProvider,
    LLMResponse,
    Message,
    RateLimited,
    TransientError,
)

#: Attempts spent on each model before moving to the next.
ATTEMPTS_PER_MODEL = 3


class FallbackProvider(LLMProvider):
    def __init__(
        self,
        members: Sequence[LLMProvider],
        *,
        name: str,
        cache: CallCache | None = None,
    ) -> None:
        if not members:
            raise LLMError("a fallback chain needs at least one provider")
        # The chain's identity is the primary's, so cache keys stay stable even
        # when a later member actually serves the call.
        super().__init__(members[0].model, cache)
        self.name = name
        self.members = list(members)
        #: Index of the member that last answered; tried first next time.
        self._preferred = 0
        self.fallbacks_used: list[str] = []
        #: Models known to be out of quota for the day; skipped on later calls so
        #: an exhausted primary does not cost a round trip on every single step.
        self.exhausted: set[str] = set()

    @property
    def supports_schema(self) -> bool:  # type: ignore[override]
        # Only true if every member can enforce a schema server-side; otherwise
        # the weakest member needs the schema in its prompt.
        return all(member.supports_schema for member in self.members)

    @property
    def active_model(self) -> str:
        return self.members[self._preferred].model

    def _order(self) -> list[int]:
        """Preferred member first, then the rest, skipping exhausted models."""
        order = [self._preferred] + [i for i in range(len(self.members)) if i != self._preferred]
        live = [i for i in order if self.members[i].model not in self.exhausted]
        # If everything is marked exhausted, try them all again anyway: a daily
        # quota may have reset, and refusing to try at all is worse than a 429.
        return live or order

    async def _invoke(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        schema: dict | None,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        unavailable: list[str] = []

        for index in self._order():
            member = self.members[index]
            try:
                response = await member.complete(
                    system=system,
                    messages=messages,
                    schema=schema,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    max_attempts=ATTEMPTS_PER_MODEL,
                )
            except (RateLimited, TransientError, LLMError) as exc:
                if not _is_capacity_problem(exc):
                    # A real error: bad schema, bad request, refusal. Falling
                    # back would fail the same way and hide the cause.
                    raise
                if _is_quota_exhausted(exc):
                    self.exhausted.add(member.model)
                unavailable.append(f"{member.model}: {_brief(exc)}")
                continue

            if index != self._preferred:
                self.fallbacks_used.append(f"{self.members[self._preferred].model} -> {member.model}")
                self._preferred = index
            return response

        raise LLMError(
            "every model in the chain is unavailable: " + "; ".join(unavailable)
        )

    async def aclose(self) -> None:
        for member in self.members:
            await member.aclose()


def _causes(exc: BaseException, limit: int = 6) -> list[BaseException]:
    """The exception and its ``__cause__`` chain.

    A member provider that exhausts its own retries wraps the real reason in an
    LLMError, so the chain has to look past the outermost exception. Checking only
    the top-level type meant a model that 503'd three times was treated as a hard
    error and the chain never fell through -- which defeats the whole point.
    """
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < limit:
        chain.append(current)
        current = current.__cause__
    return chain


def _is_quota_exhausted(exc: Exception) -> bool:
    return any(
        isinstance(cause, RateLimited) and cause.exhausted for cause in _causes(exc)
    ) or "perday" in str(exc).lower().replace("_", "")


def _is_capacity_problem(exc: Exception) -> bool:
    """Is this 'come back later' rather than 'you asked wrongly'?"""
    if any(isinstance(cause, (RateLimited, TransientError)) for cause in _causes(exc)):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "503",
            "429",
            "overload",
            "high demand",
            "unavailable",
            "quota",
            "resource_exhausted",
            "rate limit",
            "timed out",
            "timeout",
            "no longer available",   # a retired model: try the next one
            "not_found",
            "404",
        )
    )


def _brief(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return text[:200]
