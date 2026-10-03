"""Shared state for the sandbox systems.

The mock ERP and the mock internal API are separate services that must agree on
the same vendor master, purchase orders and ledger -- exactly as two real
enterprise systems share a database. A single JSON file under ``var/`` with
file locking is enough at this scale, and it has a property a real database
would not: ``reset()`` restores a byte-identical world, so a demo or a test run
is reproducible.

It also hosts the **chaos controls**. Injecting a transient 503 or a slow
response through an endpoint -- rather than with a random number generator --
means failure-recovery behaviour can be demonstrated on cue and asserted in
tests, instead of being hoped for.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = REPO_ROOT / "var" / "sandbox_state.json"


class SandboxStore:
    """JSON-file-backed store, safe for concurrent readers in one process."""

    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.reset()

    # -- persistence --------------------------------------------------------
    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.reset()
                return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, state: dict[str, Any]) -> None:
        with self._lock:
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
            temp.replace(self.path)

    def reset(self) -> dict[str, Any]:
        from .seed import seed_state

        state = seed_state()
        self.save(state)
        return state

    # -- convenience accessors ---------------------------------------------
    def vendors(self) -> list[dict[str, Any]]:
        return self.load()["vendors"]

    def vendor_by_name(self, name: str) -> dict[str, Any] | None:
        target = _norm(name)
        for vendor in self.vendors():
            if _norm(vendor["name"]) == target or target in _norm(vendor["name"]):
                return vendor
        return None

    def purchase_orders(self) -> list[dict[str, Any]]:
        return self.load()["purchase_orders"]

    def po_by_number(self, number: str) -> dict[str, Any] | None:
        target = _norm(number)
        return next((po for po in self.purchase_orders() if _norm(po["number"]) == target), None)

    def invoices(self) -> list[dict[str, Any]]:
        return self.load()["invoices"]

    def invoice_by_number(self, number: str) -> dict[str, Any] | None:
        target = _norm(number)
        return next((i for i in self.invoices() if _norm(i["number"]) == target), None)

    def invoice_by_id(self, invoice_id: str) -> dict[str, Any] | None:
        return next((i for i in self.invoices() if i["id"] == invoice_id), None)

    def user(self, username: str) -> dict[str, Any] | None:
        return next(
            (u for u in self.load()["users"] if _norm(u["username"]) == _norm(username)), None
        )

    # -- mutation -----------------------------------------------------------
    def mutate(self, fn) -> Any:
        """Read-modify-write under the lock. ``fn(state)`` returns the result."""
        with self._lock:
            state = self.load()
            result = fn(state)
            self.save(state)
            return result

    def audit(self, action: str, detail: dict[str, Any]) -> None:
        def apply(state: dict[str, Any]) -> None:
            state.setdefault("audit", []).append({"action": action, **detail})

        self.mutate(apply)

    # -- chaos controls -----------------------------------------------------
    def chaos(self) -> dict[str, Any]:
        return self.load().get("chaos", {})

    def set_chaos(self, config: dict[str, Any]) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            state["chaos"] = {**state.get("chaos", {}), **config}
            return state["chaos"]

        return self.mutate(apply)

    def consume_fault(self, key: str) -> str | None:
        """Pop one queued fault for ``key``, if any.

        Faults are consumed rather than persistent so that a retry *succeeds* --
        which is the behaviour worth demonstrating. A permanently failing
        endpoint only shows that we give up.
        """

        def apply(state: dict[str, Any]) -> str | None:
            faults: dict[str, list[str]] = state.setdefault("chaos", {}).setdefault("faults", {})
            queue = faults.get(key) or []
            if not queue:
                return None
            fault = queue.pop(0)
            faults[key] = queue
            return fault

        return self.mutate(apply)

    def queue_fault(self, key: str, fault: str, times: int = 1) -> None:
        def apply(state: dict[str, Any]) -> None:
            faults = state.setdefault("chaos", {}).setdefault("faults", {})
            faults.setdefault(key, []).extend([fault] * times)

        self.mutate(apply)


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


STORE = SandboxStore()
