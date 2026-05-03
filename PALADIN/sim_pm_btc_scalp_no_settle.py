#!/usr/bin/env python3
"""Scalp strategies on public PM+BTC tapes: buy then sell within the window (no settlement hold).

Models **spot vs PM** dislocation with a simple BTC→fair-Up map, then scalps when the book
is off that fair value by ``open_edge``; exits when **net** PnL/share ≥ ``min_net_profit``
(buy at mid+slip, sell at mid−``exit_slip`` when set else same slip), or ``max_hold_sec``
timeout, or tape end.

Rules: **≤10 round-trips per window**, optional cooldown between trades.

Strategies (each uses a tanh **implied_up(btc, start)** fair, then compares to PM mids):

1. **cheap_UP** — implied UP above market by ``open_edge`` → buy UP, sell when net ≥ ``min_net`` or time.
2. **cheap_DOWN** — implied DOWN (1−implied) above ``down`` by ``open_edge`` → buy DOWN.
3. **fade_rich_UP** — market UP above implied by ``open_edge`` → buy DOWN (fade rich UP).
4. **fade_rich_DOWN** — market DOWN rich vs implied → buy UP.
5. **tight_band_cheap_UP** — like (1) but only when ``0.36 ≤ up ≤ 0.64`` (near coin-flip liquidity).
6. **either_cheap** — each second, trade the side (UP or DOWN) with the larger cheapness if both ≥ edge.
"""

from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

Side = Literal["UP", "DOWN"]


@dataclass(slots=True)
class Row:
    el: int
    btc: float
    up: float
    dn: float


EntryFn = Callable[[list[Row], int, float, float], Side | None]


@dataclass(slots=True)
class ScalpResult:
    total_pnl: float
    trades: int
    windows: int
    windows_traded: int
    tp_exits: int
    timeout_exits: int
    win_trades: int


def load_rows(path: Path) -> tuple[str, list[Row]] | None:
    with path.open(newline="", encoding="utf-8") as f:
        rdr = list(csv.DictReader(f))
    if not rdr:
        return None
    slug = (rdr[0].get("slug") or "").strip()
    out: list[Row] = []
    for row in rdr:
        try:
            el = int(float(row["elapsed_sec"]))
        except (ValueError, KeyError):
            continue
        br = (row.get("btc_price") or "").strip()
        if not br:
            continue
        try:
            btc = float(br)
        except ValueError:
            continue
        if btc <= 0:
            continue
        try:
            up = float(row["up_price"])
            dn = float(row["down_price"])
        except (ValueError, KeyError):
            continue
        out.append(Row(el=el, btc=btc, up=up, dn=dn))
    if len(out) < 30:
        return None
    out.sort(key=lambda r: r.el)
    return slug, out


def implied_up(btc: float, start: float, sigma: float) -> float:
    """Smooth fair UP in (0.06, 0.94) from BTC distance to window start."""
    x = (btc - start) / max(sigma, 1e-6)
    return max(0.06, min(0.94, 0.5 + 0.44 * math.tanh(x * 1.35)))


def buy_px(side: Side, r: Row, slip: float) -> float:
    px = r.up if side == "UP" else r.dn
    return min(px + slip, 0.995)


def sell_px(side: Side, r: Row, slip: float) -> float:
    px = r.up if side == "UP" else r.dn
    return max(px - slip, 0.005)


def _next_index_after_el(rows: list[Row], from_idx: int, min_el: int) -> int:
    k = from_idx + 1
    while k < len(rows) and rows[k].el < min_el:
        k += 1
    return k


def scalp_window(
    rows: list[Row],
    *,
    entry: EntryFn,
    start_btc: float,
    sigma: float,
    slip: float,
    open_edge: float,
    min_net_profit: float,
    max_hold_sec: int,
    max_trades: int,
    cooldown_sec: int,
    shares: float | None = None,
    notional_usd: float | None = None,
    exit_slip: float | None = None,
) -> tuple[float, int, int, int, int]:
    """Returns (pnl_usdc, n_trades, n_tp_exits, n_timeout_eot_exits, n_win_trades).

    If ``notional_usd`` is set, each trade deploys ~that many dollars at entry
    (``shares = notional_usd / entry_buy``); ``shares`` must be None in that mode.

    ``exit_slip`` (optional): extra pessimism on sells vs ``slip`` (crossing the
    spread twice). Defaults to ``slip`` when omitted.
    """
    if (shares is None) == (notional_usd is None):
        raise ValueError("Set exactly one of shares= or notional_usd=")
    slip_x = float(exit_slip) if exit_slip is not None else float(slip)
    pnl = 0.0
    trades = 0
    n_tp = 0
    n_to = 0
    n_win = 0
    i = 0
    while i < len(rows) and trades < max_trades:
        imp = implied_up(rows[i].btc, start_btc, sigma)
        side = entry(rows, i, imp, open_edge)
        if side is None:
            i += 1
            continue
        if i + 1 >= len(rows):
            break

        entry_buy = buy_px(side, rows[i], slip)
        trade_shares = (notional_usd / entry_buy) if notional_usd is not None else float(shares)
        entry_el = rows[i].el
        exit_j = i + 1
        brk = False
        for j in range(i + 1, len(rows)):
            dt = rows[j].el - entry_el
            if dt > max_hold_sec:
                exit_j = j if j == i + 1 else j - 1
                brk = True
                break
            sp = sell_px(side, rows[j], slip_x)
            if sp - entry_buy >= min_net_profit:
                exit_j = j
                brk = True
                break
            exit_j = j
        if not brk:
            exit_j = len(rows) - 1

        exit_sell = sell_px(side, rows[exit_j], slip_x)
        net = exit_sell - entry_buy
        trade_pnl = trade_shares * net
        pnl += trade_pnl
        trades += 1
        if trade_pnl > 1e-12:
            n_win += 1
        # classify exit: TP if net target met at exit bar, else timeout/end-of-tape
        if net + 1e-9 >= min_net_profit:
            n_tp += 1
        else:
            n_to += 1

        next_el = rows[exit_j].el + cooldown_sec
        i = _next_index_after_el(rows, exit_j, next_el)
    return pnl, trades, n_tp, n_to, n_win


