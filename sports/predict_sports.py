"""Predict.fun sports discovery: league week fixtures, moneyline fav/dog, ±1.5 spreads."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from predict_feed import api_get, api_get_full, best_asks, last_api_error

# League config. ``tag_id`` is resolved at runtime from GET /v1/tags by ``tag_name``
# (mainnet EPL=546, testnet EPL=108 — do not hardcode one environment).
LEAGUES = {
    "epl": {
        "tag_name": "EPL",
        "tag_id_fallback": ("546", "108"),
        "slug_prefix": "epl",
        "football_data_code": "PL",
        "tz": "Europe/London",
    },
}

SPREAD_LINE = 1.5
_tag_id_cache: dict[str, str] = {}


@dataclass
class TeamSide:
    name: str
    abbreviation: str = ""
    is_home: bool = False
    moneyline_market_id: Optional[str] = None
    moneyline_ask: Optional[float] = None  # Yes ask = implied win ~prob


@dataclass
class SpreadMarket:
    market_id: str
    title: str
    line: float
    fav_covers_yes: bool  # True when Yes = favourite -1.5
    dog_outcome_name: str


@dataclass
class Fixture:
    league: str
    slug: str
    title: str
    kickoff: datetime
    home: TeamSide
    away: TeamSide
    draw_market_id: Optional[str] = None
    favourite: Optional[str] = None  # team name
    underdog: Optional[str] = None
    fav_ask: Optional[float] = None
    dog_ask_moneyline: Optional[float] = None
    spread: Optional[SpreadMarket] = None
    more_markets_slug: Optional[str] = None
    category_id: Optional[str] = None
    skip_reason: Optional[str] = None
    ask_snapshots: list = field(default_factory=list)


def league_config(league: str) -> dict:
    key = (league or "").strip().lower()
    if key not in LEAGUES:
        raise ValueError(f"unsupported league {league!r}; known: {sorted(LEAGUES)}")
    return LEAGUES[key]


def resolve_league_tag_id(league: str) -> str:
    """Look up Predict.fun tag id for the league (cached)."""
    key = (league or "").strip().lower()
    if key in _tag_id_cache:
        return _tag_id_cache[key]
    cfg = league_config(key)
    want = (cfg.get("tag_name") or key).casefold()
    tags = api_get("/v1/tags")
    items = tags if isinstance(tags, list) else []
    for tag in items:
        name = (tag.get("name") or "").casefold()
        if name == want:
            tag_id = str(tag.get("id"))
            _tag_id_cache[key] = tag_id
            return tag_id
    for fallback in cfg.get("tag_id_fallback") or ():
        _tag_id_cache[key] = str(fallback)
        return str(fallback)
    raise RuntimeError(f"no Predict.fun tag for league {league!r} (looked up {want!r})")


def week_bounds(now: Optional[datetime] = None, tz_name: str = "Europe/London"):
    """Monday 00:00 → next Monday 00:00 in ``tz_name`` (half-open)."""
    tz = ZoneInfo(tz_name)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday, monday + timedelta(days=7)


def _parse_iso(value) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _kickoff_from_category(cat: dict, more: Optional[dict] = None) -> Optional[datetime]:
    """Kickoff time: match ``startsAt`` (mainnet), else more-markets startsAt, else endsAt."""
    starts = _parse_iso(cat.get("startsAt"))
    if starts:
        return starts
    if more:
        dt = _parse_iso(more.get("startsAt"))
        if dt:
            return dt
    ends = _parse_iso(cat.get("endsAt"))
    if ends:
        return ends
    session = cat.get("tradingSession") or {}
    return _parse_iso(session.get("closesAt"))


def _outcome_yes_ask(market: dict) -> Optional[float]:
    for outcome in market.get("outcomes") or []:
        if str(outcome.get("indexSet")) in ("1", "Yes") or (outcome.get("name") or "").lower() == "yes":
            ask = outcome.get("bestAsk")
            if isinstance(ask, dict):
                ask = ask.get("price")
            if ask is not None:
                try:
                    return float(ask)
                except (TypeError, ValueError):
                    return None
    return None


def fetch_yes_ask(market_id) -> Optional[float]:
    """Best Yes ask from quote, else order book."""
    mid = str(market_id)
    quote = api_get(f"/v1/markets/{mid}/quote")
    if isinstance(quote, dict):
        for key in ("bestAsk", "ask", "yesBestAsk"):
            val = quote.get(key)
            if isinstance(val, dict):
                val = val.get("price")
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    break
        # nested shapes
        yes = quote.get("yes") or quote.get("outcome1") or {}
        if isinstance(yes, dict):
            val = yes.get("bestAsk") or yes.get("ask")
            if isinstance(val, dict):
                val = val.get("price")
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
    book = api_get(f"/v1/markets/{mid}/orderbook")
    if isinstance(book, dict):
        up, _down = best_asks(book)
        return up if up > 0 else None
    return None


def fetch_no_ask(market_id) -> Optional[float]:
    """Best No ask (underdog +1.5 when Yes is favourite -1.5)."""
    mid = str(market_id)
    book = api_get(f"/v1/markets/{mid}/orderbook")
    if isinstance(book, dict):
        _up, down = best_asks(book)
        return down if down > 0 else None
    quote = api_get(f"/v1/markets/{mid}/quote")
    if isinstance(quote, dict):
        bid = quote.get("bestBid") or quote.get("bid")
        if isinstance(bid, dict):
            bid = bid.get("price")
        if bid is not None:
            try:
                return round(1.0 - float(bid), 4)
            except (TypeError, ValueError):
                return None
    return None


def _iter_categories(tag_id: str, page_size: int = 50):
    """Yield OPEN SPORTS_MATCH categories for a tag."""
    after = None
    while True:
        path = (
            f"/v1/categories?status=OPEN&marketVariant=SPORTS_MATCH"
            f"&tagIds={quote(str(tag_id))}&first={page_size}"
        )
        if after:
            path += f"&after={quote(after)}"
        data, cursor = api_get_full(path)
        if data is None:
            return
        if not isinstance(data, list):
            return
        for cat in data:
            yield cat
        if not cursor or not data:
            return
        after = cursor


def fetch_category(slug: str) -> Optional[dict]:
    data = api_get(f"/v1/categories/{quote(slug, safe='')}")
    return data if isinstance(data, dict) else None


def _teams_from_category(cat: dict) -> tuple[Optional[TeamSide], Optional[TeamSide]]:
    sports = ((cat.get("variantDetails") or {}).get("sports") or {})
    teams = sports.get("teams") or cat.get("teams") or []
    home = away = None
    for raw in teams:
        side = TeamSide(
            name=(raw.get("name") or "").strip(),
            abbreviation=(raw.get("abbreviation") or "").strip(),
            is_home=bool(raw.get("isHome")),
        )
        if not side.name:
            continue
        if side.is_home:
            home = side
        else:
            away = side
    if home and away:
        return home, away
    # fallback from moneyline markets
    named = []
    for m in cat.get("markets") or []:
        if m.get("marketType") != "SPORTS_MONEYLINE":
            continue
        team = m.get("team") or {}
        name = (team.get("name") or m.get("title") or "").strip()
        if not name or name.lower() == "draw":
            continue
        named.append(TeamSide(
            name=name,
            abbreviation=(team.get("abbreviation") or "").strip(),
            is_home=False,
            moneyline_market_id=str(m.get("id")) if m.get("id") is not None else None,
        ))
    if len(named) >= 2:
        named[0].is_home = True
        return named[0], named[1]
    return home, away


def _attach_moneyline(fixture: Fixture, cat: dict) -> None:
    for m in cat.get("markets") or []:
        if m.get("marketType") != "SPORTS_MONEYLINE":
            continue
        mid = str(m.get("id")) if m.get("id") is not None else None
        title = (m.get("title") or "").strip()
        team = m.get("team") or {}
        team_name = (team.get("name") or "").strip()
        ask = _outcome_yes_ask(m)
        if title.lower() == "draw" or (not team_name and title.lower() == "draw"):
            fixture.draw_market_id = mid
            continue
        for side in (fixture.home, fixture.away):
            if not side:
                continue
            if team_name and _names_match(team_name, side.name):
                side.moneyline_market_id = mid
                side.moneyline_ask = ask
                break
            if title and (
                title.lower() == (side.abbreviation or "").lower()
                or _names_match(title, side.name)
            ):
                side.moneyline_market_id = mid
                side.moneyline_ask = ask
                break


def _names_match(a: str, b: str) -> bool:
    return normalize_team_key(a) == normalize_team_key(b) and bool(normalize_team_key(a))


def normalize_team_key(name: str) -> str:
    text = (name or "").casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\b(fc|afc|cf|sc|football|club|the)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def refresh_moneyline_asks(fixture: Fixture) -> None:
    for side in (fixture.home, fixture.away):
        if side and side.moneyline_market_id:
            ask = fetch_yes_ask(side.moneyline_market_id)
            if ask is not None:
                side.moneyline_ask = ask


def label_favourite(fixture: Fixture) -> Optional[str]:
    """Favourite = higher moneyline Yes ask among the two teams (ignore Draw)."""
    home_ask = fixture.home.moneyline_ask if fixture.home else None
    away_ask = fixture.away.moneyline_ask if fixture.away else None
    if home_ask is None and fixture.home and fixture.home.moneyline_market_id:
        home_ask = fetch_yes_ask(fixture.home.moneyline_market_id)
        fixture.home.moneyline_ask = home_ask
    if away_ask is None and fixture.away and fixture.away.moneyline_market_id:
        away_ask = fetch_yes_ask(fixture.away.moneyline_market_id)
        fixture.away.moneyline_ask = away_ask
    if home_ask is None or away_ask is None:
        fixture.skip_reason = fixture.skip_reason or "missing_moneyline_asks"
        return None
    if home_ask >= away_ask:
        fixture.favourite = fixture.home.name
        fixture.underdog = fixture.away.name
        fixture.fav_ask = home_ask
        fixture.dog_ask_moneyline = away_ask
    else:
        fixture.favourite = fixture.away.name
        fixture.underdog = fixture.home.name
        fixture.fav_ask = away_ask
        fixture.dog_ask_moneyline = home_ask
    return fixture.favourite


def _team_tokens(name: str, abbreviation: str = "") -> set[str]:
    tokens = {normalize_team_key(name)} if name else set()
    if abbreviation:
        tokens.add(abbreviation.casefold().strip())
    # significant words ≥3 chars for loose title match
    for part in normalize_team_key(name).split():
        if len(part) >= 3:
            tokens.add(part)
    return {t for t in tokens if t}


def _title_matches_favourite(title_team: str, favourite: str, fav_abbr: str = "") -> bool:
    if _names_match(title_team, favourite):
        return True
    title_key = normalize_team_key(title_team)
    fav_tokens = _team_tokens(favourite, fav_abbr)
    if title_key and title_key in fav_tokens:
        return True
    if title_team and title_team.casefold().strip() in fav_tokens:
        return True
    # "Liverpool" in "Liverpool FC"
    if title_key and any(title_key == t or title_key in t or t in title_key for t in fav_tokens if len(t) >= 4):
        return True
    return False


def _find_fav_minus_line_market(
    more: dict,
    favourite: str,
    line: float = SPREAD_LINE,
    fav_abbr: str = "",
) -> Optional[SpreadMarket]:
    target = abs(float(line))
    for m in more.get("markets") or []:
        if m.get("marketType") != "SPORTS_SPREADS":
            continue
        vd = ((m.get("variantDetails") or {}).get("sports") or {})
        raw_line = vd.get("line")
        try:
            mline = float(raw_line) if raw_line is not None else None
        except (TypeError, ValueError):
            mline = None
        title = (m.get("title") or "").strip()
        title_team = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
        if mline is not None and abs(abs(mline) - target) > 1e-9:
            continue
        if mline is None and f"-{target}" not in title.replace(" ", ""):
            continue
        if not _title_matches_favourite(title_team, favourite, fav_abbr):
            continue
        mid = m.get("id")
        if mid is None:
            continue
        dog_name = "No"
        for outcome in m.get("outcomes") or []:
            name = (outcome.get("name") or "").strip()
            if not name:
                continue
            if str(outcome.get("indexSet")) == "1" or name.lower() == "yes":
                continue
            dog_name = name
            break
        return SpreadMarket(
            market_id=str(mid),
            title=title,
            line=-target,
            fav_covers_yes=True,
            dog_outcome_name=dog_name,
        )
    return None


def attach_spread(fixture: Fixture, line: float = SPREAD_LINE) -> Optional[SpreadMarket]:
    if not fixture.favourite:
        label_favourite(fixture)
    if not fixture.favourite:
        return None
    more_slug = f"{fixture.slug}-more-markets"
    more = fetch_category(more_slug)
    if not more:
        fixture.skip_reason = fixture.skip_reason or f"no_more_markets:{last_api_error or more_slug}"
        return None
    fixture.more_markets_slug = more_slug
    fav_abbr = ""
    for side in (fixture.home, fixture.away):
        if side and side.name == fixture.favourite:
            fav_abbr = side.abbreviation or ""
            break
    spread = _find_fav_minus_line_market(more, fixture.favourite, line=line, fav_abbr=fav_abbr)
    if not spread:
        fixture.skip_reason = fixture.skip_reason or f"no_spread_{line}"
        return None
    fixture.spread = spread
    return spread


def dog_plus_ask(fixture: Fixture) -> Optional[float]:
    """Ask to buy underdog +1.5 (No side of favourite -1.5)."""
    if not fixture.spread:
        return None
    return fetch_no_ask(fixture.spread.market_id)


def fetch_spread_resolution(market_id) -> Optional[str]:
    """Winning outcome name if resolved, else None."""
    data = api_get(f"/v1/markets/{market_id}")
    if not isinstance(data, dict):
        return None
    resolution = data.get("resolution")
    if isinstance(resolution, dict) and resolution.get("name"):
        return str(resolution["name"])
    status = str(data.get("status") or "").upper()
    trading = str(data.get("tradingStatus") or "").upper()
    if status in ("RESOLVED", "SETTLED") or trading in ("RESOLVED",):
        for outcome in data.get("outcomes") or []:
            if str(outcome.get("status") or "").upper() in ("WON", "YES", "WINNER"):
                return str(outcome.get("name") or "")
    return None


def list_week_fixtures(league: str, now: Optional[datetime] = None) -> list[Fixture]:
    """Open SPORTS_MATCH fixtures for ``league`` with kickoff in the current week."""
    cfg = league_config(league)
    start, end = week_bounds(now, cfg["tz"])
    tz = ZoneInfo(cfg["tz"])
    out: list[Fixture] = []
    prefix = cfg["slug_prefix"]
    tag_id = resolve_league_tag_id(league)
    for cat in _iter_categories(tag_id):
        slug = (cat.get("slug") or "").strip()
        if not slug:
            continue
        if slug.endswith("-more-markets") or slug.endswith("-player-props"):
            continue
        if prefix and not slug.startswith(f"{prefix}-"):
            # tag filter should already scope; keep soft check
            tags = {str(t.get("name") or "").lower() for t in (cat.get("tags") or [])}
            if league.lower() not in tags and "epl" not in tags:
                continue
        home, away = _teams_from_category(cat)
        if not home or not away:
            continue
        more_slug = f"{slug}-more-markets"
        kickoff = _kickoff_from_category(cat, None)
        if kickoff is None:
            more_early = fetch_category(more_slug)
            kickoff = _kickoff_from_category(cat, more_early)
        if kickoff is None:
            continue
        kickoff_local = kickoff.astimezone(tz)
        if not (start <= kickoff_local < end):
            continue
        fixture = Fixture(
            league=league.lower(),
            slug=slug,
            title=cat.get("title") or cat.get("shortTitle") or slug,
            kickoff=kickoff,
            home=home,
            away=away,
            category_id=str(cat.get("id")) if cat.get("id") is not None else None,
            more_markets_slug=more_slug,
        )
        _attach_moneyline(fixture, cat)
        label_favourite(fixture)
        attach_spread(fixture)
        out.append(fixture)
    out.sort(key=lambda f: f.kickoff)
    return out


def decimal_odds_from_ask(ask: Optional[float]) -> Optional[float]:
    if ask is None or ask <= 0:
        return None
    return round(1.0 / ask, 4)


def max_ask_for_min_odds(min_odds: float) -> float:
    """Ask ceiling so decimal odds ≥ min_odds (e.g. 1.5 → 0.666…)."""
    return 1.0 / float(min_odds)
