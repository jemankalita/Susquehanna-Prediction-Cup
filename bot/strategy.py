"""Pure trading logic: given a market view, decide which orders to place.

Every decision is expressed as a BUY of either the YES or the NO side, so the
same code handles both directions (buying NO at p == selling YES at 1-p).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import MAX_PRICE, MIN_PRICE, TICK, Settings

TAKER_TTL_MINUTES = 2  # unfilled remainder of a take order should not linger


@dataclass(frozen=True)
class MarketView:
    market_id: str
    exchange_id: str
    title: str
    fair: float | None          # probability of YES
    best_bid: float | None      # YES bid on Super Market
    best_ask: float | None      # YES ask on Super Market
    position: float = 0.0       # +YES shares / -NO shares
    fair_spread: float = 0.0    # Kalshi bid-ask spread: wider = less certain fair
    open_yes: float = 0.0       # unfilled resting YES buy quantity (counts toward caps)
    open_no: float = 0.0        # unfilled resting NO buy quantity
    has_open_order: bool = False
    hold: bool = False          # never trade against this position (conviction basket)


@dataclass(frozen=True)
class OrderIntent:
    exchange_id: str
    side: str                   # "yes" / "no"
    quantity: int
    price: float                # price of the side being bought
    kind: str                   # "value" / "near_certain" / "maker"
    fair: float                 # fair value of the side being bought
    ttl_minutes: int | None = None

    @property
    def cost(self) -> float:
        return self.quantity * self.price


def floor_tick(p: float) -> float:
    return round(max(MIN_PRICE, min(MAX_PRICE, math.floor(p / TICK + 1e-9) * TICK)), 3)


def kelly_fraction(fair: float, price: float) -> float:
    """Optimal bankroll fraction for a binary contract bought at `price` with win prob `fair`."""
    if price >= 1 or fair <= price:
        return 0.0
    return (fair - price) / (1 - price)


def _side_views(view: MarketView) -> list[tuple[str, float, float | None, float | None, float]]:
    """(side, fair, ask, bid, held_qty) for YES and NO, each in that side's own price terms."""
    yes_ask, yes_bid = view.best_ask, view.best_bid
    no_ask = None if yes_bid is None else 1 - yes_bid
    no_bid = None if yes_ask is None else 1 - yes_ask
    return [
        ("yes", view.fair, yes_ask, yes_bid, max(view.position, 0.0) + view.open_yes),
        ("no", 1 - view.fair, no_ask, no_bid, max(-view.position, 0.0) + view.open_no),
    ]


def plan_market(view: MarketView, s: Settings, equity: float, budget: float,
                near_certain_active: bool = False) -> list[OrderIntent]:
    """Orders for one market. `budget` = cash we may still spend this cycle."""
    if view.fair is None or budget <= 0 or equity <= 0:
        return []
    intents: list[OrderIntent] = []
    for side, fair, ask, bid, held in _side_views(view):
        opposite_held = view.position < 0 if side == "yes" else view.position > 0
        if view.hold and opposite_held:
            continue  # buying this side would unwind a held conviction position
        taker = _plan_take(view.exchange_id, side, fair, ask, held, s, equity, budget, near_certain_active,
                           view.fair_spread)
        if taker:
            intents.append(taker)
            budget -= taker.cost
            continue
        maker = _plan_quote(view, side, fair, ask, bid, held, s, budget)
        if maker:
            intents.append(maker)
            budget -= maker.cost
    return intents


def _plan_take(exchange_id, side, fair, ask, held, s: Settings, equity, budget,
               near_certain_active, fair_spread=0.0) -> OrderIntent | None:
    if ask is None:
        return None
    near_certain = near_certain_active and fair >= s.near_certain_fair
    if near_certain:
        limit = floor_tick(min(s.near_certain_max_price, fair - 0.01))
        target = s.near_certain_max_exposure
        kind = "near_certain"
    else:
        limit = floor_tick(fair - (s.min_edge + s.spread_edge_mult * fair_spread))
        target = min(s.max_market_exposure, s.kelly_fraction * kelly_fraction(fair, limit) * equity)
        kind = "value"
    if ask > limit:
        return None
    room = target - held * limit
    qty = min(int(min(room, budget) / limit), s.max_order_qty)
    if qty <= 0:
        return None
    return OrderIntent(exchange_id, side, qty, limit, kind, fair, ttl_minutes=TAKER_TTL_MINUTES)


def _plan_quote(view: MarketView, side, fair, ask, bid, held, s: Settings, budget) -> OrderIntent | None:
    if not s.maker_enabled or view.has_open_order or fair >= s.near_certain_fair:
        return None
    spread = (ask if ask is not None else 1.0) - (bid if bid is not None else 0.0)
    if spread < s.maker_min_spread:
        return None
    price = floor_tick(fair - s.maker_edge)
    if price <= (bid or 0.0) or (ask is not None and price >= ask) or price <= MIN_PRICE:
        return None  # would not improve the bid, or would cross the ask
    room = min(budget, s.max_market_exposure - held * price)
    qty = min(s.maker_qty, int(room / price))
    if qty <= 0:
        return None
    return OrderIntent(view.exchange_id, side, qty, price, "maker", fair, ttl_minutes=s.maker_ttl_minutes)


def derive_missing_fairs(fairs: dict[str, float | None],
                         race_members: dict[str, list[str]]) -> dict[str, float | None]:
    """Fill a missing fair in a two-market race from the other side (they sum to ~1).

    Races with three or more listed parties are skipped: 1 - D - R would hand a real
    independent a fake near-zero fair. `race_members`: race_key -> exchange ids.
    """
    result = dict(fairs)
    for members in race_members.values():
        missing = [m for m in members if result.get(m) is None]
        if len(members) != 2 or len(missing) != 1:
            continue
        rest = sum(result[m] for m in members if m not in missing)
        result[missing[0]] = round(min(max(1 - rest, 0.0), 1.0), 4)
    return result


def plan_basket_leg(view: MarketView, leg_side: str, target_cost: float, max_price: float,
                    held_cost: float, premium: float, chunk_cost: float, ttl_minutes: int,
                    budget: float) -> OrderIntent | None:
    """Accumulate a conviction leg: lift asks up to the limit, otherwise improve the bid by one tick.

    The limit is min(max_price, fair + premium): we accept paying about fair for the
    variance, never chase far above it.
    """
    if view.has_open_order:
        return None
    side_fair = None if view.fair is None else (view.fair if leg_side == "yes" else 1 - view.fair)
    limit = floor_tick(max_price if side_fair is None else min(max_price, side_fair + premium))
    ask = view.best_ask if leg_side == "yes" else (None if view.best_bid is None else 1 - view.best_bid)
    bid = view.best_bid if leg_side == "yes" else (None if view.best_ask is None else 1 - view.best_ask)
    if ask is not None and ask <= limit:
        price = limit                                   # sweep every ask up to the limit
    else:
        price = floor_tick(min(limit, (bid or 0.0) + TICK))
        if ask is not None and price >= ask:
            return None
    spend = min(target_cost - held_cost, chunk_cost, budget)
    qty = int(spend / price) if price > 0 else 0
    if qty <= 0:
        return None
    fair_for_log = side_fair if side_fair is not None else price
    return OrderIntent(view.exchange_id, leg_side, qty, price, "basket", fair_for_log, ttl_minutes=ttl_minutes)
