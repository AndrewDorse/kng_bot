#!/usr/bin/env python3
"""Print top-K rows by WR and by isolated $1/fill PnL (live rules and/or candidates)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIVE = REPO / "PALADIN" / "shaman_v1_rules.json"
CAND = REPO / "PALADIN" / "shaman_v1_rules_candidates.json"
STAKE = 1.0


def _pnl(r: dict) -> float:
    wr, n = float(r["wr"]), int(r["n"])
    return STAKE * (wr * n - (n - wr * n))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument(
        "--source",
        choices=("live", "candidates"),
        default="candidates",
        help="Which JSON list to rank (default: candidates).",
    )
    ap.add_argument(
        "--timeframe",
        default="",
        metavar="TF",
        help='Only include this timeframe, e.g. "5m" or "15m". Empty = all.',
    )
    args = ap.parse_args()
    k = args.top
    path = LIVE if args.source == "live" else CAND
    rows: list[dict] = json.loads(path.read_text(encoding="utf-8"))
    tf = (args.timeframe or "").strip()
    if tf:
        rows = [r for r in rows if str(r.get("timeframe", "")).strip() == tf]

    for r in rows:
        r["_pnl"] = _pnl(r)
    by_wr = sorted(rows, key=lambda x: (-float(x["wr"]), -int(x["n"])))
    by_pnl = sorted(rows, key=lambda x: (-float(x["_pnl"]), -float(x["wr"])))

    label = f"{path.relative_to(REPO)}"
    if tf:
        label += f"  (timeframe={tf})"
    print(f"Source: {label}  |  {len(rows)} rows")
    print(f"est PnL = {STAKE} USDC/fill * (wins-losses), wins=wr*n")
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
