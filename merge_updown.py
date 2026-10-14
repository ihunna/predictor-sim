#!/usr/bin/env python3
"""Merge the per-strategy up/down files (e.g. btc-5-combo-updown.csv) into the shared one (btc-5-updown.csv).

Rows are sorted by window; a window already in the shared file keeps that row. Merged files are renamed to
``*.merged.bak``. Run in the folder with the logs:

    python merge_updown.py
"""
import csv
import glob
import os
import re

from logs import MARKET_CSV_HEADER

STRATEGY_FILE = re.compile(r"^(?P<prefix>[a-z0-9]+-\d+)-(?P<strategy>[a-z]+)-updown\.csv$")


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        return [row for row in reader if row and row[0].isdigit()]


def main():
    groups = {}
    for path in sorted(glob.glob("*-updown.csv")):
        match = STRATEGY_FILE.match(os.path.basename(path))
        if match:
            groups.setdefault(f"{match['prefix']}-updown.csv", []).append(path)
    if not groups:
        print("Nothing to merge.")
        return
    for main_file, extra_files in groups.items():
        rows = {}
        for path in ([main_file] if os.path.isfile(main_file) else []) + extra_files:
            for row in read_rows(path):
                rows.setdefault(row[0], row)
        tmp = f"{main_file}.tmp"
        with open(tmp, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(MARKET_CSV_HEADER)
            writer.writerows(rows[key] for key in sorted(rows, key=int))
        os.replace(tmp, main_file)
        for path in extra_files:
            os.replace(path, f"{path}.merged.bak")
        print(f"✅ {main_file}: {len(rows)} windows (merged {', '.join(extra_files)})")


if __name__ == "__main__":
    main()
