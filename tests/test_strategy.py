from dataclasses import replace

import pytest

from bot.config import load_settings
from bot.kalshi import parse_markets
from bot.markets import kalshi_candidates, parse_title
from bot.strategy import MarketView, derive_missing_fairs, floor_tick, kelly_fraction, plan_market


@pytest.fixture
def s():
    return load_settings(require_key=False)


def view(**kw):
    base = dict(market_id="1", exchange_id="10", title="t", fair=0.5, best_bid=0.49, best_ask=0.51)
    return MarketView(**{**base, **kw})


# ---------- title parsing / ticker mapping ----------
@pytest.mark.parametrize("title,expected", [
    ("Will the Democratic Party win the Kansas Senate?", "SENATEKS-26-D"),
    ("Will the Republican Party win the Florida Senate?", "SENATEFLS-26-R"),
    ("Will the Democratic Party win the PA-07 House race?", "HOUSEPA7-26-D"),
    ("Will the Republican Party win the Arizona Governor?", "GOVPARTYAZ-26-R"),
    ("Will the Democratic Party win the U.S. House?", "CONTROLH-2026-D"),
    ("Will the Republican Party win the U.S. Senate?", "CONTROLS-2026-R"),
])
def test_title_maps_to_kalshi_ticker_first(title, expected):
    assert kalshi_candidates(parse_title(title))[0] == expected


def test_independent_and_unknown_titles_are_not_mapped():
    assert kalshi_candidates(parse_title("Will the Independent Party win the Nebraska Senate?")) == []
    assert parse_title("Will it rain tomorrow?") is None


def test_parse_kalshi_payload_skips_bad_quotes():
    payload = {"markets": [
        {"ticker": "A", "yes_bid_dollars": "0.30", "yes_ask_dollars": "0.31"},
        {"ticker": "B", "yes_bid_dollars": "0.00", "yes_ask_dollars": "0.00"},
        {"ticker": "C", "yes_bid_dollars": None, "yes_ask_dollars": "0.5"},
    ]}
    quotes = parse_markets(payload)
    assert set(quotes) == {"A"} and quotes["A"].mid == pytest.approx(0.305)


# ---------- math helpers ----------
def test_floor_tick_rounds_down_and_clamps():
    assert floor_tick(0.4349) == 0.43
    assert floor_tick(0.0001) == 0.005
    assert floor_tick(1.2) == 0.995


def test_kelly_zero_without_edge():
    assert kelly_fraction(0.5, 0.6) == 0.0
    assert kelly_fraction(0.6, 0.5) == pytest.approx(0.2)


def test_derive_missing_fair_from_other_party():
    fairs = derive_missing_fairs({"d": 0.3, "r": None, "x": None}, {"race": ["d", "r"], "solo": ["x"]})
    assert fairs["r"] == pytest.approx(0.7) and fairs["x"] is None


# ---------- trading decisions ----------
def test_no_take_when_market_matches_fair(s):
    assert [i for i in plan_market(view(), s, 100_000, 50_000) if i.kind != "maker"] == []


def test_buys_yes_when_ask_well_below_fair(s):
    intents = plan_market(view(fair=0.60, best_bid=0.40, best_ask=0.50), s, 100_000, 50_000)
    take = [i for i in intents if i.kind == "value"]
    assert take and take[0].side == "yes" and 0.50 <= take[0].price <= 0.56


def test_buys_no_when_bid_well_above_fair(s):
    intents = plan_market(view(fair=0.70, best_bid=0.96, best_ask=0.97), s, 100_000, 50_000)
    take = [i for i in intents if i.kind == "value"]
    assert take and take[0].side == "no"
    assert take[0].price <= 1 - 0.70 - s.min_edge + 1e-9  # pays at most fair(NO) - edge


def test_respects_budget_and_market_cap(s):
    small = plan_market(view(fair=0.60, best_bid=0.10, best_ask=0.20), s, 1_000_000, 500)
    assert all(i.cost <= 500 for i in small)
    big = plan_market(view(fair=0.60, best_bid=0.10, best_ask=0.20), s, 10_000_000, 10_000_000)
    value = [i for i in big if i.kind == "value"]
    assert value and value[0].cost <= s.max_market_exposure + 1


