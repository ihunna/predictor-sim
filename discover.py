#!/usr/bin/env python3
"""One-off check before running the bot: does each coin/duration's computed slug resolve to a market.

Usage:
    python discover.py                    # btc eth sol bnb
    python discover.py --coins btc eth
"""
import argparse

from predict_feed import (
    CHAINLINK_FEED_IDS,
    SUPPORTED_DURATIONS,
    api_configured,
    configure_api_from_env,
    get_current_active_slug,
    get_last_lookup_error,
    get_market_metadata_for_slug,
    load_dotenv,
)


def check_slugs(coins):
    print("Markets for the current window:")
    for coin in coins:
        for duration in SUPPORTED_DURATIONS:
            slug = get_current_active_slug(coin, duration)
            market_id, category_id, _ = get_market_metadata_for_slug(slug)
            if market_id:
                status = f"market {market_id} (category {category_id})  OK"
            else:
                err = get_last_lookup_error()
                status = f"not found ({err})" if err else "not found"
            print(f"  {coin:<4} {duration:>2}m  {slug:<48} -> {status}")


def main():
    parser = argparse.ArgumentParser(description="Check Predict.fun slugs via official REST API.")
    parser.add_argument("--coins", nargs="+", default=["btc", "eth", "sol", "bnb"])
    args = parser.parse_args()
    load_dotenv()
    configure_api_from_env()
    if not api_configured():
        raise SystemExit(
            "PREDICT_API_KEY required (set in .env). Get one at https://developers.predict.fun"
        )
    check_slugs([c.lower() for c in args.coins])
    print("\nBuilt-in Chainlink feed ids:")
    for coin, feed_id in sorted(CHAINLINK_FEED_IDS.items()):
        print(f"  {coin}: {feed_id}")


if __name__ == "__main__":
    main()
