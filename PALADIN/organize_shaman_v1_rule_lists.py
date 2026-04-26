#!/usr/bin/env python3
"""
Maintain three SHAMAN v1 rule lists (repo root from ``PALADIN/``):

1. ``shaman_v1_rules.json`` — **live** (loaded by ``shaman_v1_engine.py``).
2. ``shaman_v1_rules_candidates.json`` — scan export rows not in live (WR≥60, not promoted).
3. ``shaman_v1_rules_removed.json`` — formerly live rules cut from live (append-only by key).

Removals (idempotent): rules matching ``shaman_removal_specs`` — 15m and 5m week WR < 60%
lists derived from ``REPORT_*_rules_last_week.md``. Does **not** add research rows; use
``apply_5m_live_refresh.py`` for 5m promotion batches.

Run from repo root:
  python PALADIN/organize_shaman_v1_rule_lists.py
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from shaman_removal_specs import REMOVED_15M_SPEC, REMOVED_5M_SPEC

REPO = Path(__file__).resolve().parents[1]
PAL = REPO / "PALADIN"
LIVE_JSON = PAL / "shaman_v1_rules.json"
REMOVED_JSON = PAL / "shaman_v1_rules_removed.json"
CAND_JSON = PAL / "shaman_v1_rules_candidates.json"
NEW_PAT_CSV = REPO / "exports" / "btc_binance_klines" / "new_patterns_wr60_not_in_shaman.csv"


def _key(r: dict) -> tuple[str, str, str, str]:
    return (str(r["timeframe"]), str(r["family"]), str(r["pattern_key"]).strip(), str(r["pred"]))


def _spec_key(s: tuple[str, str, str, float, int, int]) -> tuple[str, str, str]:
    return (s[0], s[1], s[2])


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    spec_15m = {_spec_key(s): s for s in REMOVED_15M_SPEC}
    spec_5m = {_spec_key(s): s for s in REMOVED_5M_SPEC}

    with LIVE_JSON.open(encoding="utf-8") as f:
        rules: list[dict] = json.load(f)

    live: list[dict] = []
    newly_removed: list[dict] = []
    for r in rules:
        tf = str(r.get("timeframe", ""))
        sk = (r["family"], str(r["pattern_key"]).strip(), r["pred"])
        if tf == "15m" and sk in spec_15m:
            w_wr, hits, wins = spec_15m[sk][3], spec_15m[sk][4], spec_15m[sk][5]
            newly_removed.append(
                {
                    **r,
                    "removed_reason": "15m backtest: week WR < 60% (see REPORT_15m_rules_last_week.md)",
                    "removed_week_eval_wr": w_wr,
                    "removed_week_hits": hits,
                    "removed_week_wins": wins,
                    "removed_at_utc": stamp,
                }
            )
        elif tf == "5m" and sk in spec_5m:
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
        else:
            live.append(r)

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

    live_keys = {_key(r) for r in live}

    candidates: list[dict] = []
    if NEW_PAT_CSV.is_file():
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
                    candidates.append(c)

    LIVE_JSON.write_text(json.dumps(live, indent=2) + "\n", encoding="utf-8")
    REMOVED_JSON.write_text(json.dumps(prev_removed, indent=2) + "\n", encoding="utf-8")
    CAND_JSON.write_text(json.dumps(candidates, indent=2) + "\n", encoding="utf-8")

    print(f"Live rules: {len(live)} (was {len(rules)}); removed this pass: {len(newly_removed)}")
    print(f"Removed total (file): {len(prev_removed)}; candidates: {len(candidates)}")
    print(f"Wrote {LIVE_JSON.name}, {REMOVED_JSON.name}, {CAND_JSON.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
