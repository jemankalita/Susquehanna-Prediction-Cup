"""Predictions Cup bot CLI.

    python run.py check            verify API key, tournament, balance
    python run.py fairs            Super Market vs Kalshi fair value for every market (no orders)
    python run.py report           positions valued at fair vs site, risk by direction, edge captured
    python run.py analyze          tracker stats: how SM players misprice vs Kalshi
    python run.py once             one dry-run cycle (logs what it WOULD trade)
    python run.py run              loop in dry-run mode
    python run.py run --live       loop and place real orders
    python run.py cancel-all       cancel every resting order
    python run.py leaderboard      top of the tournament leaderboard
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from bot import analytics, risk
from bot.config import ROOT, load_settings
from bot.engine import (EngineState, compute_fairs, load_listings, load_peak_equity, loop_seconds,
                        run_cycle)
from bot.sm_client import ApiError, SuperMarketClient
from bot.store import Store

LOG_PATH = ROOT / "logs" / "bot.log"


def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_PATH, encoding="utf-8")],
    )


def _fmt(v: float | None) -> str:
    return f"{v:6.3f}" if v is not None else "     -"


def cmd_check(client: SuperMarketClient, slug: str) -> None:
    acct = client.account()
    t = client.tournament(slug)
    print(f"account: {acct.get('username')}  tournament: {t['name']} ({t['status']})")
    print(f"balance: {t.get('myBalance')}  members: {t.get('memberCount')}  markets: {t.get('marketCount')}")


def cmd_fairs(client: SuperMarketClient, s) -> None:
    t = client.tournament(s.tournament_slug)
    listings = load_listings(client, s.tournament_slug)
    book = compute_fairs(listings, s)
    prices = client.prices([l.exchange_id for l in listings], t["id"])
    rows = []
    for l in listings:
        fair, q = book.fair.get(l.exchange_id), prices.get(l.exchange_id, {})
        bid, ask = q.get("bestBid"), q.get("bestAsk")
        edge = 0.0
        if fair is not None:
            edge = max((fair - ask) if ask is not None else 0.0, (bid - fair) if bid is not None else 0.0)
        rows.append((edge, fair, bid, ask, l.title))
    print(f"fair-value coverage: {book.coverage:.0%}\n")
    print(f"{'edge':>6} {'fair':>6} {'bid':>6} {'ask':>6}  market")
    for edge, fair, bid, ask, title in sorted(rows, key=lambda r: -r[0]):
        print(f"{edge:6.3f} {_fmt(fair)} {_fmt(bid)} {_fmt(ask)}  {title}")


def cmd_report(client: SuperMarketClient, s) -> None:
    t = client.tournament(s.tournament_slug)
    listings = {l.exchange_id: l for l in load_listings(client, s.tournament_slug)}
    book = compute_fairs(list(listings.values()), s)
    positions = [p for p in client.positions(s.tournament_slug) if not p.get("settled")]
    cash = float(t["myBalance"])
    tot_cost = tot_site = tot_fair = 0.0
    print(f"{'shares':>8} {'side':>4} {'paid':>6} {'site':>6} {'fair':>6} {'E[pnl]':>8}  market")
    for p in sorted(positions, key=lambda p: -abs(p["costBasis"])):
        qty, ex = float(p["quantity"]), str(p["exchangeId"])
        n, side = abs(qty), ("yes" if qty > 0 else "no")
        fy = book.fair.get(ex)
        fair = None if fy is None else (fy if side == "yes" else 1 - fy)
        cost, site = abs(float(p["costBasis"])), float(p["marketValue"])
        fval = n * fair if fair is not None else site
        tot_cost, tot_site, tot_fair = tot_cost + cost, tot_site + site, tot_fair + fval
        print(f"{n:8.0f} {side.upper():>4} {cost / n:6.3f} {site / n:6.3f} {_fmt(fair)} {fval - cost:8.0f}  {p['marketTitle']}")

    exposure = risk.build_exposure([(listings[str(p["exchangeId"])].race if str(p["exchangeId"]) in listings else None,
                                     float(p["quantity"]), float(p["costBasis"])) for p in positions])
    print(f"\ncash {cash:,.0f} | at risk {tot_cost:,.0f} | site value {tot_site:,.0f} "
          f"| value at fair {tot_fair:,.0f} (expected P&L {tot_fair - tot_cost:+,.0f})")
    print(f"equity at fair {cash + tot_fair:,.0f}  vs site equity {cash + tot_site:,.0f}")
    print(f"net direction: {'Dem' if exposure.net_dem >= 0 else 'Rep'} {abs(exposure.net_dem):,.0f} SUSQies "
          f"(cap {s.max_net_party_frac:.0%} of equity)")
    fills = Store().query("SELECT price, qty, fair_at_sync FROM fills")
    edge, notional = analytics.edge_captured([(p, q, f) for p, q, f in fills])
    if notional:
        print(f"edge captured on {len(fills)} logged fills: {edge:+,.0f} SUSQies on {notional:,.0f} traded "
              f"({edge / notional:+.1%})")


def cmd_analyze(s) -> None:
    rows = Store().query("SELECT ts, exchange_id, title, sm_bid, sm_ask, fair FROM snapshots "
                         "WHERE sm_bid IS NOT NULL AND sm_ask IS NOT NULL AND fair IS NOT NULL")
    snaps = [analytics.Snap(*r) for r in rows]
    if not snaps:
        print("No tracker data yet: the bot records a snapshot of every market each cycle while running.")
        return
    print(f"{len(snaps):,} snapshots across {len({x.exchange_id for x in snaps})} markets\n")
    print("Bias by fair-probability bucket (SM mid minus fair; + = SM players overpay for YES):")
    for name, n, mean in analytics.bias_by_bucket(snaps):
        print(f"  {name:>9}  n={n:>6}  {mean:+.4f}")
    print(f"\nTradable opportunities (>= {s.min_edge:.0%} edge): {analytics.opportunity_rate(snaps, s.min_edge):.1%} of snapshots")
    beta, half = analytics.reversion(snaps)
    print(f"Deviation persistence beta: {beta if beta is None else round(beta, 3)}  "
          f"half-life: {'n/a' if half is None else f'{half:.1f} cycles'}")
    lag = analytics.lead_lag(snaps)
    print(f"SM follows Kalshi moves next cycle: {'n/a' if lag is None else f'{lag:.0%} of the move'}")
    print("\nMost persistent mispricings (mean SM mid - fair):")
    for title, n, mean in analytics.persistent_mispricings(snaps):
        print(f"  {mean:+.3f}  n={n:>4}  {title}")


def cmd_leaderboard(client: SuperMarketClient, slug: str) -> None:
    board = client.leaderboard(slug)
    for row in board.get("leaderboard", [])[:20]:
        print(f"{row['rank']:>4} {row['username']:<24} pnl {row['pnl']:>12,.0f}  trades {row['tradesCount']:>5}  "
              f"volume {row.get('volume') or 0:>10,.0f}")
    print("my rank:", board.get("myRank"))


def cmd_run(client: SuperMarketClient, s, live: bool, once: bool) -> None:
    state = EngineState(peak_equity=load_peak_equity())
    logging.info("Starting %s loop (%ss normal, %ss election night)", "LIVE" if live else "DRY-RUN",
                 s.loop_seconds, s.election_night_loop_seconds)
    while True:
        started = time.monotonic()
        try:
            run_cycle(client, s, state, live)
        except ApiError as exc:
            logging.error("API error this cycle: %s", exc)
        except Exception:  # keep the loop alive; the next cycle refetches everything
            logging.exception("Unexpected error this cycle")
        if once:
            return
        time.sleep(max(1.0, loop_seconds(s) - (time.monotonic() - started)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["check", "fairs", "report", "analyze", "once", "run",
                                            "cancel-all", "leaderboard"])
    parser.add_argument("--live", action="store_true", help="place real orders (default is dry-run)")
    args = parser.parse_args()

    _setup_logging()
    s = load_settings()
    if args.command == "analyze":
        cmd_analyze(s)
        return
    client = SuperMarketClient(s.api_key)
    if args.command == "check":
        cmd_check(client, s.tournament_slug)
    elif args.command == "fairs":
        cmd_fairs(client, s)
    elif args.command == "report":
        cmd_report(client, s)
    elif args.command == "leaderboard":
        cmd_leaderboard(client, s.tournament_slug)
    elif args.command == "cancel-all":
        print(client.cancel_all(client.tournament(s.tournament_slug)["id"]))
    else:
        cmd_run(client, s, live=args.live, once=args.command == "once")


if __name__ == "__main__":
    main()
