#!/usr/bin/env python3
"""
Simulate **strategy-1 slice1000** (0.82 / 22s / 0.19) on the **newest** public 15m windows
for each of several ``--last-n`` values (default **100,200,400,800**), using the same PnL semantics
as ``sim_streak076_last_n_sheet.py`` ($1 stake, winner = higher final mid; tie windows excluded from corpus).

Writes one **CSV sheet** (UTF-8 BOM): meta SUMMARY rows, then one row per ``last_n`` with totals and averages.

Run::

  python PALADIN/sim_streak076_multin_sheet.py
  python PALADIN/sim_streak076_multin_sheet.py --last-n-list 100,200,400,800 --out-csv exports/STREAK12_MULTIN.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_PAL = Path(__file__).resolve().parent
for _p in (REPO, _PAL):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from paladin_v10_wallet_fit_simulator import DEFAULT_PUBLIC_SNAPSHOTS  # noqa: E402
from sim_streak076_sweep_last_n import load_corpus, paths_newest_first, run_one_variant  # noqa: E402

_SKEW = 0.82
_STREAK = 22
_CHEAP = 0.19


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--last-n-list",
        type=str,
        default="100,200,400,800",
        help="Comma-separated newest-N window counts (each simulated on a prefix of the same loaded corpus).",
    )
    ap.add_argument("--snapshots-dir", type=Path, default=DEFAULT_PUBLIC_SNAPSHOTS)
    ap.add_argument("--out-csv", type=Path, default=None)
    args = ap.parse_args()

    ns: list[int] = []
    for part in args.last_n_list.split(","):
        p = part.strip()
        if not p:
            continue
        ns.append(max(1, int(float(p))))
    if not ns:
        ns = [100, 200, 400, 800]
    max_n = max(ns)

    snap = args.snapshots_dir.resolve()
    if not snap.is_dir():
        print(f"Missing snapshots dir: {snap}", file=sys.stderr)
        return 2

    paths = paths_newest_first(snap, cap=max_n)
    corpus = load_corpus(paths)
    have = len(corpus)

    out = args.out_csv
    if out is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out = (REPO / "exports" / f"STREAK12_STRATEGY1_MULTIN_{ts}Z.csv").resolve()
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "row_kind",
        "requested_last_n",
        "effective_last_n",
        "paths_loaded",
        "tie_skipped",
        "corpus_non_tie",
        "hits",
        "wins",
        "losses",
        "win_rate_pct_on_hits",
        "pnl_sum_usd",
        "ev_per_window_usd",
        "hit_rate_pct_of_corpus",
        "avg_pnl_usd_per_hit",
        "avg_entry_mid_on_hits",
        "note",
    ]

    rows_out: list[dict[str, str | int | float]] = []
    rows_out.append(
        {
            "row_kind": "META",
            "requested_last_n": "",
            "effective_last_n": "",
            "paths_loaded": have,
            "tie_skipped": "",
            "corpus_non_tie": "",
            "hits": "",
            "wins": "",
            "losses": "",
            "win_rate_pct_on_hits": "",
            "pnl_sum_usd": "",
            "ev_per_window_usd": "",
            "hit_rate_pct_of_corpus": "",
            "avg_pnl_usd_per_hit": "",
            "avg_entry_mid_on_hits": "",
            "note": f"strategy=strategy1_slice1000 skew={_SKEW} streak={_STREAK} cheap={_CHEAP} $1 stake; dir={snap}",
        }
    )

    if have < max_n:
        rows_out.append(
            {
                "row_kind": "META",
                "requested_last_n": max_n,
                "effective_last_n": have,
                "paths_loaded": have,
                "tie_skipped": "",
                "corpus_non_tie": "",
                "hits": "",
                "wins": "",
                "losses": "",
                "win_rate_pct_on_hits": "",
                "pnl_sum_usd": "",
                "ev_per_window_usd": "",
                "hit_rate_pct_of_corpus": "",
                "avg_pnl_usd_per_hit": "",
                "avg_entry_mid_on_hits": "",
                "note": f"WARNING: fewer snapshots than max requested ({have} < {max_n}); rows use effective_last_n",
            }
        )

    for n in ns:
        eff = min(n, have)
        if eff < 1:
            continue
        r = run_one_variant(
            corpus[:eff],
            variant_id=f"last_{eff}",
            skew_thr=_SKEW,
            streak=_STREAK,
            cheap_thr=_CHEAP,
        )
        note = ""
        if eff < n:
            note = f"requested_{n}_but_only_{eff}_paths"
        rows_out.append(
            {
                "row_kind": "RESULT",
                "requested_last_n": n,
                "effective_last_n": eff,
                "paths_loaded": have,
                "tie_skipped": r["windows_tie_skipped"],
                "corpus_non_tie": r["windows_in_corpus_non_tie"],
                "hits": r["hits"],
                "wins": r["wins"],
                "losses": r["losses"],
                "win_rate_pct_on_hits": r["win_rate_pct_on_hits"],
                "pnl_sum_usd": r["pnl_sum_usd"],
                "ev_per_window_usd": r["ev_per_window_usd"],
                "hit_rate_pct_of_corpus": r["hit_rate_pct_of_corpus"],
                "avg_pnl_usd_per_hit": r["avg_pnl_usd_per_hit"],
                "avg_entry_mid_on_hits": r["avg_entry_mid_on_hits"],
                "note": note,
            }
        )

    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows_out:
            w.writerow({k: row.get(k, "") for k in fieldnames})

    print(f"Wrote {out}")
    for row in rows_out:
        if row["row_kind"] != "RESULT":
            continue
        print(
            f"  n={row['effective_last_n']} (req {row['requested_last_n']}): "
            f"corpus={row['corpus_non_tie']} ties={row['tie_skipped']} hits={row['hits']} "
            f"pnl_sum={float(row['pnl_sum_usd']):+.2f} ev/window={float(row['ev_per_window_usd']):+.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
