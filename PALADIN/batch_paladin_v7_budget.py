#!/usr/bin/env python3
"""
Batch-evaluate PALADIN v7 on recent BTC 15m windows that include Binance ``btc_volume``.

Param sources:
  - ``small_budget``: ``V7_SMALL_BUDGET_4ORDERS`` (legacy batch preset).
  - ``bot_config``: ``BotConfig()`` dataclass defaults (matches live engine knob mapping).

Reports aggregate settled PnL (last-mid proxy winner) for requested pool sizes.

Examples::

  python PALADIN/batch_paladin_v7_budget.py --param-source bot_config --all-windows --budget-usdc 80 --max-shares-per-side 10 --report exports/out.txt
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from dataclasses import dataclass, replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import simulate_dual_profit_hedge as dph  # noqa: E402
from config import BotConfig  # noqa: E402

from paladin_v7 import V7_SMALL_BUDGET_4ORDERS, PaladinV7Params, load_ticks_with_btc, run_window_v7
from simulate_paladin_window import (
    resolve_winner_from_last_prices,
    settled_pnl_usdc,
    window_slug_from_prices_csv,
)

DEFAULT_EXPORTS = REPO / "exports" / "window_price_snapshots_public"
WIN_EPS = 1e-6
POOLS_DEFAULT = (100, 200, 400)


def max_elapsed_in_csv(path: Path) -> int:
    mx = -1
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            mx = max(mx, int(float(row["elapsed_sec"])))
    return mx


def csv_has_btc_volume(path: Path) -> bool:
    with path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        if not r.fieldnames or "btc_volume" not in r.fieldnames:
            return False
        for row in r:
            if str(row.get("btc_volume", "")).strip():
                return True
    return False


def discover_windows_with_btc(
    exports_dir: Path,
    *,
    count: int | None,
    min_max_elapsed: int,
    slug_prefix: str = "btc-updown-15m-",
) -> list[Path]:
    all_csv = sorted(
        exports_dir.glob("*_prices.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    picked: list[Path] = []
    seen: set[str] = set()
    for p in all_csv:
        slug = window_slug_from_prices_csv(p)
        if "unknown" in slug or not slug.startswith(slug_prefix):
            continue
        if slug in seen:
            continue
        if max_elapsed_in_csv(p) < min_max_elapsed:
            continue
        if not csv_has_btc_volume(p):
            continue
        seen.add(slug)
        picked.append(p.resolve())
        if count is not None and len(picked) >= count:
            break
    return picked


def pm_series_from_ticks(ticks: list) -> list[tuple[float, float]]:
    return [(float(t.pm_u), float(t.pm_d)) for t in ticks]


def _minimal_bot_config_for_v7_defaults(*, strategy_budget_cap_usdc: float = 10.0) -> BotConfig:
    """``BotConfig`` requires key fields; sim reads v7 knobs aligned with ``BotConfig.from_env()`` for v7.

    Default ``10`` matches ``BotConfig.from_env()`` when ``BOT_STRATEGY_BUDGET_CAP_USDC`` is unset for
    ``paladin_v7``. Override with ``--budget-usdc`` (e.g. ``80`` to match a typical live ``.env``).
    """
    return BotConfig(
        private_key="0x" + "1" * 64,
        funder="0x" + "2" * 40,
        strategy_mode="paladin_v7",
        strategy_budget_cap_usdc=float(strategy_budget_cap_usdc),
    )


@dataclass(frozen=True)
class BudgetBatchPathsResult:
    paths: list[Path]
    skipped_short_collection: bool


def prepare_budget_batch_paths(
    *,
    exports_dir: Path,
    all_windows: bool,
    max_windows: int,
    min_max_elapsed: int,
    pools: tuple[int, ...],
) -> BudgetBatchPathsResult:
    """Discover and sort price CSV paths (same ordering as ``main``)."""
    collect_cap: int | None = None
    if all_windows:
        collect_cap = None
    else:
        collect_cap = max(max(pools), int(max_windows))
    paths = discover_windows_with_btc(
        exports_dir,
        count=collect_cap,
        min_max_elapsed=min_max_elapsed,
    )
    paths.sort(
        key=lambda p: dph.start_ts_from_slug(window_slug_from_prices_csv(p)),
        reverse=True,
    )
    max_pool = max(pools)
    need = max_pool if not all_windows else len(paths)
    skipped_short = bool(not all_windows and len(paths) < need)
    return BudgetBatchPathsResult(paths=paths, skipped_short_collection=skipped_short)


def simulate_v7_budget_on_paths(
    paths: list[Path],
    params: PaladinV7Params,
) -> tuple[list[float], list[int], list[str], int, int]:
    """Run v7 on each path; returns (pnls, orders_per_window, slugs, skipped_ticks, imbalanced_end)."""
    pnls: list[float] = []
    orders: list[int] = []
    slugs: list[str] = []
    skipped = 0
    imbalanced_end = 0
    for path in paths:
        slug, ticks = load_ticks_with_btc(path)
        if len(ticks) < 900 or not slug:
            skipped += 1
            continue
        st = run_window_v7(ticks, params=params)
        pm = pm_series_from_ticks(ticks)
        w, _, _ = resolve_winner_from_last_prices(pm)
        pnl = settled_pnl_usdc(st.snapshot_metrics(), w)
        pnls.append(float(pnl))
        n_ord = len(st.trades)
        orders.append(n_ord)
        slugs.append(slug)
        if abs(float(st.size_up) - float(st.size_down)) > 0.05:
            imbalanced_end += 1
    return pnls, orders, slugs, skipped, imbalanced_end


def format_v7_budget_report(
    *,
    exports_dir: Path,
    param_source: str,
    run_label: str,
    pools: tuple[int, ...],
    params: PaladinV7Params,
    all_windows: bool,
    pnls: list[float],
    orders: list[int],
    slugs: list[str],
    skipped: int,
    collected_paths: int,
    imbalanced_end: int,
) -> str:
    """Build the same UTF-8 report text as a single batch run."""
    lines: list[str] = []
    lines.append("paladin_v7_budget_batch | Binance volume windows | last-mid proxy winner")
    lines.append(f"exports={exports_dir}")
    lines.append(f"param_source={param_source}")
    if run_label:
        lines.append(f"run_label={run_label}")
    lines.append(
        f"preset=budget {params.budget_usdc} base_order {params.base_order_shares} max/side {params.max_shares_per_side} "
        f"layer2_dip={params.layer2_dip_below_avg} cheap_pair_sum_max={params.cheap_pair_sum_max} "
        f"cheap_hedge_slip={params.cheap_hedge_slip_buffer} "
        f"forced_hedge_max_book_sum={params.forced_hedge_max_book_sum}"
    )
    lines.append(f"windows_simulated={len(pnls)} skipped_empty_ticks={skipped} collected_paths={collected_paths}")
    if orders:
        lines.append(f"orders_per_window: min={min(orders)} max={max(orders)} mean={sum(orders)/len(orders):.2f}")
        lines.append(
            f"windows_imbalanced_at_end={imbalanced_end} "
            "(model inventory skew > 0.05 sh; live API reconcile/flatten not simulated)"
        )
    if pnls:
        grand = float(sum(pnls))
        lines.append(
            f"TOTAL_PNL_ALL_WINDOWS_USD={grand:.2f}\tn_windows={len(pnls)}\tmean={grand/len(pnls):.4f}"
        )
    else:
        lines.append("TOTAL_PNL_ALL_WINDOWS_USD=0.00\tn_windows=0")
    lines.append("")

    for n in pools:
        nn = min(n, len(pnls))
        if nn <= 0:
            lines.append(f"pool_{n}\t(no data)")
            continue
        sub = pnls[:nn]
        tot = sum(sub)
        wins = sum(1 for x in sub if x > WIN_EPS)
        osub = orders[:nn]
        lines.append(
            f"pool_{n}\tn={nn}\ttotal_pnl_usd={tot:.2f}\tmean={tot/nn:.4f}\t"
            f"win_rate_pct={100.0*wins/nn:.1f}\twins={wins}\t"
            f"avg_orders={sum(osub)/len(osub):.2f}"
        )

    if all_windows and pnls:
        nn = len(pnls)
        tot = sum(pnls)
        wins = sum(1 for x in pnls if x > WIN_EPS)
        lines.append(
            f"pool_all\tn={nn}\ttotal_pnl_usd={tot:.2f}\tmean={tot/nn:.4f}\t"
            f"win_rate_pct={100.0*wins/nn:.1f}\twins={wins}\t"
            f"avg_orders={sum(orders)/len(orders):.2f}"
        )

    if pnls:
        lines.append("")
        lines.append("--- distribution (full simulated prefix) ---")
        sd = statistics.pstdev(pnls) if len(pnls) >= 2 else 0.0
        lines.append(f"pnl_usd: min={min(pnls):.4f} max={max(pnls):.4f} stdev={sd:.4f}")
        sorted_p = sorted(pnls)
        for pct in (5, 25, 50, 75, 95):
            idx = min(len(sorted_p) - 1, max(0, int(round((pct / 100.0) * (len(sorted_p) - 1)))))
            lines.append(f"p{pct}_pnl_usd={sorted_p[idx]:.4f}")
        lines.append("")
        lines.append("--- worst 15 windows by settled PnL (proxy winner) ---")
        ranked = sorted(zip(pnls, slugs, orders), key=lambda x: x[0])[:15]
        for pnl, sl, no in ranked:
            lines.append(f"{pnl:+.4f}\torders={no}\t{sl}")
        grand = float(sum(pnls))
        lines.append("")
        lines.append("--- total PnL (all simulated windows; same as line after imbalance stats) ---")
        lines.append(
            f"TOTAL_PNL_ALL_WINDOWS_USD={grand:.2f}\tn_windows={len(pnls)}\tmean={grand/len(pnls):.4f}"
        )

    return "\n".join(lines) + "\n"


def paladin_v7_params_from_bot_config(cfg: BotConfig) -> PaladinV7Params:
    """Same field mapping as ``paladin_v7_live_engine._v7_params_from_config`` (sim has no API reconcile)."""
    return PaladinV7Params(
        budget_usdc=float(cfg.strategy_budget_cap_usdc),
        base_order_shares=float(cfg.paladin_v7_base_order_shares),
        max_shares_per_side=float(cfg.paladin_v7_max_shares_per_side),
        min_notional=float(cfg.paladin_v7_min_notional),
        min_shares=float(cfg.paladin_v7_min_shares),
        volume_lookback_sec=int(cfg.paladin_v7_volume_lookback_sec),
        volume_spike_ratio=float(cfg.paladin_v7_volume_spike_ratio),
        volume_floor=float(cfg.paladin_v7_volume_floor),
        btc_abs_move_min_usd=float(cfg.paladin_v7_btc_abs_move_min_usd),
        first_leg_max_pm=float(cfg.paladin_v7_first_leg_max_pm),
        cheap_other_margin=float(cfg.paladin_v7_cheap_other_margin),
        cheap_pair_sum_max=float(cfg.paladin_v7_cheap_pair_sum_max),
        cheap_pair_avg_sum_nonforced_max=float(cfg.paladin_v7_cheap_pair_avg_sum_nonforced_max),
        cheap_hedge_slip_buffer=float(cfg.paladin_v7_cheap_hedge_slip_buffer),
        cheap_hedge_min_delay_sec=float(cfg.paladin_v7_cheap_hedge_min_delay_sec),
        hedge_timeout_seconds=float(cfg.paladin_v7_hedge_timeout_seconds),
        forced_hedge_max_book_sum=float(cfg.paladin_v7_forced_hedge_max_book_sum),
        layer2_dip_below_avg=float(cfg.paladin_v7_layer2_dip_below_avg),
        pair_cooldown_sec=float(cfg.paladin_v7_pair_cooldown_sec),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch PALADIN v7 (small budget) on BTC+Binance windows")
    ap.add_argument("--exports-dir", type=Path, default=DEFAULT_EXPORTS)
    ap.add_argument(
        "--all-windows",
        action="store_true",
        help="Simulate every eligible *_prices.csv in --exports-dir (distinct slugs, btc_volume, coverage).",
    )
    ap.add_argument("--max-windows", type=int, default=400, help="Collect up to this many distinct slugs.")
    ap.add_argument("--min-max-elapsed", type=int, default=800)
    ap.add_argument(
        "--pools",
        type=str,
        default="100,200,400",
        help="Comma-separated pool sizes (prefix of collected list).",
    )
    ap.add_argument(
        "--param-source",
        choices=("small_budget", "bot_config"),
        default="small_budget",
        help="small_budget=V7_SMALL_BUDGET_4ORDERS; bot_config=BotConfig() defaults (live-aligned knobs).",
    )
    ap.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write the same summary (plus extras) to this UTF-8 text file.",
    )
    ap.add_argument(
        "--max-shares-per-side",
        type=float,
        default=None,
        help="Override PaladinV7Params.max_shares_per_side (bot_config/small_budget base).",
    )
    ap.add_argument(
        "--base-order-shares",
        type=float,
        default=None,
        help="Override PaladinV7Params.base_order_shares.",
    )
    ap.add_argument(
        "--run-label",
        type=str,
        default="",
        help="Printed in the report header for A/B identification.",
    )
    ap.add_argument(
        "--budget-usdc",
        type=float,
        default=10.0,
        help="Sim strategy cap (BOT_STRATEGY_BUDGET_CAP_USDC). Use 80 to mirror common live KNG3 .env.",
    )
    args = ap.parse_args()

    pools = tuple(int(x.strip()) for x in args.pools.split(",") if x.strip())
    if not pools:
        pools = POOLS_DEFAULT

    prep = prepare_budget_batch_paths(
        exports_dir=args.exports_dir,
        all_windows=args.all_windows,
        max_windows=args.max_windows,
        min_max_elapsed=args.min_max_elapsed,
        pools=pools,
    )
    paths = prep.paths
    max_pool = max(pools)
    need = max_pool if not args.all_windows else len(paths)
    if prep.skipped_short_collection:
        print(
            f"WARN: only {len(paths)} windows with btc_volume+coverage>={args.min_max_elapsed}; "
            f"requested pools up to {need}."
        )

    if args.param_source == "bot_config":
        params = paladin_v7_params_from_bot_config(
            _minimal_bot_config_for_v7_defaults(strategy_budget_cap_usdc=float(args.budget_usdc))
        )
    else:
        params = V7_SMALL_BUDGET_4ORDERS
    if args.max_shares_per_side is not None:
        params = replace(params, max_shares_per_side=float(args.max_shares_per_side))
    if args.base_order_shares is not None:
        params = replace(params, base_order_shares=float(args.base_order_shares))
    pnls, orders, slugs, skipped, imbalanced_end = simulate_v7_budget_on_paths(paths, params)
    text = format_v7_budget_report(
        exports_dir=args.exports_dir,
        param_source=args.param_source,
        run_label=args.run_label,
        pools=pools,
        params=params,
        all_windows=args.all_windows,
        pnls=pnls,
        orders=orders,
        slugs=slugs,
        skipped=skipped,
        collected_paths=len(paths),
        imbalanced_end=imbalanced_end,
    )
    print(text, end="")

    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
        print(f"Wrote report: {args.report}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
