#!/usr/bin/env python3
"""
Append top-N by WR and top-N by isolated $1/fill PnL from ``shaman_v1_rules_candidates.json``
into ``shaman_v1_rules.json`` (dedupe by key; skip keys already live).

Run from repo root:
  python PALADIN/promote_candidates_top_wr_pnl.py
  python PALADIN/promote_candidates_top_wr_pnl.py --wr 7 --pnl 7
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from shaman_removal_specs import REMOVED_15M_SPEC, REMOVED_5M_SPEC

REPO = Path(__file__).resolve().parents[1]
PAL = REPO / "PALADIN"
LIVE_JSON = PAL / "shaman_v1_rules.json"
CAND_JSON = PAL / "shaman_v1_rules_candidates.json"
STAKE = 1.0


def _spec_blocked(r: dict) -> bool:
    tf = str(r["timeframe"]).strip()
    sk = (str(r["family"]).strip(), str(r["pattern_key"]).strip(), str(r["pred"]).strip())
    if tf == "5m":
        return sk in {(a[0], a[1], a[2]) for a in REMOVED_5M_SPEC}
    if tf == "15m":
        return sk in {(a[0], a[1], a[2]) for a in REMOVED_15M_SPEC}
    return False


def _key(r: dict) -> tuple[str, str, str, str]:
    return (
        str(r["timeframe"]).strip(),
        str(r["family"]).strip(),
        str(r["pattern_key"]).strip(),
        str(r["pred"]).strip(),
    )


def _pnl(r: dict) -> float:
    wr, n = float(r["wr"]), int(r["n"])
    wins = wr * n
    losses = n - wins
    return STAKE * (wins - losses)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wr", type=int, default=7, help="How many top-WR candidates to promote.")
    ap.add_argument("--pnl", type=int, default=7, help="How many top-PnL candidates to promote.")
    args = ap.parse_args()

    live: list[dict] = json.loads(LIVE_JSON.read_text(encoding="utf-8"))
    cand_raw: list[dict] = json.loads(CAND_JSON.read_text(encoding="utf-8"))
    live_keys = {_key(r) for r in live}

    cand = [r for r in cand_raw if not _spec_blocked(r)]
    skipped = len(cand_raw) - len(cand)

    for r in cand:
        r["_pnl"] = _pnl(r)

    by_wr = sorted(cand, key=lambda x: (-float(x["wr"]), -int(x["n"])))
    by_pnl = sorted(cand, key=lambda x: (-float(x["_pnl"]), -float(x["wr"])))

    pick: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for r in by_wr[: max(0, args.wr)]:
        k = _key(r)
        if k in seen:
            continue
        seen.add(k)
        pick.append(r)
    for r in by_pnl[: max(0, args.pnl)]:
        k = _key(r)
        if k in seen:
            continue
        seen.add(k)
        pick.append(r)

    added = 0
    for r in pick:
        k = _key(r)
        if k in live_keys:
            continue
        live.append(
            {
                "timeframe": k[0],
                "family": k[1],
                "pattern_key": k[2],
                "pred": k[3],
                "wr": float(r["wr"]),
                "n": int(r["n"]),
            }
        )
        live_keys.add(k)
        added += 1

    LIVE_JSON.write_text(json.dumps(live, indent=2) + "\n", encoding="utf-8")
    msg = (
        f"Promoted {added} new live rules (union top-{args.wr} WR + top-{args.pnl} PnL, deduped); "
        f"live total {len(live)}"
    )
    if skipped:
        msg += f"; skipped {skipped} candidates blocked by shaman_removal_specs"
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
