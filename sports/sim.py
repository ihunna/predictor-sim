"""Per-match state machine: watch dog +1.5 → sim fill → hold to settle."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sports.logs import log_ask, log_trade
from sports.predict_sports import (
    Fixture,
    decimal_odds_from_ask,
    dog_plus_ask,
    fetch_spread_resolution,
    max_ask_for_min_odds,
)
from sports.strength import StrengthGate, StrengthService


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


@dataclass
class MatchState:
    fixture: Fixture
    status: str = "new"  # new|skipped|watching|filled|missed|settled
    strength: Optional[StrengthGate] = None
    fill_ask: Optional[float] = None
    fill_at: Optional[datetime] = None
    contracts: float = 0.0
    cost_usd: float = 0.0
    resolution: Optional[str] = None
    pnl: Optional[float] = None
    notes: str = ""
    last_ask: Optional[float] = None
    lowest_ask: Optional[float] = None


@dataclass
class SportsSim:
    league: str
    capital: float = 100.0
    contracts: float = 10.0
    min_odds: float = 1.5
    trades_path: str = ""
    asks_path: str = ""
    strength: StrengthService = field(default_factory=StrengthService)
    states: dict[str, MatchState] = field(default_factory=dict)

    def max_ask(self) -> float:
        return max_ask_for_min_odds(self.min_odds)

    def _base_row(self, state: MatchState, action: str) -> dict:
        fx = state.fixture
        dog = state.strength.dog if state.strength else None
        fav = state.strength.fav if state.strength else None
        ask = state.fill_ask if state.fill_ask is not None else state.last_ask
        return {
            "league": fx.league,
            "slug": fx.slug,
            "title": fx.title,
            "kickoff": _iso(fx.kickoff),
            "home": fx.home.name if fx.home else "",
            "away": fx.away.name if fx.away else "",
            "favourite": fx.favourite or "",
            "underdog": fx.underdog or "",
            "fav_ml_ask": fx.fav_ask if fx.fav_ask is not None else "",
            "dog_ml_ask": fx.dog_ask_moneyline if fx.dog_ask_moneyline is not None else "",
            "spread_market_id": fx.spread.market_id if fx.spread else "",
            "spread_title": fx.spread.title if fx.spread else "",
            "dog_plus_ask": ask if ask is not None else "",
            "dog_plus_odds": decimal_odds_from_ask(ask) if ask is not None else "",
            "dog_table_pos": dog.position if dog and dog.position is not None else "",
            "fav_table_pos": fav.position if fav and fav.position is not None else "",
            "dog_form": dog.form if dog else "",
            "dog_form_pts": dog.form_points if dog else "",
            "strength_ok": "" if not state.strength else ("1" if state.strength.passed else "0"),
            "strength_reasons": state.strength.reasons if state.strength else fx.skip_reason or "",
            "action": action,
            "fill_cents": "" if state.fill_ask is None else round(state.fill_ask * 100, 2),
            "contracts": state.contracts or "",
            "cost_usd": state.cost_usd or "",
            "capital_after": round(self.capital, 4),
            "resolution": state.resolution or "",
            "pnl": "" if state.pnl is None else round(state.pnl, 4),
            "notes": state.notes,
        }

    def ingest(self, fixtures: list[Fixture]) -> list[MatchState]:
        """Evaluate strength; skip weak dogs; queue strong dogs for watching."""
        ready = []
        for fx in fixtures:
            if fx.slug in self.states:
                ready.append(self.states[fx.slug])
                continue
            state = MatchState(fixture=fx)
            self.states[fx.slug] = state

            if not fx.favourite or not fx.underdog:
                state.status = "skipped"
                state.notes = fx.skip_reason or "no_fav_dog"
                if self.trades_path:
                    log_trade(self.trades_path, self._base_row(state, "skip_no_fav_dog"))
                ready.append(state)
                continue

            if not fx.spread:
                state.status = "skipped"
                state.notes = fx.skip_reason or "no_spread"
                if self.trades_path:
                    log_trade(self.trades_path, self._base_row(state, "skip_no_spread"))
                ready.append(state)
                continue

            gate = self.strength.evaluate(self.league, fx.underdog, fx.favourite)
            state.strength = gate
            if not gate.passed:
                state.status = "skipped"
                state.notes = gate.reasons
                if self.trades_path:
                    log_trade(self.trades_path, self._base_row(state, "skip_weak_dog"))
                ready.append(state)
                continue

            state.status = "watching"
            if self.trades_path:
                log_trade(self.trades_path, self._base_row(state, "watching"))
            ready.append(state)
        return ready

    def poll_watching(self, now: Optional[datetime] = None) -> None:
        """Poll asks for watching matches; fill when ask ≤ ceiling; miss at kickoff."""
        now = now or _utc_now()
        ceiling = self.max_ask()
        for state in list(self.states.values()):
            if state.status != "watching":
                continue
            fx = state.fixture
            kickoff = fx.kickoff
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)
            seconds_left = (kickoff - now).total_seconds()
            ask = dog_plus_ask(fx)
            state.last_ask = ask
            if ask is not None and ask > 0:
                if state.lowest_ask is None or ask < state.lowest_ask:
                    state.lowest_ask = ask
            odds = decimal_odds_from_ask(ask)
            if self.asks_path and fx.spread:
                log_ask(self.asks_path, {
                    "slug": fx.slug,
                    "spread_market_id": fx.spread.market_id,
                    "dog_plus_ask": ask if ask is not None else "",
                    "dog_plus_odds": odds if odds is not None else "",
                    "seconds_to_kickoff": int(seconds_left),
                })

            if ask is not None and ask > 0 and ask <= ceiling + 1e-12:
                self._fill(state, ask, now)
                continue

            if seconds_left <= 0:
                state.status = "missed"
                state.notes = f"miss_kickoff:last_ask={ask};need_ask<={ceiling:.4f}"
                if self.trades_path:
                    log_trade(self.trades_path, self._base_row(state, "miss_kickoff"))

    def _fill(self, state: MatchState, ask: float, when: datetime) -> None:
        contracts = float(self.contracts)
        cost = contracts * ask
        if cost > self.capital + 1e-9:
            state.notes = f"insufficient_capital:need={cost:.4f};have={self.capital:.4f}"
            # Keep watching in case capital frees up; still record attempt
            if self.trades_path:
                log_trade(self.trades_path, self._base_row(state, "skip_no_capital"))
            return
        self.capital -= cost
        state.status = "filled"
        state.fill_ask = ask
        state.fill_at = when
        state.contracts = contracts
        state.cost_usd = cost
        state.notes = f"taker_fill@{ask:.4f};odds={decimal_odds_from_ask(ask)}"
        if self.trades_path:
            log_trade(self.trades_path, self._base_row(state, "fill"))

    def settle_filled(self) -> None:
        """Resolve filled positions from predict.fun market resolution."""
        for state in list(self.states.values()):
            if state.status != "filled" or not state.fixture.spread:
                continue
            winner = fetch_spread_resolution(state.fixture.spread.market_id)
            if not winner:
                continue
            state.resolution = winner
            dog_name = state.fixture.underdog or ""
            dog_outcome = (state.fixture.spread.dog_outcome_name or "").strip()
            # Win if resolved outcome is the dog side (No / dog abbr), not the favourite laying -1.5.
            fav = state.fixture.favourite or ""
            won = False
            w = winner.casefold()
            if dog_outcome and w == dog_outcome.casefold():
                won = True
            elif dog_name and (w == dog_name.casefold() or dog_name.casefold() in w or w in dog_name.casefold()):
                won = True
            elif w in ("no", "underdog"):
                won = True
            elif fav and w == fav.casefold():
                won = False
            elif w in ("yes",):
                won = False

            payout = state.contracts if won else 0.0
            state.pnl = payout - state.cost_usd
            self.capital += payout
            state.status = "settled"
            state.notes = f"settled:{winner};{'win' if won else 'loss'}"
            if self.trades_path:
                log_trade(self.trades_path, self._base_row(state, "settle"))

    def summary(self) -> dict:
        counts = {}
        for state in self.states.values():
            counts[state.status] = counts.get(state.status, 0) + 1
        filled = [s for s in self.states.values() if s.status in ("filled", "settled")]
        settled = [s for s in self.states.values() if s.status == "settled"]
        pnl = sum(s.pnl or 0.0 for s in settled)
        return {
            "capital": round(self.capital, 4),
            "counts": counts,
            "n_filled": len(filled),
            "n_settled": len(settled),
            "settled_pnl": round(pnl, 4),
        }
