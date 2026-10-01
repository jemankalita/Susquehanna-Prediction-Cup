"""Portfolio-level risk: per-race and national-direction (Dem vs Rep) exposure caps.

Every election contract is, underneath, a bet on one party. YES on "Dem wins X" and
NO on "Rep wins X" are the same bet, and all races share one national environment.
So the bot caps exposure per race-direction and on the net Dem-minus-Rep book.
Exposure is measured as SUSQies at risk (cost basis).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .markets import Race


@dataclass(frozen=True)
class Exposure:
    by_race: dict[tuple[str, str], float] = field(default_factory=dict)  # (race_key, "D"/"R") -> cost
    net_dem: float = 0.0                                                  # Dem-direction cost minus Rep-direction cost


def direction(race: Race | None, side: str) -> str | None:
    """Which party a position pays out on: YES on a party = that party; NO on a party = the other one."""
    if race is None or race.party not in ("D", "R"):
        return None
    if side == "yes":
        return race.party
    return "R" if race.party == "D" else "D"


def build_exposure(positions: list[tuple[Race | None, float, float]]) -> Exposure:
    """positions: (race, signed_qty, cost_basis)."""
    by_race: dict[tuple[str, str], float] = {}
    net = 0.0
    for race, qty, cost in positions:
        d = direction(race, "yes" if qty > 0 else "no")
        if d is None or qty == 0:
            continue
        key = (race.race_key, d)
        by_race[key] = by_race.get(key, 0.0) + abs(cost)
        net += abs(cost) if d == "D" else -abs(cost)
    return Exposure(by_race, net)


def allowed_cost(exp: Exposure, race: Race | None, side: str, cost: float,
                 max_race: float, max_net: float) -> float:
    """Largest part of `cost` that keeps both caps satisfied (0 if none)."""
    d = direction(race, side)
    if d is None:
        return min(cost, max_race)
    room_race = max_race - exp.by_race.get((race.race_key, d), 0.0)
    signed_net = exp.net_dem if d == "D" else -exp.net_dem  # current net in this trade's direction
    room_net = max_net - signed_net                          # reducing trades get extra room automatically
    return max(0.0, min(cost, room_race, room_net))


def add(exp: Exposure, race: Race | None, side: str, cost: float) -> Exposure:
    """New Exposure after adding a trade (immutable update)."""
    d = direction(race, side)
    if d is None or cost <= 0:
        return exp
    key = (race.race_key, d)
    by_race = {**exp.by_race, key: exp.by_race.get(key, 0.0) + cost}
    return Exposure(by_race, exp.net_dem + (cost if d == "D" else -cost))