def entry_cheap_up(rows: list[Row], i: int, imp: float, oe: float) -> Side | None:
    r = rows[i]
    return "UP" if imp - r.up >= oe else None


def entry_cheap_down(rows: list[Row], i: int, imp: float, oe: float) -> Side | None:
    r = rows[i]
    idn = 1.0 - imp
    return "DOWN" if idn - r.dn >= oe else None


def entry_fade_rich_up(rows: list[Row], i: int, imp: float, oe: float) -> Side | None:
    r = rows[i]
    return "DOWN" if r.up - imp >= oe else None


def entry_fade_rich_down(rows: list[Row], i: int, imp: float, oe: float) -> Side | None:
    r = rows[i]
    idn = 1.0 - imp
    return "UP" if r.dn - idn >= oe else None


def entry_tight_band_cheap_up(rows: list[Row], i: int, imp: float, oe: float) -> Side | None:
    r = rows[i]
    if not (0.36 <= r.up <= 0.64):
        return None
    return "UP" if imp - r.up >= oe else None


def entry_tight_band_cheap_up_factory(band_lo: float, band_hi: float) -> EntryFn:
    """Strategy 5 with configurable UP mid band ``[band_lo, band_hi]``."""

    def _f(rows: list[Row], i: int, imp: float, oe: float) -> Side | None:
        r = rows[i]
        if not (band_lo <= r.up <= band_hi):
            return None
        return "UP" if imp - r.up >= oe else None

    return _f


def entry_tight_band_cheap_down_factory(band_lo: float, band_hi: float) -> EntryFn:
    """Tight-band cheap DOWN: implied DOWN (1−implied UP) vs ``down`` mid."""

    def _f(rows: list[Row], i: int, imp: float, oe: float) -> Side | None:
        r = rows[i]
        idn = 1.0 - imp
        if not (band_lo <= r.dn <= band_hi):
            return None
        return "DOWN" if idn - r.dn >= oe else None

    return _f


def entry_prst1_bidirectional_factory(band_lo: float, band_hi: float) -> EntryFn:
    """PRST1: cheap UP or cheap DOWN in band; if both, take larger mispricing edge."""

    def _f(rows: list[Row], i: int, imp: float, oe: float) -> Side | None:
        r = rows[i]
        idn = 1.0 - imp
        eu = imp - r.up
        ed = idn - r.dn
        up_ok = band_lo <= r.up <= band_hi and eu >= oe
        dn_ok = band_lo <= r.dn <= band_hi and ed >= oe
        if up_ok and dn_ok:
            return "UP" if eu >= ed else "DOWN"
        if up_ok:
            return "UP"
        if dn_ok:
            return "DOWN"
        return None

    return _f


def entry_either_cheap(rows: list[Row], i: int, imp: float, oe: float) -> Side | None:
    """Take the stronger of cheap-UP vs cheap-DOWN vs implied (same oe)."""
    r = rows[i]
    eu = imp - r.up
    ed = (1.0 - imp) - r.dn
    if eu >= oe and ed >= oe:
        return "UP" if eu >= ed else "DOWN"
    if eu >= oe:
        return "UP"
    if ed >= oe:
        return "DOWN"
    return None


