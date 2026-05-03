#!/usr/bin/env python3
"""Sweep PRST1-style scalp params on the same ``last N`` enriched windows; find configs with high total PnL.

Preloads tapes once. $1 notional per trade. Writes top rows + first config meeting ``--target-pnl``."""

from __future__ import annotations

import argparse
import csv
import itertools
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import enrich_oldest_windows_with_btc_price as enrich

from PALADIN.sim_pm_btc_scalp_no_settle import (
    Row,
    entry_cheap_up,
    entry_either_cheap,
    entry_prst1_bidirectional_factory,
    entry_tight_band_cheap_up_factory,
    implied_up,
    buy_px,
    sell_px,
    scalp_window,
    load_rows,
)


def _last_n_paths(snap_dir: Path, n: int) -> list[Path]:
    wins = enrich.public_windows(snap_dir.resolve(), complete_only=True)
    wins.sort(key=lambda w: w.start_ts)
    n = max(0, n)
    sel = wins[-n:] if n else []
    return [w.path for w in sel]


def _preload(paths: list[Path]) -> list[tuple[str, list[Row]]]:
    out: list[tuple[str, list[Row]]] = []
    for path in paths:
        got = load_rows(path)
        if got is None:
            continue
        slug, rows = got
        out.append((slug, rows))
    return out


def _run_preloaded(
    tapes: list[tuple[str, list[Row]]],
    entry,
    *,
    sigma: float,
    slip: float,
    open_edge: float,
    min_net: float,
    max_hold: int,
    max_trades: int,
    cooldown: int,
    notional_usd: float,
) -> tuple[float, int, int, int, int, int, int]:
    tot = 0.0
    n_tr = 0
    w_traded = 0
    tp_all = 0
    to_all = 0
    win_all = 0
    for _slug, rows in tapes:
        start_btc = rows[0].btc
        pnl, nt, ntp, nto, nw = scalp_window(
            rows,
            entry=entry,
            start_btc=start_btc,
            sigma=sigma,
            slip=slip,
            open_edge=open_edge,
            min_net_profit=min_net,
            max_hold_sec=max_hold,
            max_trades=max_trades,
            cooldown_sec=cooldown,
            shares=None,
            notional_usd=notional_usd,
            exit_slip=None,
        )
        tot += pnl
        n_tr += nt
        tp_all += ntp
        to_all += nto
        win_all += nw
        if nt:
            w_traded += 1
    return tot, n_tr, len(tapes), w_traded, tp_all, to_all, win_all


@dataclass(slots=True)
class Trial:
    name: str
    sigma: float
    slip: float
    open_edge: float
    min_net: float
    max_hold: int
    max_trades: int
    cooldown: int
    band_lo: float | None
    band_hi: float | None
    total_pnl: float
    trades: int
    windows: int
    windows_traded: int
    tp_exits: int
    timeout_exits: int
    win_trades: int


