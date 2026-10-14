"""Status-line formatting and the 6-column updown log, copied from the Polymarket bot."""
import csv
import os
import sys
import time
from datetime import datetime

MARKET_CSV_HEADER = [
    "Time stamp",
    "price_to_beat",
    "final_price",
    "lowest_up",
    "lowest_down",
    "outcome",
]


GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"


def color_signed(value, text):
    """Green when ``value`` > 0, red when < 0, plain at 0."""
    if value > 0:
        return f"{GREEN}{text}{RESET}"
    if value < 0:
        return f"{RED}{text}{RESET}"
    return text


def print_line(message):
    """Prints a full line over the in-place status line instead of appending to it."""
    sys.stdout.write("\r\033[K" + message + "\n")
    sys.stdout.flush()


def format_time(seconds):
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins:02d}:{secs:02d}"


def format_dollar(price):
    return f"${price:.2f}" if price else "--"


def format_ticker(label, price, price_to_beat):
    if price <= 0:
        return f"{label}: --"
    if price_to_beat <= 0:
        return f"{label}: {format_dollar(price)} (--)"
    move = price - price_to_beat
    return f"{label}: {format_dollar(price)} ({color_signed(move, f'{move:+.2f}')})"


def format_token_cents(raw):
    if raw <= 0 or raw > 1.0:
        return "0¢"
    return f"{round(raw * 100)}¢"


def format_lowest_cents(lowest_seen):
    if lowest_seen != float('inf') and lowest_seen > 0:
        return f"{round(lowest_seen * 100)}¢"
    return "N/A"


def update_window_progress(duration, window_start, window_end, formatted_message):
    """Updates a single persistent line showing window progress and token ask data."""
    now = time.time()
    elapsed = now - window_start
    remaining = window_end - now
    if remaining < 0:
        remaining = 0

    total = max(1, window_end - window_start)
    progress = max(0.0, min(1.0, elapsed / total))
    filled_blocks = int(progress * 20)
    bar = "█" * filled_blocks + "-" * (20 - filled_blocks)

    start_dt = datetime.fromtimestamp(window_start)
    window_label = f"{duration}m {start_dt.strftime('%H:%M')} window"
    time_str = format_time(remaining)

    # Pad to clear leftover chars from longer previous lines
    line = f"{window_label} [{bar}] {time_str} remaining | {formatted_message}"
    sys.stdout.write("\r" + line + " " * 12)
    sys.stdout.flush()


def log_to_csv(filename, timestamp, price_to_beat, final_price, lowest_up, lowest_down, outcome):
    """Appends one window's result to the updown file, unless that window is already in it (several bots of the
    same coin and duration share the file)."""
    file_exists = os.path.isfile(filename)
    if file_exists:
        with open(filename, newline="", encoding="utf-8") as f:
            if any(row and row[0] == str(timestamp) for row in csv.reader(f)):
                return
    with open(filename, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(MARKET_CSV_HEADER)
        writer.writerow([
            timestamp,
            format_dollar(price_to_beat) if price_to_beat else "",
            format_dollar(final_price) if final_price else "",
            lowest_up,
            lowest_down,
            outcome,
        ])
