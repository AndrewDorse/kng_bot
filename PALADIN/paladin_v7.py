#!/usr/bin/env python3
"""
PALADIN v7 (sim): BTC-spike entries only, with the current balance-first hedge logic.

1) **New risk** opens only on BTC volume spike + price jump. This applies when flat and also when an
   existing book is already balanced. Side = BTC momentum direction, subject to ``first_leg_max_pm``,
   base-order share sizing, and the normal pair cooldown when re-entering from a balanced book.
   Balanced re-entry clips are additionally ignored unless the buy price is inside the configured
   20c..80c band.
2) **Second leg / rebalance**: every material imbalance uses one ``pending_second`` path. Cheap first hedge still
   uses ``opened_avg + opposite_px + slip <= _nonforced_pair_cap()``. Cheap re-balance after later entries uses the
   avg of the *opposite VWAP side* plus the current price of the side being bought, plus slip. That means
   ``avg_higher_vwap + current_lower_vwap_px + slip`` when buying the lower-VWAP side, and
   ``avg_lower_vwap + current_higher_vwap_px + slip`` when buying the higher-VWAP side. Forced balance still fires
   after ``hedge_timeout_seconds``.
3) The old ``-5c`` / ``-20c`` layer-entry paths are disabled. New risk comes from BTC spikes only.

Uses ``SimState`` / ``try_buy`` from the PALADIN window harness.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from simulate_paladin_window import SimState, try_buy

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
    # First leg, layer-2 dip add, and hedge clip size (see BOT_PALADIN_V7_BASE_ORDER_SHARES).
    base_order_shares: float = 5.0
    max_shares_per_side: float = 25.0
    min_notional: float = 1.0
    min_shares: float = 5.0

    volume_lookback_sec: int = 60
    volume_spike_ratio: float = 2.5
    volume_floor: float = 1e-6
    btc_abs_move_min_usd: float = 2.0

    first_leg_max_pm: float = 0.62
    balanced_entry_min_pm: float = 0.20
    balanced_entry_max_pm: float = 0.80
    cheap_other_margin: float = 0.04
    cheap_pair_sum_max: float = 0.99
    # Ceiling for *our* economics: cheap hedge (held VWAP + opposite + slip). Spike first leg uses first_leg_max_pm only.
    cheap_pair_avg_sum_nonforced_max: float = 0.96
    # Live FAK often walks the book above the WS mid used for gating; require headroom so
    # avg_first + (mid_opposite + buffer) <= cheap cap (avoids approving hedges that fill >1 pair avg).
    cheap_hedge_slip_buffer: float = 0.012
    # Do not allow ok_cheap until first-leg age >= this (seconds since first leg). 0 = legacy: hedge as soon
    # as the cheap gate passes. Forced hedge at hedge_timeout_seconds is unaffected.
    cheap_hedge_min_delay_sec: float = 0.0
    hedge_timeout_seconds: float = 30.0
    # Not used to block timed forced hedges (see ``paladin_v7_step``); kept for dashboards / future tuning.
    forced_hedge_max_book_sum: float = 1.30

    # Legacy layer-entry threshold kept on params, but spike-only mode no longer uses it.
    layer2_dip_below_avg: float = 0.05
    # Hedge-price cap starts at 1 - this deduction, then tightens by layer_level_offset_step per layer.
    cheap_balance_start_deduction: float = 0.08
    # Legacy layer tightening knob kept on params; spike-only mode no longer uses it for entries.
    layer_level_offset_step: float = 0.01
    # Legacy lower-VWAP deep-dip threshold kept on params, but spike-only mode no longer uses it.
    layer2_low_vwap_dip_below_avg: float = 0.20
    # Legacy layer cutoff kept on params; spike-only mode has no non-spike layer-entry path.
    no_new_layers_last_seconds: float = 60.0
    # Treat |up−down| <= this (shares) as balanced for spike re-entry checks (default 1.0).
    balance_share_tolerance: float = 1.0
    # Imbalance: buy lighter side when pm_light + VWAP(heavy) < this (0.97 = 97¢ pair proxy vs heavy leg).
    imbalance_repair_max_pair_sum: float = 0.97

    # Seconds after a completed pair before the next layer‑2 *add* may fire (default 1; min 1 replay second).
    layer2_cooldown_sec: float = 5.0
    # Legacy cooldown knob kept on the params object; spike-based re-entry is disabled in the strategy.
    pair_cooldown_sec: float = 5.0


@dataclass(slots=True)
class PaladinV7Runner:
    st: SimState = field(default_factory=SimState)
    pending_second: tuple[Side, float, float, int] | None = None
    last_completed_pair_elapsed: int = -1_000_000


def _current_layer_level(st: SimState, base_order_shares: float) -> int:
    base = max(1e-9, float(base_order_shares))
    top = max(float(st.size_up), float(st.size_down))
    if top <= 1e-9:
        return 0
    # Use the larger side so an in-flight layer hedge (e.g. 10 vs 5) already counts as the next tier.
    tier = int(round(top / base))
    return max(0, tier - 1)


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


def _lead_side(pm_u: float, pm_d: float) -> Side:
    """Higher Polymarket mid = favorite (tie → up)."""
    if pm_u > pm_d + 1e-12:
        return "up"
    if pm_d > pm_u + 1e-12:
        return "down"
    return "up"


def _higher_vwap_side(st: SimState, pm_u: float, pm_d: float) -> Side:
    """Layer dip leg: whichever outcome has the higher held VWAP (tie → ``_lead_side``)."""
    au, ad = float(st.avg_up), float(st.avg_down)
    if au > ad + 1e-12:
        return "up"
    if ad > au + 1e-12:
        return "down"
    return _lead_side(pm_u, pm_d)


def _lower_vwap_side(st: SimState, pm_u: float, pm_d: float) -> Side:
    """Lower held VWAP leg (tie → opposite of PM *lead* = underdog by mid)."""
    au, ad = float(st.avg_up), float(st.avg_down)
    if ad < au - 1e-12:
        return "down"
    if au < ad - 1e-12:
        return "up"
    ls = _lead_side(pm_u, pm_d)
    return "down" if ls == "up" else "up"


def _nonforced_pair_cap(p: PaladinV7Params) -> float:
    """Ceiling for cheap hedge: held VWAP + opposite mid + slip."""
    base = min(float(p.cheap_pair_sum_max), 1.0 - float(p.cheap_other_margin))
    return min(base, float(p.cheap_pair_avg_sum_nonforced_max))


def _clamp_shares(st: SimState, side: Side, sh: float, cap: float, min_sh: float) -> float:
    cur = st.size_up if side == "up" else st.size_down
    room = float(cap) - cur
    if room < min_sh - 1e-9:
        return 0.0
    return min(float(sh), room)


def _entry_shares(
    st: SimState,
    side: Side,
    desired_shares: float,
    *,
    px: float,
    cap: float,
    min_sh: float,
    min_notional: float,
) -> float:
    """Fixed-size clip for new-risk buys; require room for the whole clip."""
    cur = float(st.size_up) if side == "up" else float(st.size_down)
    room = float(cap) - cur
    desired = float(desired_shares)
    if room < float(min_sh) - 1e-9:
        return 0.0
    if room + 1e-9 < desired:
        return 0.0
    sh = desired
    if sh * float(px) + 1e-9 < float(min_notional):
        return 0.0
    return sh


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
    base_sz = float(p.base_order_shares)
    su, sd = float(st.size_up), float(st.size_down)
    gap_now = abs(su - sd)
    flat_now = su <= 1e-9 and sd <= 1e-9
    balance_order_gap_trigger = max(0.0, min_sh - 1.0)
    layer_level = _current_layer_level(st, base_sz)
    layer_offset_step = max(0.0, float(p.layer_level_offset_step))
    dynamic_balance_dip = max(0.0, float(p.cheap_balance_start_deduction) + layer_level * layer_offset_step)
    # Universal balance engine: if the live book is materially imbalanced, the next action must be
    # on the lighter side. Use one pending path for first hedge and later re-balancing.
    if runner.pending_second is None and (not flat_now) and gap_now > balance_order_gap_trigger + 1e-9:
        if su > sd + 1e-9:
            runner.pending_second = ("down", gap_now, float(st.avg_up), int(t))
        elif sd > su + 1e-9:
            runner.pending_second = ("up", gap_now, float(st.avg_down), int(t))

    # --- Pending hedge (second leg on *other* side) ---
    # Same cheap / forced logic for hedges after any first leg or later layer add.
    if runner.pending_second is not None:
        side_o, sh_need, avg_first, t0 = runner.pending_second
        su, sd = float(st.size_up), float(st.size_down)
        cur_gap = abs(su - sd)
        both_nonflat = su > 1e-9 and sd > 1e-9
        # If the remaining skew is 4 shares or less, do not keep forcing 5-share rebalance clips.
        if cur_gap <= balance_order_gap_trigger + 1e-9:
            runner.pending_second = None
            runner.last_completed_pair_elapsed = int(t)
            return
        # Tiny residue inside the balance tolerance is also considered complete.
        if cur_gap <= max(1e-6, float(p.balance_share_tolerance)):
            runner.pending_second = None
            runner.last_completed_pair_elapsed = int(t)
            return
        px_o = pm_u if side_o == "up" else pm_d
        age = float(t) - float(t0)
        forced = age + 1e-9 >= float(p.hedge_timeout_seconds)

        # Universal cheap-price rule:
        # - first hedge from one-sided inventory: keep pair-cost cap logic (<0.96 held+opp+slip)
        # - after extra layers, cheap balance uses the avg of the opposite VWAP side plus the
        #   current price of the side we are buying, plus slip. So:
        #   * buying lower-VWAP side: avg_higher_vwap + current_lower_vwap + slip
        #   * buying higher-VWAP side: avg_lower_vwap + current_higher_vwap + slip
        #   and this must fit under 1.00 - dynamic_balance_dip (0.94 at layer 2, 0.93 at layer 3, ...).
        min_cheap_age = max(0.0, float(p.cheap_hedge_min_delay_sec))
        ok_cheap = False
        cheap_limit_px = 0.0
        if both_nonflat:
            slip = max(0.0, float(p.cheap_hedge_slip_buffer))
            hi_vwap = "up" if float(st.avg_up) >= float(st.avg_down) else "down"
            lo_vwap = "down" if hi_vwap == "up" else "up"
            opp_vwap = lo_vwap if side_o == hi_vwap else hi_vwap
            avg_opp_vwap = float(st.avg_up) if opp_vwap == "up" else float(st.avg_down)
            cap = max(0.01, 1.0 - dynamic_balance_dip)
            cheap_limit_px = max(0.01, min(0.99, cap - float(avg_opp_vwap) - slip))
            ok_cheap = age + 1e-9 >= min_cheap_age
        else:
            slip = max(0.0, float(p.cheap_hedge_slip_buffer))
            cap = _nonforced_pair_cap(p)
            cheap_limit_px = max(0.01, min(0.99, cap - float(avg_first) - slip))
            ok_cheap = age + 1e-9 >= min_cheap_age

        # Forced must run on timeout even when pm_u+pm_d > forced_hedge_max_book_sum (wide/late-window mids);
        # otherwise cheap can fail forever and inventory stays one-sided.
        ok_forced = forced

        if ok_cheap or ok_forced:
            # Hedge/balance always clips one fixed order at a time; do not fire full-gap repair orders.
            hedge_target = min_sh
            hedge_min_sh = min_sh
            balance_cap = max(float(p.max_shares_per_side), su, sd)
            sh_exec = _clamp_shares(st, side_o, hedge_target, balance_cap, hedge_min_sh)
            if sh_exec >= hedge_min_sh - 1e-9:
                hedge_mn = float(p.min_notional)
                # Once timeout hits, force must take precedence over the resting cheap path.
                px_exec = px_o if ok_forced else float(cheap_limit_px)
                # Polymarket rejects sub-$1 notional on many BUY paths; lift px so the clip clears ~$1.
                notion_floor = max(float(hedge_mn), 1.0)
                need_px = notion_floor / max(float(sh_exec), 1e-9)
                if float(px_exec) * float(sh_exec) + 1e-9 < notion_floor:
                    px_exec = min(0.99, max(float(px_exec), float(need_px)))
                if sh_exec * float(px_exec) + 1e-9 < notion_floor:
                    return
                reason = "v7_hedge_forced" if ok_forced else "v7_hedge_cheap"
                filled = buy(
                    st,
                    t=t,
                    side=side_o,
                    shares=sh_exec,
                    px=px_exec,
                    reason=reason,
                    budget=p.budget_usdc,
                    min_notional=hedge_mn,
                    min_shares=hedge_min_sh,
                    pm_u=pm_u,
                    pm_d=pm_d,
                )
                if filled > 1e-9:
                    # Live FAK can partially fill; do not clear pending until hedge need is exhausted
                    # (clearing early caused extra same-side clips / double hedges on the next ticks).
                    rem = max(0.0, float(cur_gap) - float(filled))
                    if rem <= 1e-6:
                        runner.pending_second = None
                        runner.last_completed_pair_elapsed = int(t)
                    else:
                        runner.pending_second = (side_o, rem, avg_first, t0)
        return

    bal_tol = max(0.0, float(p.balance_share_tolerance))
    min_sz_gate = max(0.0, min_sh - bal_tol)
    su, sd = float(st.size_up), float(st.size_down)
    balanced = abs(su - sd) <= bal_tol + 1e-9
    flat = su <= 1e-9 and sd <= 1e-9
    both = min(su, sd) + 1e-9 >= min_sz_gate

    # --- Spike-only new risk: flat start or balanced re-entry ---
    can_open = flat or (balanced and both)
    if not can_open:
        return
    pair_cd = max(5.0, float(p.pair_cooldown_sec))
    if float(t) - float(runner.last_completed_pair_elapsed) < pair_cd and not flat:
        return
    if not (_volume_spike(ticks, t, p) and _price_jump(ticks, t, p)):
        return
    mom = _btc_momentum_side(ticks, t)
    if mom is None:
        return
    px_1 = pm_u if mom == "up" else pm_d
    if (not flat) and (
        px_1 < float(p.balanced_entry_min_pm) - 1e-9
        or px_1 > float(p.balanced_entry_max_pm) + 1e-9
    ):
        return
    if px_1 + 1e-9 > float(p.first_leg_max_pm):
        return
    sh1 = _entry_shares(
        st,
        mom,
        base_sz,
        px=px_1,
        cap=p.max_shares_per_side,
        min_sh=min_sh,
        min_notional=p.min_notional,
    )
    if sh1 < min_sh - 1e-9:
        return
    reason = "v7_balanced_btc_spike" if not flat else "v7_first_binance_spike"
    matched = buy(
        st,
        t=t,
        side=mom,
        shares=sh1,
        px=px_1,
        reason=reason,
        budget=p.budget_usdc,
        min_notional=p.min_notional,
        min_shares=min_sh,
        pm_u=pm_u,
        pm_d=pm_d,
    )
    if matched > 1e-9:
        other: Side = "down" if mom == "up" else "up"
        leg_avg = float(st.avg_up) if mom == "up" else float(st.avg_down)
        runner.pending_second = (other, float(matched), leg_avg, int(t))


# Tight sim: $10 budget, 10 shares/side cap, 5-share base orders (batch preset label kept for scripts).
V7_SMALL_BUDGET_4ORDERS = PaladinV7Params(
    budget_usdc=10.0,
    base_order_shares=5.0,
    max_shares_per_side=16.0,
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
    """Instant-fill replay (no spike / cheap delay). Batch PnL uses ``paladin_v7_delay2s_replay`` instead."""
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
