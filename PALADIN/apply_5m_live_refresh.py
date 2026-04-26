#!/usr/bin/env python3
"""
Remove live **5m** rules with last-week WR < 60% (see ``REPORT_5m_rules_last_week.md``),
append top **30** rows from ``exports/btc_binance_klines/new_5m_wr60_not_in_live.csv``
(WR > 60%, not already live), append ``shaman_v1_rules_removed.json``, refresh
``shaman_v1_rules_candidates.json``.

Run from repo root:
  python PALADIN/apply_5m_live_refresh.py
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from shaman_removal_specs import REMOVED_5M_SPEC

REPO = Path(__file__).resolve().parents[1]
PAL = REPO / "PALADIN"
LIVE_JSON = PAL / "shaman_v1_rules.json"
REMOVED_JSON = PAL / "shaman_v1_rules_removed.json"
CAND_JSON = PAL / "shaman_v1_rules_candidates.json"
NEW_PAT_CSV = REPO / "exports" / "btc_binance_klines" / "new_patterns_wr60_not_in_shaman.csv"
NEW_5M_CSV = REPO / "exports" / "btc_binance_klines" / "new_5m_wr60_not_in_live.csv"

def _key(r: dict) -> tuple[str, str, str, str]:
    return (str(r["timeframe"]), str(r["family"]), str(r["pattern_key"]).strip(), str(r["pred"]))


def _spec_key(s: tuple[str, str, str, float, int, int]) -> tuple[str, str, str]:
    return (s[0], s[1], s[2])


def _rebuild_candidates(live_keys: set[tuple[str, str, str, str]]) -> list[dict]:
    out: list[dict] = []
    if not NEW_PAT_CSV.is_file():
        return out
    with NEW_PAT_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            c = {
                "timeframe": row["timeframe"].strip(),
                "family": row["family"].strip(),
                "pattern_key": row["pattern_key"].strip(),
                "pred": row["pred"].strip(),
                "wr": float(row["wr"]),
                "n": int(row["n"]),
            }
            if _key(c) not in live_keys:
                out.append(c)
    return out


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    spec_5m = {_spec_key(s): s for s in REMOVED_5M_SPEC}

    if not NEW_5M_CSV.is_file():
        print(f"Missing {NEW_5M_CSV}", file=sys.stderr)
        return 1

    with LIVE_JSON.open(encoding="utf-8") as f:
        rules: list[dict] = json.load(f)

    live: list[dict] = []
    newly_removed: list[dict] = []
    for r in rules:
        if r.get("timeframe") != "5m":
            live.append(r)
            continue
        sk = (r["family"], str(r["pattern_key"]).strip(), r["pred"])
        if sk not in spec_5m:
            live.append(r)
            continue
        w_wr, hits, wins = spec_5m[sk][3], spec_5m[sk][4], spec_5m[sk][5]
        newly_removed.append(
            {
                **r,
                "removed_reason": "5m backtest: week WR < 60% (see REPORT_5m_rules_last_week.md)",
                "removed_week_eval_wr": w_wr,
                "removed_week_hits": hits,
                "removed_week_wins": wins,
                "removed_at_utc": stamp,
            }
        )

    live_keys = {_key(r) for r in live}
    added = 0
    with NEW_5M_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if added >= 30:
                break
            wr = float(row["wr"])
            if wr <= 0.60 + 1e-12:
                continue
            c = {
                "timeframe": "5m",
                "family": row["family"].strip(),
                "pattern_key": row["pattern_key"].strip(),
                "pred": row["pred"].strip(),
                "wr": wr,
                "n": int(row["n"]),
            }
            k = _key(c)
            if k in live_keys:
                continue
            live.append(c)
            live_keys.add(k)
            added += 1

    prev_removed: list[dict] = []
    if REMOVED_JSON.is_file():
        prev_removed = json.loads(REMOVED_JSON.read_text(encoding="utf-8"))
        if not isinstance(prev_removed, list):
            prev_removed = []

    prev_keys = {_key(x) for x in prev_removed}
    for x in newly_removed:
        if _key(x) not in prev_keys:
            prev_removed.append(x)
            prev_keys.add(_key(x))

    candidates = _rebuild_candidates({_key(r) for r in live})

    LIVE_JSON.write_text(json.dumps(live, indent=2) + "\n", encoding="utf-8")
    REMOVED_JSON.write_text(json.dumps(prev_removed, indent=2) + "\n", encoding="utf-8")
    CAND_JSON.write_text(json.dumps(candidates, indent=2) + "\n", encoding="utf-8")

    print(f"Removed 5m (this pass): {len(newly_removed)}; added from research: {added}; live total: {len(live)}")
    print(f"Removed file entries: {len(prev_removed)}; candidates: {len(candidates)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
