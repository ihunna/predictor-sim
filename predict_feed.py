"""Predict.fun data: GraphQL lookups (market per window, result, Chainlink bars) and one live WebSocket
(order book asks + Chainlink price). No API key; the site's endpoints only need its Origin header."""
import calendar
import json
import sys
import threading
import time
import zoneinfo
from collections import deque
from datetime import datetime

import requests
import websocket

GRAPHQL_URL = "https://graphql.predict.fun/graphql"
WS_URL = "wss://ws.predict.fun/ws"
ORIGIN = "https://predict.fun"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)
GRAPHQL_HEADERS = {
    "origin": ORIGIN,
    "referer": ORIGIN + "/",
    "content-type": "application/json",
    "accept": "application/graphql-response+json, application/json",
    "user-agent": USER_AGENT,
}

SUPPORTED_DURATIONS = (5, 15, 60)
COIN_FULL_NAMES = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana", "bnb": "bnb"}
CHAINLINK_FEED_IDS = {"btc": 1, "eth": 2, "bnb": 3}

PRICE_HISTORY_SECONDS = 3900
PRICE_STALE_SECONDS = 15
PRICE_BOUNDARY_TOLERANCE_SECONDS = 10
MARKET_WATCH_SECONDS = 180
WS_QUIET_SECONDS = 40
WS_CONNECT_TIMEOUT_SECONDS = 60


# --- window / slug (same scheme as the Polymarket bot) ---

