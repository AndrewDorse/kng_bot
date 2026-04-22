#!/usr/bin/env python3
"""
A/B PALADIN on the N most recent BTC price windows.

BASE (canonical): post-fill avg_up+avg_down capped at 0.97, symmetric fallback
  obeys blend cap (skip_first_blend=False), 10/side, gap 100s, force hedge 45s.

Variants:
  L — Legacy: blended cap 1.03 + skip_first_blend=True (old default)
  A — Same as BASE (strict 0.97); empty overrides
  E — BASE + force hedge 90s
  I — BASE + second_leg_must_improve_leg_avg (doc-style hedge discipline)
  B/C/D — book/ROI variants (optional)

Examples:
  python ab_second_leg_recent10.py --count 100 --count 200 --variants L,A,E,I
  python ab_second_leg_recent10.py --count 100 --variants E --max-shares-per-side 10 --max-shares-per-side 20
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path

from calibrate_ladder_wallet_windows import discover_windows_recent
from paladin_engine import PaladinParams
from simulate_paladin_window import (
    forward_fill_prices,
    load_prices_by_elapsed,
    resolve_winner_from_last_prices,
    run_window,
    settled_pnl_usdc,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PRICES_DIR = REPO_ROOT / "exports" / "window_price_snapshots_public"

PL = PaladinParams(
    profit_lock_min_shares_per_side=100.0,
    roi_lock_min_each=0.99,
    profit_lock_usdc_each_scenario=float("inf"),
)

BASE_KW: dict = {
    "budget_usdc": 80.0,
    "params": PL,
    "pair_sum_max": 1.0,
    "single_leg_max_px": 0.55,
    "pair_only": True,
    "stagger_pair_entry": True,
    "stagger_hedge_force_after_seconds": 45.0,
    "target_min_roi": 0.0,
    "cooldown_seconds": 0.0,
    "dynamic_clip_cap": 12.0,
    "pair_size_pick": "max_feasible",
    "max_shares_per_side": 10.0,
    "pair_sum_tighten_per_fill": 0.0,
    "pair_sum_min_floor": 0.90,
    "second_leg_book_improve_eps": 0.013,
    "max_blended_pair_avg_sum": 0.97,
    "pending_hedge_bypass_imbalance_shares": 10.0,
    "discipline_relax_after_forced_sec": 60.0,
    "min_elapsed_for_flat_open": 24,
    "stagger_winning_side_first_when_position": False,
    "stagger_symmetric_fallback_when_balanced": True,
    "stagger_symmetric_fallback_roi_discount": 0.03,
    "stagger_symmetric_fallback_skip_first_leg_blend_cap": False,
    "stagger_alternate_first_leg_when_balanced": True,
    "min_elapsed_between_pair_starts": 100.0,
    "entry_trailing_min_low_seconds": None,
    "entry_trailing_low_slippage": 0.02,
    "second_leg_must_improve_leg_avg": False,
}

VARIANT_DEFS: dict[str, tuple[str, dict]] = {
    "L": (
        "L_legacy_blended103_skip1stblend",
        {
            "max_blended_pair_avg_sum": 1.03,
            "stagger_symmetric_fallback_skip_first_leg_blend_cap": True,
        },
    ),
    "A": ("A_strict_blended097_force45s", {}),
    "B": ("B_tighter_book_eps0032", {"second_leg_book_improve_eps": 0.032}),
    "C": ("C_target_min_roi_3pct", {"target_min_roi": 0.03}),
    "D": ("D_second_leg_must_improve_only", {"second_leg_must_improve_leg_avg": True}),
    "E": ("E_strict097_force90s", {"stagger_hedge_force_after_seconds": 90.0}),
    "I": (
        "I_strict097_force45_improve2nd",
        {"second_leg_must_improve_leg_avg": True},
    ),
}


def run_batch(
    *,
    windows: list,
    variant_keys: list[str],
    max_shares_per_side_opts: list[float] | None = None,
) -> list[tuple[str, float, float, int, float, float, float]]:
    """Returns rows: label, mean_pnl, total_pnl, n_pos, mean_spent, total_spent, mean_trades."""
    rows: list[tuple[str, float, float, int, float, float, float]] = []
    nwin = len(windows)
    cap_opts: list[float | None]
    if max_shares_per_side_opts:
        cap_opts = [float(x) for x in max_shares_per_side_opts]
    else:
        cap_opts = [None]

    for max_sh in cap_opts:
        for key in variant_keys:
            label, overrides = VARIANT_DEFS[key]
            kw = deepcopy(BASE_KW)
            pl = kw.pop("params")
            kw.update(overrides)
            kw["params"] = pl
            if max_sh is not None:
                kw["max_shares_per_side"] = float(max_sh)

            pnls: list[float] = []
            spent_list: list[float] = []
            trades_n: list[int] = []
            pair_avg: list[float] = []

            for w in windows:
                raw = load_prices_by_elapsed(w.prices_csv)
                series = forward_fill_prices(raw)
                st = run_window(series, **kw)
                win_side, _, _ = resolve_winner_from_last_prices(series)
                pnl = settled_pnl_usdc(st.snapshot_metrics(), win_side)
                pnls.append(float(pnl))
                spent_list.append(float(st.spent_usdc))
                trades_n.append(len(st.trades))
                if st.size_up > 1e-9 and st.size_down > 1e-9:
                    pair_avg.append(float(st.avg_up + st.avg_down))
                else:
                    pair_avg.append(float("nan"))

            n = len(pnls)
            mean_p = sum(pnls) / n
            tot_p = sum(pnls)
            mean_sp = sum(spent_list) / n
            tot_sp = sum(spent_list)
            pos = sum(1 for x in pnls if x > 1e-9)
            mean_tr = sum(trades_n) / n
            avg_pair_cost = sum(x for x in pair_avg if x == x) / max(
                1, sum(1 for x in pair_avg if x == x)
            )

            row_label = label
            if max_sh is not None:
                row_label = f"{label} [max {max_sh:g}/side]"
            rows.append((row_label, mean_p, tot_p, pos, mean_sp, tot_sp, mean_tr))
            print(
                f"{row_label}\n"
                f"  mean_settle_pnl={mean_p:.4f}  total={tot_p:.2f}  +windows={pos}/{nwin}  "
                f"mean_spent={mean_sp:.2f}  total_spent={tot_sp:.2f}  mean_trades={mean_tr:.1f}\n"
                f"  mean(avg_up+avg_down) when both legs exist ~ {avg_pair_cost:.4f}\n"
            )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="A/B PALADIN variants on recent windows")
    ap.add_argument(
        "--count",
        type=int,
        action="append",
        dest="counts",
        default=None,
        help="Number of recent windows (repeat for multiple batches, e.g. --count 100 --count 200).",
    )
    ap.add_argument(
        "--variants",
        type=str,
        default="L,A,E,I",
        help="Comma-separated keys: L,A,B,C,D,E,I (default L,A,E,I).",
    )
    ap.add_argument("--min-max-elapsed", type=int, default=800)
    ap.add_argument(
        "--max-shares-per-side",
        type=float,
        action="append",
        dest="max_shares_per_side",
        default=None,
        help="Repeat to sweep inventory cap (e.g. --max-shares-per-side 10 --max-shares-per-side 20).",
    )
    args = ap.parse_args()

    counts = args.counts if args.counts else [10]
    keys = [k.strip().upper() for k in args.variants.split(",") if k.strip()]
    for k in keys:
        if k not in VARIANT_DEFS:
            raise SystemExit(f"Unknown variant {k!r}; choose from {sorted(VARIANT_DEFS)}")

    for n in counts:
        windows = discover_windows_recent(
            prices_dir=PRICES_DIR, count=n, min_max_elapsed=int(args.min_max_elapsed)
        )
        if not windows:
            raise SystemExit("No windows matched (check exports/window_price_snapshots_public).")

        print("=" * 72)
        caps = args.max_shares_per_side
        cap_s = ",".join(str(int(c)) for c in caps) if caps else "default"
        print(f"WINDOW COUNT = {n}  |  variants = {','.join(keys)}  |  max_sh/side = {cap_s}")
        print(f"First slug: {windows[0].slug}  |  Last slug: {windows[-1].slug}")
        print("=" * 72)
        print()

        summary = run_batch(
            windows=windows,
            variant_keys=keys,
            max_shares_per_side_opts=args.max_shares_per_side,
        )

        print(f"=== Rank by mean settle PnL (n={n} windows) ===")
        for label, mean_p, tot_p, pos, mean_sp, tot_sp, mean_tr in sorted(
            summary, key=lambda r: r[1], reverse=True
        ):
            print(
                f"  {mean_p:.4f}  {label}  | total_pnl={tot_p:.2f} spend={tot_sp:.1f} "
                f"+{pos}/{n} mean_trades={mean_tr:.1f}"
            )
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