def _iter_grids():
    """Staged grids (~120k trials) so 100 tapes stay interactive on one core."""
    bands = [
        (0.28, 0.72),
        (0.30, 0.70),
        (0.32, 0.68),
        (0.34, 0.66),
        (0.22, 0.78),
    ]

    def emit(kind: str, blo, bhi, mn_list, oe_list, sig_list, slip_list, hold_list, mt_list, cd_list):
        for mn, oe, sig, slip, hold, mt, cd in itertools.product(
            mn_list, oe_list, sig_list, slip_list, hold_list, mt_list, cd_list
        ):
            yield (kind, blo, bhi, mn, oe, sig, slip, hold, mt, cd)

    # Stage A — around prior PRST1 + more trade budget
    mn_a = [0.10, 0.08, 0.065, 0.05, 0.04, 0.03]
    oe_a = [0.025, 0.04, 0.055, 0.065, 0.08]
    sig_a = [100.0, 140.0, 180.0]
    slip_a = [0.006, 0.009, 0.012]
    hold_a = [90, 135, 180]
    mt_a = [10, 20, 35]
    cd_a = [0, 2]
    for kind in ("either_cheap", "cheap_up"):
        yield from emit(kind, None, None, mn_a, oe_a, sig_a, slip_a, hold_a, mt_a, cd_a)
    for blo, bhi in bands:
        for kind in ("tight_up", "bi_band"):
            yield from emit(kind, blo, bhi, mn_a, oe_a, sig_a, slip_a, hold_a, mt_a, cd_a)

    # Stage B — smaller TP / longer holds / more round-trips
    mn_b = [0.025, 0.02, 0.015, 0.012, 0.01]
    oe_b = [0.02, 0.03, 0.045, 0.06, 0.08]
    sig_b = [90.0, 130.0, 180.0]
    slip_b = [0.005, 0.008]
    hold_b = [75, 120, 180, 240]
    mt_b = [25, 40, 60]
    cd_b = [0, 1, 2]
    for kind in ("either_cheap", "cheap_up"):
        yield from emit(kind, None, None, mn_b, oe_b, sig_b, slip_b, hold_b, mt_b, cd_b)
    for blo, bhi in bands:
        for kind in ("tight_up", "bi_band"):
            yield from emit(kind, blo, bhi, mn_b, oe_b, sig_b, slip_b, hold_b, mt_b, cd_b)

    # Stage C — optimistic micro-slip + many trades (explore upper bound on this tape)
    mn_c = [0.01, 0.008, 0.006, 0.004, 0.003]
    oe_c = [0.018, 0.028, 0.04, 0.055]
    sig_c = [110.0, 150.0]
    slip_c = [0.002, 0.003, 0.004]
    hold_c = [100, 180, 260]
    mt_c = [50, 80]
    cd_c = [0, 1]
    for kind in ("either_cheap", "cheap_up"):
        yield from emit(kind, None, None, mn_c, oe_c, sig_c, slip_c, hold_c, mt_c, cd_c)
    for blo, bhi in bands:
        for kind in ("tight_up", "bi_band"):
            yield from emit(kind, blo, bhi, mn_c, oe_c, sig_c, slip_c, hold_c, mt_c, cd_c)


