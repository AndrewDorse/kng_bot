#!/usr/bin/env python3
"""
Append every row from ``shaman_v1_rules_candidates.json`` into ``shaman_v1_rules.json``:
- Dedupe by (timeframe, family, pattern_key, pred); existing live rows win.
- Drop internal duplicate live rows (keep first occurrence).
- Skip keys blocked by ``shaman_removal_specs`` (same cuts as ``organize_shaman_v1_rule_lists``).

Run from repo root:
  python PALADIN/merge_all_candidates_into_live.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from shaman_removal_specs import REMOVED_15M_SPEC, REMOVED_5M_SPEC

REPO = Path(__file__).resolve().parents[1]
PAL = REPO / "PALADIN"
LIVE_JSON = PAL / "shaman_v1_rules.json"
CAND_JSON = PAL / "shaman_v1_rules_candidates.json"


def _key(r: dict) -> tuple[str, str, str, str]:
    return (
        str(r["timeframe"]).strip(),
        str(r["family"]).strip(),
        str(r["pattern_key"]).strip(),
        str(r["pred"]).strip(),
    )


def _spec_blocked_row(tf: str, fam: str, pk: str, pred: str) -> bool:
    sk = (fam.strip(), pk.strip(), pred.strip())
    if tf == "5m":
        return sk in {(a[0], a[1], a[2]) for a in REMOVED_5M_SPEC}
    if tf == "15m":
        return sk in {(a[0], a[1], a[2]) for a in REMOVED_15M_SPEC}
    return False


def _normalize_live_row(r: dict) -> dict:
    return {
        "timeframe": str(r["timeframe"]).strip(),
        "family": str(r["family"]).strip(),
        "pattern_key": str(r["pattern_key"]).strip(),
        "pred": str(r["pred"]).strip(),
        "wr": float(r["wr"]),
        "n": int(r["n"]),
    }


def main() -> int:
    live: list[dict] = json.loads(LIVE_JSON.read_text(encoding="utf-8"))
    cand: list[dict] = json.loads(CAND_JSON.read_text(encoding="utf-8"))

    seen: set[tuple[str, str, str, str]] = set()
    deduped_live: list[dict] = []
    internal_dup = 0
    for r in live:
        k = _key(r)
        if k in seen:
            internal_dup += 1
            continue
        seen.add(k)
        deduped_live.append(_normalize_live_row(r))

    live_keys = set(seen)
    added = 0
    skipped_spec = 0
    skipped_already = 0
    for r in cand:
        row = _normalize_live_row(r)
        k = _key(row)
        if _spec_blocked_row(k[0], k[1], k[2], k[3]):
            skipped_spec += 1
            continue
        if k in live_keys:
            skipped_already += 1
            continue
        deduped_live.append(row)
        live_keys.add(k)
        added += 1

    LIVE_JSON.write_text(json.dumps(deduped_live, indent=2) + "\n", encoding="utf-8")
    print(
        f"Live: {len(deduped_live)} rows (removed {internal_dup} internal duplicate(s); "
        f"added {added} from candidates; skipped {skipped_spec} spec-blocked; "
        f"{skipped_already} candidate(s) already live)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
