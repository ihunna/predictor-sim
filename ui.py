"""Terminal output: a live Rich dashboard (default) or plain lines (--plain, for nohup/log files).

Both classes take the same calls from run.py."""
import threading
import time
from datetime import datetime

from logs import (
    GREEN,
    RED,
    RESET,
    color_signed,
    format_dollar,
    format_lowest_cents,
    format_ticker,
    format_time,
    format_token_cents,
    print_line,
    update_window_progress,
)

HISTORY_ROWS = 12
SIDEBAR_WIDTH = 44
BOOK_GAP = 3
BOOK_BAR_SHARE = 0.35
EVENT_ROWS = 4


def hhmm(epoch):
    return datetime.fromtimestamp(epoch).strftime("%H:%M")


def stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def move_pct(start, end):
    return (end - start) / start * 100 if start and end else None


def skip_text(state):
    reasons = [n for n in state.notes if n != "not_traded"]
    return ", ".join(reasons) or "skipped"


def skip_label(state):
    """Short reason for the dashboard: no PTB, prev move, joined late."""
    for note in state.notes:
        if note == "no_ptb":
            return "no PTB"
        if note == "prev_move_unknown":
            return "last move unknown"
        if note.startswith("prev_move_"):
            return "prev move"
        if note.startswith("joined_late_"):
            return "joined late"
    return "skipped"


# --- plain lines ---

def _color_outcome(outcome):
    if outcome == "Up":
        return f"{GREEN}Up{RESET}"
    if outcome == "Down":
        return f"{RED}Down{RESET}"
    return outcome


def _format_pnl(pnl):
    return color_signed(pnl, f"{pnl:+.2f}") if pnl is not None else "n/a"


