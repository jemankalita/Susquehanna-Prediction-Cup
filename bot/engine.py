"""One trading cycle: gather data -> fair values -> tracker snapshot -> risk gates -> orders."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import journal, risk
from .config import ROOT, Settings
from .kalshi import Quote, fetch_quotes
from .markets import Race, kalshi_candidates, parse_title
from .sm_client import ApiError, SuperMarketClient
from .store import Store
from .strategy import MarketView, OrderIntent, derive_missing_fairs, plan_basket_leg, plan_market

log = logging.getLogger(__name__)

HALT_FILE = ROOT / "HALT"                 # create this file to stop all new orders
STATE_FILE = ROOT / "logs" / "state.json"
MARKET_REFRESH_S = 30 * 60
MAX_ORDERS_PER_CYCLE = 15                  # stays under the 30 writes/min budget
MIN_FAIR_COVERAGE = 0.5                    # circuit breaker: need fairs for half the mapped markets
FAIR_CONFIRM_TOLERANCE = 0.03              # fair must be stable across two cycles before we trade on it
STALE_MARGIN_NEAR_CERTAIN = 0.0            # cancel a near-certain bid only once it is above fair
FILLS_PAGE = 100
EQUITY_BASIS = "kalshi_fair"           # what peak_equity in state.json is measured in


@dataclass(frozen=True)
class Listing:
    market_id: str
    exchange_id: str
    title: str
    race: Race | None


@dataclass(frozen=True)
class FairBook:
    fair: dict[str, float | None]          # exchange_id -> fair YES probability
    quote: dict[str, Quote]                # exchange_id -> raw Kalshi quote (when mapped directly)
    coverage: float


@dataclass(frozen=True)
class Snapshot:
    cash: float
    positions: dict[str, dict]
    open_orders: list[dict]
    prices: dict[str, dict]

    @property
    def site_value(self) -> float:
        return sum(float(p.get("marketValue") or 0.0) for p in self.positions.values())

    @property
    def cost_basis(self) -> float:
        return sum(abs(float(p.get("costBasis") or 0.0)) for p in self.positions.values())

    def fair_value(self, fairs: dict[str, float | None]) -> float:
        """Positions valued at Kalshi fair (site valuation prices lag badly); site value as fallback."""
        total = 0.0
        for ex, p in self.positions.items():
            qty, fair_yes = float(p.get("quantity") or 0.0), fairs.get(ex)
            if fair_yes is None:
                total += float(p.get("marketValue") or 0.0)
            else:
                total += qty * fair_yes if qty > 0 else -qty * (1 - fair_yes)
        return total


@dataclass
class EngineState:
    tournament_id: str = ""
    listings: list[Listing] = field(default_factory=list)
    listings_loaded_at: float = 0.0
    prev_fairs: dict[str, float] = field(default_factory=dict)
    peak_equity: float = 0.0
    store: Store = field(default_factory=Store)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def loop_seconds(s: Settings) -> int:
    return s.election_night_loop_seconds if _night(s) else s.loop_seconds


# ---------------------------------------------------------------- data
def load_listings(client: SuperMarketClient, slug: str) -> list[Listing]:
    listings = []
    for m in client.tournament_markets(slug):
        yes = next((e for e in m.get("exchanges", []) if (e.get("option") or "YES").upper() == "YES"), None)
        if yes is not None:
            listings.append(Listing(str(m["id"]), str(yes["id"]), m["title"], parse_title(m["title"])))
    return listings


def compute_fairs(listings: list[Listing], s: Settings) -> FairBook:
    candidates = {
        l.exchange_id: ([s.kalshi_overrides[l.market_id]] if l.market_id in s.kalshi_overrides
                        else kalshi_candidates(l.race) if l.race else [])
        for l in listings
    }
    quotes = fetch_quotes([t for ts in candidates.values() for t in ts])
    direct = {ex: next((quotes[t] for t in ts if t in quotes), None) for ex, ts in candidates.items()}
    fairs = {ex: (q.mid if q and q.spread <= s.max_fair_spread else None) for ex, q in direct.items()}

    races: dict[str, list[str]] = {}
    for l in listings:
        if l.race:
            races.setdefault(l.race.race_key, []).append(l.exchange_id)
    fairs = derive_missing_fairs(fairs, races)
    fairs.update({l.exchange_id: s.fair_overrides[l.market_id] for l in listings if l.market_id in s.fair_overrides})

    mapped = [ex for ex, ts in candidates.items() if ts]
    coverage = sum(1 for ex in mapped if fairs.get(ex) is not None) / len(mapped) if mapped else 0.0
    return FairBook(fairs, {ex: q for ex, q in direct.items() if q}, coverage)


def take_snapshot(client: SuperMarketClient, s: Settings, state: EngineState) -> Snapshot:
    tournament = client.tournament(s.tournament_slug)
    state.tournament_id = tournament["id"]
    prices = client.prices([l.exchange_id for l in state.listings], state.tournament_id)
    positions = {str(p["exchangeId"]): p for p in client.positions(s.tournament_slug) if not p.get("settled")}
    return Snapshot(float(tournament.get("myBalance") or 0.0), positions,
                    client.open_orders(state.tournament_id), prices)


def record_tracker(state: EngineState, book: FairBook, snap: Snapshot) -> None:
    ts = journal.utc_now_iso()
    rows = []
    for l in state.listings:
        q, k = snap.prices.get(l.exchange_id, {}), book.quote.get(l.exchange_id)
        rows.append((ts, l.exchange_id, l.market_id, l.title, q.get("bestBid"), q.get("bestAsk"),
                     q.get("latestPrice"), k.bid if k else None, k.ask if k else None, book.fair.get(l.exchange_id)))
    state.store.save_snapshots(rows)


def sync_fills(client: SuperMarketClient, s: Settings, state: EngineState, book: FairBook) -> None:
    fills = [f for f in client.recent_fills(s.tournament_slug, FILLS_PAGE)
             if all(f.get(k) is not None for k in ("id", "exchangeId", "side", "price", "quantity"))]
    new = state.store.save_fills(fills, book.fair)
    if new:
        log.info("Recorded %d new fills", new)


# ---------------------------------------------------------------- cycle
def run_cycle(client: SuperMarketClient, s: Settings, state: EngineState, live: bool) -> None:
    if utc_now() >= parse_utc(s.stop_trading_utc):
        log.info("Past stop_trading_utc; not trading.")
        return
    if time.time() - state.listings_loaded_at > MARKET_REFRESH_S or not state.listings:
        state.listings = load_listings(client, s.tournament_slug)
        state.listings_loaded_at = time.time()

    book = compute_fairs(state.listings, s)
    if book.coverage < MIN_FAIR_COVERAGE:
        log.warning("Circuit breaker: fair coverage %.0f%% < %.0f%%, skipping cycle",
                    book.coverage * 100, MIN_FAIR_COVERAGE * 100)
        return
    snap = take_snapshot(client, s, state)
    try:
        record_tracker(state, book, snap)
        sync_fills(client, s, state, book)
    except (ApiError, sqlite3.Error, KeyError, ValueError, TypeError) as exc:
        log.warning("Tracker/fill sync skipped this cycle: %s", exc)

    equity = snap.cash + snap.fair_value(book.fair)
    state.peak_equity = max(state.peak_equity, equity)
    log.info("cash=%.0f at_risk=%.0f fair_equity=%.0f site_equity=%.0f coverage=%.0f%%",
             snap.cash, snap.cost_basis, equity, snap.cash + snap.site_value, book.coverage * 100)

    basket_ex = _basket_exchanges(s, state)
    cancelled = cancel_stale_orders(client, s, state, [o for o in snap.open_orders
                                                       if str(o["exchangeId"]) not in basket_ex], book.fair, live)
    halted = halt_reason(s, state, equity)
    if halted:
        log.warning("Not opening new positions: %s", halted)
    else:
        plans = plan_all(s, state, book, snap, cancelled, equity)
        execute_all(client, s, state, snap, plans, cancelled, equity, live)
    state.prev_fairs = {k: v for k, v in book.fair.items() if v is not None}
    save_state(state, equity, snap.cash)


def _pending_by_side(open_orders: list[dict], cancelled: set[str]) -> dict[tuple[str, str], float]:
    pending: dict[tuple[str, str], float] = {}
    for o in open_orders:
        ex = str(o["exchangeId"])
        if ex in cancelled or o.get("action") != "buy":
            continue
        key = (ex, o.get("side", "yes"))
        pending[key] = pending.get(key, 0.0) + float(o.get("quantity") or 0)
    return pending


def _night(s: Settings) -> bool:
    return utc_now() >= parse_utc(s.near_certain_active_from_utc)


def _committed(snap: Snapshot, cancelled: set[str]) -> float:
    return sum(float(o.get("quantity") or 0) * float(o.get("priceLimit") or 0)
               for o in snap.open_orders if str(o["exchangeId"]) not in cancelled)


def _basket_exchanges(s: Settings, state: EngineState) -> set[str]:
    legs = {leg.market_id for leg in s.basket_legs}
    return {l.exchange_id for l in state.listings if l.market_id in legs}


def _leg_held_cost(snap: Snapshot, exchange_id: str, side: str) -> float:
    p = snap.positions.get(exchange_id)
    if not p:
        return 0.0
    qty = float(p.get("quantity") or 0)
    same_side = qty != 0 and (qty > 0) == (side == "yes")
    return abs(float(p.get("costBasis") or 0)) if same_side else 0.0


def basket_remaining(s: Settings, state: EngineState, snap: Snapshot) -> float:
    """SUSQies still to be invested in the basket (reserved away from the trading strategies)."""
    by_market = {l.market_id: l.exchange_id for l in state.listings}
    return sum(max(leg.target_cost - _leg_held_cost(snap, by_market.get(leg.market_id, ""), leg.side), 0.0)
               for leg in s.basket_legs)


def _budget(s: Settings, snap: Snapshot, cancelled: set[str], equity: float, reserved: float = 0.0) -> float:
    """Cash the trading strategies may use. The cash reserve is released on election night."""
    reserve = 0.0 if _night(s) else s.cash_reserve_frac * equity
    exposure_room = max(s.max_total_exposure_frac * equity - snap.cost_basis, 0.0)
    return min(snap.cash - reserve, exposure_room) - _committed(snap, cancelled) - reserved


def _basket_budget(s: Settings, snap: Snapshot, cancelled: set[str], equity: float) -> float:
    return snap.cash - s.cash_reserve_frac * equity - _committed(snap, cancelled)


def plan_all(s: Settings, state: EngineState, book: FairBook, snap: Snapshot,
             cancelled: set[str], equity: float) -> list[tuple[Listing, OrderIntent]]:
    budget = _budget(s, snap, cancelled, equity, basket_remaining(s, state, snap))
    night = _night(s)
    open_ex = {str(o["exchangeId"]) for o in snap.open_orders}
    pending = _pending_by_side(snap.open_orders, cancelled)
    basket_ex = _basket_exchanges(s, state)
    plans: list[tuple[Listing, OrderIntent]] = plan_basket(s, state, book, snap, cancelled, equity, open_ex)
    for l in state.listings:
        fair = book.fair.get(l.exchange_id)
        if l.exchange_id in basket_ex or _excluded(l, s) or not _stable(l.exchange_id, fair, state.prev_fairs):
            continue
        q, k = snap.prices.get(l.exchange_id, {}), book.quote.get(l.exchange_id)
        view = MarketView(
            market_id=l.market_id, exchange_id=l.exchange_id, title=l.title, fair=fair,
            best_bid=q.get("bestBid"), best_ask=q.get("bestAsk"),
            position=float(snap.positions.get(l.exchange_id, {}).get("quantity") or 0.0),
            fair_spread=k.spread if k else s.max_fair_spread,
            open_yes=pending.get((l.exchange_id, "yes"), 0.0),
            open_no=pending.get((l.exchange_id, "no"), 0.0),
            has_open_order=l.exchange_id in open_ex and l.exchange_id not in cancelled,
            hold=l.market_id in s.hold_market_ids,
        )
        plans.extend((l, i) for i in plan_market(view, s, equity, budget, night))
    # Basket first, then takers, then quotes; highest edge first within each group.
    order = {"basket": 0, "near_certain": 1, "value": 1, "maker": 2}
    return sorted(plans, key=lambda p: (order.get(p[1].kind, 3), -(p[1].fair - p[1].price)))


def plan_basket(s: Settings, state: EngineState, book: FairBook, snap: Snapshot, cancelled: set[str],
                equity: float, open_ex: set[str]) -> list[tuple[Listing, OrderIntent]]:
    by_market = {l.market_id: l for l in state.listings}
    budget = _basket_budget(s, snap, cancelled, equity)
    plans = []
    for leg in s.basket_legs:
        l = by_market.get(leg.market_id)
        if l is None or budget <= 0:
            continue
        q = snap.prices.get(l.exchange_id, {})
        view = MarketView(market_id=l.market_id, exchange_id=l.exchange_id, title=l.title,
                          fair=book.fair.get(l.exchange_id), best_bid=q.get("bestBid"), best_ask=q.get("bestAsk"),
                          has_open_order=l.exchange_id in open_ex and l.exchange_id not in cancelled)
        intent = plan_basket_leg(view, leg.side, leg.target_cost, leg.max_price,
                                 _leg_held_cost(snap, l.exchange_id, leg.side), s.basket_premium,
                                 s.basket_chunk_cost, s.basket_ttl_minutes, budget)
        if intent:
            plans.append((l, intent))
            budget -= intent.cost
    return plans


def execute_all(client: SuperMarketClient, s: Settings, state: EngineState, snap: Snapshot,
                plans: list[tuple[Listing, OrderIntent]], cancelled: set[str], equity: float, live: bool) -> None:
    """Place orders in priority order, shrinking each to the race / national-direction caps."""
    races = {l.exchange_id: l.race for l in state.listings}
    basket_ex = _basket_exchanges(s, state)
    held = [(races.get(ex), float(p.get("quantity") or 0), float(p.get("costBasis") or 0))
            for ex, p in snap.positions.items() if ex not in basket_ex]
    resting = [(races.get(str(o["exchangeId"])),
                float(o.get("quantity") or 0) * (1 if o.get("side") == "yes" else -1),
                float(o.get("quantity") or 0) * float(o.get("priceLimit") or 0))
               for o in snap.open_orders
               if str(o["exchangeId"]) not in (cancelled | basket_ex) and o.get("action") == "buy"]
    exposure = risk.build_exposure(held + resting)
    budget = _budget(s, snap, cancelled, equity, basket_remaining(s, state, snap))
    basket_budget = _basket_budget(s, snap, cancelled, equity)
    max_net = s.max_net_party_frac * equity
    placed = 0
    for listing, intent in plans:
        if placed >= MAX_ORDERS_PER_CYCLE:
            break
        if intent.kind == "basket":  # deliberate conviction risk: own budget, no race/net caps
            if intent.cost <= basket_budget and execute(client, state, listing, intent, live):
                basket_budget -= intent.cost
                placed += 1
            continue
        allowed = min(risk.allowed_cost(exposure, listing.race, intent.side, intent.cost,
                                        s.max_race_exposure, max_net), budget)
        qty = int(allowed / intent.price)
        if qty < 1:
            continue
        sized = OrderIntent(intent.exchange_id, intent.side, qty, intent.price, intent.kind,
                            intent.fair, intent.ttl_minutes)
        if execute(client, state, listing, sized, live):
            exposure = risk.add(exposure, listing.race, sized.side, sized.cost)
            budget -= sized.cost
            placed += 1


# ---------------------------------------------------------------- gates
def _excluded(l: Listing, s: Settings) -> bool:
    return l.market_id in s.exclude_market_ids or bool(l.race and l.race.state in s.exclude_states)


def _stable(exchange_id: str, fair: float | None, prev: dict[str, float]) -> bool:
    return fair is not None and exchange_id in prev and abs(prev[exchange_id] - fair) <= FAIR_CONFIRM_TOLERANCE


def halt_reason(s: Settings, state: EngineState, equity: float) -> str | None:
    if HALT_FILE.exists():
        return f"HALT file present ({HALT_FILE})"
    floor = state.peak_equity * (1 - s.max_drawdown_frac)
    if state.peak_equity and equity < floor:
        return f"drawdown kill switch: equity {equity:.0f} < {floor:.0f}"
    return None


def cancel_stale_orders(client, s: Settings, state: EngineState, open_orders, fairs, live) -> set[str]:
    """Cancel our resting buys whose price is no longer safely below fair. Returns exchanges cleared."""
    stale: set[str] = set()
    for o in open_orders:
        ex = str(o["exchangeId"])
        fair_yes, price = fairs.get(ex), o.get("priceLimit")
        if fair_yes is None or price is None or o.get("action") != "buy":
            continue
        side_fair = fair_yes if o.get("side") == "yes" else 1 - fair_yes
        margin = STALE_MARGIN_NEAR_CERTAIN if side_fair >= s.near_certain_fair else s.maker_edge / 2
        if price > side_fair - margin:
            stale.add(ex)
    cancelled: set[str] = set()
    for ex in stale:
        log.info("Cancelling stale orders on exchange %s", ex)
        if not live:
            cancelled.add(ex)
            continue
        try:
            client.cancel_all(state.tournament_id, ex)
            cancelled.add(ex)
        except ApiError as exc:
            log.error("cancel-all failed on %s: %s", ex, exc)
    return cancelled


# ---------------------------------------------------------------- execution / state
def execute(client: SuperMarketClient, state: EngineState, l: Listing, intent: OrderIntent, live: bool) -> bool:
    row = {
        "mode": "live" if live else "dry", "kind": intent.kind, "market_id": l.market_id,
        "exchange_id": l.exchange_id, "title": l.title, "side": intent.side, "price": intent.price,
        "qty": intent.quantity, "fair": round(intent.fair, 4), "edge": round(intent.fair - intent.price, 4),
    }
    log.info("%s %s BUY %s %d @ %.3f (fair %.3f) %s", row["mode"].upper(), intent.kind, intent.side,
             intent.quantity, intent.price, intent.fair, l.title)
    if not live:
        _record(state, {**row, "result": "dry-run"})
        return True
    expires = None
    if intent.ttl_minutes:
        expires = (utc_now() + timedelta(minutes=intent.ttl_minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    try:
        resp = client.place_order(l.exchange_id, intent.side, "buy", intent.quantity, intent.price,
                                  state.tournament_id, expires)
        _record(state, {**row, "result": f"ok traded={resp.get('quantityTraded')} open={resp.get('open')}"})
        return True
    except ApiError as exc:
        log.error("Order failed: %s", exc)
        _record(state, {**row, "result": f"error {exc.code}"})
        return False


def _record(state: EngineState, row: dict) -> None:
    journal.record(row)
    state.store.save_order({**row, "ts": journal.utc_now_iso()})


def save_state(state: EngineState, equity: float, cash: float) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "updated_utc": journal.utc_now_iso(), "equity": round(equity, 2), "cash": round(cash, 2),
        "peak_equity": round(state.peak_equity, 2), "equity_basis": EQUITY_BASIS,
    }, indent=2), encoding="utf-8")


def load_peak_equity() -> float:
    try:
        saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        # A peak measured on a different basis (old site-valuation equity) would mis-fire the kill switch.
        return float(saved.get("peak_equity", 0.0)) if saved.get("equity_basis") == EQUITY_BASIS else 0.0
    except (OSError, ValueError):
        return 0.0
