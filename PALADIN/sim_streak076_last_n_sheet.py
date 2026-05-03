#!/usr/bin/env python3
"""
Simulate **strategy-1 slice1000** (same defaults as KNG6: skew **0.82**, streak **22** s, cheap **0.19**) on the **newest N** public **15m** windows,
then write a **CSV sheet**: summary totals & averages, then one row per window.

Rule: **22** consecutive seconds ``max(up,down) >= 0.82``, then first second either leg ``<= 0.19``;
**$1** stake at that mid; PnL ``1/entry-1`` if win else ``-1``; winner = higher final mid (ties skipped).

Run::

  python PALADIN/sim_streak076_last_n_sheet.py --last-n 100
  python PALADIN/sim_streak076_last_n_sheet.py --last-n 100 --out-csv exports/STREAK076_LAST100.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_PAL = Path(__file__).resolve().parent
for _p in (REPO, _PAL):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from paladin_v10_wallet_fit_simulator import (  # noqa: E402
    DEFAULT_PUBLIC_SNAPSHOTS,
    discover_public_btc15m_prices_paths,
    slug_from_public_prices_filename,
)
from simulate_paladin_window import (  # noqa: E402
    forward_fill_prices,
    load_prices_by_elapsed,
    resolve_winner_from_last_prices,
)
from sim_public_pool_cheap_winner_comeback import (  # noqa: E402
    Side,
    _first_cheap_after_skew_streak,
    _pnl_dollar_stake,
)

_SKEW = 0.82
_STREAK = 22
_CHEAP = 0.19


def _slug_epoch(p: Path) -> int:
    s = slug_from_public_prices_filename(p.name) or ""
    m = re.search(r"-(\d+)$", s)
    return int(m.group(1)) if m else 0


def paths_newest_first(snap: Path, *, cap: int) -> list[Path]:
    paths = discover_public_btc15m_prices_paths(snap, max_unique_slugs=max(cap * 3, 5000))
    paths.sort(key=_slug_epoch, reverse=True)
    return paths[:cap]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last-n", type=int, default=100, help="Newest N unique 15m snapshot paths.")
    ap.add_argument("--snapshots-dir", type=Path, default=DEFAULT_PUBLIC_SNAPSHOTS)
    ap.add_argument(
        "--out-csv",
        type=Path,
        default=None,
        help="Output CSV (default exports/STREAK076_LAST{n}_<utc>.csv).",
    )
    args = ap.parse_args()

    n_req = max(1, int(args.last_n))
    snap = args.snapshots_dir.resolve()
    if not snap.is_dir():
        print(f"Missing snapshots dir: {snap}", file=sys.stderr)
        return 2

    paths = paths_newest_first(snap, cap=n_req)
    from datetime import datetime, timezone

    out = args.out_csv
    if out is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out = (REPO / "exports" / f"STREAK076_LAST{n_req}_{ts}Z.csv").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    rows_detail: list[dict[str, str | float | int]] = []
    hits = wins = 0
    pnl_sum = 0.0
    entry_sum_hits = 0.0
    n_tie_skip = 0
    n_included = 0

    for p in paths:
        slug = slug_from_public_prices_filename(p.name) or p.stem
        raw = load_prices_by_elapsed(p)
        series = forward_fill_prices(raw, window_sec=900)
        winner, _, _ = resolve_winner_from_last_prices(series)
        if winner == "tie":
            n_tie_skip += 1
            rows_detail.append(
                {
                    "slug": slug,
                    "included": 0,
                    "hit": 0,
                    "side": "",
                    "entry_mid": "",
                    "winner": "",
                    "won": "",
                    "pnl_usd": 0.0,
                    "note": "tie_skip",
                }
            )
            continue

        n_included += 1
        side, entry = _first_cheap_after_skew_streak(
            series, skew_thr=_SKEW, streak=_STREAK, cheap_thr=_CHEAP
        )
        if side is None or entry is None:
            rows_detail.append(
                {
                    "slug": slug,
                    "included": 1,
                    "hit": 0,
                    "side": "",
                    "entry_mid": "",
                    "winner": winner,
                    "won": "",
                    "pnl_usd": 0.0,
                    "note": "",
                }
            )
            continue

        hits += 1
        won_b = side == winner
        if won_b:
            wins += 1
        pnl = _pnl_dollar_stake(entry, won_b)
        pnl_sum += pnl
        entry_sum_hits += float(entry)
        rows_detail.append(
            {
                "slug": slug,
                "included": 1,
                "hit": 1,
                "side": side,
                "entry_mid": round(float(entry), 6),
                "winner": winner,
                "won": 1 if won_b else 0,
                "pnl_usd": round(pnl, 6),
                "note": "",
            }
        )

    losses = hits - wins
    wr_pct = 100.0 * wins / hits if hits else 0.0
    ev_per_win = pnl_sum / n_included if n_included else 0.0
    avg_pnl_hit = pnl_sum / hits if hits else 0.0
    avg_entry_hit = entry_sum_hits / hits if hits else 0.0

    summary_rows = [
        {"row_kind": "SUMMARY", "metric": "strategy", "value": "strategy-1 slice1000 (22x max>=0.82 then cheap<=0.19), $1 stake"},
        {"row_kind": "SUMMARY", "metric": "snapshots_dir", "value": str(snap)},
        {"row_kind": "SUMMARY", "metric": "paths_requested_newest_first", "value": str(n_req)},
        {"row_kind": "SUMMARY", "metric": "windows_tie_skipped", "value": str(n_tie_skip)},
        {"row_kind": "SUMMARY", "metric": "windows_in_corpus_non_tie", "value": str(n_included)},
        {"row_kind": "SUMMARY", "metric": "trade_hits", "value": str(hits)},
        {"row_kind": "SUMMARY", "metric": "trade_wins", "value": str(wins)},
        {"row_kind": "SUMMARY", "metric": "trade_losses", "value": str(losses)},
        {"row_kind": "SUMMARY", "metric": "win_rate_pct", "value": f"{wr_pct:.4f}"},
        {"row_kind": "SUMMARY", "metric": "pnl_sum_usd", "value": f"{pnl_sum:.6f}"},
        {"row_kind": "SUMMARY", "metric": "ev_per_window_usd", "value": f"{ev_per_win:.6f}"},
        {"row_kind": "SUMMARY", "metric": "hit_rate_pct_of_corpus", "value": f"{(100.0 * hits / n_included) if n_included else 0.0:.4f}"},
        {"row_kind": "SUMMARY", "metric": "avg_pnl_usd_per_hit", "value": f"{avg_pnl_hit:.6f}"},
        {"row_kind": "SUMMARY", "metric": "avg_entry_mid_on_hits", "value": f"{avg_entry_hit:.6f}"},
    ]

    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["row_kind", "metric", "value", "slug", "included", "hit", "side", "entry_mid", "winner", "won", "pnl_usd", "note"],
            extrasaction="ignore",
        )
        w.writeheader()
        for r in summary_rows:
            w.writerow(
                {
                    "row_kind": r["row_kind"],
                    "metric": r["metric"],
                    "value": r["value"],
                    "slug": "",
                    "included": "",
                    "hit": "",
                    "side": "",
                    "entry_mid": "",
                    "winner": "",
                    "won": "",
                    "pnl_usd": "",
                    "note": "",
                }
            )
        w.writerow(
            {
                "row_kind": "DETAIL_HEADER",
                "metric": "",
                "value": "",
                "slug": "slug",
                "included": "included",
                "hit": "hit",
                "side": "side",
                "entry_mid": "entry_mid",
                "winner": "winner",
                "won": "won",
                "pnl_usd": "pnl_usd",
                "note": "note",
            }
        )
        for d in rows_detail:
            w.writerow(
                {
                    "row_kind": "WINDOW",
                    "metric": "",
                    "value": "",
                    "slug": d["slug"],
                    "included": d["included"],
                    "hit": d["hit"],
                    "side": d["side"],
                    "entry_mid": d["entry_mid"],
                    "winner": d["winner"],
                    "won": d["won"],
                    "pnl_usd": d["pnl_usd"],
                    "note": d["note"],
                }
            )

    print(f"Wrote {out}")
    print(f"  corpus_non_tie={n_included}  tie_skipped={n_tie_skip}  hits={hits}  wins={wins}  WR={wr_pct:.2f}%  pnl_sum={pnl_sum:+.2f}  ev/window={ev_per_win:+.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