def _shares_short(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{n:.0f}"


def _side_line(state, label):
    lowest = state.lowest_up if label == "Up" else state.lowest_down
    lowest_left = state.lowest_up_time_left if label == "Up" else state.lowest_down_time_left
    if not state.trading:
        fill = "not traded"
    else:
        parts = []
        for o in state.side_orders(label):
            tag = {"hedge": " lock-in", "profit": " extra"}.get(o.kind, "")
            if o.filled:
                parts.append(f"✅ {o.contracts} @ {round(o.fill_price * 100)}¢{tag} at {format_time(o.fill_time_left)} "
                             f"left" + (" (taker)" if o.taker else ""))
            elif state.cancelled_time_left is not None:
                parts.append(f"🛑 {o.contracts} @ {round(o.price * 100)}¢ cancelled at "
                             f"{format_time(state.cancelled_time_left)} left")
            else:
                parts.append(f"⏳ {o.contracts} @ {round(o.price * 100)}¢{tag} not filled")
        if state.stop_side == label:
            parts.append(f"🛑 sold {state.stop_contracts} @ {round(state.stop_price * 100)}¢ (stop-loss) at "
                         f"{format_time(state.stop_time_left)} left")
        fill = " + ".join(parts) or "never placed"
    low_at = f" at {format_time(lowest_left)} left" if lowest_left is not None else ""
    return f"   {label:<5} {fill:<34} | low {format_lowest_cents(lowest)}{low_at}"


PENDING_MAX_SECONDS = 15


class PlainUI:
    """While a window's result is pending, the next window's lines are held back so the result prints first."""

    def __init__(self, sim, coin, duration):
        self.sim = sim
        self.duration = duration
        self.lock = threading.Lock()
        self.pending_since = None
        self.held = []

    def start(self):
        pass

    def stop(self):
        self._release()

    def _emit(self, message):
        with self.lock:
            if self.pending_since is not None:
                self.held.append(message)
                return
        print_line(message)

    def _release(self):
        with self.lock:
            held, self.held = self.held, []
            self.pending_since = None
        for message in held:
            print_line(message)

    def event(self, message):
        self._emit(message)

    def settlement_pending(self, state):
        with self.lock:
            self.pending_since = time.time()

    def window_started(self, state, title, slug, market_id, ptb, prev_move, skip_message):
        self._emit(f"\n[{stamp()}] 🚀 {self.duration}m window started: {title} (slug {slug}, market {market_id})")
        self._emit(f"📊 Price to Beat (Baseline): {format_dollar(ptb) if ptb > 0 else '--'}")
        if skip_message:
            self._emit(f"⏸️  {skip_message}")

    def prev_move_known(self, state, prev_move, skip_message):
        prev_text = color_signed(prev_move, f"{prev_move:+.3f}%") if prev_move is not None else "unknown"
        if skip_message:
            self._emit(f"⏸️  {skip_message}")
        elif state.trading:
            self._emit(f"📈 Last window moved {prev_text}: placing both {self.sim.limit_cents}¢ limits.")
        else:
            self._emit(f"📈 Last window moved {prev_text}.")

    def tick(self, state, up_ask, down_ask, ptb, price, socket_info):
        with self.lock:
            pending_since = self.pending_since
        if pending_since is not None:
            if time.time() - pending_since < PENDING_MAX_SECONDS:
                return
            self._release()
        display_str = (
            f"PTB: {format_dollar(ptb) if ptb else '--'} | {format_ticker('Chainlink', price, ptb)} | "
            f"Up: {format_token_cents(up_ask)} (low {format_lowest_cents(state.lowest_up)}) | "
            f"Down: {format_token_cents(down_ask)} (low {format_lowest_cents(state.lowest_down)}) | "
            f"{self.sim.display_status()}"
        )
        update_window_progress(self.duration, state.window_start, state.window_end, display_str)

    def window_ended(self, state, outcome, start_price, end_price, pnl, source):
        if not state.trading:
            result = f"not traded ({skip_text(state)})"
        else:
            result = f"fills {state.fill_type} | pnl {_format_pnl(pnl)}"
            if pnl is not None:
                result += f" | capital ≈ ${self.sim.capital + pnl:.2f}"
        pct = move_pct(start_price, end_price)
        move = (
            color_signed(end_price - start_price, f"{end_price - start_price:+.2f} ({pct:+.3f}%)")
            if pct is not None else "--"
        )
        sep = "─" * 72
        prices = ("Predict.fun's official start/end prices" if source == "official"
                  else "Predict.fun's resolution (official prices not pushed)")
        print_line(sep)
        print_line(
            f"🏁 {self.duration}m {hhmm(state.window_start)} window ended: {_color_outcome(outcome)} | "
            f"{format_dollar(start_price)} → {format_dollar(end_price)} {move}"
        )
        print_line(_side_line(state, "Up"))
        print_line(_side_line(state, "Down"))
        print_line(f"   Result: {result}")
        print_line(f"   (from {prices})")
        print_line(sep)
        self._release()

    def confirmed(self, state, outcome, live_outcome, pnl, capital, source):
        if outcome not in ("Up", "Down", "Tie"):
            head = f"⚠️ {hhmm(state.window_start)} no result from {source} (no official prices, not resolved yet)"
        elif outcome == live_outcome:
            head = f"✅ {hhmm(state.window_start)} confirmed by {source}: {_color_outcome(outcome)}"
        else:
            head = (
                f"⚠️ {hhmm(state.window_start)} result from {source} is {_color_outcome(outcome)}, "
                f"first read said {_color_outcome(live_outcome)}"
            )
        result = f"pnl {_format_pnl(pnl)}" if state.trading else "not traded"
        self._emit(f"{head} | {result} | capital ${capital:.2f} | logged")


# --- Rich dashboard ---

class Dashboard:
    def __init__(self, sim, coin, duration):
        from rich.console import Console

        self.sim = sim
        self.coin = coin
        self.duration = duration
        self.console = Console()
        self.lock = threading.Lock()
        self.start_capital = sim.capital
        self.live = None
        self.state = None
        self.title = ""
        self.ptb = 0.0
        self.price = 0.0
        self.prev_move = None
        self.up_ask = 0.0
        self.down_ask = 0.0
        self.socket_info = {}
        self.history = []
        self.events = []

    def start(self):
        from rich.live import Live

        self.live = Live(get_renderable=self.render, console=self.console, refresh_per_second=4,
                         redirect_stdout=True, redirect_stderr=True, screen=True)
        self.live.start()

    def stop(self):
        if self.live is not None:
            self.live.stop()

    # updates from run.py

    def event(self, message):
        with self.lock:
            self.events.append(f"{datetime.now().strftime('%H:%M:%S')}  {message.strip()}")
            del self.events[:-EVENT_ROWS]

    def window_started(self, state, title, slug, market_id, ptb, prev_move, skip_message):
        with self.lock:
            self.state = state
            self.title = title
            self.ptb = ptb
            self.prev_move = prev_move
            self.up_ask = self.down_ask = 0.0
            self.events = []
        if skip_message:
            self.event(f"{hhmm(state.window_start)}: {skip_message}")

    def prev_move_known(self, state, prev_move, skip_message):
        with self.lock:
            if self.state is state:
                self.prev_move = prev_move
        if skip_message:
            self.event(f"{hhmm(state.window_start)}: {skip_message}")

    def tick(self, state, up_ask, down_ask, ptb, price, socket_info):
        with self.lock:
            self.state = state
            self.up_ask, self.down_ask = up_ask, down_ask
            self.ptb, self.price = ptb, price
            self.socket_info = socket_info

    def _row(self, window_start):
        for row in self.history:
            if row["window_start"] == window_start:
                return row
        return None

    def settlement_pending(self, state):
        with self.lock:
            self.history.insert(0, {
                "window_start": state.window_start,
                "outcome": "…",
                "move": None,
                "trading": state.trading,
                "skip": skip_label(state) if not state.trading else "",
                "fill_type": state.fill_type,
                "pnl": None,
                "status": "settling",
            })
            del self.history[HISTORY_ROWS * 4:]

    def window_ended(self, state, outcome, start_price, end_price, pnl, source):
        with self.lock:
            row = self._row(state.window_start)
            if row is None:
                return
            row.update(outcome=outcome, move=move_pct(start_price, end_price), pnl=pnl,
                       status="official" if source == "official" else "live")

    def confirmed(self, state, outcome, live_outcome, pnl, capital, source):
        with self.lock:
            for row in self.history:
                if row["window_start"] == state.window_start:
                    row["outcome"] = outcome
                    row["pnl"] = pnl
                    if outcome not in ("Up", "Down", "Tie"):
                        row["status"] = "no result"
                    else:
                        row["status"] = "confirmed" if outcome == live_outcome else "differs"
                    break

    # rendering

    def render(self):
        from rich import box
        from rich.console import Group
        from rich.panel import Panel
        from rich.progress_bar import ProgressBar
        from rich.table import Table
        from rich.text import Text

        with self.lock:
            state = self.state
            history = [dict(r) for r in self.history]
            events = list(self.events)
            up_ask, down_ask = self.up_ask, self.down_ask
            ptb, price, prev_move = self.ptb, self.price, self.prev_move
            socket_info = dict(self.socket_info)
            title_text = self.title

        sim = self.sim
        now = time.time()
        parts = []

        # window + price
        if state is not None:
            total = max(1, state.window_end - state.window_start)
            elapsed = min(total, max(0, now - state.window_start))
            remaining = max(0, state.window_end - now)
            window = Table.grid(padding=(0, 2))
            window.add_row(
                Text(f"Window {hhmm(state.window_start)}–{hhmm(state.window_end)}", style="bold"),
                ProgressBar(total=total, completed=elapsed, width=36),
                Text(f"{format_time(remaining)} left", style="bold cyan"),
            )
            parts.append(window)
            title_line = Text()
            title_line.append(title_text, style="dim")
            title_line.append("   last window ", style="dim")
            if prev_move is None:
                title_line.append("--", style="dim")
            else:
                title_line.append(f"{prev_move:+.3f}%",
                                  style="bold green" if prev_move > 0 else "bold red" if prev_move < 0 else "bold")
            parts.append(title_line)

            price_line = Text()
            price_line.append("PTB ", style="dim")
            price_line.append(format_dollar(ptb) if ptb else "--", style="bold")
            price_line.append("    Chainlink ", style="dim")
            price_line.append(format_dollar(price) if price else "--", style="bold")
            if ptb and price:
                move = price - ptb
                style = "bold green" if move > 0 else "bold red" if move < 0 else "bold"
                arrow = "▲" if move > 0 else "▼" if move < 0 else "•"
                price_line.append(f"   {arrow} {move:+.2f} ({move / ptb * 100:+.3f}%)", style=style)
            parts.append(price_line)

            if not state.trading:
                parts.append(Text(f"⏸  Not trading this window: {skip_text(state)}", style="bold yellow"))

            def clock(time_left):
                return datetime.fromtimestamp(state.window_end - time_left).strftime("%H:%M:%S")

            parts.append(Text(""))
            if up_ask > 0 and down_ask > 0:
                up_share = up_ask / (up_ask + down_ask)
                bar_width = 40
                up_cells = round(up_share * bar_width)
                chance = Text()
                chance.append(f"Up {up_share * 100:.0f}% ", style="bold green")
                chance.append("█" * up_cells, style="green")
                chance.append("█" * (bar_width - up_cells), style="red")
                chance.append(f" {100 - up_share * 100:.0f}% Down", style="bold red")
                parts.append(chance)

            cancel_at = state.window_end - sim.cancel_left_seconds() if sim.cancel_left_pct > 0 else None
            cards = Table.grid(expand=True, padding=(0, 1))
            cards.add_column(ratio=1)
            cards.add_column(ratio=1)
            row = []
            for label, arrow, ask, low, low_left, color in (
                ("Up", "▲", up_ask, state.lowest_up, state.lowest_up_time_left, "green"),
                ("Down", "▼", down_ask, state.lowest_down, state.lowest_down_time_left, "red"),
            ):
                order_lines = []
                if not state.trading:
                    order_lines.append(Text("not trading", style="dim"))
                elif state.decision_pending:
                    order_lines.append(Text("checking last window…", style="dim"))
                elif state.placed_time_left is None:
                    order_lines.append(Text("not placed yet", style="dim"))
                else:
                    for o in state.side_orders(label):
                        tag = {"hedge": " lock-in", "profit": " extra"}.get(o.kind, "")
                        if o.filled:
                            order_lines.append(Text(
                                f"✅ bought {o.contracts} @ {round(o.fill_price * 100)}¢{tag} at "
                                f"{clock(o.fill_time_left)}" + (" (instant)" if o.taker else ""),
                                style="bold green"))
                        elif state.cancelled_time_left is not None:
                            order_lines.append(Text(
                                f"🛑 cancelled at {clock(state.cancelled_time_left)} (nothing filled)",
                                style="bold yellow"))
                        elif state.stop_time_left is not None:
                            order_lines.append(Text(f"🛑 {o.contracts} @ {round(o.price * 100)}¢ cancelled (stop-loss)",
                                                    style="dim"))
                        else:
                            line = Text(f"⏳ buying {o.contracts} @ {round(o.price * 100)}¢{tag}", style="yellow")
                            if cancel_at and state.fill_type == "none":
                                line.append(f"  (cancel {datetime.fromtimestamp(cancel_at).strftime('%H:%M:%S')})",
                                            style="dim")
                            order_lines.append(line)
                if state.stop_side == label:
                    order_lines.append(Text(
                        f"🛑 sold {state.stop_contracts} @ {round(state.stop_price * 100)}¢ (stop-loss) at "
                        f"{clock(state.stop_time_left)}", style="bold red"))
                if state.would_stop and state.would_stop[0] == label:
                    side_, n_, bid_, left_ = state.would_stop
                    order_lines.append(Text(
                        f"stop-loss would sell {n_} @ {round(bid_ * 100)}¢ at {clock(left_)} (paused, logged)",
                        style="dim"))
                filled_left = state.first_fill_time_left(label)
                cheapest = Text()
                cheapest.append("cheapest ", style="dim")
                cheapest.append(format_lowest_cents(low), style="bold")
                if low_left is not None:
                    cheapest.append(" at ", style="dim")
                    cheapest.append(clock(low_left), style="bold")
                price_text = Text(format_token_cents(ask) if ask > 0 else "--", style=f"bold {color}")
                body = Group(price_text, cheapest, *order_lines)
                filled = state.trading and filled_left is not None
                row.append(Panel(
                    body,
                    title=f"[bold {color}]{arrow} {label.upper()}[/]",
                    title_align="left",
                    border_style=f"bold {color}" if filled else color if state.trading else "dim",
                    box=box.HEAVY if filled else box.ROUNDED,
                    padding=(0, 1),
                ))
            cards.add_row(*row)
            parts.append(cards)

            if state.trading and state.fill_type != "none":
                _, if_up = sim.compute_result(state, "Up", state.maker_fee_bps, state.taker_fee_bps)
                _, if_down = sim.compute_result(state, "Down", state.maker_fee_bps, state.taker_fee_bps)
                summary = Text("→ ", style="bold")
                summary.append(f"Holding Up {state.held('Up')} · Down {state.held('Down')}: ", style="bold")
                if abs(if_up - if_down) < 0.005:
                    summary.append(f"{if_up:+.2f}", style="bold green" if if_up >= 0 else "bold red")
                    summary.append(" whoever wins", style="bold")
                else:
                    summary.append("if Up wins ")
                    summary.append(f"{if_up:+.2f}", style="bold green" if if_up >= 0 else "bold red")
                    summary.append(" · if Down wins ")
                    summary.append(f"{if_down:+.2f}", style="bold green" if if_down >= 0 else "bold red")
                parts.append(summary)
        else:
            parts.append(Text("Waiting for the first window...", style="dim"))

        # socket + events
        connected = socket_info.get("connected")
        age = socket_info.get("book_age")
        footer = Text()
        footer.append("● ", style="green" if connected else "red")
        footer.append("connected" if connected else "disconnected", style="dim")
        footer.append(f"   book {age:.1f}s old" if age is not None else "   no book yet", style="dim")
        footer.append("   next market ready" if socket_info.get("next_ready") else "", style="dim")
        parts.append(footer)
        for line in events:
            parts.append(Text(line, style="dim"))

        skip = f"skip ≥{sim.max_prev_move_pct:.2f}%" if sim.max_prev_move_pct > 0 else "no filter"
        header = (f"Predict.fun dual · {self.coin.upper()} {self.duration}m · {sim.limit_cents}¢ × {sim.contracts} "
                  f"· {skip}")
        main = Panel(Group(*parts), title=f"[bold]{header}[/bold]", subtitle=datetime.now().strftime("%H:%M:%S"),
                     border_style="cyan", padding=(0, 1))

        book = self.render_book(state, up_ask, down_ask)
        wallet = self.render_wallet(state, up_ask, down_ask, history)
        past = self.render_history(history)
        if self.console.width < 120:
            return Group(main, book, wallet, past)
        layout = Table.grid(expand=True)
        layout.add_column(ratio=1)
        layout.add_column(width=SIDEBAR_WIDTH)
        layout.add_row(Group(main, book), Group(wallet, past))
        return layout

    def render_book(self, state, up_ask, down_ask):
        """Live order book for Up and Down side by side, Polymarket style: asks (red) above the spread, bids (blue)
        below, Total = running dollars from the spread outward, shaded bar behind each row sized by that total."""
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        header = Text()
        header.append("Volume ", style="dim")
        volume = state.volume_live if state is not None else None
        header.append(f"${volume:,.0f}" if volume is not None else "--", style="bold")
        header.append(" traded this window", style="dim")

        levels = state.book_now if state is not None else None
        if not levels:
            body = Group(header, Text("No order book yet", style="dim"))
            return Panel(body, title="[bold]Order book[/bold]", border_style="cyan", padding=(0, 1))

        column = self.console.width - SIDEBAR_WIDTH if self.console.width >= 120 else self.console.width
        width = max(30, (column - 4 - BOOK_GAP) // 2)
        ask_rows = max(len(side["asks"]) for side in levels.values())

        def line(price, shares, total, text_style="", bar=0, bar_style=""):
            price_w = 8
            total_w = 12
            shares_w = width - price_w - total_w
            body = f"{price:<{price_w}}{shares:>{shares_w - 4}}    {total:>{total_w}}"
            text = Text(body[:width].ljust(width))
            if text_style:
                text.stylize(text_style, 0, price_w)
            if bar:
                text.stylize(bar_style, 0, min(width, bar))
            return text

        def money(value):
            return f"${value:,.2f}" if value < 100 else f"${value:,.0f}"

        def side_lines(label, color, book):
            asks, bids = book["asks"], book["bids"]
            ask_totals, bid_totals, running = [], [], 0.0
            for price, size in asks:
                running += price * size
                ask_totals.append(running)
            running = 0.0
            for price, size in bids:
                running += price * size
                bid_totals.append(running)
            biggest = max(ask_totals + bid_totals + [1.0])

            lines = [Text(label, style=f"bold {color}"), line("Price", "Shares", "Total", "dim")]
            lines[-1].stylize("dim")
            lines.extend(Text(" " * width) for _ in range(ask_rows - len(asks)))
            for (price, size), total in reversed(list(zip(asks, ask_totals))):
                lines.append(line(f"{round(price * 100)}¢", f"{size:,.1f}", money(total), "bold #f0616d",
                                  round(total / biggest * width * BOOK_BAR_SHARE), "on #3b1f26"))
            if asks and bids:
                middle = f"Spread: {round((asks[0][0] - bids[0][0]) * 100)}¢"
            else:
                middle = "one-sided"
            lines.append(Text(middle.center(width), style="dim"))
            for (price, size), total in zip(bids, bid_totals):
                lines.append(line(f"{round(price * 100)}¢", f"{size:,.1f}", money(total), "bold #4c9aff",
                                  round(total / biggest * width * BOOK_BAR_SHARE), "on #1b2b40"))
            return Group(*lines)

        sides = Table.grid(padding=(0, BOOK_GAP))
        sides.add_column(width=width)
        sides.add_column(width=width)
        sides.add_row(side_lines("▲ Up", "green", levels["up"]), side_lines("▼ Down", "red", levels["down"]))
        return Panel(Group(header, sides), title="[bold]Order book[/bold]", border_style="cyan", padding=(0, 1))

    def render_history(self, history):
        """Sidebar: finished windows, newest first, plus totals."""
        from rich import box
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        hist = Table(box=box.SIMPLE, expand=True, pad_edge=False, padding=(0, 1))
        for col, justify in (("Time", "left"), ("Winner", "left"), ("Bought", "left"), ("Profit", "right"),
                             ("Final", "center")):
            hist.add_column(col, justify=justify)
        for row in history[:HISTORY_ROWS]:
            outcome = row["outcome"]
            outcome_style = "green" if outcome == "Up" else "red" if outcome == "Down" else "yellow"
            if row["trading"]:
                bought = {"both": "Up+Down", "only_up": "Up", "only_down": "Down", "none": "nothing"}.get(
                    row["fill_type"], row["fill_type"])
                pnl = row["pnl"]
                pnl_text = Text(f"{pnl:+.2f}", style="bold green" if pnl > 0 else "bold red" if pnl < 0 else "dim") \
                    if pnl is not None else Text("…", style="dim")
            else:
                bought = Text(f"skip: {row['skip']}", style="dim")
                pnl_text = Text("")
            status = row["status"]
            if status == "confirmed":
                mark = Text("✅")
            elif status in ("settling", "official", "live"):
                mark = Text("⏳")
            else:
                mark = Text("⚠️", style="bold yellow")
            hist.add_row(hhmm(row["window_start"]), Text(outcome, style=outcome_style), bought, pnl_text, mark)

        traded = [r for r in history if r["trading"] and r["pnl"] is not None
                  and r["status"] not in ("settling", "official", "live")]
        both = sum(1 for r in traded if r["fill_type"] == "both")
        one = [r for r in traded if r["fill_type"] in ("only_up", "only_down")]
        one_wins = sum(1 for r in one if r["pnl"] > 0)
        skipped = sum(1 for r in history if not r["trading"])

        parts = []
        if history:
            parts.append(hist)
        else:
            parts.append(Text("No windows finished yet", style="dim"))
        parts.append(Text(f"Traded {len(traded)} · Both {both} · One side {len(one)} "
                          f"({one_wins}W/{len(one) - one_wins}L) · Skipped {skipped}", style="dim"))
        return Panel(Group(*parts), title="[bold]Past windows[/bold]", border_style="cyan", padding=(0, 1))

    def render_wallet(self, state, up_ask, down_ask, history):
        """Sidebar: balance, the open position marked at the current bid, pending results, equity."""
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        sim = self.sim

        def signed(value, text=None, bold=False):
            style = "green" if value > 0 else "red" if value < 0 else "dim"
            return Text(text if text is not None else f"{value:+.2f}", style=f"bold {style}" if bold else style)

        cost = 0.0
        value = 0.0
        position = []
        if state is not None and state.trading:
            up_bid = 1.0 - down_ask if down_ask > 0 else 0.0
            down_bid = 1.0 - up_ask if up_ask > 0 else 0.0
            for label, bid in (("Up", up_bid), ("Down", down_bid)):
                bought = state.contracts(label)
                if not bought:
                    continue
                n = state.held(label)
                avg = state.avg_price(label)
                cost += state.cost(label)
                value += bid * n
                if state.stop_side == label:
                    value += state.stop_price * state.stop_contracts
                if n:
                    position.append((label, n, avg, bid, (bid - avg) * n))

        pending = [r for r in history
                   if r["status"] in ("official", "live") and r["trading"] and r["pnl"] is not None]
        pending_pnl = sum(r["pnl"] for r in pending)
        equity = sim.capital + pending_pnl + (value - cost)
        self.peak_equity = max(getattr(self, "peak_equity", self.start_capital), equity)
        drawdown = equity - self.peak_equity
        session = equity - self.start_capital

        grid = Table.grid(padding=(0, 1), expand=True)
        grid.add_column(style="dim")
        grid.add_column(justify="right")
        grid.add_row("Balance", Text(f"${sim.capital:.2f}", style="bold"))
        grid.add_row("In play", Text(f"${cost:.2f}", style="yellow" if cost else "dim"))
        for label, n, price, bid, unrealized in position:
            grid.add_row(
                Text(f"  {label} {n} @ {price * 100:.0f}¢→{round(bid * 100)}¢",
                     style="green" if label == "Up" else "red"),
                signed(unrealized),
            )
        if pending:
            grid.add_row(f"Pending ({len(pending)})", signed(pending_pnl))
        grid.add_row("", "")
        grid.add_row(Text("Equity", style="bold"), signed(session, f"${equity:.2f}", bold=True))
        pct = session / self.start_capital * 100 if self.start_capital else 0.0
        grid.add_row("Session", signed(session, f"{session:+.2f} ({pct:+.1f}%)"))
        grid.add_row("Drawdown", signed(drawdown, f"{drawdown:+.2f}") if drawdown < 0 else Text("0.00", style="dim"))

        recent = [r for r in history if r["trading"] and r["pnl"] is not None][:15]
        streak = Text()
        for r in reversed(recent):
            streak.append("■", style="green" if r["pnl"] > 0 else "red" if r["pnl"] < 0 else "dim")
        parts = [grid, Text(""), Text("Last results", style="dim"), streak if recent else Text("--", style="dim")]
        border = "green" if session > 0 else "red" if session < 0 else "cyan"
        return Panel(Group(*parts), title="[bold]Wallet[/bold]", border_style=border, padding=(0, 1))
