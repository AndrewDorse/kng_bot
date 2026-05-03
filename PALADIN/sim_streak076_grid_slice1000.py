#!/usr/bin/env python3
"""
Grid-search skew / streak / cheap on the **newest** public 15m windows (default **1000**),
then rank configs that keep **every** disjoint **100-window** slice (non-overlapping, newest-first)
**strictly PnL-positive** on replay mids ($1 stake, same rules as ``run_one_variant``).

Outputs a CSV with the **best 5** passing configs (by total ``pnl_sum_usd``), plus a full-results CSV
if ``--out-all-csv`` is set.

Run::

  python PALADIN/sim_streak076_grid_slice1000.py
  python PALADIN/sim_streak076_grid_slice1000.py --out-top exports/STREAK_TOP5_SLICES.csv --out-all exports/STREAK_GRID_ALL.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_PAL = Path(__file__).resolve().parent
for _p in (REPO, _PAL):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from paladin_v10_wallet_fit_simulator import DEFAULT_PUBLIC_SNAPSHOTS  # noqa: E402
from sim_streak076_sweep_last_n import load_corpus, paths_newest_first, run_one_variant  # noqa: E402


@dataclass
class EvalRow:
    skew: float
    streak: int
    cheap: float
    full: dict[str, float | int | str]
    slice_pnls: list[float]
    min_slice_pnl: float
    all_slices_positive: bool
    n_slices: int


def _frange(lo: float, hi: float, step: float) -> list[float]:
    out: list[float] = []
    x = lo
    while x <= hi + 1e-9:
        out.append(round(x, 4))
        x += step
    return out


def evaluate(
    corpus: list,
    *,
    skew: float,
    streak: int,
    cheap: float,
    slice_size: int,
) -> EvalRow:
    vid = f"g_{skew}_{streak}_{cheap}"
    full = run_one_variant(
        corpus,
        variant_id=vid,
        skew_thr=skew,
        streak=streak,
        cheap_thr=cheap,
    )
    n = len(corpus)
    n_slices = n // slice_size
    slice_pnls: list[float] = []
    for s in range(n_slices):
        chunk = corpus[s * slice_size : (s + 1) * slice_size]
        r = run_one_variant(
            chunk,
            variant_id=f"{vid}_s{s}",
            skew_thr=skew,
            streak=streak,
            cheap_thr=cheap,
        )
        slice_pnls.append(float(r["pnl_sum_usd"]))
    min_sp = min(slice_pnls) if slice_pnls else float("-inf")
    all_pos = bool(slice_pnls) and all(p > 0.0 for p in slice_pnls)
    return EvalRow(
        skew=skew,
        streak=streak,
        cheap=cheap,
        full=full,
        slice_pnls=slice_pnls,
        min_slice_pnl=min_sp,
        all_slices_positive=all_pos,
        n_slices=len(slice_pnls),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--total-n", type=int, default=1000, help="Newest N paths to load (trimmed to available).")
    ap.add_argument("--slice-size", type=int, default=100)
    ap.add_argument("--snapshots-dir", type=Path, default=DEFAULT_PUBLIC_SNAPSHOTS)
    ap.add_argument("--out-top", type=Path, default=None, help="Top-5 passing configs CSV.")
    ap.add_argument("--out-all-csv", type=Path, default=None, help="Optional: every grid point CSV.")
    args = ap.parse_args()

    snap = args.snapshots_dir.resolve()
    if not snap.is_dir():
        print(f"Missing snapshots dir: {snap}", file=sys.stderr)
        return 2

    total_n = max(100, int(args.total_n))
    slice_sz = max(10, int(args.slice_size))
    paths = paths_newest_first(snap, cap=total_n)
    corpus_full = load_corpus(paths)
    have = len(corpus_full)
    if have < slice_sz:
        print(f"Need at least {slice_sz} windows; have {have}", file=sys.stderr)
        return 2

    use_n = min(total_n, have)
    corpus = corpus_full[:use_n]
    n_slices = use_n // slice_sz
    if n_slices < 1:
        print("Corpus too small for one slice", file=sys.stderr)
        return 2

    # Search grid (edit here if you want finer/coarser search)
    skews = _frange(0.70, 0.84, 0.02)
    streaks = list(range(6, 23, 2))  # 6..22 even
    cheaps = _frange(0.15, 0.22, 0.01)

    rows: list[EvalRow] = []
    n_grid = 0
    for sk in skews:
        for st in streaks:
            for ch in cheaps:
                n_grid += 1
                rows.append(evaluate(corpus, skew=sk, streak=st, cheap=ch, slice_size=slice_sz))

    passing = [r for r in rows if r.all_slices_positive]
    passing.sort(key=lambda r: float(r.full["pnl_sum_usd"]), reverse=True)
    top5 = passing[:5]

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_top = args.out_top
    if out_top is None:
        out_top = (REPO / "exports" / f"STREAK_SLICE1000_TOP5_{ts}Z.csv").resolve()
    out_top = out_top.resolve()
    out_top.parent.mkdir(parents=True, exist_ok=True)

    slice_cols = [f"slice_{i}_pnl_usd" for i in range(n_slices)]
    fieldnames = [
        "rank",
        "skew_thr",
        "streak_sec",
        "cheap_thr",
        "all_slices_positive",
        "n_slices",
        "min_slice_pnl_usd",
        "windows_non_tie_full",
        "tie_skipped_full",
        "hits_full",
        "wins_full",
        "win_rate_pct_on_hits_full",
        "pnl_sum_usd_full",
        "ev_per_window_usd_full",
        "hit_rate_pct_full",
    ] + slice_cols

    with out_top.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for i, r in enumerate(top5, start=1):
            row: dict[str, str | int | float] = {
                "rank": i,
                "skew_thr": r.skew,
                "streak_sec": r.streak,
                "cheap_thr": r.cheap,
                "all_slices_positive": int(r.all_slices_positive),
                "n_slices": r.n_slices,
                "min_slice_pnl_usd": round(r.min_slice_pnl, 6),
                "windows_non_tie_full": r.full["windows_in_corpus_non_tie"],
                "tie_skipped_full": r.full["windows_tie_skipped"],
                "hits_full": r.full["hits"],
                "wins_full": r.full["wins"],
                "win_rate_pct_on_hits_full": r.full["win_rate_pct_on_hits"],
                "pnl_sum_usd_full": r.full["pnl_sum_usd"],
                "ev_per_window_usd_full": r.full["ev_per_window_usd"],
                "hit_rate_pct_full": r.full["hit_rate_pct_of_corpus"],
            }
            for j, pnl in enumerate(r.slice_pnls):
                row[f"slice_{j}_pnl_usd"] = round(pnl, 6)
            for j in range(len(r.slice_pnls), n_slices):
                row[f"slice_{j}_pnl_usd"] = ""
            w.writerow(row)

    if args.out_all_csv is not None:
        all_path = args.out_all_csv.resolve()
        all_path.parent.mkdir(parents=True, exist_ok=True)
        fn = [
            "skew_thr",
            "streak_sec",
            "cheap_thr",
            "all_slices_positive",
            "min_slice_pnl_usd",
            "pnl_sum_usd_full",
            "ev_per_window_usd_full",
            "hits_full",
        ] + slice_cols
        with all_path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fn)
            w.writeheader()
            for r in rows:
                row = {
                    "skew_thr": r.skew,
                    "streak_sec": r.streak,
                    "cheap_thr": r.cheap,
                    "all_slices_positive": int(r.all_slices_positive),
                    "min_slice_pnl_usd": round(r.min_slice_pnl, 6),
                    "pnl_sum_usd_full": r.full["pnl_sum_usd"],
                    "ev_per_window_usd_full": r.full["ev_per_window_usd"],
                    "hits_full": r.full["hits"],
                }
                for j, pnl in enumerate(r.slice_pnls):
                    row[f"slice_{j}_pnl_usd"] = round(pnl, 6)
                for j in range(len(r.slice_pnls), n_slices):
                    row[f"slice_{j}_pnl_usd"] = ""
                w.writerow(row)

    print(f"Corpus: loaded={have} used={use_n} slice_size={slice_sz} n_slices={n_slices} grid={n_grid}")
    print(f"Passing all-slices-positive: {len(passing)}")
    print(f"Wrote top 5 (or fewer): {out_top}")
    if not top5:
        print("No config had every slice > 0. Run with --out-all-csv and relax criteria, or widen grid.", file=sys.stderr)
        # Fallback: best by min_slice_pnl then total pnl
        rows.sort(key=lambda r: (r.min_slice_pnl, float(r.full["pnl_sum_usd"])), reverse=True)
        top5 = rows[:5]
        with out_top.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for i, r in enumerate(top5, start=1):
                row = {
                    "rank": i,
                    "skew_thr": r.skew,
                    "streak_sec": r.streak,
                    "cheap_thr": r.cheap,
                    "all_slices_positive": int(r.all_slices_positive),
                    "n_slices": r.n_slices,
                    "min_slice_pnl_usd": round(r.min_slice_pnl, 6),
                    "windows_non_tie_full": r.full["windows_in_corpus_non_tie"],
                    "tie_skipped_full": r.full["windows_tie_skipped"],
                    "hits_full": r.full["hits"],
                    "wins_full": r.full["wins"],
                    "win_rate_pct_on_hits_full": r.full["win_rate_pct_on_hits"],
                    "pnl_sum_usd_full": r.full["pnl_sum_usd"],
                    "ev_per_window_usd_full": r.full["ev_per_window_usd"],
                    "hit_rate_pct_full": r.full["hit_rate_pct_of_corpus"],
                }
                for j, pnl in enumerate(r.slice_pnls):
                    row[f"slice_{j}_pnl_usd"] = round(pnl, 6)
                for j in range(len(r.slice_pnls), n_slices):
                    row[f"slice_{j}_pnl_usd"] = ""
                w.writerow(row)
        print("(Wrote fallback top 5 by min_slice_pnl × total pnl — not all slices positive.)")
    else:
        for i, r in enumerate(top5, start=1):
            print(
                f"  {i}. skew={r.skew} streak={r.streak} cheap={r.cheap} | "
                f"pnl_full={float(r.full['pnl_sum_usd']):+.2f} ev={float(r.full['ev_per_window_usd']):+.4f} | "
                f"min_slice={r.min_slice_pnl:+.2f} hits={r.full['hits']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
