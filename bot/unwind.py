"""Free cash by closing positions: buy the opposite side, which the exchange nets into cash.

Holding n shares of side S and buying n shares of the other side pays back 1 SUSQie per pair,
so buying the opposite side at price p is the same as selling S at 1 - p. We only accept
selling at or above fair - giveup.
"""
from __future__ import annotations

from dataclasses import dataclass

from .strategy import floor_tick


@dataclass(frozen=True)
class Close:
    exchange_id: str
    title: str
    held_side: str       # side we hold
    buy_side: str        # opposite side we buy to net out
    quantity: int
    limit: float         # max price for the opposite side
    min_receive: float   # worst per-share price we accept for the side held
    cost_basis: float

    @property
    def min_cash(self) -> float:
        return self.quantity * self.min_receive


def plan_unwind(positions: list[dict], fairs: dict[str, float | None], keep_exchanges: set[str],
                giveup: float) -> list[Close]:
    """One close per open position that isn't kept, has a fair value, and isn't settled."""
    closes = []
    for p in positions:
        ex, qty = str(p["exchangeId"]), float(p.get("quantity") or 0)
        fair_yes = fairs.get(ex)
        if ex in keep_exchanges or p.get("settled") or qty == 0 or fair_yes is None:
            continue
        held = "yes" if qty > 0 else "no"
        side_fair = fair_yes if held == "yes" else 1 - fair_yes
        min_receive = max(side_fair - giveup, 0.0)
        limit = floor_tick(1 - min_receive)
        closes.append(Close(ex, p.get("marketTitle", ""), held, "no" if held == "yes" else "yes",
                            int(abs(qty)), limit, round(1 - limit, 4), abs(float(p.get("costBasis") or 0))))
    return sorted(closes, key=lambda c: -c.min_cash)
