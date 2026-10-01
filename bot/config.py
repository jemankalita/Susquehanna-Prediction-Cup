"""Loads settings from config.json and the API key from .env / environment."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SM_BASE_URL = "https://sig.thesuper.market/api/v1"
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
TICK = 0.005
MIN_PRICE = 0.005
MAX_PRICE = 0.995


@dataclass(frozen=True)
class BasketLeg:
    """A conviction position the bot accumulates patiently and holds to settlement."""
    market_id: str
    side: str            # "yes" / "no"
    target_cost: float   # SUSQies to put in
    max_price: float     # hard ceiling per share


@dataclass(frozen=True)
class Settings:
    api_key: str
    tournament_slug: str
    loop_seconds: int
    election_night_loop_seconds: int
    # value trading: take quotes that are mispriced vs the Kalshi fair value
    min_edge: float
    max_fair_spread: float
    spread_edge_mult: float
    kelly_fraction: float
    max_market_exposure: float
    max_total_exposure_frac: float
    cash_reserve_frac: float
    max_drawdown_frac: float
    max_race_exposure: float
    max_net_party_frac: float
    max_order_qty: int
    # near-certain outcomes (called races on election night)
    near_certain_fair: float
    near_certain_max_price: float
    near_certain_max_exposure: float
    near_certain_active_from_utc: str
    # passive quotes inside wide spreads
    maker_enabled: bool
    maker_min_spread: float
    maker_edge: float
    maker_qty: int
    maker_ttl_minutes: int
    # scope / safety
    exclude_states: tuple[str, ...]
    exclude_market_ids: tuple[str, ...]
    hold_market_ids: tuple[str, ...]
    basket_legs: tuple[BasketLeg, ...] = ()
    basket_premium: float = 0.01        # max we pay above Kalshi fair while accumulating
    basket_chunk_cost: float = 3000.0   # max SUSQies per leg per cycle
    basket_ttl_minutes: int = 10
    kalshi_overrides: dict[str, str] = field(default_factory=dict)
    fair_overrides: dict[str, float] = field(default_factory=dict)
    stop_trading_utc: str = "2026-11-04T16:55:00Z"


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_settings(config_path: Path | None = None, require_key: bool = True) -> Settings:
    config_path = config_path or ROOT / "config.json"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    env = _read_dotenv(ROOT / ".env")
    api_key = os.environ.get("SM_API_KEY") or env.get("SM_API_KEY", "")
    if require_key and not api_key:
        raise SystemExit("SM_API_KEY missing: put it in predictions-cup-bot/.env (see .env.example)")

    v, nc, mk, risk, scope = raw["value"], raw["near_certain"], raw["maker"], raw["risk"], raw["scope"]
    return Settings(
        api_key=api_key,
        tournament_slug=raw["tournament_slug"],
        loop_seconds=int(raw["loop_seconds"]),
        election_night_loop_seconds=int(raw["election_night_loop_seconds"]),
        min_edge=float(v["min_edge"]),
        max_fair_spread=float(v["max_fair_spread"]),
        spread_edge_mult=float(v["spread_edge_mult"]),
        kelly_fraction=float(v["kelly_fraction"]),
        max_market_exposure=float(v["max_market_exposure"]),
        max_total_exposure_frac=float(risk["max_total_exposure_frac"]),
        cash_reserve_frac=float(risk["cash_reserve_frac"]),
        max_drawdown_frac=float(risk["max_drawdown_frac"]),
        max_race_exposure=float(risk["max_race_exposure"]),
        max_net_party_frac=float(risk["max_net_party_frac"]),
        max_order_qty=int(risk["max_order_qty"]),
        near_certain_fair=float(nc["min_fair"]),
        near_certain_max_price=float(nc["max_price"]),
        near_certain_max_exposure=float(nc["max_market_exposure"]),
        near_certain_active_from_utc=nc["active_from_utc"],
        maker_enabled=bool(mk["enabled"]),
        maker_min_spread=float(mk["min_spread"]),
        maker_edge=float(mk["edge"]),
        maker_qty=int(mk["qty"]),
        maker_ttl_minutes=int(mk["ttl_minutes"]),
        exclude_states=tuple(scope["exclude_states"]),
        exclude_market_ids=tuple(str(x) for x in scope["exclude_market_ids"]),
        hold_market_ids=tuple(str(x) for x in scope["hold_market_ids"]),
        **_basket(raw.get("basket", {})),
        kalshi_overrides={str(k): str(t) for k, t in raw.get("kalshi_overrides", {}).items()},
        fair_overrides={str(k): float(p) for k, p in raw.get("fair_overrides", {}).items()},
        stop_trading_utc=raw.get("stop_trading_utc", "2026-11-04T16:55:00Z"),
    )


def _basket(raw: dict) -> dict:
    if not raw.get("enabled"):
        return {}
    legs = tuple(BasketLeg(str(l["market_id"]), l["side"], float(l["target_cost"]), float(l["max_price"]))
                 for l in raw.get("legs", []))
    for leg in legs:
        if leg.side not in ("yes", "no") or not 0 < leg.max_price < 1 or leg.target_cost <= 0:
            raise SystemExit(f"invalid basket leg: {leg}")
    return {
        "basket_legs": legs,
        "basket_premium": float(raw.get("premium", 0.01)),
        "basket_chunk_cost": float(raw.get("chunk_cost", 3000)),
        "basket_ttl_minutes": int(raw.get("ttl_minutes", 10)),
    }