def window_bounds(duration, at=None):
    """(start, end) epoch seconds of the window containing ``at`` (now if None)."""
    now_utc = calendar.timegm(time.gmtime()) if at is None else int(at)
    if duration == 60:
        et_zone = zoneinfo.ZoneInfo("America/New_York")
        start_of_hour_et = datetime.fromtimestamp(now_utc, et_zone).replace(minute=0, second=0, microsecond=0)
        start = int(start_of_hour_et.timestamp())
        return start, start + 3600
    seconds = duration * 60
    start = (now_utc // seconds) * seconds
    return start, start + seconds


def get_current_active_slug(coin, duration, at=None):
    """Computes the exact slug for short-duration or hourly markets (Predict.fun uses Polymarket's URL scheme)."""
    if duration == 60:
        et_zone = zoneinfo.ZoneInfo("America/New_York")
        now_et = datetime.now(et_zone) if at is None else datetime.fromtimestamp(at, et_zone)

        month_name = now_et.strftime("%B").lower()
        day = now_et.strftime("%d").lstrip("0")
        year = now_et.strftime("%Y")

        hour_12 = now_et.strftime("%I").lstrip("0")
        ampm = now_et.strftime("%p").lower()

        full_coin_name = COIN_FULL_NAMES.get(coin, coin)
        return f"{full_coin_name}-up-or-down-{month_name}-{day}-{year}-{hour_12}{ampm}-et"

    window_start, _ = window_bounds(duration, at)
    return f"{coin}-updown-{duration}m-{window_start}"


# --- GraphQL ---

def graphql(query, variables=None, timeout=5, operation=None):
    """Returns the ``data`` dict, or None on any failure."""
    payload = {"query": query, "variables": variables or {}}
    if operation:
        payload["operationName"] = operation
    try:
        resp = requests.post(
            GRAPHQL_URL,
            json=payload,
            headers=GRAPHQL_HEADERS,
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        body = resp.json()
        if body.get("errors"):
            return None
        return body.get("data")
    except Exception:
        return None


CATEGORY_QUERY = """query GetCategory($categoryId: ID!) {
  category(id: $categoryId) {
    numericId
    status
    title
    markets { edges { node { id } } }
  }
}"""

MARKET_QUERY = """query GetMarket($marketId: ID!) {
  market(id: $marketId) {
    id
    status
    chancePercentage
    statistics { volumeTotalUsd }
    makerFeeBps
    takerFeeBps
    outcomes { edges { node { id name index status } } }
    resolution { id name index status createdAt }
  }
}"""

_market_cache = {}


def get_market_metadata_for_slug(slug):
    """Fetches (market_id, category_id, title) for a window slug; (None, None, None) if not listed."""
    if slug in _market_cache:
        return _market_cache[slug]

    data = graphql(CATEGORY_QUERY, {"categoryId": slug})
    category = (data or {}).get("category")
    if not category:
        return None, None, None
    edges = (category.get("markets") or {}).get("edges") or []
    if not edges:
        return None, None, None

    market_id = str(edges[0]["node"]["id"])
    result = (market_id, str(category.get("numericId")), category.get("title") or slug)
    _market_cache[slug] = result
    return result


def fetch_market(market_id):
    """{'resolved', 'resolution', 'maker_fee_bps', 'taker_fee_bps'} or None."""
    data = graphql(MARKET_QUERY, {"marketId": str(market_id)}, operation="GetMarket")
    market = (data or {}).get("market")
    if not market:
        return None
    resolution = (market.get("resolution") or {}).get("name")
    return {
        "resolved": resolution in ("Up", "Down"),
        "resolution": resolution,
        "status": market.get("status"),
        "volume_usd": float((market.get("statistics") or {}).get("volumeTotalUsd") or 0.0),
        "maker_fee_bps": int(market.get("makerFeeBps") or 0),
        "taker_fee_bps": int(market.get("takerFeeBps") or 0),
    }


def latest_category_slug(prefix):
    data = graphql("query($slug: String!) { latestCategorySlug(slug: $slug) }", {"slug": prefix})
    return (data or {}).get("latestCategorySlug")


# --- live socket ---

def book_depth(book, within=0.05):
    """Shares on offer for each side: at the best ask, and within ``within`` of it. Down's asks are the Up book's
    bids seen from the other side (price 1 - bid). Keys: up_ask_size, up_ask_depth, down_ask_size, down_ask_depth."""
    def levels(rows, flip):
        out = []
        for row in rows or []:
            try:
                price, size = float(row[0]), float(row[1])
            except (TypeError, ValueError, IndexError):
                continue
            out.append((round(1.0 - price, 4) if flip else price, size))
        return sorted(out)

    result = {}
    for side, rows, flip in (("up", book.get("asks"), False), ("down", book.get("bids"), True)):
        lv = levels(rows, flip)
        best = lv[0][0] if lv else None
        result[f"{side}_ask_size"] = sum(s for p, s in lv if p == best) if lv else 0.0
        result[f"{side}_ask_depth"] = sum(s for p, s in lv if p <= best + within + 1e-9) if lv else 0.0
    return result


def book_levels(book, n=6):
    """Top ``n`` price levels of each side's book, Polymarket style: asks cheapest first, bids highest first.
    Down's book is the Up book flipped (Down asks = 1 - Up bids, Down bids = 1 - Up asks)."""
    def rows(raw):
        out = []
        for row in raw or []:
            try:
                out.append((float(row[0]), float(row[1])))
            except (TypeError, ValueError, IndexError):
                continue
        return out

    asks, bids = rows(book.get("asks")), rows(book.get("bids"))
    flip = lambda levels: [(round(1.0 - p, 4), s) for p, s in levels]
    return {
        "up": {"asks": sorted(asks)[:n], "bids": sorted(bids, reverse=True)[:n]},
        "down": {"asks": sorted(flip(bids))[:n], "bids": sorted(flip(asks), reverse=True)[:n]},
    }


def best_asks(book):
    """(up_ask, down_ask) from a YES/Up order book; 0.0 where a side is empty."""
    asks = book.get("asks") or []
    bids = book.get("bids") or []
    try:
        up = float(asks[0][0]) if asks else 0.0
    except (TypeError, ValueError, IndexError):
        up = 0.0
    try:
        down = round(1.0 - float(bids[0][0]), 4) if bids else 0.0
    except (TypeError, ValueError, IndexError):
        down = 0.0
    return up, down


class PredictWS:
    """One persistent socket: the Chainlink price for ``feed_id`` plus the order books of the active and next market."""

    def __init__(self, feed_id):
        self.feed_id = feed_id
        self.price_topic = f"chainlinkAssetPriceUpdate/{feed_id}"
        self.ws = None
        self.connected = False
        self.ever_connected = False
        self.is_running = True
        self.lock = threading.Lock()
        self.request_id = 0
        self.markets = []
        self.books = {}
        self.book_times = {}
        self.watched = {}
        self.variants = {}
        self.prices = deque()
        self.last_message = time.time()
        self.disconnected_at = time.time()
        threading.Thread(target=self._run_loop, daemon=True).start()
        threading.Thread(target=self._watchdog_loop, daemon=True).start()

    # public

    def set_market(self, market_id):
        """Make ``market_id`` the only subscribed book; the next window's book is added later by ``prepare_next``.

        Market-change topics (which carry the official start/end price) stay subscribed for
        ``MARKET_WATCH_SECONDS`` so the previous window's end price still arrives after the switch."""
        market_id = str(market_id)
        now = time.time()
        with self.lock:
            stale = [m for m in self.markets if m != market_id]
            already = market_id in self.markets
            self.markets = [market_id]
            for m in stale:
                self.books.pop(m, None)
                self.book_times.pop(m, None)
                if m in self.watched:
                    self.watched[m] = now
            expired = [m for m, t in self.watched.items() if m != market_id and now - t > MARKET_WATCH_SECONDS]
            for m in expired:
                self.watched.pop(m, None)
                self.variants.pop(m, None)
            new_watch = market_id not in self.watched
            self.watched[market_id] = now
        for m in stale:
            self._send("unsubscribe", [f"predictOrderbook/{m}"])
        for m in expired:
            self._send("unsubscribe", [f"predictMarketChanged/{m}"])
        if not already:
            self._send("subscribe", [f"predictOrderbook/{market_id}"])
        if new_watch:
            self._send("subscribe", [f"predictMarketChanged/{market_id}"])

    def prepare_next(self, market_id):
        market_id = str(market_id)
        with self.lock:
            if market_id in self.markets:
                return
            self.markets.append(market_id)
            new_watch = market_id not in self.watched
            self.watched[market_id] = time.time()
        self._send("subscribe", [f"predictOrderbook/{market_id}"])
        if new_watch:
            self._send("subscribe", [f"predictMarketChanged/{market_id}"])

    def official_prices(self, market_id):
        """(start_price, end_price) Predict.fun pushed for ``market_id``; 0.0 where not pushed yet."""
        with self.lock:
            data = dict(self.variants.get(str(market_id)) or {})
        return float(data.get("startPrice") or 0.0), float(data.get("endPrice") or 0.0)

    def wait_for_end_price(self, market_id, timeout):
        """Waits up to ``timeout`` seconds for the official end price; returns (start, end)."""
        deadline = time.time() + timeout
        while True:
            start, end = self.official_prices(market_id)
            if end or time.time() >= deadline:
                return start, end
            time.sleep(0.1)

    def asks(self, market_id):
        """(up_ask, down_ask) of the latest book for ``market_id``; (0.0, 0.0) before the first book."""
        with self.lock:
            book = self.books.get(str(market_id))
        return best_asks(book) if book else (0.0, 0.0)

    def depth(self, market_id):
        """``book_depth`` of the latest book for ``market_id``; None before the first book."""
        with self.lock:
            book = self.books.get(str(market_id))
        return book_depth(book) if book else None

    def levels(self, market_id, n=6):
        """``book_levels`` of the latest book for ``market_id``; None before the first book."""
        with self.lock:
            book = self.books.get(str(market_id))
        return book_levels(book, n) if book else None

    def book_age(self, market_id):
        """Seconds since the last book for ``market_id``; None before the first."""
        with self.lock:
            ts = self.book_times.get(str(market_id))
        return time.time() - ts if ts else None

    def next_ready(self):
        with self.lock:
            return len(self.markets) > 1

    def latest_price(self):
        with self.lock:
            if not self.prices:
                return 0.0
            ts, value = self.prices[-1]
        return value if time.time() - ts <= PRICE_STALE_SECONDS else 0.0

    def price_at(self, epoch_seconds):
        """Last price at or before ``epoch_seconds``; 0.0 if none close enough."""
        with self.lock:
            for ts, value in reversed(self.prices):
                if ts <= epoch_seconds:
                    return value if epoch_seconds - ts <= PRICE_BOUNDARY_TOLERANCE_SECONDS else 0.0
        return 0.0

    def price_before(self, epoch_seconds):
        """Last price strictly before ``epoch_seconds`` (the live stand-in for the window's closing price)."""
        with self.lock:
            for ts, value in reversed(self.prices):
                if ts < epoch_seconds:
                    return value if epoch_seconds - ts <= PRICE_BOUNDARY_TOLERANCE_SECONDS else 0.0
        return 0.0

    def wait_for_tick_from(self, epoch_seconds, timeout=3.0):
        """Wait until a tick at or after ``epoch_seconds`` arrives, so every earlier tick is in."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if self.prices and self.prices[-1][0] >= epoch_seconds:
                    return True
            time.sleep(0.1)
        return False

    def first_price_from(self, epoch_seconds):
        """First price at or after ``epoch_seconds`` (the live stand-in for the window's opening price)."""
        with self.lock:
            for ts, value in self.prices:
                if ts >= epoch_seconds:
                    return value if ts - epoch_seconds <= PRICE_BOUNDARY_TOLERANCE_SECONDS else 0.0
        return 0.0

    # internals

    def _send(self, method, params):
        ws = self.ws
        if ws is None or not self.connected:
            return
        with self.lock:
            self.request_id += 1
            request_id = self.request_id
        try:
            ws.send(json.dumps({"requestId": request_id, "method": method, "params": params}))
        except Exception:
            pass

    def _on_open(self, ws):
        now = time.time()
        with self.lock:
            took = now - self.disconnected_at if self.disconnected_at else 0.0
            verb = "reconnected" if self.ever_connected else "connected"
            self.connected = True
            self.ever_connected = True
            self.disconnected_at = None
            self.last_message = now
            markets = list(self.markets)
            watched = list(self.watched)
        sys.stdout.write(f"\n🔌 Predict socket {verb} in {took:.1f}s\n")
        sys.stdout.flush()
        self._send("setLanguage", ["en"])
        self._send("subscribe", [self.price_topic])
        for m in markets:
            self._send("subscribe", [f"predictOrderbook/{m}"])
        for m in watched:
            self._send("subscribe", [f"predictMarketChanged/{m}"])

    def _on_message(self, ws, message):
        self.last_message = time.time()
        try:
            msg = json.loads(message)
        except ValueError:
            return
        if not isinstance(msg, dict) or msg.get("type") != "M":
            return
        topic = msg.get("topic") or ""
        data = msg.get("data")
        if topic == "heartbeat":
            try:
                ws.send(json.dumps({"method": "heartbeat", "data": data}))
            except Exception:
                pass
        elif topic.startswith("predictOrderbook/") and isinstance(data, dict):
            market_id = topic.split("/", 1)[1]
            with self.lock:
                if market_id in self.markets:
                    self.books[market_id] = data
                    self.book_times[market_id] = time.time()
        elif topic.startswith("predictMarketChanged/") and isinstance(data, dict):
            variant = (data.get("patch") or {}).get("variantData")
            if isinstance(variant, dict):
                market_id = topic.split("/", 1)[1]
                with self.lock:
                    self.variants.setdefault(market_id, {}).update(
                        {k: v for k, v in variant.items() if k in ("startPrice", "endPrice") and v}
                    )
        elif topic == self.price_topic and isinstance(data, dict):
            try:
                ts = float(data.get("timestamp") or data.get("publishTime") or 0)
                value = float(data.get("price") or 0)
            except (TypeError, ValueError):
                return
            if ts > 1e12:
                ts /= 1000.0
            if ts <= 0 or value <= 0:
                return
            with self.lock:
                if self.prices and ts < self.prices[-1][0]:
                    return
                self.prices.append((ts, value))
                cutoff = ts - PRICE_HISTORY_SECONDS
                while self.prices and self.prices[0][0] < cutoff:
                    self.prices.popleft()

    def _mark_disconnected(self):
        with self.lock:
            if self.connected or self.disconnected_at is None:
                self.disconnected_at = time.time()
            self.connected = False

    def _close_ws(self):
        ws = self.ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _on_close(self, ws, code, msg):
        self._mark_disconnected()

    def _on_error(self, ws, error):
        self._mark_disconnected()

    def _watchdog_loop(self):
        while self.is_running:
            time.sleep(1)
            now = time.time()
            with self.lock:
                connected = self.connected
                down_for = now - self.disconnected_at if self.disconnected_at else 0.0
            quiet_for = now - self.last_message
            if connected and quiet_for > WS_QUIET_SECONDS:
                sys.stdout.write(f"\n⚠️ Predict socket quiet for {quiet_for:.0f}s while connected, reconnecting...\n")
                sys.stdout.flush()
                self._mark_disconnected()
                self._close_ws()
            elif not connected and down_for > WS_CONNECT_TIMEOUT_SECONDS:
                sys.stdout.write(f"\n⚠️ Predict socket not connected after {down_for:.0f}s, retrying...\n")
                sys.stdout.flush()
                with self.lock:
                    self.disconnected_at = now
                self._close_ws()

    def _run_loop(self):
        while self.is_running:
            try:
                self.ws = websocket.WebSocketApp(
                    WS_URL,
                    header=[f"User-Agent: {USER_AGENT}"],
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_close=self._on_close,
                    on_error=self._on_error,
                )
                self.ws.run_forever(origin=ORIGIN, ping_interval=20, ping_timeout=10)
            except Exception:
                pass
            self._mark_disconnected()
            if self.is_running:
                time.sleep(2)
