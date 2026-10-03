"""Central configuration, loaded from environment with sane defaults.

Everything tunable about a run lives here so that the runtime never reads
``os.environ`` directly -- that keeps tests hermetic and makes the budget
ceilings auditable from one place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(REPO_ROOT / ".env")


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Budget:
    """Hard ceilings. The kernel aborts rather than burn a free-tier quota."""

    max_steps: int = 60
    max_llm_calls: int = 120
    max_consecutive_failures: int = 3
    max_replans: int = 4
    max_plan_cycles: int = 8
    wall_clock_seconds: int = 900


@dataclass(frozen=True)
class Settings:
    provider: str = "gemini"
    model: str = "gemini-3.8-flash"
    llm_cache: bool = True
    headless: bool = True

    erp_url: str = "http://127.0.0.1:8781"
    api_url: str = "http://127.0.0.1:8782"

    #: A publicly reachable instance must not let a passing visitor spend the
    #: owner's model quota -- the free tier is 20 requests per day per model, so
    #: one curious click could exhaust a demo for everyone. In public mode the
    #: dashboard still shows every past run, its evidence and its full decision
    #: stream; only *starting* a new run needs the token.
    public_demo: bool = False
    demo_token: str = ""

    company_dir: Path = field(default_factory=lambda: REPO_ROOT / "company")
    drive_dir: Path = field(default_factory=lambda: REPO_ROOT / "sandbox" / "drive")
    var_dir: Path = field(default_factory=lambda: REPO_ROOT / "var")
    artifacts_dir: Path = field(default_factory=lambda: REPO_ROOT / "artifacts")

    budget: Budget = field(default_factory=Budget)

    @property
    def cassette_dir(self) -> Path:
        return REPO_ROOT / "tests" / "cassettes"

    @property
    def event_db(self) -> Path:
        return self.var_dir / "events.sqlite3"

    @property
    def memory_db(self) -> Path:
        return self.var_dir / "memory.sqlite3"

    def ensure_dirs(self) -> None:
        for d in (self.var_dir, self.artifacts_dir, self.cassette_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    return Settings(
        provider=os.getenv("OPERATOR_LLM_PROVIDER", "gemini").strip().lower(),
        model=os.getenv("OPERATOR_LLM_MODEL", "gemini-3.8-flash").strip(),
        llm_cache=_flag("OPERATOR_LLM_CACHE", True),
        headless=_flag("OPERATOR_HEADLESS", True),
        erp_url=os.getenv("OPERATOR_SANDBOX_ERP", "http://127.0.0.1:8781").rstrip("/"),
        api_url=os.getenv("OPERATOR_SANDBOX_API", "http://127.0.0.1:8782").rstrip("/"),
        public_demo=_flag("OPERATOR_PUBLIC_DEMO", False),
        demo_token=os.getenv("OPERATOR_DEMO_TOKEN", "").strip(),
        budget=Budget(
            max_steps=_int("OPERATOR_MAX_STEPS", 60),
            max_llm_calls=_int("OPERATOR_MAX_LLM_CALLS", 120),
            max_plan_cycles=_int("OPERATOR_MAX_PLAN_CYCLES", 8),
            wall_clock_seconds=_int("OPERATOR_WALL_CLOCK_SECONDS", 900),
        ),
    )


SETTINGS = load_settings()
