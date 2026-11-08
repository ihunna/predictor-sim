"""Terminal UI for the sports underdog +1.5 sim: Rich dashboard (default) or --plain lines.

Layout mirrors the crypto dashboard: main panel + wallet/history sidebar, cyan panels,
bold status colors, human-readable reasons (no raw gate dumps in the table).
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

from sports.predict_sports import decimal_odds_from_ask, max_ask_for_min_odds

EVENT_ROWS = 5
SIDEBAR_WIDTH = 42
HISTORY_ROWS = 10

STATUS_ORDER = ("watching", "filled", "settled", "missed", "skipped", "new")


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _utc_now():
    return datetime.now(timezone.utc)


def _as_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _kickoff_label(dt) -> str:
    dt = _as_utc(dt)
    if not dt:
        return "--"
    return dt.strftime("%a %H:%M")


def _short(name: str, width: int = 16) -> str:
    text = (name or "?").replace(" FC", "").replace(" AFC", "").replace(" & Hove Albion", "")
    text = text.replace("Manchester United", "Man United").replace("Manchester City", "Man City")
    text = text.replace("Tottenham Hotspur", "Tottenham").replace("Wolverhampton Wanderers", "Wolves")
    text = text.replace("Brighton Albion", "Brighton").replace("Nottingham Forest", "Nott'm Forest")
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "…"


def _fmt_ask(ask: Optional[float]) -> str:
    if ask is None or ask <= 0:
        return "--"
    return f"{round(ask * 100)}¢"


def _fmt_odds(ask: Optional[float]) -> str:
    odds = decimal_odds_from_ask(ask)
    return f"{odds:.2f}" if odds else "--"


def _countdown(kickoff) -> str:
    kickoff = _as_utc(kickoff)
    if not kickoff:
        return "--"
    secs = int((kickoff - _utc_now()).total_seconds())
    if secs <= 0:
        return "kicked off"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m:02d}:{s:02d}"


def _human_skip(state) -> str:
    """Short plain-English skip reason for the table."""
    notes = state.notes or ""
    if state.status == "missed":
        return "never hit min odds"
    if "no_spread" in notes or state.status == "skipped" and not state.fixture.spread:
        return "no +1.5 market"
    if "no_fav" in notes:
        return "no fav/dog"
    gate = state.strength
    if not gate or not gate.dog:
        return notes[:40] or "skipped"
    dog = gate.dog
    bits = []
    if not dog.table_ok:
        bits.append(f"table weak (P{dog.position})" if dog.position else "table weak")
    if not dog.form_ok:
        if not dog.form:
            bits.append("no form")
        else:
            bits.append(f"form {dog.form} ({dog.form_points}p)")
    return " · ".join(bits) or "weak dog"


def _form_cells(form: str):
    """Colored W/D/L letters for Rich Text."""
    from rich.text import Text

    out = Text()
    if not form:
        out.append("--", style="dim")
        return out
    for ch in form.upper():
        if ch == "W":
            out.append("W", style="bold green")
        elif ch == "D":
            out.append("D", style="bold yellow")
        elif ch == "L":
            out.append("L", style="bold red")
    return out


class PlainSportsUI:
    def __init__(self, sim, league: str):
        self.sim = sim
        self.league = league
        self.lock = threading.Lock()

    def start(self):
        pass

    def stop(self):
        pass

    def event(self, message: str):
        print(f"[{stamp()}] {message}", flush=True)

    def refresh(self):
        summary = self.sim.summary()
        ceiling = max_ask_for_min_odds(self.sim.min_odds)
        print(
            f"[{stamp()}] ${summary['capital']:.2f}  "
            f"watch={summary['counts'].get('watching', 0)}  "
            f"fill={summary['counts'].get('filled', 0)}  "
            f"skip={summary['counts'].get('skipped', 0)}  "
            f"pnl={summary['settled_pnl']:+.2f}  "
            f"target≤{_fmt_ask(ceiling)}",
            flush=True,
        )
        for state in sorted(
            self.sim.states.values(),
            key=lambda s: (STATUS_ORDER.index(s.status) if s.status in STATUS_ORDER else 99,
                           s.fixture.kickoff),
        ):
            fx = state.fixture
            ask = state.fill_ask if state.fill_ask is not None else state.last_ask
            if state.status == "watching":
                detail = f"ask {_fmt_ask(ask)} ({_fmt_odds(ask)})  in {_countdown(fx.kickoff)}"
            elif state.status in ("filled", "settled"):
                detail = f"filled {_fmt_ask(state.fill_ask)}  pnl={state.pnl}"
            else:
                detail = _human_skip(state)
            print(
                f"  {_kickoff_label(fx.kickoff)}  {state.status:<9}  "
                f"{_short(fx.underdog, 16):<16}  +1.5  {detail}",
                flush=True,
            )


class SportsDashboard:
    """Live Rich dashboard: watching cards + wallet/history sidebar (crypto-style)."""

    def __init__(self, sim, league: str):
        from rich.console import Console

        self.sim = sim
        self.league = league
        self.console = Console()
        self.lock = threading.Lock()
        self.live = None
        self.events: list[str] = []
        self.start_capital = float(sim.capital)
        self.peak_equity = float(sim.capital)

    def start(self):
        from rich.live import Live

        self.live = Live(
            get_renderable=self.render,
            console=self.console,
            refresh_per_second=2,
            redirect_stdout=False,
            redirect_stderr=False,
            screen=True,
        )
        self.live.start()

    def stop(self):
        if self.live is not None:
            self.live.stop()
            self.live = None

    def event(self, message: str):
        # Keep event feed short and readable — strip long gate dumps.
        text = message.strip()
        if "dog[" in text and "form_pts" in text:
            # compress SKIP lines
            if text.startswith("SKIP"):
                parts = text.split(":", 1)
                text = parts[0] + (" · " + _compress_gate(parts[1]) if len(parts) > 1 else "")
        with self.lock:
            self.events.append(f"{stamp()}  {text}")
            del self.events[:-EVENT_ROWS]

    def refresh(self):
        pass

    def render(self):
        from rich.panel import Panel
        from rich.text import Text

        try:
            return self._render()
        except Exception as e:
            return Panel(Text(f"UI render error: {e}", style="bold red"), border_style="red")

    def _states_by_status(self):
        groups = {k: [] for k in STATUS_ORDER}
        for state in self.sim.states.values():
            groups.setdefault(state.status, []).append(state)
        for key in groups:
            groups[key].sort(key=lambda s: s.fixture.kickoff)
        return groups

    def _render(self):
        from rich.console import Group
        from rich.panel import Panel

        with self.lock:
            events = list(self.events)

        main = self._render_main(events)
        wallet = self._render_wallet()
        history = self._render_history()

        if self.console.width < 110:
            return Group(main, wallet, history)

        from rich.table import Table

        layout = Table.grid(expand=True)
        layout.add_column(ratio=1)
        layout.add_column(width=SIDEBAR_WIDTH)
        layout.add_row(main, Group(wallet, history))
        return layout

    def _render_main(self, events):
        from rich import box
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        sim = self.sim
        summary = sim.summary()
        ceiling = max_ask_for_min_odds(sim.min_odds)
        counts = summary.get("counts") or {}
        groups = self._states_by_status()

        # --- header (crypto-style strategy line) ---
        strategy = Text()
        strategy.append("dog +1.5", style="bold cyan")
        strategy.append(f"  ≥{sim.min_odds:.2f} odds", style="dim")
        strategy.append(f"  (buy ≤{_fmt_ask(ceiling)})", style="dim")
        strategy.append(f"  ×{sim.contracts:g} contracts", style="dim")

        meta = Text("Watch strong underdogs · buy when ask hits target · hold to settle", style="dim")
        parts = [strategy, meta, Text("")]

        # --- watching / filled cards (each match fully enclosed) ---
        watching = groups.get("watching") or []
        filled = groups.get("filled") or []
        active = watching + filled
        if active:
            for state in active:
                parts.append(self._match_card(state, ceiling, hero=True))
                parts.append(Text(""))
        else:
            parts.append(Text("No matches watching — waiting for a strong dog at price", style="dim"))
            parts.append(Text(""))

        # --- settled / missed (also enclosed, quieter) ---
        done = (groups.get("settled") or []) + (groups.get("missed") or [])
        if done:
            for state in done:
                parts.append(self._match_card(state, ceiling, hero=False))
                parts.append(Text(""))

        # --- skipped compact table ---
        skipped = groups.get("skipped") or []
        if skipped:
            skip_table = Table(box=box.SIMPLE, expand=True, pad_edge=False, padding=(0, 1),
                               show_header=True, header_style="dim")
            skip_table.add_column("Kickoff", style="dim")
            skip_table.add_column("Dog")
            skip_table.add_column("Why skipped", style="dim")
            for state in skipped:
                fx = state.fixture
                skip_table.add_row(
                    _kickoff_label(fx.kickoff),
                    _short(fx.underdog, 18),
                    _human_skip(state),
                )
            parts.append(Panel(skip_table, title="[dim]Skipped[/dim]", border_style="dim", padding=(0, 0)))

        # --- events ---
        if events:
            ev = Text()
            for i, line in enumerate(events):
                if i:
                    ev.append("\n")
                ev.append(line, style="dim")
            parts.append(Text(""))
            parts.append(ev)

        bits = []
        for key in ("watching", "filled", "settled", "missed", "skipped"):
            n = counts.get(key, 0)
            if n:
                bits.append(f"{key} {n}")
        subtitle = " · ".join(bits) if bits else datetime.now().strftime("%H:%M:%S")

        return Panel(
            Group(*parts),
            title=f"[bold]Predict.fun sports · {self.league.upper()}[/bold]",
            subtitle=subtitle,
            border_style="cyan",
            padding=(0, 1),
        )

    def _ask_panels(self, state, ceiling: float):
        """Crypto-style price cards: live dog ask vs buy target."""
        from rich import box
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        ask = state.fill_ask if state.fill_ask is not None else state.last_ask
        low = state.lowest_ask
        ready = bool(ask and ask > 0 and ask <= ceiling + 1e-12)
        filled = state.status in ("filled", "settled")
        # Crypto-style: green = good/buyable price, red = too expensive
        ask_color = "green" if (ready or filled) else "red" if (ask and ask > 0) else "dim"

        # --- left: live / fill ask (like Up card = green when good) ---
        price = Text(
            _fmt_ask(ask) if ask and ask > 0 else "--",
            style=f"bold {ask_color}",
        )
        odds_line = Text()
        odds_line.append("odds ", style="dim")
        odds_line.append(_fmt_odds(ask), style=f"bold {ask_color}")

        cheap = Text()
        cheap.append("cheapest ", style="dim")
        if low and low > 0:
            low_ready = low <= ceiling + 1e-12
            cheap.append(_fmt_ask(low), style="bold green" if low_ready else "bold red")
            if ask and ask > 0 and abs(ask - low) > 1e-9:
                cheap.append(f"  now {_fmt_ask(ask)}", style=f"bold {ask_color}")
        else:
            cheap.append("--", style="dim")

        status_line = Text()
        if state.status == "filled":
            status_line.append(
                f"[filled] bought {state.contracts:g} @ {_fmt_ask(state.fill_ask)}",
                style="bold green",
            )
        elif state.status == "settled":
            pnl = state.pnl
            style = "bold green" if (pnl or 0) > 0 else "bold red" if (pnl or 0) < 0 else "dim"
            status_line.append(f"[settled] {state.resolution or '?'}  ", style="dim")
            if pnl is not None:
                status_line.append(f"{pnl:+.2f}", style=style)
        elif state.status == "watching":
            if ready:
                status_line.append("[ready] ask at/under target — will buy", style="bold green")
            elif ask and ask > 0:
                gap_c = round((ask - ceiling) * 100)
                status_line.append(f"[open] watching  need −{gap_c}¢ to buy", style="bold red")
            else:
                status_line.append("[open] waiting for book…", style="dim")
        elif state.status == "missed":
            status_line.append("[miss] never hit target before kickoff", style="bold red")

        dog_body = Group(price, odds_line, cheap, status_line)
        dog_panel = Panel(
            dog_body,
            title=f"[bold {ask_color}]DOG +1.5[/]",
            title_align="left",
            border_style="dim",
            box=box.ROUNDED,
            padding=(0, 1),
        )

        # --- right: target card (always the green "buy zone") ---
        target_price = Text(f"≤{_fmt_ask(ceiling)}", style="bold green")
        target_odds = Text()
        target_odds.append("min odds ", style="dim")
        target_odds.append(f"{self.sim.min_odds:.2f}", style="bold green")

        gap_line = Text()
        if ask and ask > 0:
            if ready:
                gap_line.append("in range", style="bold green")
            else:
                gap_c = round((ask - ceiling) * 100)
                gap_line.append("gap ", style="dim")
                gap_line.append(f"+{gap_c}¢", style="bold red")
        else:
            gap_line.append("—", style="dim")

        # proximity bar: green = toward target, red = still too high
        bar = Text()
        if ask and ask > 0 and ceiling > 0:
            span = 0.15  # 15¢ above target → empty
            over = max(0.0, min(span, ask - ceiling))
            filled_cells = int(round((1.0 - over / span) * 20))
            bar.append("████████████████████"[:filled_cells], style="green")
            bar.append("████████████████████"[: 20 - filled_cells], style="red")
        else:
            bar.append("░░░░░░░░░░░░░░░░░░░░", style="dim")

        target_body = Group(target_price, target_odds, gap_line, bar)
        target_panel = Panel(
            target_body,
            title="[bold green]TARGET[/]",
            title_align="left",
            border_style="dim",
            box=box.ROUNDED,
            padding=(0, 1),
        )

        cards = Table.grid(expand=True, padding=(0, 1))
        cards.add_column(ratio=1)
        cards.add_column(ratio=1)
        cards.add_row(dog_panel, target_panel)
        return cards

    def _match_card(self, state, ceiling: float, hero: bool):
        """One fully enclosed card per match: header + ask panels + meta."""
        from rich import box
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        fx = state.fixture
        dog = fx.underdog or "?"
        fav = fx.favourite or "?"
        dog_s = state.strength.dog if state.strength else None
        ask = state.fill_ask if state.fill_ask is not None else state.last_ask
        ready = bool(ask and ask > 0 and ask <= ceiling + 1e-12)

        # --- title row ---
        badge = Text()
        border = "cyan"
        title_style = "cyan"
        if state.status == "watching":
            badge.append("WATCH", style="bold cyan")
            border = "bold cyan" if ready else "cyan"
            title_style = "cyan"
        elif state.status == "filled":
            badge.append("FILLED", style="bold yellow")
            border = "yellow"
            title_style = "yellow"
        elif state.status == "settled":
            won = (state.pnl or 0) > 0
            badge.append("WIN" if won else "LOSS", style="bold green" if won else "bold red")
            border = "green" if won else "red"
            title_style = "green" if won else "red"
        elif state.status == "missed":
            badge.append("MISS", style="bold yellow")
            border = "dim"
            title_style = "yellow"
        else:
            badge.append(state.status.upper(), style="dim")
            border = "dim"

        head = Table.grid(expand=True)
        head.add_column(ratio=1)
        head.add_column(justify="right")
        left = Text()
        left.append_text(badge)
        left.append("  ")
        left.append(_short(dog, 24), style="bold")
        left.append(" +1.5", style="bold cyan")
        right = Text()
        right.append("covers vs ", style="dim")
        right.append(_short(fav, 20), style="bold")
        head.add_row(left, right)

        match_line = Text(
            f"{_short(fx.home.name if fx.home else '', 18)} vs {_short(fx.away.name if fx.away else '', 18)}",
            style="dim",
        )

        # --- meta footer inside the same card ---
        meta = Table.grid(expand=True, padding=(0, 3))
        meta.add_column()
        meta.add_column()
        meta.add_column()

        ko = Text()
        ko.append("Kickoff ", style="dim")
        ko.append(_kickoff_label(fx.kickoff), style="bold")
        if state.status == "watching":
            ko.append(f"   {_countdown(fx.kickoff)} left", style="cyan")

        form = Text()
        form.append("Form ", style="dim")
        if dog_s and dog_s.form:
            form.append_text(_form_cells(dog_s.form))
            form.append(f"  {dog_s.form_points}p", style="dim")
        else:
            form.append("--", style="dim")

        table = Text()
        table.append("Table ", style="dim")
        if dog_s and dog_s.position:
            fav_pos = state.strength.fav.position if state.strength and state.strength.fav else None
            table.append(f"P{dog_s.position}", style="bold")
            if fav_pos:
                table.append(f" · fav P{fav_pos}", style="dim")
        else:
            table.append("--", style="dim")

        meta.add_row(ko, form, table)

        body_parts = [head, match_line, Text(""), self._ask_panels(state, ceiling), Text(""), meta]

        if state.status == "filled":
            body_parts.append(Text(""))
            body_parts.append(Text(
                f"-> Holding {state.contracts:g} @ {_fmt_ask(state.fill_ask)}  "
                f"cost ${state.cost_usd:.2f}  · hold to settle",
                style="bold yellow",
            ))
        elif state.status == "settled" and state.pnl is not None:
            body_parts.append(Text(""))
            style = "bold green" if state.pnl > 0 else "bold red" if state.pnl < 0 else "dim"
            line = Text("-> Settled ", style="dim")
            line.append(f"{state.resolution or '?'}  ", style="bold")
            line.append(f"{state.pnl:+.2f}", style=style)
            body_parts.append(line)

        panel_title = Text()
        panel_title.append(_short(dog, 20), style="bold")
        panel_title.append(" +1.5", style=f"bold {title_style}")

        return Panel(
            Group(*body_parts),
            title=panel_title,
            title_align="left",
            border_style=border,
            box=box.HEAVY if (hero and (ready or state.status == "filled")) else box.ROUNDED,
            padding=(0, 1),
        )

    def _render_wallet(self):
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        sim = self.sim
        summary = sim.summary()
        counts = summary.get("counts") or {}

        in_play = sum(
            s.cost_usd for s in sim.states.values()
            if s.status in ("filled",) and s.cost_usd
        )
        # Mark filled positions at last ask if still open (pre-settle)
        marked = 0.0
        for s in sim.states.values():
            if s.status != "filled" or not s.contracts:
                continue
            ask = s.last_ask if s.last_ask else s.fill_ask
            if ask:
                marked += s.contracts * ask
        unsettled_pnl = marked - in_play if in_play else 0.0
        settled_pnl = summary.get("settled_pnl") or 0.0
        equity = sim.capital + in_play + unsettled_pnl
        # capital already reduced on fill; equity = cash + mark of open fills
        equity = sim.capital + marked
        session = equity - self.start_capital
        self.peak_equity = max(self.peak_equity, equity)
        drawdown = equity - self.peak_equity

        def signed(value, text=None, bold=False):
            style = "green" if value > 0 else "red" if value < 0 else "dim"
            return Text(
                text if text is not None else f"{value:+.2f}",
                style=f"bold {style}" if bold else style,
            )

        grid = Table.grid(padding=(0, 1), expand=True)
        grid.add_column(style="dim")
        grid.add_column(justify="right")
        grid.add_row("Balance", Text(f"${sim.capital:.2f}", style="bold"))
        grid.add_row("In play", Text(f"${in_play:.2f}", style="yellow" if in_play else "dim"))
        for s in sim.states.values():
            if s.status != "filled":
                continue
            fx = s.fixture
            grid.add_row(
                Text(f"  {_short(fx.underdog, 12)} +1.5", style="cyan"),
                Text(f"{s.contracts:g}@{_fmt_ask(s.fill_ask)}", style="yellow"),
            )
        if unsettled_pnl:
            grid.add_row("Unrealized", signed(unsettled_pnl))
        grid.add_row("Settled", signed(settled_pnl) if settled_pnl else Text("0.00", style="dim"))
        grid.add_row("", "")
        grid.add_row(Text("Equity", style="bold"), signed(session, f"${equity:.2f}", bold=True))
        pct = session / self.start_capital * 100 if self.start_capital else 0.0
        grid.add_row("Session", signed(session, f"{session:+.2f} ({pct:+.1f}%)"))
        grid.add_row(
            "Drawdown",
            signed(drawdown, f"{drawdown:+.2f}") if drawdown < 0 else Text("0.00", style="dim"),
        )
        grid.add_row("", "")
        grid.add_row("Watching", Text(str(counts.get("watching", 0)), style="cyan"))
        grid.add_row("Filled", Text(str(counts.get("filled", 0)), style="yellow"))
        grid.add_row("Skipped", Text(str(counts.get("skipped", 0)), style="dim"))

        target = max_ask_for_min_odds(sim.min_odds)
        tip = Text(f"Buy dog +1.5 at ≤{_fmt_ask(target)}", style="dim")

        border = "green" if session > 0 else "red" if session < 0 else "cyan"
        return Panel(Group(grid, Text(""), tip), title="[bold]Wallet[/bold]",
                     border_style=border, padding=(0, 1))

    def _render_history(self):
        from rich import box
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        rows = []
        for state in self.sim.states.values():
            if state.status not in ("settled", "missed", "filled"):
                continue
            rows.append(state)
        rows.sort(key=lambda s: s.fixture.kickoff, reverse=True)

        hist = Table(box=box.SIMPLE, expand=True, pad_edge=False, padding=(0, 1))
        hist.add_column("Match", overflow="ellipsis")
        hist.add_column("Result", justify="right")
        for state in rows[:HISTORY_ROWS]:
            fx = state.fixture
            label = _short(fx.underdog, 14) + " +1.5"
            if state.status == "settled" and state.pnl is not None:
                style = "green" if state.pnl > 0 else "red" if state.pnl < 0 else "dim"
                hist.add_row(label, Text(f"{state.pnl:+.2f}", style=f"bold {style}"))
            elif state.status == "filled":
                hist.add_row(label, Text("open", style="yellow"))
            else:
                hist.add_row(Text(label, style="dim"), Text("miss", style="dim"))

        # streak squares like crypto wallet
        streak = Text()
        settled = [s for s in rows if s.status == "settled" and s.pnl is not None][:15]
        for s in reversed(settled):
            streak.append("■", style="green" if s.pnl > 0 else "red" if s.pnl < 0 else "dim")

        parts = []
        if rows:
            parts.append(hist)
        else:
            parts.append(Text("No fills yet", style="dim"))
        parts.append(Text(""))
        parts.append(Text("Last results", style="dim"))
        parts.append(streak if settled else Text("--", style="dim"))
        return Panel(Group(*parts), title="[bold]Activity[/bold]", border_style="cyan", padding=(0, 1))


def _compress_gate(raw: str) -> str:
    text = raw.strip()
    if "table_weak" in text and "form_weak" in text:
        return "weak table + form"
    if "table_weak" in text:
        return "weak table"
    if "form_weak" in text:
        return "weak form"
    if "top_half" in text or "within_" in text:
        return "gates ok?"
    return text[:48]
