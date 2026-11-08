"""Dollar budget: pre-call estimates, post-call actuals, a ledger of every call.

Usage: `budget = Budget(max_usd=5.0)`; engine entry points take `budget=`. Before every API call the LLM wrapper calls
`reserve()` with the worst-case estimate (input chars/4 tokens + `max_tokens` of output) and raises `BudgetExceeded`
if `spent + reserved + estimate > max_usd`. After the call `record()` stores the actual usage and cost. Reservations make
the check safe under concurrency. Unpriced models (see `llm.PRICE_PER_MTOK`) cost 0 and are never gated.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .llm import Usage, estimate_cost


class BudgetExceeded(RuntimeError):
    def __init__(self, message: str, *, estimate_usd: float = 0.0, remaining_usd: float = 0.0):
        super().__init__(message)
        self.estimate_usd, self.remaining_usd = estimate_usd, remaining_usd


@dataclass
class LedgerEntry:
    purpose: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Budget:
    max_usd: float = math.inf
    spent_usd: float = 0.0
    ledger: list[LedgerEntry] = field(default_factory=list)
    reserved_usd: float = 0.0  # in-flight worst-case estimates

    @classmethod
    def unlimited(cls) -> "Budget":
        return cls(max_usd=math.inf)

    def remaining(self) -> float:
        """Dollars still available, net of in-flight reservations (never negative)."""
        return max(0.0, self.max_usd - self.spent_usd - self.reserved_usd)

    @staticmethod
    def estimate(model: str, input_chars: int, max_tokens: int) -> float:
        return estimate_cost(model, input_chars, max_tokens)

    def can_afford(self, usd: float) -> bool:
        return self.spent_usd + self.reserved_usd + usd <= self.max_usd + 1e-12

    def reserve(self, model: str, input_chars: int, max_tokens: int, purpose: str = "") -> float:
        est = self.estimate(model, input_chars, max_tokens)
        if not self.can_afford(est):
            raise BudgetExceeded(
                f"{purpose or 'call'} on {model}: worst-case estimate ${est:.4f} exceeds remaining budget "
                f"${self.remaining():.4f} (max ${self.max_usd:.2f})", estimate_usd=est, remaining_usd=self.remaining())
        self.reserved_usd += est
        return est

    def release(self, reserved: float) -> None:
        self.reserved_usd = max(0.0, self.reserved_usd - reserved)

    def record(self, purpose: str, model: str, usage: Usage) -> float:
        cost = usage.estimated_cost(model) or 0.0
        self.spent_usd += cost
        self.ledger.append(LedgerEntry(purpose, model, usage.input_tokens, usage.output_tokens,
                                       usage.cache_read_tokens, usage.cache_write_tokens, cost))
        return cost

    def by_purpose(self) -> list[dict]:
        agg: dict[tuple[str, str], dict] = {}
        for e in self.ledger:
            a = agg.setdefault((e.purpose, e.model), {"purpose": e.purpose, "model": e.model, "calls": 0,
                                                      "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
            a["calls"] += 1
            a["input_tokens"] += e.input_tokens + e.cache_read_tokens + e.cache_write_tokens
            a["output_tokens"] += e.output_tokens
            a["cost_usd"] = round(a["cost_usd"] + e.cost_usd, 6)
        return sorted(agg.values(), key=lambda a: (a["purpose"], a["model"]))

    def summary(self) -> dict:
        return {"max_usd": None if math.isinf(self.max_usd) else self.max_usd, "spent_usd": round(self.spent_usd, 6),
                "remaining_usd": None if math.isinf(self.max_usd) else round(self.remaining(), 6),
                "calls": len(self.ledger), "by_purpose": self.by_purpose()}
