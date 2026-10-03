import pytest

from bot import analytics, risk
from bot.markets import parse_title
from bot.store import Store

DEM_AK = parse_title("Will the Democratic Party win the Alaska Senate?")
REP_AK = parse_title("Will the Republican Party win the Alaska Senate?")
DEM_KS = parse_title("Will the Democratic Party win the Kansas Senate?")
IND_NE = parse_title("Will the Independent Party win the Nebraska Senate?")


# ---------- risk ----------
def test_yes_dem_and_no_rep_are_the_same_bet():
    assert risk.direction(DEM_AK, "yes") == risk.direction(REP_AK, "no") == "D"
    assert risk.direction(DEM_AK, "no") == "R"
    assert risk.direction(IND_NE, "yes") is None


def test_build_exposure_aggregates_race_and_net():
    exp = risk.build_exposure([(DEM_AK, 1000, 600.0), (REP_AK, -1000, 700.0), (DEM_KS, -500, 350.0)])
    assert exp.by_race[(DEM_AK.race_key, "D")] == pytest.approx(1300)
    assert exp.net_dem == pytest.approx(1300 - 350)


def test_race_cap_clips_trade():
    exp = risk.build_exposure([(DEM_AK, 1000, 14_000.0)])
    assert risk.allowed_cost(exp, REP_AK, "no", 5_000, max_race=15_000, max_net=1e9) == pytest.approx(1_000)


def test_net_cap_blocks_adding_but_allows_reducing():
    exp = risk.Exposure({}, net_dem=40_000)
    assert risk.allowed_cost(exp, DEM_KS, "yes", 10_000, max_race=1e9, max_net=45_000) == pytest.approx(5_000)
    assert risk.allowed_cost(exp, DEM_KS, "no", 10_000, max_race=1e9, max_net=45_000) == pytest.approx(10_000)


def test_add_is_immutable():
    exp = risk.Exposure()
    new = risk.add(exp, DEM_AK, "yes", 100)
    assert exp.net_dem == 0 and new.net_dem == 100


# ---------- analytics ----------
def _snaps(devs, fair=0.5, ex="1"):
    return [analytics.Snap(f"2026-10-02T00:{i:02d}:00Z", ex, "t", fair + d - 0.005, fair + d + 0.005, fair)
            for i, d in enumerate(devs)]


def test_bias_bucket_detects_longshot_overpricing():
    snaps = _snaps([0.03] * 20, fair=0.05)
    bucket = {name: mean for name, _, mean in analytics.bias_by_bucket(snaps)}
    assert bucket["0%-10%"] == pytest.approx(0.03)


def test_reversion_half_life():
    devs, d = [], 0.08
    for _ in range(30):
        devs.append(d)
        d *= 0.5
    beta, half = analytics.reversion(_snaps(devs))
    assert beta == pytest.approx(0.5, abs=0.01) and half == pytest.approx(1.0, abs=0.05)


def test_opportunity_rate():
    snaps = _snaps([0.0] * 9 + [-0.06])
    assert analytics.opportunity_rate(snaps, 0.04) == pytest.approx(0.1)


def test_edge_captured():
    edge, notional = analytics.edge_captured([(0.90, 100, 0.95), (0.40, 50, None)])
    assert edge == pytest.approx(5.0) and notional == pytest.approx(110.0)


# ---------- store ----------
def test_store_dedupes_fills(tmp_path):
    store = Store(tmp_path / "t.db")
    fill = {"id": 1, "orderId": 9, "exchangeId": "10", "marketId": "5", "side": "no",
            "price": 0.6, "quantity": -100, "filledAt": "2026-10-02T00:00:00Z"}
    assert store.save_fills([fill], {"10": 0.3}) == 1
    assert store.save_fills([fill], {"10": 0.3}) == 0
    assert store.query("SELECT fair_at_sync, qty FROM fills") == [(pytest.approx(0.7), 100.0)]


def test_old_peak_on_different_basis_is_ignored(tmp_path, monkeypatch):
    import json
    from bot import engine
    f = tmp_path / "state.json"
    monkeypatch.setattr(engine, "STATE_FILE", f)
    f.write_text(json.dumps({"peak_equity": 150000}))
    assert engine.load_peak_equity() == 0.0
    f.write_text(json.dumps({"peak_equity": 150000, "equity_basis": engine.EQUITY_BASIS}))
    assert engine.load_peak_equity() == 150000


# ---------- unwind ----------
def test_unwind_plans_opposite_buy_at_fair_minus_giveup():
    from bot.unwind import plan_unwind
    positions = [{"exchangeId": "1", "quantity": -1000, "costBasis": 950, "marketTitle": "R RI"},   # hold NO
                 {"exchangeId": "2", "quantity": 500, "costBasis": 400, "marketTitle": "D MN"},     # hold YES
                 {"exchangeId": "3", "quantity": 100, "costBasis": 10, "marketTitle": "basket"}]
    closes = {c.exchange_id: c for c in plan_unwind(positions, {"1": 0.01, "2": 0.875, "3": 0.1}, {"3"}, 0.02)}
    assert set(closes) == {"1", "2"}
    no_close = closes["1"]                      # NO fair 0.99 -> sell >= 0.97 -> buy YES <= 0.03
    assert no_close.buy_side == "yes" and no_close.limit == pytest.approx(0.03) and no_close.quantity == 1000
    yes_close = closes["2"]                     # YES fair 0.875 -> sell >= 0.855 -> buy NO <= 0.145
    assert yes_close.buy_side == "no" and yes_close.limit == pytest.approx(0.145)
    assert yes_close.min_receive >= 0.855 - 1e-9
