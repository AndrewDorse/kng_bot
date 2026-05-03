#!/usr/bin/env python3
"""
Run **multiple skew-streak-cheap variants** on the **same newest N** public 15m windows,
using the same entry rule as ``_first_cheap_after_skew_streak`` and ``_pnl_dollar_stake``.

Default: **10** variants: strategy-1 **slice1000** (0.82 / 22s / 0.19) plus legacy/ablations, one CSV with per-variant
totals and averages; console prints variants sorted by EV per non-tie window.

Run::

  python PALADIN/sim_streak076_sweep_last_n.py --last-n 100
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
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
    _first_cheap_after_skew_streak,
    _pnl_dollar_stake,
)


# Ten variants: (id, skew_thr, streak_seconds, cheap_thr)
DEFAULT_VARIANTS: list[tuple[str, float, int, float]] = [
    ("strategy1_slice1000", 0.82, 22, 0.19),
    ("legacy_streak12_076_cheap19", 0.76, 12, 0.19),
    ("legacy_streak20_cheap19", 0.76, 20, 0.19),
    ("cheap18", 0.76, 20, 0.18),
    ("cheap20", 0.76, 20, 0.20),
    ("streak15", 0.76, 15, 0.19),
    ("streak25", 0.76, 25, 0.19),
    ("skew78", 0.78, 20, 0.19),
    ("skew74", 0.74, 20, 0.19),
    ("combo_078_15_cheap18", 0.78, 15, 0.18),
]


def _slug_epoch(p: Path) -> int:
    s = slug_from_public_prices_filename(p.name) or ""
    m = re.search(r"-(\d+)$", s)
    return int(m.group(1)) if m else 0


def paths_newest_first(snap: Path, *, cap: int) -> list[Path]:
    paths = discover_public_btc15m_prices_paths(snap, max_unique_slugs=max(cap * 3, 5000))
    paths.sort(key=_slug_epoch, reverse=True)
    return paths[:cap]


@dataclass
class WindowRow:
    slug: str
    series: list[tuple[float, float]]
    winner: str  # "up" | "down" | "tie"


def load_corpus(paths: list[Path]) -> list[WindowRow]:
    out: list[WindowRow] = []
    for p in paths:
        slug = slug_from_public_prices_filename(p.name) or p.stem
        raw = load_prices_by_elapsed(p)
        series = forward_fill_prices(raw, window_sec=900)
        winner, _, _ = resolve_winner_from_last_prices(series)
        out.append(WindowRow(slug=slug, series=series, winner=winner))
    return out


def run_one_variant(
    corpus: list[WindowRow],
    *,
    variant_id: str,
    skew_thr: float,
    streak: int,
    cheap_thr: float,
) -> dict[str, float | int | str]:
    hits = wins = 0
    pnl_sum = 0.0
    entry_sum_hits = 0.0
    n_tie_skip = 0
    n_included = 0

    for w in corpus:
        if w.winner == "tie":
            n_tie_skip += 1
            continue
        n_included += 1
        side, entry = _first_cheap_after_skew_streak(
            w.series, skew_thr=skew_thr, streak=streak, cheap_thr=cheap_thr
        )
        if side is None or entry is None:
            continue
        hits += 1
        won_b = side == w.winner  # type: ignore[comparison-overlap]
        if won_b:
            wins += 1
        pnl_sum += _pnl_dollar_stake(entry, won_b)
        entry_sum_hits += float(entry)

    losses = hits - wins
    wr_pct = 100.0 * wins / hits if hits else 0.0
    ev_per_win = pnl_sum / n_included if n_included else 0.0
    avg_pnl_hit = pnl_sum / hits if hits else 0.0
    avg_entry_hit = entry_sum_hits / hits if hits else 0.0
    hit_rate = 100.0 * hits / n_included if n_included else 0.0

    return {
        "variant_id": variant_id,
        "skew_thr": skew_thr,
        "streak_sec": streak,
        "cheap_thr": cheap_thr,
        "windows_tie_skipped": n_tie_skip,
        "windows_in_corpus_non_tie": n_included,
        "hits": hits,
        "wins": wins,
        "losses": losses,
        "win_rate_pct_on_hits": round(wr_pct, 4),
        "pnl_sum_usd": round(pnl_sum, 6),
        "ev_per_window_usd": round(ev_per_win, 6),
        "hit_rate_pct_of_corpus": round(hit_rate, 4),
        "avg_pnl_usd_per_hit": round(avg_pnl_hit, 6),
        "avg_entry_mid_on_hits": round(avg_entry_hit, 6),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last-n", type=int, default=100)
    ap.add_argument("--snapshots-dir", type=Path, default=DEFAULT_PUBLIC_SNAPSHOTS)
    ap.add_argument("--out-csv", type=Path, default=None)
    args = ap.parse_args()

    n_req = max(1, int(args.last_n))
    snap = args.snapshots_dir.resolve()
    if not snap.is_dir():
        print(f"Missing snapshots dir: {snap}", file=sys.stderr)
        return 2

    paths = paths_newest_first(snap, cap=n_req)
    corpus = load_corpus(paths)

    results: list[dict[str, float | int | str]] = []
    for vid, sk, st, ch in DEFAULT_VARIANTS:
        results.append(run_one_variant(corpus, variant_id=vid, skew_thr=sk, streak=st, cheap_thr=ch))

    results_sorted = sorted(results, key=lambda r: float(r["ev_per_window_usd"]), reverse=True)

    out = args.out_csv
    if out is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out = (REPO / "exports" / f"STREAK076_SWEEP_LAST{n_req}_{ts}Z.csv").resolve()
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "rank_by_ev",
        "variant_id",
        "skew_thr",
        "streak_sec",
        "cheap_thr",
        "windows_tie_skipped",
        "windows_in_corpus_non_tie",
        "hits",
        "wins",
        "losses",
        "win_rate_pct_on_hits",
        "pnl_sum_usd",
        "ev_per_window_usd",
        "hit_rate_pct_of_corpus",
        "avg_pnl_usd_per_hit",
        "avg_entry_mid_on_hits",
    ]

    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for i, r in enumerate(results_sorted, start=1):
            row = {k: r.get(k, "") for k in fieldnames if k != "rank_by_ev"}
            row["rank_by_ev"] = i
            w.writerow(row)

    best = results_sorted[0]
    print(f"Wrote {out}")
    print(f"Corpus: non_tie={best['windows_in_corpus_non_tie']}  tie_skipped={best['windows_tie_skipped']}  (same for all variants)\n")
    print("Rank (by EV per included window):")
    for i, r in enumerate(results_sorted, start=1):
        print(
            f"  {i:2}. {str(r['variant_id']):<24}  skew={r['skew_thr']} streak={r['streak_sec']} cheap={r['cheap_thr']}  "
            f"EV={float(r['ev_per_window_usd']):+.4f}  pnl_sum={float(r['pnl_sum_usd']):+.2f}  "
            f"hits={r['hits']} WR={float(r['win_rate_pct_on_hits']):.1f}%"
        )
    print(f"\nBest variant by EV/window: {best['variant_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
