#!/usr/bin/env python3
"""One-off check before running the bot: does each coin/duration's computed slug resolve to a market,
and which Chainlink feed id belongs to which coin.

Usage:
    python discover.py                    # btc eth sol bnb, feed ids 1-40
    python discover.py --coins btc eth --max-feed 20
"""
import argparse
import time

from predict_feed import (
    CHAINLINK_FEED_IDS,
    SUPPORTED_DURATIONS,
    get_current_active_slug,
    get_market_metadata_for_slug,
    graphql,
)

FEED_PROBE_QUERY = """query($feedId: ID!) {
  assetOhlc(priceFeedId: $feedId, resolution: _1m, provider: CHAINLINK, countback: 1) {
    data { edges { node { timestamp close } } }
  }
}"""


def check_slugs(coins):
    print("Markets for the current window:")
    for coin in coins:
        for duration in SUPPORTED_DURATIONS:
            slug = get_current_active_slug(coin, duration)
            market_id, category_id, _ = get_market_metadata_for_slug(slug)
            status = f"market {market_id} (category {category_id})  OK" if market_id else "not found"
            print(f"  {coin:<4} {duration:>2}m  {slug:<48} -> {status}")


def scan_feeds(max_feed):
    print(f"\nChainlink feed ids 1-{max_feed} (latest 1m close):")
    known = {v: k for k, v in CHAINLINK_FEED_IDS.items()}
    now = time.time()
    for feed_id in range(1, max_feed + 1):
        data = graphql(FEED_PROBE_QUERY, {"feedId": str(feed_id)})
        try:
            edges = data["assetOhlc"]["data"]["edges"]
        except (TypeError, KeyError):
            edges = []
        if not edges:
            continue
        node = edges[-1]["node"]
        age_min = (now - float(node["timestamp"])) / 60
        label = known.get(feed_id, "?")
        print(f"  id {feed_id:>3}: {float(node['close']):>14,.4f}  ({age_min:,.0f} min old)  coin={label}")


def main():
    parser = argparse.ArgumentParser(description="Check Predict.fun slugs and Chainlink feed ids.")
    parser.add_argument("--coins", nargs="+", default=["btc", "eth", "sol", "bnb"])
    parser.add_argument("--max-feed", type=int, default=40)
    args = parser.parse_args()
    check_slugs([c.lower() for c in args.coins])
    scan_feeds(args.max_feed)


if __name__ == "__main__":
    main()
