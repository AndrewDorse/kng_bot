#!/usr/bin/env python3
"""Append rows from SHAMAN_*_GATE6M_*.csv into shaman_v1_rules.json (dedupe by live key).

Each new rule uses the 6m slice for primary ``wr`` / ``n``; shorter-window fields are filled
from the CSV when present, else sensible fallbacks so the JSON matches existing live shape.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PAL = REPO / "PALADIN"
LIVE_JSON = PAL / "shaman_v1_rules.json"

sys.path.insert(0, str(PAL))
from shaman_removal_specs import REMOVED_15M_SPEC, REMOVED_5M_SPEC  # noqa: E402

_SPEC_5M = {(a[0], a[1], a[2]) for a in REMOVED_5M_SPEC}
_SPEC_15M = {(a[0], a[1], a[2]) for a in REMOVED_15M_SPEC}


def _blocked_by_removal_spec(rule: dict[str, Any]) -> bool:
    tf = str(rule["timeframe"]).strip()
    sk = (
        str(rule["family"]).strip(),
        str(rule["pattern_key"]).strip(),
        str(rule["pred"]).strip(),
    )
    if tf == "5m" and sk in _SPEC_5M:
        return True
    if tf == "15m" and sk in _SPEC_15M:
        return True
    return False


def _f(row: dict[str, str], key: str) -> float | None:
    v = (row.get(key) or "").strip()
    if not v:
        return None
    return float(v)


def _i(row: dict[str, str], key: str) -> int | None:
    v = (row.get(key) or "").strip()
    if not v:
        return None
    return int(float(v))


def _rule_key(r: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(r["timeframe"]).strip(),
        str(r["family"]).strip(),
        str(r["pattern_key"]).strip(),
        str(r["pred"]).strip(),
    )


def _signal_key(r: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(r["timeframe"]).strip(),
        str(r["pattern_key"]).strip(),
        str(r["pred"]).strip(),
    )


def _row_to_live_rule(row: dict[str, str]) -> dict[str, Any]:
    wr6 = float(_f(row, "wr_6m") or 0.0)
    n6 = int(_i(row, "hits_6m") or 0)
    wr7 = _f(row, "wr_7d")
    n7 = _i(row, "hits_7d")
    wr28 = _f(row, "wr_28d")
    n28 = _i(row, "hits_28d")
    wr2m = _f(row, "wr_2m")
    n2m = _i(row, "hits_2m")
    wr5m = _f(row, "wr_5m")
    n5m = _i(row, "hits_5m")

    if wr7 is None or n7 is None or n7 <= 0:
        if wr5m is not None and n5m is not None and n5m > 0:
            wr7, n7 = wr5m, n5m
        else:
            wr7, n7 = wr6, max(1, min(n6, 50))
    if wr28 is None or n28 is None or n28 <= 0:
        if wr2m is not None and n2m is not None and n2m > 0:
            wr28, n28 = wr2m, n2m
        else:
            wr28, n28 = wr6, max(1, min(n6, 200))
    if wr2m is None or n2m is None or n2m <= 0:
        wr2m, n2m = wr6, n6

    return {
        "timeframe": str(row["timeframe"]).strip(),
        "family": str(row["family"]).strip(),
        "pattern_key": str(row["pattern_key"]).strip(),
        "pred": str(row["pred"]).strip(),
        "wr": wr6,
        "n": n6,
        "wr_7d": float(wr7),
        "hits_7d": int(n7),
        "wr_1m": float(wr28),
        "hits_1m": int(n28),
        "wr_2m": float(wr2m),
        "hits_2m": int(n2m),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "csvs",
        nargs="*",
        type=Path,
        default=[
            REPO / "exports/btc_binance_klines/SHAMAN_15M_GATE6M_WR60_H400_20260501_211255Z.csv",
            REPO / "exports/btc_binance_klines/SHAMAN_5M_GATE6M_WR60_H400_20260501_212455Z.csv",
        ],
        help="Gate export CSV paths (default: known 20260501 gate files)",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    live: list[dict[str, Any]] = json.loads(LIVE_JSON.read_text(encoding="utf-8"))
    existing = {_rule_key(r) for r in live}

    incoming: list[dict[str, str]] = []
    for p in args.csvs:
        if not p.is_file():
            print(f"skip missing: {p}", file=sys.stderr)
            continue
        with p.open(encoding="utf-8", newline="") as f:
            incoming.extend(list(csv.DictReader(f)))

    # Exact duplicate keys within CSV batch
    seen_csv: set[tuple[str, str, str, str]] = set()
    dup_in_csv: list[tuple[str, str, str, str]] = []
    for row in incoming:
        rule = _row_to_live_rule(row)
        k = _rule_key(rule)
        if k in seen_csv:
            dup_in_csv.append(k)
        seen_csv.add(k)

    appended = 0
    skipped_live = 0
    skipped_spec = 0
    new_rules: list[dict[str, Any]] = []
    for row in incoming:
        rule = _row_to_live_rule(row)
        k = _rule_key(rule)
        if k in existing:
            skipped_live += 1
            continue
        if _blocked_by_removal_spec(rule):
            skipped_spec += 1
            continue
        existing.add(k)
        new_rules.append(rule)
        appended += 1

    # Same signal (tf, pattern, pred), different family — among appended + cross live
    by_sig: defaultdict[tuple[str, str, str], list[str]] = defaultdict(list)
    for r in live + new_rules:
        sk = _signal_key(r)
        fam = str(r["family"]).strip()
        if fam not in by_sig[sk]:
            by_sig[sk].append(fam)

    mirror_pairs = [sk for sk, fams in by_sig.items() if len(fams) > 1]

    print(f"live_before={len(live)} csv_rows={len(incoming)}")
    print(f"appended={appended} skipped_already_live={skipped_live} skipped_removal_spec={skipped_spec}")
    print(f"dup_keys_within_csv_batch={len(dup_in_csv)}")
    print(f"signal_keys_with_multiple_families={len(mirror_pairs)} (Z/V mirrors ok)")

    if dup_in_csv:
        print("first_dup_in_csv:", dup_in_csv[0], file=sys.stderr)

    if args.dry_run:
        print("dry-run: not writing")
        return 0

    out = live + new_rules
    LIVE_JSON.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {LIVE_JSON} live_after={len(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