def _entry_for(kind: str, blo: float | None, bhi: float | None):
    if kind == "either_cheap":
        return entry_either_cheap
    if kind == "cheap_up":
        return entry_cheap_up
    if kind == "tight_up":
        assert blo is not None and bhi is not None
        return entry_tight_band_cheap_up_factory(blo, bhi)
    if kind == "bi_band":
        assert blo is not None and bhi is not None
        return entry_prst1_bidirectional_factory(blo, bhi)
    raise ValueError(kind)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snap-dir", type=Path, default=enrich.DEFAULT_SNAP_DIR)
    ap.add_argument("--last", type=int, default=100)
    ap.add_argument("--notional-usd", type=float, default=1.0)
    ap.add_argument("--target-pnl", type=float, default=50.0)
    ap.add_argument("--top-k", type=int, default=30)
    ap.add_argument("--out-csv", type=str, default="")
    args = ap.parse_args()

    paths = _last_n_paths(args.snap_dir.resolve(), int(args.last))
    if not paths:
        print("No windows.", file=sys.stderr)
        return 2
    tapes = _preload(paths)
    if not tapes:
        print("No loadable tapes.", file=sys.stderr)
        return 2

    print(f"Loaded {len(tapes)} tapes (requested paths={len(paths)})", flush=True)
    print(f"Target total_pnl >= ${args.target_pnl:.2f} | notional=${args.notional_usd}/trade", flush=True)

    best: list[Trial] = []
    hit: Trial | None = None
    n_done = 0
    for spec in _iter_grids():
        kind, blo, bhi, mn, oe, sig, slip, hold, mt, cd = spec
        entry = _entry_for(kind, blo, bhi)
        tot, n_tr, nw, wtd, tp, to_, wins = _run_preloaded(
            tapes,
            entry,
            sigma=sig,
            slip=slip,
            open_edge=oe,
            min_net=mn,
            max_hold=hold,
            max_trades=mt,
            cooldown=cd,
            notional_usd=float(args.notional_usd),
        )
        n_done += 1
        if n_done % 8000 == 0:
            print(f"  tried {n_done} configs, best so far ${best[0].total_pnl:.4f}" if best else f"  tried {n_done} configs", flush=True)

        tr = Trial(
            name=kind,
            sigma=sig,
            slip=slip,
            open_edge=oe,
            min_net=mn,
            max_hold=hold,
            max_trades=mt,
            cooldown=cd,
            band_lo=blo,
            band_hi=bhi,
            total_pnl=tot,
            trades=n_tr,
            windows=nw,
            windows_traded=wtd,
            tp_exits=tp,
            timeout_exits=to_,
            win_trades=wins,
        )
        best.append(tr)
        best.sort(key=lambda t: t.total_pnl, reverse=True)
        best[:] = best[: max(200, args.top_k)]

        if hit is None and tot >= float(args.target_pnl):
            hit = tr
            print(f"\n*** first config >= ${args.target_pnl:.2f} at trial {n_done}: ${tot:.4f} ({kind}) ***\n", flush=True)

    best.sort(key=lambda t: t.total_pnl, reverse=True)
    top = best[: args.top_k]

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    out = (
        Path(args.out_csv).resolve()
        if str(args.out_csv).strip()
        else _REPO / "exports" / "btc_binance_klines" / f"PRST1_SWEEP_LAST{args.last}_{ts}.csv"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "rank",
        "name",
        "band_lo",
        "band_hi",
        "sigma",
        "slip",
        "open_edge",
        "min_net",
        "max_hold",
        "max_trades",
        "cooldown",
        "total_pnl_usd",
        "trades",
        "windows",
        "windows_traded",
        "win_trades",
        "wr_pct",
        "avg_pnl_per_trade",
        "tp_exits",
        "timeout_exits",
    ]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i, t in enumerate(top, 1):
            wr = (100.0 * t.win_trades / t.trades) if t.trades else 0.0
            avg = (t.total_pnl / t.trades) if t.trades else 0.0
            w.writerow(
                {
                    "rank": i,
                    "name": t.name,
                    "band_lo": t.band_lo if t.band_lo is not None else "",
                    "band_hi": t.band_hi if t.band_hi is not None else "",
                    "sigma": t.sigma,
                    "slip": t.slip,
                    "open_edge": t.open_edge,
                    "min_net": t.min_net,
                    "max_hold": t.max_hold,
                    "max_trades": t.max_trades,
                    "cooldown": t.cooldown,
                    "total_pnl_usd": round(t.total_pnl, 6),
                    "trades": t.trades,
                    "windows": t.windows,
                    "windows_traded": t.windows_traded,
                    "win_trades": t.win_trades,
                    "wr_pct": round(wr, 4),
                    "avg_pnl_per_trade": round(avg, 6),
                    "tp_exits": t.tp_exits,
                    "timeout_exits": t.timeout_exits,
                }
            )

    print(f"\nEvaluated {n_done} configurations.")
    print(f"Wrote top {len(top)} -> {out}")
    if hit:
        t = hit
        print("\nFirst config meeting target:")
        print(
            f"  {t.name} band=({t.band_lo},{t.band_hi}) sig={t.sigma} slip={t.slip} oe={t.open_edge} "
            f"mn={t.min_net} hold={t.max_hold} max_tr={t.max_trades} cd={t.cooldown}"
        )
        print(f"  total_pnl=${t.total_pnl:.4f} trades={t.trades} avg/trade={t.total_pnl/max(t.trades,1):.6f}")
    else:
        t = top[0]
        print(f"\nTarget not met. Best total_pnl=${t.total_pnl:.4f}")
        print(
            f"  {t.name} band=({t.band_lo},{t.band_hi}) sig={t.sigma} slip={t.slip} oe={t.open_edge} "
            f"mn={t.min_net} hold={t.max_hold} max_tr={t.max_trades} cd={t.cooldown}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
