# Susquehanna Predictions Cup Bot

An automated trading bot for the **SIG Predictions Cup 2026**, a play-money prediction-market contest on the
2026 U.S. midterm elections, run on [sig.thesuper.market](https://sig.thesuper.market). Each participant starts
with 100,000 SUSQies, and the three highest balances after every market settles win cash prizes.

The bot treats **Kalshi's public order book as fair value**. It trades only where Super Market players
misprice a race, controls risk across correlated races, and holds an optional conviction basket for
top-3 upside.

> Bots are permitted by the official rules (one account per person, respect the API rate limits).
> SUSQies are non-monetary contest currency. Nothing here is investment advice.

---

## How it makes money

| Strategy | Idea | When |
|---|---|---|
| **Value** | Buy YES or NO when the Super Market ask is at least `min_edge` (plus half the Kalshi spread) below fair. Sized with fractional Kelly. | Always |
| **Maker** | In thin books (e.g. 0.015 bid / 0.93 ask), rest a bid at `fair - edge` to catch other players' market orders. Quotes expire automatically. | Always |
| **Near-certain** | Once Kalshi prices a race at 97% or more (it has been called), buy it on Super Market before the 4-hourly settlement. | Election night |
| **Basket** | Patiently accumulate a correlated set of underdogs (a thesis) and hold them to settlement for top-3 upside. Never sold by the bot. | Optional |

Every decision is expressed as a **buy** of the YES or NO side (buying NO at *p* equals selling YES at *1 - p*),
so one code path handles both directions.

### Why the leaderboard is misleading

The site values open positions at a lagging "valuation price" that can sit far from the market. For example,
a contract trading at 0.045 was valued at 0.31. Buying long shots at 1–5¢ therefore shows 5–10x paper gains.
Prizes use the balance **after settlement**, where those long shots pay 0. `python run.py report` shows equity
at fair value next to the site's number.

---

## Risk management

- **Per-market cap**: `value.max_market_exposure`.
- **Per-race cap**: `risk.max_race_exposure`. YES-Dem and NO-Rep in the same race count as one bet.
- **National-direction cap**: `risk.max_net_party_frac`. All races share one national environment, so net
  Dem-minus-Rep money at risk is capped as a share of equity.
- **Resting orders count**: unfilled orders count toward every cap, so partial fills can't stack.
- **Equity at fair value**: sizing and the drawdown kill switch use Kalshi fair values, not the site's valuation.
- **Kill switches**: it stops opening positions at a 35% drawdown from peak, or when a `HALT` file exists.
- **Circuit breaker**: it skips the cycle if fewer than 50% of markets have a fair value.
- **Two-cycle confirmation**: a fair value must be stable across two cycles before it trades on it.
- **Stale-order cancel**: resting orders are cancelled when fair value moves against them.
- **Settlement-rule guard**: Georgia is excluded, because a runoff could settle differently from Kalshi.
- **Rate limits**: at most 85 reads and 25 writes per minute (the account limit is 100 and 30).
- **Dry-run by default**: nothing is sent without `--live`.

---

## Market tracker and analytics

Every cycle stores Super Market bid/ask/last and Kalshi bid/ask for every market in `logs/bot.db` (SQLite),
together with every order and fill. It reuses data the cycle already fetched, so there are no extra API calls.

`python run.py analyze` reports:

- **long-shot bias** by probability bucket (do players overpay for 5–15% underdogs?);
- how often a tradable edge (≥ `min_edge`) appears;
- **mean reversion**: persistence and half-life of mispricings;
- **lead-lag**: how much of a Kalshi move Super Market makes in the next cycle;
- the markets with the most persistent mispricings.

`python run.py report` shows each position's price paid, site price, fair value and expected P&L, plus net
direction exposure and the **edge captured** on logged fills.

---

## Setup

Requires Python 3.10+.

```bash
git clone https://github.com/jemankalita/Susquehanna-Prediction-Cup.git
cd Susquehanna-Prediction-Cup
python -m pip install -r requirements.txt
cp .env.example .env          # Windows: copy .env.example .env
```

1. Create an account at sig.thesuper.market, confirm your email, and accept the Terms.
2. Profile → **API Keys** → create a key with scopes **read + trade**, and paste it into `.env` as `SM_API_KEY=...`.
3. Verify, then dry-run before going live:

```bash
python run.py check        # username and balance
python run.py fairs        # edge / fair / bid / ask for every market
python run.py once         # one dry-run cycle (logs what it would trade)
python run.py run          # dry-run loop
python run.py run --live   # real orders
```

Keep the machine awake while it runs, especially on election night.

## Commands

| Command | Description |
|---|---|
| `check` | verify API key, tournament, balance |
| `fairs` | Super Market vs Kalshi edge table |
| `report` | positions at fair value, exposure, edge captured |
| `analyze` | tracker statistics |
| `once` | single dry-run cycle |
| `run [--live]` | trading loop (60s; 20s on election night) |
| `cancel-all` | cancel every resting order |
| `leaderboard` | top 20 |

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `value.min_edge` | minimum mispricing vs fair before taking (0.04 = 4 points) |
| `value.spread_edge_mult` | extra edge required per unit of Kalshi spread |
| `value.kelly_fraction` | Kelly fraction (0.5 = half-Kelly) |
| `value.max_market_exposure` | max SUSQies at risk per market |
| `risk.max_race_exposure` | max SUSQies per race and direction |
| `risk.max_net_party_frac` | max net Dem-minus-Rep exposure, as a share of equity |
| `risk.cash_reserve_frac` | cash kept free (released on election night) |
| `risk.max_drawdown_frac` | kill-switch threshold |
| `near_certain.*` | election-night settings |
| `maker.*` | passive quoting settings |
| `basket.legs` | conviction positions: `market_id`, `side`, `target_cost`, `max_price` |
| `scope.exclude_states` / `hold_market_ids` | markets to skip / never sell |
| `fair_overrides` / `kalshi_overrides` | manual fair values / ticker mappings |

## Architecture

```
run.py                  CLI entry point
bot/
  engine.py             one trading cycle: data -> fair values -> tracker -> risk gates -> orders
  strategy.py           pure decision logic (value / maker / near-certain / basket, Kelly sizing)
  risk.py               per-race and national-direction exposure caps (immutable)
  markets.py            market title -> Kalshi ticker mapping
  kalshi.py             public Kalshi quotes (fair-value source)
  sm_client.py          Super Market API client: rate limiter, retries, idempotency keys
  store.py              SQLite: snapshots, orders, fills
  analytics.py          bias, reversion, lead-lag, edge-captured statistics
  journal.py            human-readable CSV journal
  config.py             typed settings from config.json + .env
tests/                  unit tests (python -m pytest -q)
```

## Testing

```bash
python -m pytest -q
```

The tests cover ticker mapping, YES/NO price conversion, Kelly sizing, budget and cap enforcement, resting-order
accounting, near-certain activation, maker quoting, basket accumulation, risk aggregation, analytics and the
SQLite store.

## Limits

- Super Market prices track Kalshi closely, so day-to-day edges are small. The largest expected gains come from
  catching sloppy market orders and from election night.
- Kalshi is assumed to be the sharp price. Where a Super Market contract's settlement wording differs from
  Kalshi's (runoffs, ranked-choice counts), the fair value can be wrong.
- No strategy guarantees a top-3 finish.

## License

MIT
