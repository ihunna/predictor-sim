"""6-hour hourly ATR for scalp macro vol gate (Binance 1h high−low mean)."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

BINANCE_HOSTS = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
)

COIN_SYMBOLS = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "bnb": "BNBUSDT",
}

# Rolling window of completed 1h candles used for the scalp ATR gate.
ATR_HOURS = 6


def binance_symbol(coin: str) -> str:
    return COIN_SYMBOLS.get((coin or "btc").strip().lower(), "BTCUSDT")


def atr_from_ranges(ranges_usd: list[float]) -> float | None:
    """Mean of hourly high−low ranges; None if empty."""
    if not ranges_usd:
        return None
    return sum(ranges_usd) / len(ranges_usd)


# Back-compat alias used by older tests/imports.
atr_24h_from_ranges = atr_from_ranges


def fetch_hourly_ranges(symbol: str = "BTCUSDT", hours: int = ATR_HOURS, timeout: float = 10.0) -> list[float]:
    """Completed 1h candle (high−low) for the last ``hours`` hours from Binance."""
    limit = max(1, int(hours))
    last_err: Exception | None = None
    for host in BINANCE_HOSTS:
        url = f"{host}/api/v3/klines?symbol={symbol}&interval=1h&limit={limit}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                data = json.load(resp)
            now_ms = time.time() * 1000
            ranges = []
            for k in data:
                # k[6] = close time; skip still-open candle
                if k[6] >= now_ms:
                    continue
                high, low = float(k[2]), float(k[3])
                ranges.append(high - low)
            return ranges[-limit:] if ranges else ranges
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, IndexError) as e:
            last_err = e
            continue
    if last_err:
        raise last_err
    return []


def fetch_atr(coin: str = "btc", hours: int = ATR_HOURS) -> float | None:
    ranges = fetch_hourly_ranges(binance_symbol(coin), hours=hours)
    return atr_from_ranges(ranges)


fetch_atr_24h = fetch_atr  # back-compat


class HourlyAtr:
    """Thread-safe cache of rolling hourly ATR; seed/refresh from Binance."""

    def __init__(self, coin: str = "btc", hours: int = ATR_HOURS, refresh_seconds: float = 3600.0):
        self.coin = (coin or "btc").strip().lower()
        self.hours = int(hours)
        self.refresh_seconds = float(refresh_seconds)
        self._lock = threading.Lock()
        self._atr: float | None = None
        self._fetched_at: float = 0.0
        self._error: str = ""

    @property
    def atr(self) -> float | None:
        with self._lock:
            return self._atr

    @property
    def ready(self) -> bool:
        return self.atr is not None

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._error

    def set_atr(self, value: float | None) -> None:
        """Test/inject hook."""
        with self._lock:
            self._atr = None if value is None else float(value)
            self._fetched_at = time.time()
            self._error = ""

    def refresh(self, force: bool = False) -> float | None:
        with self._lock:
            age = time.time() - self._fetched_at
            if not force and self._atr is not None and age < self.refresh_seconds:
                return self._atr
        try:
            value = fetch_atr(self.coin, hours=self.hours)
            with self._lock:
                self._atr = value
                self._fetched_at = time.time()
                self._error = "" if value is not None else "empty_klines"
            return value
        except Exception as e:
            with self._lock:
                self._error = str(e)
            return self.atr

    def seed_background(self) -> None:
        threading.Thread(target=self.refresh, kwargs={"force": True}, daemon=True).start()


# Back-compat alias for run.py / older imports.
HourlyAtr24h = HourlyAtr
