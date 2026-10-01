"""Tracker analytics: how do Super Market players misprice vs Kalshi, and how much edge have we captured?"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from statistics import fmean

BUCKETS = [(0.0, 0.10), (0.10, 0.30), (0.30, 0.70), (0.70, 0.90), (0.90, 1.01)]


@dataclass(frozen=True)
class Snap:
    ts: str
    exchange_id: str
    title: str
    bid: float
    ask: float
    fair: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def dev(self) -> float:
        """Super Market mid minus fair: >0 means SM players overprice YES."""
        return self.mid - self.fair


def _slope(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 10:
        return None
    mx, my = fmean(xs), fmean(ys)
    var = sum((x - mx) ** 2 for x in xs)
    return None if var == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var


def bias_by_bucket(snaps: list[Snap]) -> list[tuple[str, int, float]]:
    """Mean SM-minus-fair by fair-probability bucket. Positive in low buckets = long-shot bias."""
    out = []
    for lo, hi in BUCKETS:
        devs = [s.dev for s in snaps if lo <= s.fair < hi]
        out.append((f"{lo:.0%}-{min(hi, 1):.0%}", len(devs), fmean(devs) if devs else 0.0))
    return out


def opportunity_rate(snaps: list[Snap], min_edge: float) -> float:
    """Share of snapshots where the SM book offered at least min_edge vs fair on either side."""
    if not snaps:
        return 0.0
    hits = sum(1 for s in snaps if s.ask <= s.fair - min_edge or s.bid >= s.fair + min_edge)
    return hits / len(snaps)


def _series(snaps: list[Snap]) -> dict[str, list[Snap]]:
    by_ex: dict[str, list[Snap]] = defaultdict(list)
    for s in sorted(snaps, key=lambda s: s.ts):
        by_ex[s.exchange_id].append(s)
    return by_ex


def reversion(snaps: list[Snap]) -> tuple[float | None, float | None]:
    """(persistence beta, half-life in snapshots) of the SM-vs-fair deviation."""
    xs, ys = [], []
    for series in _series(snaps).values():
        for a, b in zip(series, series[1:]):
            xs.append(a.dev)
            ys.append(b.dev)
    beta = _slope(xs, ys)
    if beta is None or not 0 < beta < 1:
        return beta, None
    return beta, math.log(0.5) / math.log(beta)


def lead_lag(snaps: list[Snap]) -> float | None:
    """Fraction of a Kalshi move that the SM mid makes in the NEXT snapshot (lag => tradable)."""
    xs, ys = [], []
    for series in _series(snaps).values():
        for a, b, c in zip(series, series[1:], series[2:]):
            xs.append(b.fair - a.fair)
            ys.append(c.mid - b.mid)
    return _slope(xs, ys)


def persistent_mispricings(snaps: list[Snap], min_obs: int = 10, top: int = 10) -> list[tuple[str, int, float]]:
    rows = []
    for series in _series(snaps).values():
        if len(series) >= min_obs:
            rows.append((series[0].title, len(series), fmean(s.dev for s in series)))
    return sorted(rows, key=lambda r: -abs(r[2]))[:top]


def edge_captured(fills: list[tuple[float, float, float | None]]) -> tuple[float, float]:
    """fills: (price, qty, side_fair). Returns (expected edge in SUSQies, notional traded)."""
    edge = sum((fair - price) * qty for price, qty, fair in fills if fair is not None)
    notional = sum(price * qty for price, qty, _ in fills)
    return edge, notional
