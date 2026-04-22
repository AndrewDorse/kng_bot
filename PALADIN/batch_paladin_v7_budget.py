#!/usr/bin/env python3
"""
Batch-evaluate PALADIN v7 on recent BTC 15m windows that include Binance ``btc_volume``.

Default: ``V7_SMALL_BUDGET_4ORDERS`` ($10 budget, 5-share clips, max 10 sh/side, max 4 fills).
Reports aggregate settled PnL (last-mid proxy) for pool sizes 100, 200, 400.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from paladin_v7 import V7_SMALL_BUDGET_4ORDERS, PaladinV7Params, load_ticks_with_btc, run_window_v7
from simulate_paladin_window import (
    resolve_winner_from_last_prices,
    settled_pnl_usdc,
    window_slug_from_prices_csv,
)

REPO = Path(__file__).resolve().parents[1]
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
    count: int,
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
        if len(picked) >= count:
            break
    return picked


def pm_series_from_ticks(ticks: list) -> list[tuple[float, float]]:
    return [(float(t.pm_u), float(t.pm_d)) for t in ticks]


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch PALADIN v7 (small budget) on BTC+Binance windows")
    ap.add_argument("--exports-dir", type=Path, default=DEFAULT_EXPORTS)
    ap.add_argument("--max-windows", type=int, default=400, help="Collect up to this many distinct slugs.")
    ap.add_argument("--min-max-elapsed", type=int, default=800)
    ap.add_argument(
        "--pools",
        type=str,
        default="100,200,400",
        help="Comma-separated pool sizes (prefix of collected list).",
    )
    args = ap.parse_args()

    pools = tuple(int(x.strip()) for x in args.pools.split(",") if x.strip())
    if not pools:
        pools = POOLS_DEFAULT

    paths = discover_windows_with_btc(
        args.exports_dir,
        count=max(pools),
        min_max_elapsed=args.min_max_elapsed,
    )
    max_pool = max(pools)
    need = max_pool
    if len(paths) < need:
        print(
            f"WARN: only {len(paths)} windows with btc_volume+coverage>={args.min_max_elapsed}; "
            f"requested pools up to {need}."
        )

    params: PaladinV7Params = V7_SMALL_BUDGET_4ORDERS
    pnls: list[float] = []
    orders: list[int] = []
    skipped = 0

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
        orders.append(len(st.trades))

    print("paladin_v7_budget_batch | Binance volume windows | last-mid proxy winner")
    print(f"exports={args.exports_dir}")
    print(
        f"preset=budget {params.budget_usdc} clip {params.clip_shares} max/side {params.max_shares_per_side} "
        f"max_orders={params.max_orders}"
    )
    print(f"windows_simulated={len(pnls)} skipped_empty_ticks={skipped} collected_paths={len(paths)}")
    if orders:
        print(f"orders_per_window: min={min(orders)} max={max(orders)} mean={sum(orders)/len(orders):.2f}")
    print()

    for n in pools:
        nn = min(n, len(pnls))
        if nn <= 0:
            print(f"pool_{n}\t(no data)")
            continue
        sub = pnls[:nn]
        tot = sum(sub)
        wins = sum(1 for x in sub if x > WIN_EPS)
        osub = orders[:nn]
        print(
            f"pool_{n}\tn={nn}\ttotal_pnl_usd={tot:.2f}\tmean={tot/nn:.4f}\t"
            f"win_rate_pct={100.0*wins/nn:.1f}\twins={wins}\t"
            f"avg_orders={sum(osub)/len(osub):.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
