#!/usr/bin/env python3
"""Last-N enriched windows: compare PRST1-style configs under **realistic** friction.

The prior tape sweep used ``slip=0.002``, which is far below typical Polymarket
taker cost and **overstates** PnL; ``EITHER_CHEAP`` also churns into one-way PM
legs (buy/sell into a falling book). This script prints a small CSV + table:

- **Conservative UP** (tight band): production-shaped defaults.
- **Either-cheap** with symmetric **0.010** slip and with **wider exit** slip
  (``exit_slip=0.014``) to approximate bid/ask on both legs.
- **Tape-overfit row** (0.002 slip): shown as a warning baseline only."""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import enrich_oldest_windows_with_btc_price as enrich

from PALADIN.sim_pm_btc_scalp_no_settle import (
    entry_either_cheap,
    entry_tight_band_cheap_up_factory,
    run_strategy,
)


def _last_n_paths(snap_dir: Path, n: int) -> list[Path]:
    wins = enrich.public_windows(snap_dir.resolve(), complete_only=True)
    wins.sort(key=lambda w: w.start_ts)
    n = max(0, n)
    sel = wins[-n:] if n else []
    return [w.path for w in sel]


@dataclass(frozen=True, slots=True)
class Scenario:
    label: str
    entry_kind: str  # tight_up | either
    sigma: float
    slip: float
    exit_slip: float | None
    open_edge: float
    min_net: float
    band_lo: float
    band_hi: float
    max_hold: int
    max_trades: int
    cooldown: int


def _entry(sc: Scenario):
    if sc.entry_kind == "tight_up":
        return entry_tight_band_cheap_up_factory(sc.band_lo, sc.band_hi)
    return entry_either_cheap


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snap-dir", type=Path, default=enrich.DEFAULT_SNAP_DIR)
    ap.add_argument("--last", type=int, default=100)
    ap.add_argument("--notional-usd", type=float, default=1.0)
    ap.add_argument("--out-csv", type=str, default="")
    args = ap.parse_args()

    paths = _last_n_paths(args.snap_dir.resolve(), int(args.last))
    if not paths:
        print("No windows.", file=sys.stderr)
        return 2

    scenarios: tuple[Scenario, ...] = (
        Scenario(
            label="tight_up_conservative",
            entry_kind="tight_up",
            sigma=130.0,
            slip=0.008,
            exit_slip=0.012,
            open_edge=0.065,
            min_net=0.10,
            band_lo=0.32,
            band_hi=0.68,
            max_hold=135,
            max_trades=10,
            cooldown=2,
        ),
        Scenario(
            label="tight_up_harsher_exit",
            entry_kind="tight_up",
            sigma=130.0,
            slip=0.008,
            exit_slip=0.016,
            open_edge=0.065,
            min_net=0.10,
            band_lo=0.32,
            band_hi=0.68,
            max_hold=135,
            max_trades=10,
            cooldown=2,
        ),
        Scenario(
            label="either_cheap_realistic_sym",
            entry_kind="either",
            sigma=110.0,
            slip=0.010,
            exit_slip=None,
            open_edge=0.028,
            min_net=0.008,
            band_lo=0.32,
            band_hi=0.68,
            max_hold=100,
            max_trades=50,
            cooldown=0,
        ),
        Scenario(
            label="either_cheap_realistic_asym",
            entry_kind="either",
            sigma=110.0,
            slip=0.008,
            exit_slip=0.014,
            open_edge=0.028,
            min_net=0.012,
            band_lo=0.32,
            band_hi=0.68,
            max_hold=100,
            max_trades=30,
            cooldown=1,
        ),
        Scenario(
            label="either_cheap_tape_overfit_DO_NOT_LIVE",
            entry_kind="either",
            sigma=110.0,
            slip=0.002,
            exit_slip=None,
            open_edge=0.028,
            min_net=0.008,
            band_lo=0.32,
            band_hi=0.68,
            max_hold=100,
            max_trades=50,
            cooldown=0,
        ),
    )

    rows_out: list[dict[str, object]] = []
    print(
        f"PRST1 realistic sheet | last {args.last} windows | paths={len(paths)} | "
        f"${args.notional_usd}/trade\n",
        flush=True,
    )
    for sc in scenarios:
        r = run_strategy(
            paths,
            _entry(sc),
            sigma=sc.sigma,
            slip=sc.slip,
            open_edge=sc.open_edge,
            min_net=sc.min_net,
            max_hold=sc.max_hold,
            max_trades=sc.max_trades,
            cooldown=sc.cooldown,
            shares=None,
            notional_usd=float(args.notional_usd),
            exit_slip=sc.exit_slip,
        )
        wr = (100.0 * r.win_trades / r.trades) if r.trades else 0.0
        avg = (r.total_pnl / r.trades) if r.trades else 0.0
        rows_out.append(
            {
                "label": sc.label,
                "entry": sc.entry_kind,
                "slip": sc.slip,
                "exit_slip": sc.exit_slip if sc.exit_slip is not None else sc.slip,
                "sigma": sc.sigma,
                "open_edge": sc.open_edge,
                "min_net": sc.min_net,
                "max_hold": sc.max_hold,
                "max_trades": sc.max_trades,
                "cooldown": sc.cooldown,
                "windows": r.windows,
                "windows_traded": r.windows_traded,
                "trades": r.trades,
                "win_trades": r.win_trades,
                "wr_pct": round(wr, 2),
                "total_pnl_usd": round(r.total_pnl, 4),
                "avg_pnl_per_trade_usd": round(avg, 6),
                "tp_exits": r.tp_exits,
                "timeout_exits": r.timeout_exits,
            }
        )
        print(
            f"  {sc.label:42s}  PnL=${r.total_pnl:8.4f}  trades={r.trades:4d}  "
            f"WR={wr:5.1f}%  slip={sc.slip} exit_slip={sc.exit_slip or sc.slip}",
            flush=True,
        )

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    out = (
        Path(args.out_csv).resolve()
        if str(args.out_csv).strip()
        else _REPO / "exports" / "btc_binance_klines" / f"PRST1_REALISTIC_LAST{args.last}_{ts}.csv"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        for row in rows_out:
            w.writerow(row)
    print(f"\nWrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
