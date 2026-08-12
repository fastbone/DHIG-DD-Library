"""Token accounting.

Every API call in the app funnels its ``usage`` object through :func:`record`
so the UI can show what an ingest sweep or a question actually cost. Prices are
US$ per million tokens, list rates.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute TTL
CACHE_WRITE_MULTIPLIER_1H = 2.0
CACHE_READ_MULTIPLIER = 0.1


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    cost_usd: float = 0.0
    by_model: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "calls": self.calls,
            "cost_usd": round(self.cost_usd, 4),
            "by_model": {k: round(v, 4) for k, v in self.by_model.items()},
        }


def cost_of(model: str, usage, *, cache_ttl_1h: bool = False) -> float:
    """Cost in USD for one response's usage object."""
    price_in, price_out = PRICES.get(model, (5.0, 25.0))
    fresh_in = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    write_mult = CACHE_WRITE_MULTIPLIER_1H if cache_ttl_1h else CACHE_WRITE_MULTIPLIER
    return (
        fresh_in * price_in
        + cache_read * price_in * CACHE_READ_MULTIPLIER
        + cache_write * price_in * write_mult
        + out * price_out
    ) / 1_000_000


class Meter:
    """Thread-safe running total, plus a global lifetime total."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.usage = Usage()

    def record(self, model: str, usage, *, cache_ttl_1h: bool = False) -> float:
        cost = cost_of(model, usage, cache_ttl_1h=cache_ttl_1h)
        with self._lock:
            u = self.usage
            u.calls += 1
            u.input_tokens += getattr(usage, "input_tokens", 0) or 0
            u.output_tokens += getattr(usage, "output_tokens", 0) or 0
            u.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
            u.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
            u.cost_usd += cost
            u.by_model[model] = u.by_model.get(model, 0.0) + cost
        return cost

    def snapshot(self) -> dict:
        with self._lock:
            return self.usage.as_dict()

    def reset(self) -> None:
        with self._lock:
            self.usage = Usage()


lifetime = Meter()


# --- attribution ---------------------------------------------------------
#
# Who is spending, passed explicitly. A context variable would remove the
# argument from three signatures, but the spending happens inside async
# generators, and a contextvar set across a `yield` belongs to whichever context
# resumes the generator rather than to the generator itself. Mis-attributed spend
# is a silent wrong number in someone's budget, so the boring option wins.

@dataclass(frozen=True)
class Attribution:
    username: str | None
    budget: str            # "ask" | "index"
    kind: str              # analyst | verifier | carder
    ref: str | None = None

    def as_kind(self, kind: str) -> "Attribution":
        """Same payer, different label — the verifier inside an analyst's turn."""
        return Attribution(self.username, self.budget, kind, self.ref)


# Set at startup to append to the ledger. A hook rather than importing db here,
# so this module stays free of app dependencies and safe to import anywhere.
sink: Callable[["Attribution", str, float, dict], None] | None = None


def _as_dict(usage) -> dict:
    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


def record(
    model: str,
    usage,
    *,
    meter: Meter | None = None,
    cache_ttl_1h: bool = False,
    attribution: "Attribution | None" = None,
) -> float:
    """Record usage against the lifetime meter, a scoped one, and the ledger.

    With no attribution the call is still metered but not charged to anyone —
    which is what should happen to work nobody initiated.
    """
    cost = lifetime.record(model, usage, cache_ttl_1h=cache_ttl_1h)
    if meter is not None:
        meter.record(model, usage, cache_ttl_1h=cache_ttl_1h)
    if attribution is not None and sink is not None:
        try:
            sink(attribution, model, cost, _as_dict(usage))
        except Exception:  # noqa: BLE001 — accounting must not fail the request
            pass
    return cost
