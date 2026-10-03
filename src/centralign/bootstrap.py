"""Assembly point: build a configured operator from settings.

One function both the CLI and the web dashboard call, so there is no chance of
the two running subtly different systems -- a different policy file, a stale
memory index -- and reaching different conclusions about the same run.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import SETTINGS, Settings
from .events.log import EventLog
from .llm.base import LLMProvider
from .llm.registry import build_provider
from .memory.store import MemoryStore
from .policy.guard import PolicyGuard
from .runtime.kernel import Kernel, build_registry
from .tools.registry import ToolRegistry


@dataclass
class Operator:
    settings: Settings
    llm: LLMProvider
    registry: ToolRegistry
    memory: MemoryStore
    policy: PolicyGuard
    events: EventLog
    kernel: Kernel
    documents_loaded: int

    async def aclose(self) -> None:
        await self.llm.aclose()
        self.memory.close()
        self.events.close()


class UnavailableProvider(LLMProvider):
    """Stands in when no provider could be built.

    Inspection commands (`tools`, `doctor`) must work before a key is configured,
    otherwise the first thing a new user sees is a crash. Any attempt to actually
    reason through this provider fails with the original configuration error.
    """

    name = "unavailable"

    def __init__(self, reason: str) -> None:
        super().__init__("unavailable", cache=None)
        self.reason = reason

    async def _invoke(self, **_: object):
        from .llm.base import LLMError

        raise LLMError(f"no LLM provider is configured: {self.reason}")


def build_operator(
    settings: Settings | None = None, *, reindex: bool = True, require_llm: bool = True
) -> Operator:
    settings = settings or SETTINGS
    settings.ensure_dirs()

    events = EventLog(settings.event_db)
    memory = MemoryStore(settings.memory_db)
    # Re-ingested on every start: company documents are edited by people, and a
    # stale index is a silent behaviour change that is very hard to debug.
    loaded = memory.ingest_company_dir(settings.company_dir) if reindex else 0

    policy = PolicyGuard.from_file(settings.company_dir / "policies.yaml")
    registry = build_registry(settings)
    try:
        llm = build_provider(settings)
    except Exception as exc:  # noqa: BLE001 - surfaced by `doctor`, not swallowed
        if require_llm:
            raise
        llm = UnavailableProvider(str(exc))

    kernel = Kernel(
        settings=settings,
        llm=llm,
        registry=registry,
        memory=memory,
        policy=policy,
        events=events,
    )
    return Operator(
        settings=settings,
        llm=llm,
        registry=registry,
        memory=memory,
        policy=policy,
        events=events,
        kernel=kernel,
        documents_loaded=loaded,
    )
