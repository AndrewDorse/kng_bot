#!/usr/bin/env python3
"""
A/B PALADIN v7 on the *same* discovered window list: default BotConfig caps vs larger caps.

Prints an explicit **TOTAL_PNL_ALL_WINDOWS_USD** block for both arms at the top (and each
full ``batch_paladin_v7_budget`` report below). Use this when a hand-written summary would
hide the grand total.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from batch_paladin_v7_budget import (  # noqa: E402
    DEFAULT_EXPORTS,
    POOLS_DEFAULT,
    _minimal_bot_config_for_v7_defaults,
    format_v7_budget_report,
    paladin_v7_params_from_bot_config,
    prepare_budget_batch_paths,
    simulate_v7_budget_on_paths,
)


def _grand_total(pnls: list[float]) -> float:
    return float(sum(pnls)) if pnls else 0.0


def _prefix_pnl(pnls: list[float], k: int) -> tuple[float, int]:
    """Sum of first ``min(k, len(pnls))`` windows (same order as batch ``pool_*`` prefixes)."""
    n = min(int(k), len(pnls))
    if n <= 0:
        return 0.0, 0
    s = float(sum(pnls[:n]))
    return s, n


def _ab_summary_lines(
    *,
    paths_n: int,
    na: int,
    nb: int,
    arm_a_label: str,
    arm_b_label: str,
    pnls_a: list[float],
    pnls_b: list[float],
    prefix_ns: tuple[int, ...],
) -> list[str]:
    ga, gb = _grand_total(pnls_a), _grand_total(pnls_b)
    lines = [
        "",
        "########## A/B PALADIN v7 - TOTAL PNL (sum over all simulated windows) ##########",
        f"shared_paths_collected={paths_n}  windows_simulated_A={na}  windows_simulated_B={nb}",
        f"[A] {arm_a_label}",
        f"    TOTAL_PNL_ALL_WINDOWS_USD={ga:.2f}\tn_windows={na}"
        + (f"\tmean={ga/na:.4f}" if na else ""),
        f"[B] {arm_b_label}",
        f"    TOTAL_PNL_ALL_WINDOWS_USD={gb:.2f}\tn_windows={nb}"
        + (f"\tmean={gb/nb:.4f}" if nb else ""),
        f"[B-A] TOTAL_PNL_DELTA_USD={gb - ga:.2f}",
        "--- AB prefix pools (first N windows = batch pool_N; same sort order) ---",
    ]
    for k in prefix_ns:
        sa, ua = _prefix_pnl(pnls_a, k)
        sb, ub = _prefix_pnl(pnls_b, k)
        if ua <= 0 and ub <= 0:
            lines.append(f"FIRST_{k}_WINDOWS: (no rows)")
            continue
        ma = sa / ua if ua else 0.0
        mb = sb / ub if ub else 0.0
        lines.append(
            f"[A] TOTAL_PNL_FIRST_{k}_WINDOWS_USD={sa:.2f}\tn_prefix={ua}\tmean={ma:.4f}"
        )
        lines.append(
            f"[B] TOTAL_PNL_FIRST_{k}_WINDOWS_USD={sb:.2f}\tn_prefix={ub}\tmean={mb:.4f}"
        )
        lines.append(f"[B-A] TOTAL_PNL_DELTA_FIRST_{k}_WINDOWS_USD={sb - sa:+.2f}")
    lines.append("###################################################################################")
    lines.append("")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="A/B v7 share caps on the same window pool")
    ap.add_argument("--exports-dir", type=Path, default=DEFAULT_EXPORTS)
    ap.add_argument("--max-windows", type=int, default=400)
    ap.add_argument("--min-max-elapsed", type=int, default=800)
    ap.add_argument("--pools", type=str, default="100,200,400")
    ap.add_argument(
        "--report-a",
        type=Path,
        default=None,
        help="Write arm A full report to this path.",
    )
    ap.add_argument("--report-b", type=Path, default=None, help="Write arm B full report.")
    args = ap.parse_args()

    pools = tuple(int(x.strip()) for x in args.pools.split(",") if x.strip())
    if not pools:
        pools = POOLS_DEFAULT

    prep = prepare_budget_batch_paths(
        exports_dir=args.exports_dir,
        all_windows=False,
        max_windows=args.max_windows,
        min_max_elapsed=args.min_max_elapsed,
        pools=pools,
    )
    paths = prep.paths
    max_pool = max(pools)
    need = max_pool
    if prep.skipped_short_collection:
        print(
            f"WARN: only {len(paths)} windows with btc_volume+coverage>={args.min_max_elapsed}; "
            f"requested pools up to {need}.",
            file=sys.stderr,
        )

    base = paladin_v7_params_from_bot_config(_minimal_bot_config_for_v7_defaults())
    arm_a_label = "AB_arm_A_botconfig_default_cap10_base5"
    arm_b_label = "AB_arm_B_cap20_base10"
    params_a = base
    params_b = replace(
        base,
        max_shares_per_side=20.0,
        base_order_shares=10.0,
    )

    pnls_a, ord_a, slug_a, skip_a, imb_a = simulate_v7_budget_on_paths(paths, params_a)
    pnls_b, ord_b, slug_b, skip_b, imb_b = simulate_v7_budget_on_paths(paths, params_b)

    na, nb = len(pnls_a), len(pnls_b)

    prefix_ns = (100, 200)
    summary = _ab_summary_lines(
        paths_n=len(paths),
        na=na,
        nb=nb,
        arm_a_label=arm_a_label,
        arm_b_label=arm_b_label,
        pnls_a=pnls_a,
        pnls_b=pnls_b,
        prefix_ns=prefix_ns,
    )
    print("\n".join(summary), end="")

    text_a = format_v7_budget_report(
        exports_dir=args.exports_dir,
        param_source="bot_config",
        run_label=arm_a_label,
        pools=pools,
        params=params_a,
        all_windows=False,
        pnls=pnls_a,
        orders=ord_a,
        slugs=slug_a,
        skipped=skip_a,
        collected_paths=len(paths),
        imbalanced_end=imb_a,
    )
    text_b = format_v7_budget_report(
        exports_dir=args.exports_dir,
        param_source="bot_config",
        run_label=arm_b_label,
        pools=pools,
        params=params_b,
        all_windows=False,
        pnls=pnls_b,
        orders=ord_b,
        slugs=slug_b,
        skipped=skip_b,
        collected_paths=len(paths),
        imbalanced_end=imb_b,
    )

    print("==================== ARM A (full report) ====================")
    print(text_a, end="")
    print("==================== ARM B (full report) ====================")
    print(text_b, end="")

    # Same numeric block as the opening banner (skip duplicate title line summary[1]).
    repeat = ["", "########## REPEAT: A/B summary (same as top banner) ##########"] + summary[2:-1] + [""]
    print("\n".join(repeat), end="")

    if args.report_a is not None:
        args.report_a.parent.mkdir(parents=True, exist_ok=True)
        args.report_a.write_text(text_a, encoding="utf-8")
        print(f"Wrote arm A: {args.report_a}", file=sys.stderr)
    if args.report_b is not None:
        args.report_b.parent.mkdir(parents=True, exist_ok=True)
        args.report_b.write_text(text_b, encoding="utf-8")
        print(f"Wrote arm B: {args.report_b}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
