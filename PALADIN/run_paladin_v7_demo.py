#!/usr/bin/env python3
"""
Run PALADIN v7 on 10 recent BTC 15m windows that include Binance ``btc_price`` + ``btc_volume``.

Prints, per window: slug, all simulated buys, then the 30-row end-of-30s bucket table (prices + inventory).
"""

from __future__ import annotations

import csv
from pathlib import Path

from paladin_v7 import PaladinV7Params, load_ticks_with_btc, run_window_v7
from simulate_paladin_window import (
    forward_fill_prices,
    load_prices_by_elapsed,
    print_sim_bucket_table,
    replay_inventory_from_trades,
    resolve_winner_from_last_prices,
    settled_pnl_usdc,
)

REPO = Path(__file__).resolve().parents[1]
PRICES_DIR = REPO / "exports" / "window_price_snapshots_public"


def _has_btc_columns(path: Path) -> bool:
    with path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        if not r.fieldnames:
            return False
        if "btc_volume" not in r.fieldnames or "btc_price" not in r.fieldnames:
            return False
        row = next(r, None)
        if not row:
            return False
        return bool(str(row.get("btc_volume", "")).strip())


def discover_10_with_btc() -> list[Path]:
    out: list[Path] = []
    for p in sorted(PRICES_DIR.glob("*_prices.csv"), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.name.startswith("202") and "btc-updown-15m-" not in p.name:
            continue
        if not _has_btc_columns(p):
            continue
        out.append(p)
        if len(out) >= 10:
            break
    return out


def _p(*args: object, **kwargs: object) -> None:
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)


def main() -> int:
    paths = discover_10_with_btc()
    if len(paths) < 10:
        _p(f"WARN: only {len(paths)} windows with btc_price+btc_volume; need exports enrichment.")
    params = PaladinV7Params()
    for idx, path in enumerate(paths, start=1):
        slug, ticks = load_ticks_with_btc(path)
        if len(ticks) < 900 or not slug:
            _p(f"\n=== [{idx}] SKIP {path.name} (no ticks/slug) ===\n")
            continue
        st = run_window_v7(ticks, params=params)
        by_e = load_prices_by_elapsed(path)
        pm_series = forward_fill_prices(by_e)
        sim_states = replay_inventory_from_trades(st.trades)
        w, _, _ = resolve_winner_from_last_prices(pm_series)
        pnl = settled_pnl_usdc(st.snapshot_metrics(), w)

        _p()
        _p("=" * 88)
        _p(f"=== PALADIN v7 window [{idx}/{len(paths)}] slug={slug}")
        _p(f"file={path.name}")
        _p(
            f"params: clip={params.clip_shares} max/side={params.max_shares_per_side} "
            f"budget={params.budget_usdc} vol_ratio={params.volume_spike_ratio} "
            f"lookback={params.volume_lookback_sec}s btc_move>={params.btc_abs_move_min_usd} "
            f"hedge_timeout={params.hedge_timeout_seconds}s"
        )
        _p(f"trades={len(st.trades)} spent={st.spent_usdc:.2f} proxy_winner={w} settled_pnl_usdc={pnl:.2f}")
        _p("-" * 88)
        _p("all_buys: elapsed_sec\tside\tshares\tprice\tnotional\treason")
        for tr in st.trades:
            _p(
                f"{tr.elapsed_sec}\t{tr.side}\t{tr.shares:.4f}\t{tr.price:.4f}\t"
                f"{tr.notional:.2f}\t{tr.reason}"
            )
        print_sim_bucket_table(pm_series, sim_states)
        _p()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
