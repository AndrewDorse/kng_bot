#!/usr/bin/env python3
"""
PALADIN v7 (sim): Binance per-second volume spike + BTC price impulse → Polymarket legs.

1) **First leg** when (rolling Binance base-volume vs lookback mean) spikes *and* BTC price moves
   in the same second; side = momentum (price up → UP token, down → DOWN token).
2) **Second leg** (hedge) on the opposite outcome token: prefer a *cheap* fill when
   **first-leg VWAP + current opposite mid** <= ``min(cheap_pair_sum_max, 1 - cheap_other_margin)``
   (held + quote for the hedge leg — not live ``pm_u+pm_d``, which sits ~1.0). If still open after
   ``hedge_timeout_seconds``, force hedge when pm_up+pm_down <= ``forced_hedge_max_book_sum``.
3) **Refill** after a balanced pair: smaller clip (>= min_shares) when both mids are below
   leg averages (symmetric improvement), and book sum is tight enough.

Uses the same ``SimState`` / ``try_buy`` / ``improves_leg`` as the PALADIN window harness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import csv
from typing import Any, Callable, Literal

from simulate_paladin_window import SimState, improves_leg, try_buy

TryBuyFn = Callable[..., float]

Side = Literal["up", "down"]


@dataclass(slots=True)
class WindowTick:
    """One replay second: Polymarket mids + Binance spot (1s kline fields from exports)."""

    pm_u: float
    pm_d: float
    btc_px: float
    btc_vol: float


@dataclass(slots=True)
class PaladinV7Params:
    budget_usdc: float = 400.0
    clip_shares: float = 10.0
    max_shares_per_side: float = 80.0
    min_notional: float = 1.0
    min_shares: float = 5.0

    volume_lookback_sec: int = 60
    volume_spike_ratio: float = 2.5
    volume_floor: float = 1e-6
    btc_abs_move_min_usd: float = 2.0

    first_leg_max_pm: float = 0.62
    cheap_other_margin: float = 0.04
    cheap_pair_sum_max: float = 0.99
    hedge_timeout_seconds: float = 90.0
    forced_hedge_max_book_sum: float = 1.30

    refill_clip_fraction: float = 0.5
    refill_max_pair_sum: float = 0.985

    pair_cooldown_sec: float = 20.0
    # Max simulated fills per window (0 = unlimited). New first legs / refills are skipped
    # unless there is room for the legs (first+hedge needs 2 slots; symmetric refill needs 2).
    max_orders: int = 0


@dataclass(slots=True)
class PaladinV7Runner:
    st: SimState = field(default_factory=SimState)
    pending_second: tuple[Side, float, float, int] | None = None
    last_completed_pair_elapsed: int = -1_000_000


def load_ticks_with_btc(path: Path, *, window_sec: int = 900) -> tuple[str, list[WindowTick]]:
    """
    Load ``*_prices.csv`` with optional Binance columns. Forward-fills PM and BTC fields.
    Returns (slug, ticks). Empty ticks if file has no usable ``btc_volume`` / ``btc_price``.
    """
    by_e: dict[int, dict[str, str]] = {}
    slug = ""
    with path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        if not r.fieldnames or "btc_volume" not in r.fieldnames or "btc_price" not in r.fieldnames:
            return "", []
        for row in r:
            try:
                e = int(float(row["elapsed_sec"]))
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= e < window_sec:
                by_e[e] = row
                slug = (row.get("slug") or slug).strip()

    if not by_e:
        return slug or "", []

    last_u, last_d = 0.5, 0.5
    last_bpx, last_bvol = 0.0, 0.0
    ticks: list[WindowTick] = []
    for t in range(window_sec):
        row = by_e.get(t)
        if row:
            try:
                last_u = float(row["up_price"])
                last_d = float(row["down_price"])
            except (KeyError, TypeError, ValueError):
                pass
            try:
                v = (row.get("btc_volume") or "").strip()
                if v != "":
                    last_bvol = float(v)
                p = (row.get("btc_price") or "").strip()
                if p != "":
                    last_bpx = float(p)
            except (TypeError, ValueError):
                pass
        ticks.append(WindowTick(pm_u=last_u, pm_d=last_d, btc_px=last_bpx, btc_vol=last_bvol))

    if all(x.btc_px <= 0.0 for x in ticks):
        return slug, []
    return slug, ticks


def _rolling_mean_vol(ticks: list[WindowTick], t: int, lookback: int) -> float:
    lo = max(0, t - lookback)
    if lo >= t:
        return ticks[0].btc_vol if t == 0 else 0.0
    s = 0.0
    for i in range(lo, t):
        s += max(0.0, ticks[i].btc_vol)
    n = t - lo
    return s / max(1, n)


def _btc_momentum_side(ticks: list[WindowTick], t: int) -> Side | None:
    if t <= 0 or ticks[t].btc_px <= 0.0:
        return None
    prev = ticks[t - 1].btc_px
    cur = ticks[t].btc_px
    if prev <= 0.0:
        return None
    d = cur - prev
    if abs(d) < 1e-9:
        lo = max(0, t - 5)
        prev2 = ticks[lo].btc_px
        if prev2 <= 0.0:
            return None
        d = cur - prev2
    if d > 0:
        return "up"
    if d < 0:
        return "down"
    return None


def _volume_spike(ticks: list[WindowTick], t: int, p: PaladinV7Params) -> bool:
    v = max(0.0, ticks[t].btc_vol)
    base = _rolling_mean_vol(ticks, t, int(p.volume_lookback_sec))
    thresh = float(p.volume_spike_ratio) * max(float(p.volume_floor), base)
    return v + 1e-12 >= thresh


def _price_jump(ticks: list[WindowTick], t: int, p: PaladinV7Params) -> bool:
    if t <= 0 or ticks[t].btc_px <= 0.0:
        return False
    prev = ticks[t - 1].btc_px
    if prev <= 0.0:
        return False
    return abs(ticks[t].btc_px - prev) >= float(p.btc_abs_move_min_usd)


def _clamp_shares(st: SimState, side: Side, sh: float, cap: float, min_sh: float) -> float:
    cur = st.size_up if side == "up" else st.size_down
    room = float(cap) - cur
    if room < min_sh - 1e-9:
        return 0.0
    return min(float(sh), room)


def paladin_v7_step(
    runner: PaladinV7Runner,
    t: int,
    ticks: list[WindowTick],
    *,
    params: PaladinV7Params,
    try_buy_fn: TryBuyFn | None = None,
) -> None:
    st = runner.st
    p = params
    tick = ticks[t]
    pm_u, pm_d = float(tick.pm_u), float(tick.pm_d)
    buy: Any = try_buy_fn if try_buy_fn is not None else try_buy
    min_sh = float(p.min_shares)
    mo = int(p.max_orders)
    n_tr = len(st.trades)

    # --- Pending hedge (second leg on *other* side) ---
    if runner.pending_second is not None:
        side_o, sh_need, avg_first, t0 = runner.pending_second
        px_o = pm_u if side_o == "up" else pm_d
        age = float(t) - float(t0)
        forced = age + 1e-9 >= float(p.hedge_timeout_seconds)

        # Non-forced: held first-leg VWAP + this tick's opposite mid (same anchor as FAK px).
        # Tightest of book cap and (1 - margin) keeps sub-$1 pair discipline without gating on pm_u+pm_d.
        pair_held_quote_sum = float(avg_first) + float(px_o)
        cap = min(float(p.cheap_pair_sum_max), 1.0 - float(p.cheap_other_margin))
        ok_cheap = pair_held_quote_sum + 1e-9 <= cap

        ok_forced = forced and (pm_u + pm_d) + 1e-9 <= float(p.forced_hedge_max_book_sum)

        if mo > 0 and n_tr >= mo:
            return

        if ok_cheap or ok_forced:
            sh_exec = _clamp_shares(st, side_o, sh_need, p.max_shares_per_side, min_sh)
            if sh_exec >= min_sh - 1e-9:
                # If mid*shares < CLOB min notional (e.g. $1), still complete the hedge in sim.
                hedge_mn = float(p.min_notional)
                if sh_exec * px_o + 1e-9 < hedge_mn:
                    hedge_mn = 0.0
                reason = "v7_hedge_forced" if ok_forced and not ok_cheap else "v7_hedge_cheap"
                filled = buy(
                    st,
                    t=t,
                    side=side_o,
                    shares=sh_exec,
                    px=px_o,
                    reason=reason,
                    budget=p.budget_usdc,
                    min_notional=hedge_mn,
                    min_shares=min_sh,
                )
                if filled > 1e-9:
                    # Live FAK can partially fill; do not clear pending until hedge need is exhausted
                    # (clearing early caused extra same-side clips / double hedges on the next ticks).
                    rem = float(sh_need) - float(filled)
                    if rem <= 1e-6:
                        runner.pending_second = None
                        runner.last_completed_pair_elapsed = int(t)
                    else:
                        runner.pending_second = (side_o, rem, avg_first, t0)
        return

    balanced = abs(st.size_up - st.size_down) <= 1e-9
    flat = st.size_up <= 1e-9 and st.size_down <= 1e-9
    both = st.size_up > 1e-9 and st.size_down > 1e-9

    # --- Refill: smaller symmetric clip when book is tight and both legs improve ---
    refill_sh = max(min_sh, float(p.clip_shares) * float(p.refill_clip_fraction))
    if mo > 0 and n_tr + 2 > mo:
        pass  # skip refill; not enough order budget for two legs
    elif balanced and both and (float(t) - float(runner.last_completed_pair_elapsed)) >= 1.0:
        sh_u = _clamp_shares(st, "up", refill_sh, p.max_shares_per_side, min_sh)
        sh_d = _clamp_shares(st, "down", refill_sh, p.max_shares_per_side, min_sh)
        if (
            sh_u >= min_sh - 1e-9
            and sh_d >= min_sh - 1e-9
            and (pm_u + pm_d) + 1e-9 <= float(p.refill_max_pair_sum)
            and pm_u + 1e-9 < st.avg_up
            and pm_d + 1e-9 < st.avg_down
            and improves_leg(st.size_up, st.avg_up, pm_u, sh_u)
            and improves_leg(st.size_down, st.avg_down, pm_d, sh_d)
        ):
            up_fill = buy(
                st,
                t=t,
                side="up",
                shares=sh_u,
                px=pm_u,
                reason="v7_refill_up",
                budget=p.budget_usdc,
                min_notional=p.min_notional,
                min_shares=min_sh,
            )
            # Only add the paired Down clip if the Up clip fully sized (avoids one-sided refill on partial FAK).
            if up_fill + 1e-9 >= float(sh_u):
                sh_d2 = _clamp_shares(st, "down", refill_sh, p.max_shares_per_side, min_sh)
                if sh_d2 >= min_sh - 1e-9 and improves_leg(st.size_down, st.avg_down, pm_d, sh_d2):
                    buy(
                        st,
                        t=t,
                        side="down",
                        shares=sh_d2,
                        px=pm_d,
                        reason="v7_refill_dn",
                        budget=p.budget_usdc,
                        min_notional=p.min_notional,
                        min_shares=min_sh,
                    )
            return

    # --- New first leg on Binance spike + jump ---
    can_open = flat or (balanced and both)
    if not can_open:
        return
    if float(t) - float(runner.last_completed_pair_elapsed) < float(p.pair_cooldown_sec) and not flat:
        return

    if mo > 0 and n_tr + 2 > mo:
        return

    if not (_volume_spike(ticks, t, p) and _price_jump(ticks, t, p)):
        return

    mom = _btc_momentum_side(ticks, t)
    if mom is None:
        return

    px_1 = pm_u if mom == "up" else pm_d
    if px_1 + 1e-9 > float(p.first_leg_max_pm):
        return

    sh1 = _clamp_shares(st, mom, p.clip_shares, p.max_shares_per_side, min_sh)
    if sh1 < min_sh - 1e-9:
        return

    matched = buy(
        st,
        t=t,
        side=mom,
        shares=sh1,
        px=px_1,
        reason="v7_first_binance_spike",
        budget=p.budget_usdc,
        min_notional=p.min_notional,
        min_shares=min_sh,
    )
    # Live FAK can partially fill; hedge must target actual shares and leg VWAP (not requested clip / signal px).
    if matched > 1e-9:
        other: Side = "down" if mom == "up" else "up"
        leg_avg = float(st.avg_up) if mom == "up" else float(st.avg_down)
        runner.pending_second = (other, float(matched), leg_avg, int(t))


# Tight sim: $10 budget, 10 shares/side cap, 5-share clips, at most 4 fills (two spike pairs or one pair+refill).
V7_SMALL_BUDGET_4ORDERS = PaladinV7Params(
    budget_usdc=10.0,
    clip_shares=5.0,
    max_shares_per_side=10.0,
    max_orders=4,
    min_notional=1.0,
    min_shares=5.0,
    forced_hedge_max_book_sum=1.5,
    cheap_pair_sum_max=0.995,
)


def run_window_v7(
    ticks: list[WindowTick],
    *,
    params: PaladinV7Params | None = None,
    try_buy_fn: TryBuyFn | None = None,
) -> SimState:
    p = params or PaladinV7Params()
    runner = PaladinV7Runner()
    for t in range(len(ticks)):
        paladin_v7_step(runner, t, ticks, params=p, try_buy_fn=try_buy_fn)
    return runner.st


__all__ = [
    "PaladinV7Params",
    "PaladinV7Runner",
    "TryBuyFn",
    "V7_SMALL_BUDGET_4ORDERS",
    "WindowTick",
    "load_ticks_with_btc",
    "paladin_v7_step",
    "run_window_v7",
]
