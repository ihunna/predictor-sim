#!/usr/bin/env python3
"""Sports underdog +1.5 sim bot on predict.fun (paper fills + CSV logs).

Usage:
    python -m sports.run --league epl
    python -m sports.run --league epl --plain
    python -m sports.run --league epl --min-odds 1.5 --contracts 10 --capital 100 --poll-seconds 30

Requires PREDICT_API_KEY and FOOTBALL_DATA_API_KEY in .env.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

# Allow `python sports/run.py` from repo root.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from predict_feed import (  # noqa: E402
    api_configured,
    configure_api_from_env,
    last_api_error,
    load_dotenv,
    set_api_key,
)
from sports.logs import default_log_paths  # noqa: E402
from sports.predict_sports import (  # noqa: E402
    LEAGUES,
    list_week_fixtures,
    max_ask_for_min_odds,
    resolve_league_tag_id,
)
from sports.sim import SportsSim  # noqa: E402
from sports.strength import StrengthService  # noqa: E402
from sports.ui import PlainSportsUI, SportsDashboard  # noqa: E402


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Predict.fun sports underdog +1.5 sim (paper).")
    p.add_argument("--league", default="epl", choices=sorted(LEAGUES), help="League key (default epl)")
    p.add_argument("--week", default="current", help="Only 'current' supported in v1")
    p.add_argument("--capital", type=float, default=100.0)
    p.add_argument("--contracts", type=float, default=10.0)
    p.add_argument("--min-odds", type=float, default=1.5, help="Min decimal odds on dog +1.5")
    p.add_argument("--poll-seconds", type=float, default=30.0)
    p.add_argument("--once", action="store_true", help="Scan + one poll pass, then exit")
    p.add_argument("--plain", action="store_true", help="Plain lines instead of live dashboard")
    p.add_argument("--api-key", default="", help="Override PREDICT_API_KEY")
    p.add_argument("--football-data-key", default="", help="Override FOOTBALL_DATA_API_KEY")
    p.add_argument("--log-dir", default="", help="CSV directory (default ./logs)")
    p.add_argument("--max-place-gap", type=int, default=6)
    p.add_argument("--min-form-points", type=int, default=5)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()
    if args.api_key:
        set_api_key(args.api_key.strip())
    else:
        configure_api_from_env()
    if not api_configured():
        print("PREDICT_API_KEY required (set in .env or --api-key).", file=sys.stderr)
        return 1

    fd_key = (args.football_data_key or os.environ.get("FOOTBALL_DATA_API_KEY") or "").strip()
    strength = StrengthService(api_key=fd_key)
    if not strength.configured():
        print("Warning: FOOTBALL_DATA_API_KEY missing — all dogs will skip as weak.", file=sys.stderr)

    if args.week != "current":
        print("Only --week current is supported in v1.", file=sys.stderr)
        return 1

    trades_path, asks_path = default_log_paths(args.league, args.log_dir or None)
    sim = SportsSim(
        league=args.league,
        capital=args.capital,
        contracts=args.contracts,
        min_odds=args.min_odds,
        trades_path=trades_path,
        asks_path=asks_path,
        strength=strength,
    )

    ui = PlainSportsUI(sim, args.league) if args.plain else SportsDashboard(sim, args.league)

    _evaluate = strength.evaluate

    def evaluate(league, underdog, favourite, max_place_gap=args.max_place_gap,
                 min_form_points=args.min_form_points):
        return _evaluate(
            league, underdog, favourite,
            max_place_gap=max_place_gap,
            min_form_points=min_form_points,
        )

    strength.evaluate = evaluate  # type: ignore

    ceiling = max_ask_for_min_odds(args.min_odds)
    ui.event(
        f"sports sim {args.league} min_odds={args.min_odds} max_ask={ceiling:.4f} "
        f"contracts={args.contracts} capital={args.capital}"
    )
    ui.event(f"logs {trades_path}")

    try:
        tag = resolve_league_tag_id(args.league)
        ui.event(f"predict.fun tag {args.league}={tag}")
        fixtures = list_week_fixtures(args.league)
    except Exception as e:
        print(f"fixture load failed: {e} ({last_api_error})", file=sys.stderr)
        return 1

    ui.event(f"found {len(fixtures)} fixtures this week")
    states = sim.ingest(fixtures)
    watching = sum(1 for s in states if s.status == "watching")
    skipped = sum(1 for s in states if s.status == "skipped")
    ui.event(f"watching={watching} skipped={skipped}")
    for state in states:
        fx = state.fixture
        if state.status == "watching":
            ui.event(f"WATCH {_iso_short(fx.kickoff)} dog={fx.underdog} vs fav={fx.favourite}")
        elif state.status == "skipped":
            reason = (state.strength.reasons if state.strength else state.notes) or "skip"
            ui.event(f"SKIP  {_iso_short(fx.kickoff)} {fx.underdog}: {reason[:70]}")

    def tick():
        before = {slug: s.status for slug, s in sim.states.items()}
        sim.poll_watching()
        sim.settle_filled()
        for slug, state in sim.states.items():
            if before.get(slug) != state.status:
                fx = state.fixture
                if state.status == "filled":
                    ui.event(
                        f"FILL  {fx.underdog} +1.5 @ {round((state.fill_ask or 0) * 100)}¢ "
                        f"cost ${state.cost_usd:.2f}"
                    )
                elif state.status == "missed":
                    ui.event(f"MISS  {fx.underdog} +1.5 (kickoff, ask never ≤ ceiling)")
                elif state.status == "settled":
                    ui.event(
                        f"SETTLE {fx.underdog} +1.5 → {state.resolution} "
                        f"pnl={state.pnl:+.2f}" if state.pnl is not None else
                        f"SETTLE {fx.underdog} +1.5 → {state.resolution}"
                    )
        ui.refresh()

    ui.start()
    try:
        tick()
        if args.once:
            time.sleep(1.5 if not args.plain else 0)
            return 0

        ui.event(f"polling every {args.poll_seconds}s — Ctrl-C to stop")
        known = set(sim.states)
        while True:
            time.sleep(max(1.0, float(args.poll_seconds)))
            try:
                fresh = list_week_fixtures(args.league)
                for state in sim.ingest(fresh):
                    if state.fixture.slug not in known and state.status == "watching":
                        fx = state.fixture
                        ui.event(
                            f"WATCH {_iso_short(fx.kickoff)} dog={fx.underdog} vs fav={fx.favourite}"
                        )
                    known.add(state.fixture.slug)
            except Exception as e:
                ui.event(f"refresh error: {e}")
            tick()
    except KeyboardInterrupt:
        ui.event("stopped")
        tick()
    finally:
        ui.stop()
    return 0


def _iso_short(dt) -> str:
    if not dt:
        return "?"
    return dt.astimezone(timezone.utc).strftime("%a %d %b %H:%M")


if __name__ == "__main__":
    raise SystemExit(main())
