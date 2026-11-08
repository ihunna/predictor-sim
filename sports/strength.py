"""Underdog strength from football-data.org standings + last-5 form."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import requests

from sports.predict_sports import league_config, normalize_team_key

FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"

# Manual aliases: predict.fun / common names → football-data keys after normalize.
NAME_ALIASES = {
    normalize_team_key("Man United"): normalize_team_key("Manchester United"),
    normalize_team_key("Manchester Utd"): normalize_team_key("Manchester United"),
    normalize_team_key("Man City"): normalize_team_key("Manchester City"),
    normalize_team_key("Spurs"): normalize_team_key("Tottenham Hotspur"),
    normalize_team_key("Tottenham"): normalize_team_key("Tottenham Hotspur"),
    normalize_team_key("Wolves"): normalize_team_key("Wolverhampton Wanderers"),
    normalize_team_key("Wolverhampton"): normalize_team_key("Wolverhampton Wanderers"),
    normalize_team_key("Nottingham Forest"): normalize_team_key("Nottingham Forest"),
    normalize_team_key("Nottm Forest"): normalize_team_key("Nottingham Forest"),
    normalize_team_key("Newcastle"): normalize_team_key("Newcastle United"),
    normalize_team_key("West Ham"): normalize_team_key("West Ham United"),
    normalize_team_key("Brighton"): normalize_team_key("Brighton & Hove Albion"),
    normalize_team_key("Brighton and Hove Albion"): normalize_team_key("Brighton Hove Albion"),
    normalize_team_key("Leicester"): normalize_team_key("Leicester City"),
    normalize_team_key("Leeds"): normalize_team_key("Leeds United"),
    normalize_team_key("Ipswich"): normalize_team_key("Ipswich Town"),
    normalize_team_key("Bournemouth"): normalize_team_key("AFC Bournemouth"),
    normalize_team_key("Sheffield Utd"): normalize_team_key("Sheffield United"),
    normalize_team_key("Hull"): normalize_team_key("Hull City"),
    normalize_team_key("Coventry"): normalize_team_key("Coventry City"),
    normalize_team_key("Sunderland"): normalize_team_key("Sunderland AFC"),
}


@dataclass
class TeamStrength:
    name: str
    position: Optional[int]
    table_size: int
    form: str  # e.g. WDLWW
    form_points: int
    table_ok: bool
    form_ok: bool
    strong: bool
    reasons: str


@dataclass
class StrengthGate:
    dog: Optional[TeamStrength]
    fav: Optional[TeamStrength]
    passed: bool
    reasons: str


def form_points(form: str) -> int:
    """Sum W=3 D=1 L=0 over characters (ignores commas/spaces)."""
    pts = 0
    for ch in (form or "").upper():
        if ch == "W":
            pts += 3
        elif ch == "D":
            pts += 1
    return pts


def _alias_key(name: str) -> str:
    key = normalize_team_key(name)
    return NAME_ALIASES.get(key, key)


def _match_standing(standings: list[dict], team_name: str) -> Optional[dict]:
    want = _alias_key(team_name)
    if not want:
        return None
    best = None
    for row in standings:
        team = row.get("team") or {}
        candidates = [
            team.get("name"),
            team.get("shortName"),
            team.get("tla"),
        ]
        for cand in candidates:
            if not cand:
                continue
            key = _alias_key(str(cand))
            if key == want or want in key or key in want:
                return row
            if len(want) >= 5 and (want in key or key in want):
                best = best or row
    return best


def _result_letter(match: dict, team_id: int) -> Optional[str]:
    score = match.get("score") or {}
    full = score.get("fullTime") or {}
    home_goals, away_goals = full.get("home"), full.get("away")
    if home_goals is None or away_goals is None:
        return None
    home_id = ((match.get("homeTeam") or {}).get("id"))
    away_id = ((match.get("awayTeam") or {}).get("id"))
    try:
        home_id = int(home_id)
        away_id = int(away_id)
        home_goals = int(home_goals)
        away_goals = int(away_goals)
    except (TypeError, ValueError):
        return None
    if team_id == home_id:
        if home_goals > away_goals:
            return "W"
        if home_goals < away_goals:
            return "L"
        return "D"
    if team_id == away_id:
        if away_goals > home_goals:
            return "W"
        if away_goals < home_goals:
            return "L"
        return "D"
    return None


class StrengthService:
    """Cached football-data.org standings + form built from finished matches.

    Standings ``form`` is often null on the free API, so last-N results are derived
    from ``GET /competitions/{code}/matches?status=FINISHED``.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = (api_key or "").strip() or None
        self._standings_cache: dict[str, list[dict]] = {}
        self._form_cache: dict[str, dict[int, str]] = {}  # code -> team_id -> form letters
        self.last_error: Optional[str] = None

    def configured(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str) -> Optional[dict]:
        if not self.api_key:
            self.last_error = "no FOOTBALL_DATA_API_KEY"
            return None
        url = f"{FOOTBALL_DATA_BASE}{path}"
        try:
            resp = requests.get(
                url,
                headers={"X-Auth-Token": self.api_key, "accept": "application/json"},
                timeout=20,
            )
        except Exception as e:
            self.last_error = f"football-data {type(e).__name__}: {e}"
            return None
        if resp.status_code != 200:
            self.last_error = f"football-data HTTP {resp.status_code}"
            return None
        try:
            return resp.json()
        except Exception as e:
            self.last_error = f"football-data JSON {type(e).__name__}: {e}"
            return None

    def fetch_standings(self, competition_code: str) -> list[dict]:
        code = (competition_code or "").upper()
        if code in self._standings_cache:
            return self._standings_cache[code]
        body = self._get(f"/competitions/{code}/standings")
        if not body:
            return []
        tables = body.get("standings") or []
        total = []
        for block in tables:
            if (block.get("type") or "").upper() == "TOTAL":
                total = block.get("table") or []
                break
        if not total and tables:
            total = tables[0].get("table") or []
        self._standings_cache[code] = total
        self.last_error = None
        return total

    def fetch_form_by_team_id(self, competition_code: str, form_games: int = 5) -> dict[int, str]:
        """Last ``form_games`` finished league results per team id (oldest→newest letters)."""
        code = (competition_code or "").upper()
        cache_key = f"{code}:{form_games}"
        if cache_key in self._form_cache:
            return self._form_cache[cache_key]

        end = date.today()
        start = end - timedelta(days=90)
        body = self._get(
            f"/competitions/{code}/matches?status=FINISHED"
            f"&dateFrom={start.isoformat()}&dateTo={end.isoformat()}"
        )
        if not body:
            self._form_cache[cache_key] = {}
            return {}

        matches = list(body.get("matches") or [])
        matches.sort(key=lambda m: m.get("utcDate") or "")

        by_team: dict[int, list[str]] = {}
        for match in matches:
            for side in ("homeTeam", "awayTeam"):
                tid = (match.get(side) or {}).get("id")
                try:
                    tid = int(tid)
                except (TypeError, ValueError):
                    continue
                letter = _result_letter(match, tid)
                if not letter:
                    continue
                by_team.setdefault(tid, []).append(letter)

        forms = {tid: "".join(letters[-form_games:]) for tid, letters in by_team.items()}
        self._form_cache[cache_key] = forms
        self.last_error = None
        return forms

    def _form_for_row(self, competition_code: str, row: dict, form_games: int) -> str:
        # Prefer standings.form when the API fills it; else derive from matches.
        form_raw = row.get("form")
        if form_raw:
            letters = [c for c in str(form_raw).upper() if c in "WDL"]
            if letters:
                return "".join(letters[-form_games:])
        team_id = ((row.get("team") or {}).get("id"))
        try:
            team_id = int(team_id)
        except (TypeError, ValueError):
            return ""
        return self.fetch_form_by_team_id(competition_code, form_games=form_games).get(team_id, "")

    def team_strength(
        self,
        league: str,
        team_name: str,
        fav_position: Optional[int] = None,
        top_half: bool = True,
        max_place_gap: int = 6,
        min_form_points: int = 5,
        form_games: int = 5,
    ) -> TeamStrength:
        cfg = league_config(league)
        code = cfg["football_data_code"]
        rows = self.fetch_standings(code)
        table_size = len(rows) or 20
        row = _match_standing(rows, team_name)
        if not row:
            return TeamStrength(
                name=team_name,
                position=None,
                table_size=table_size,
                form="",
                form_points=0,
                table_ok=False,
                form_ok=False,
                strong=False,
                reasons=f"team_not_in_table:{self.last_error or team_name}",
            )
        position = int(row.get("position") or 0) or None
        form = self._form_for_row(code, row, form_games)
        pts = form_points(form)

        half = max(1, table_size // 2)
        table_ok = False
        table_bits = []
        if position is None:
            table_bits.append("no_position")
        else:
            if top_half and position <= half:
                table_ok = True
                table_bits.append(f"top_half:{position}/{table_size}")
            if fav_position is not None and abs(position - fav_position) <= max_place_gap:
                table_ok = True
                table_bits.append(f"within_{max_place_gap}_of_fav:{position}_vs_{fav_position}")
            if not table_ok:
                table_bits.append(f"table_weak:{position}/{table_size}")

        form_ok = pts >= min_form_points and len(form) > 0
        form_bits = [f"form:{form or 'n/a'}", f"form_pts:{pts}", f"need>={min_form_points}"]
        if not form_ok:
            form_bits.append("form_weak")

        strong = table_ok and form_ok
        reasons = ",".join(table_bits + form_bits)
        return TeamStrength(
            name=team_name,
            position=position,
            table_size=table_size,
            form=form,
            form_points=pts,
            table_ok=table_ok,
            form_ok=form_ok,
            strong=strong,
            reasons=reasons,
        )

    def evaluate(
        self,
        league: str,
        underdog: str,
        favourite: str,
        max_place_gap: int = 6,
        min_form_points: int = 5,
    ) -> StrengthGate:
        fav = self.team_strength(
            league, favourite, fav_position=None,
            max_place_gap=max_place_gap, min_form_points=min_form_points,
        )
        dog = self.team_strength(
            league,
            underdog,
            fav_position=fav.position,
            max_place_gap=max_place_gap,
            min_form_points=min_form_points,
        )
        passed = bool(dog.strong)
        reasons = f"dog[{dog.reasons}]; fav_pos={fav.position}"
        if not self.configured():
            passed = False
            reasons = f"no_football_data_key;{reasons}"
        elif self.last_error and not dog.position:
            passed = False
            reasons = f"{self.last_error};{reasons}"
        return StrengthGate(dog=dog, fav=fav, passed=passed, reasons=reasons)