def run_strategy(
    paths: list[Path],
    entry: EntryFn,
    *,
    sigma: float,
    slip: float,
    open_edge: float,
    min_net: float,
    max_hold: int,
    max_trades: int,
    cooldown: int,
    shares: float | None = None,
    notional_usd: float | None = None,
    exit_slip: float | None = None,
) -> ScalpResult:
    tot = 0.0
    n_tr = 0
    w_traded = 0
    tp_all = 0
    to_all = 0
    win_all = 0
    for path in paths:
        got = load_rows(path)
        if got is None:
            continue
        _slug, rows = got
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
            shares=shares,
            notional_usd=notional_usd,
            exit_slip=exit_slip,
        )
        tot += pnl
        n_tr += nt
        tp_all += ntp
        to_all += nto
        win_all += nw
        if nt:
            w_traded += 1
    return ScalpResult(
        total_pnl=tot,
        trades=n_tr,
        windows=len(paths),
        windows_traded=w_traded,
        tp_exits=tp_all,
        timeout_exits=to_all,
        win_trades=win_all,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--public-dir", type=Path, default=Path(__file__).resolve().parents[1] / "public")
    ap.add_argument("--sigma", type=float, default=185.0, help="BTC USD scale for implied-Up curve")
    ap.add_argument("--slip", type=float, default=0.012)
    ap.add_argument("--open-edge", type=float, default=0.07, help="Min mispricing vs implied to enter (pre-slip)")
    ap.add_argument("--min-net", type=float, default=0.05, help="Min net $/share buy→sell (after slip)")
    ap.add_argument("--max-hold-sec", type=int, default=150)
    ap.add_argument("--max-trades", type=int, default=10)
    ap.add_argument("--cooldown-sec", type=int, default=3)
    ap.add_argument("--shares", type=float, default=10.0)
    ap.add_argument(
        "--notional-usd",
        type=float,
        default=None,
        help="If set, position size = notional/entry each trade (use instead of --shares)",
    )
    args = ap.parse_args()
    paths = sorted(args.public_dir.glob("*_btc-updown-15m-*_prices.csv"))
    if not paths:
        raise SystemExit(f"No tapes in {args.public_dir}")

    strategies: list[tuple[str, EntryFn]] = [
        ("1_cheap_UP_vs_btc_implied", entry_cheap_up),
        ("2_cheap_DOWN_vs_btc_implied", entry_cheap_down),
        ("3_fade_rich_UP_buy_DOWN", entry_fade_rich_up),
        ("4_fade_rich_DOWN_buy_UP", entry_fade_rich_down),
        ("5_tight_band_cheap_UP_only", entry_tight_band_cheap_up),
        ("6_either_side_cheaper_vs_implied", entry_either_cheap),
    ]

    print("Scalp (no settlement), public pool")
    print(f"  tapes: {len(paths)} | sigma={args.sigma} | slip={args.slip}")
    print(f"  open_edge>={args.open_edge} | min_net>={args.min_net} | max_hold={args.max_hold_sec}s")
    sz = f"notional=${args.notional_usd}/trade" if args.notional_usd else f"shares={args.shares}"
    print(f"  max_trades/window={args.max_trades} | cooldown={args.cooldown_sec}s | {sz}")
    print()

    for label, ent in strategies:
        r = run_strategy(
            paths,
            ent,
            sigma=args.sigma,
            slip=args.slip,
            open_edge=args.open_edge,
            min_net=args.min_net,
            max_hold=args.max_hold_sec,
            max_trades=args.max_trades,
            cooldown=args.cooldown_sec,
            shares=None if args.notional_usd else args.shares,
            notional_usd=args.notional_usd,
        )
        avg = r.total_pnl / r.trades if r.trades else 0.0
        wr = (100.0 * r.win_trades / r.trades) if r.trades else 0.0
        print(label)
        print(f"  total_pnl_usdc: {r.total_pnl:.2f}")
        print(f"  round_trips:    {r.trades}")
        print(f"  windows with trades: {r.windows_traded}")
        print(f"  win_rate: {wr:.1f}%  ({r.win_trades}/{r.trades})")
        print(f"  avg pnl / trade: {avg:.4f}")
        print(
            f"  exits: take_profit={r.tp_exits}  timeout_or_eot={r.timeout_exits}  "
            f"(tp_share={(100.0 * r.tp_exits / r.trades):.1f}% of trades)"
            if r.trades
            else "  exits: (no trades)"
        )
        print()

    # second pass: stricter 10c min net
    if args.min_net < 0.095:
        print("=== same with --min-net 0.10 (10c net per share) ===\n")
        for label, ent in strategies:
            r = run_strategy(
                paths,
                ent,
                sigma=args.sigma,
                slip=args.slip,
                open_edge=args.open_edge,
                min_net=0.10,
                max_hold=args.max_hold_sec,
                max_trades=args.max_trades,
                cooldown=args.cooldown_sec,
                shares=None if args.notional_usd else args.shares,
                notional_usd=args.notional_usd,
            )
            avg = r.total_pnl / r.trades if r.trades else 0.0
            print(f"{label}")
            print(f"  total_pnl_usdc: {r.total_pnl:.2f} | trades: {r.trades} | avg/trade: {avg:.4f}")
            print()


if __name__ == "__main__":
    main()
