#!/usr/bin/env python3
"""Print top-K candidates by WR and by isolated $3/fill PnL."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CAND = REPO / "PALADIN" / "shaman_v1_rules_candidates.json"
STAKE = 3.0


def _pnl(r: dict) -> float:
    wr, n = float(r["wr"]), int(r["n"])
    return STAKE * (wr * n - (n - wr * n))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()
    k = args.top
    rows: list[dict] = json.loads(CAND.read_text(encoding="utf-8"))
    for r in rows:
        r["_pnl"] = _pnl(r)
    by_wr = sorted(rows, key=lambda x: (-float(x["wr"]), -int(x["n"])))
    by_pnl = sorted(rows, key=lambda x: (-float(x["_pnl"]), -float(x["wr"])))

    print(f"Candidates: {len(rows)} (est PnL = {STAKE} USDC/fill * (wins-losses), wins=wr*n)")
    print()
    print(f"=== Top {k} by WR ===")
    for i, r in enumerate(by_wr[:k], 1):
        print(
            f"{i:2}. {r['timeframe']:3} wr={r['wr']:.4f} n={r['n']:3} pnl~{r['_pnl']:+.2f}  "
            f"{r['family']} | {r['pattern_key']} | pred={r['pred']}"
        )
    print()
    print(f"=== Top {k} by est PnL ===")
    for i, r in enumerate(by_pnl[:k], 1):
        print(
            f"{i:2}. {r['timeframe']:3} pnl~{r['_pnl']:+.2f} wr={r['wr']:.4f} n={r['n']:3}  "
            f"{r['family']} | {r['pattern_key']} | pred={r['pred']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