def test_hold_position_is_not_unwound(s):
    held = view(fair=0.30, best_bid=0.60, best_ask=0.61, position=1000, hold=True)
    assert all(i.side != "no" for i in plan_market(held, s, 100_000, 50_000))


def test_near_certain_only_after_activation(s):
    called = view(fair=0.995, best_bid=0.94, best_ask=0.95)
    before = plan_market(called, s, 100_000, 90_000, near_certain_active=False)
    after = plan_market(called, s, 100_000, 90_000, near_certain_active=True)
    assert any(i.kind == "near_certain" for i in after)
    assert not any(i.kind == "near_certain" for i in before)


def test_maker_quote_improves_bid_without_crossing(s):
    wide = view(fair=0.91, best_bid=0.015, best_ask=0.93)
    quotes = [i for i in plan_market(wide, s, 100_000, 50_000) if i.kind == "maker" and i.side == "yes"]
    assert quotes and 0.015 < quotes[0].price < 0.93 and quotes[0].price <= 0.91 - s.maker_edge


def test_maker_disabled(s):
    s2 = replace(s, maker_enabled=False)
    wide = view(fair=0.91, best_bid=0.015, best_ask=0.93)
    assert not [i for i in plan_market(wide, s2, 100_000, 50_000) if i.kind == "maker"]


def test_maker_respects_market_cap(s):
    capped = view(fair=0.91, best_bid=0.015, best_ask=0.93, position=s.max_market_exposure / 0.88 + 10)
    assert not [i for i in plan_market(capped, s, 1_000_000, 500_000) if i.kind == "maker" and i.side == "yes"]


def test_no_derived_fair_in_three_way_race():
    fairs = derive_missing_fairs({"d": 0.01, "r": 0.72, "i": None}, {"race": ["d", "r", "i"]})
    assert fairs["i"] is None


def test_resting_orders_count_toward_market_cap(s):
    cheap = view(fair=0.60, best_bid=0.10, best_ask=0.20)
    full = view(fair=0.60, best_bid=0.10, best_ask=0.20, open_yes=s.max_market_exposure / 0.5 + 10)
    assert [i for i in plan_market(cheap, s, 10_000_000, 10_000_000) if i.kind == "value"]
    assert not [i for i in plan_market(full, s, 10_000_000, 10_000_000) if i.kind == "value"]


# ---------- basket ----------
from bot.strategy import plan_basket_leg


def _leg(v, held=0.0, budget=50_000, target=15_000, max_price=0.11):
    return plan_basket_leg(v, "yes", target, max_price, held, 0.01, 3000, 10, budget)


def test_basket_lifts_asks_up_to_fair_plus_premium():
    i = _leg(view(fair=0.0855, best_bid=0.09, best_ask=0.09))
    assert i and i.kind == "basket" and i.price == 0.095 and i.cost <= 3000


def test_basket_improves_bid_when_ask_too_high():
    i = _leg(view(fair=0.0855, best_bid=0.085, best_ask=0.12))
    assert i and i.price == 0.09


def test_basket_never_exceeds_max_price_or_target():
    assert _leg(view(fair=0.30, best_bid=0.29, best_ask=0.30), max_price=0.11).price <= 0.11
    assert _leg(view(fair=0.0855, best_bid=0.09, best_ask=0.09), held=15_000) is None


def test_basket_waits_for_its_open_order():
    assert _leg(view(fair=0.0855, best_bid=0.09, best_ask=0.09, has_open_order=True)) is None


def test_basket_no_side_uses_complement_prices():
    v = view(fair=0.915, best_bid=0.88, best_ask=0.885)   # NO ask = 0.12, NO bid = 0.115
    i = plan_basket_leg(v, "no", 10_000, 0.11, 0.0, 0.01, 3000, 10, 50_000)
    assert i and i.side == "no" and i.price <= 0.095
