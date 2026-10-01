"""Parses Super Market titles and maps them to Kalshi tickers (the fair-value source)."""
from __future__ import annotations

import re
from dataclasses import dataclass

STATE_CODES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
    "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN",
    "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}
PARTY_CODES = {"Democratic": "D", "Republican": "R", "Independent": "I"}
# Senate seats that are special elections on Kalshi (different event ticker).
SPECIAL_SENATE = {"FL": "SENATEFLS-26", "OH": "SENATEOHS-26"}

_TITLE_RE = re.compile(r"Will the (\w+) Party win the (.+?)\?$")
_HOUSE_RE = re.compile(r"^([A-Z]{2})-(\d{2}) House race$")


@dataclass(frozen=True)
class Race:
    party: str          # "D" / "R" / "I"
    office: str         # "senate" / "governor" / "house" / "control_house" / "control_senate"
    state: str | None   # two-letter code, None for chamber control
    district: int | None = None

    @property
    def race_key(self) -> str:
        """Identifies the race independent of party (used to pair D/R markets)."""
        return f"{self.office}:{self.state or 'US'}:{self.district or ''}"


def parse_title(title: str) -> Race | None:
    match = _TITLE_RE.match(title.strip())
    if not match:
        return None
    party = PARTY_CODES.get(match.group(1))
    if party is None:
        return None
    subject = match.group(2)
    if subject.startswith("the "):
        subject = subject[4:]

    if subject == "U.S. House":
        return Race(party, "control_house", None)
    if subject == "U.S. Senate":
        return Race(party, "control_senate", None)
    house = _HOUSE_RE.match(subject)
    if house:
        return Race(party, "house", house.group(1), int(house.group(2)))
    for suffix, office in ((" Senate", "senate"), (" Governor", "governor")):
        if subject.endswith(suffix):
            state = STATE_CODES.get(subject[: -len(suffix)])
            return Race(party, office, state) if state else None
    return None


def kalshi_candidates(race: Race) -> list[str]:
    """Possible Kalshi tickers for a race, most likely first. Independents are not mapped."""
    if race.party not in ("D", "R"):
        return []
    p = race.party
    if race.office == "control_house":
        return [f"CONTROLH-2026-{p}"]
    if race.office == "control_senate":
        return [f"CONTROLS-2026-{p}"]
    st = race.state
    if race.office == "senate":
        events = [SPECIAL_SENATE[st]] if st in SPECIAL_SENATE else []
        events += [f"SENATE{st}-26", f"SENATEPARTY{st}-26", f"SENATEPARTY-{st}-26"]
        return [f"{e}-{p}" for e in events]
    if race.office == "governor":
        # Kalshi suffixes some governor events with the inauguration year instead of 26.
        return [f"GOVPARTY{st}-{yy}-{p}" for yy in ("26", "27", "28")] + [f"GOVPARTY-{st}-26-{p}"]
    if race.office == "house":
        n = race.district
        return [f"HOUSE{st}{n}-26-{p}", f"HOUSEPARTY-{st}{n:02d}-26-{p}", f"HOUSE{st}{n:02d}-26-{p}"]
    return []
