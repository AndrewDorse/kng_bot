#!/usr/bin/env python3
"""
PALADIN v7 (sim): Binance per-second volume spike + BTC price impulse → Polymarket legs.

1) **First leg** when (rolling Binance base-volume vs lookback mean) spikes *and* BTC price moves
   in the same second; side = momentum (price up → UP token, down → DOWN token). Gated by
   ``first_leg_max_pm`` only — **not** by ``_nonforced_pair_cap`` (that cap is for hedges / layer-2 hedge only).
2) **Second leg** (hedge) on the opposite outcome — **one code path for every incomplete pair**, including after
   an opening spike, a higher‑VWAP dip add, a lower‑VWAP dip add, or a balanced spike add (all set
   ``pending_second`` the same way): *cheap* when **held VWAP on the opened side + opposite mid + slip** <=
   ``_nonforced_pair_cap``; *forced* when age >= ``hedge_timeout_seconds`` (always tries — **not** gated on
   ``pm_u+pm_d``; a book-sum gate would strand hedges when mids sum > ``forced_hedge_max_book_sum``).
   ``forced_hedge_max_book_sum`` remains on ``PaladinV7Params`` for logging / future use.
3) **Extra layers** when book is **balanced** (see ``balance_share_tolerance``): (a) **Higher‑VWAP** leg: mid
   **<** that leg's avg minus ``layer2_dip_below_avg`` (default 5¢); (b) **Lower‑VWAP** (“losing” avg) leg: mid
   **<** that leg's avg minus ``layer2_low_vwap_dip_below_avg`` (default 20¢); (c) **Binance spike** first leg
   (same as opening) when balanced. Each tick tries (a) then (b) then (c). ``balance_share_tolerance`` (default
   1 share) treats e.g. 4.95 vs 5.06 or 4.5 vs 5.5 as balanced; ``min_shares`` gate on a leg is relaxed by the
   same tolerance for these layer adds.
4) **Imbalance repair** when ``|up−down|`` exceeds ``balance_share_tolerance`` and neither side is flat: buy the
   **lighter** side (up to the gap) when ``pm_light + avg_heavy < imbalance_repair_max_pair_sum`` (default 0.97),
   e.g. 10 UP @ 0.525 avg and 5 DOWN → buy DOWN when ``pm_d + 0.525 < 0.97``. No ``pending_second``; this only
   catches up inventory.

Uses ``SimState`` / ``try_buy`` from the PALADIN window harness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import csv
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
    max_shares_per_side: float = 16.0
    min_notional: float = 1.0
    min_shares: float = 5.0

    volume_lookback_sec: int = 60
    volume_spike_ratio: float = 2.5
    volume_floor: float = 1e-6
    btc_abs_move_min_usd: float = 2.0

    first_leg_max_pm: float = 0.62
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
    hedge_timeout_seconds: float = 90.0
    # Not used to block timed forced hedges (see ``paladin_v7_step``); kept for dashboards / future tuning.
    forced_hedge_max_book_sum: float = 1.30

    # Layer: higher-VWAP leg's mid must be < that leg's avg minus this (default 5¢).
    layer2_dip_below_avg: float = 0.05
    # Layer: lower-VWAP leg's mid must be < that leg's avg minus this (default 20¢).
    layer2_low_vwap_dip_below_avg: float = 0.20
    # Treat |up−down| <= this (shares) as balanced for layers + spike-from-balanced (default 1.0).
    balance_share_tolerance: float = 1.0
    # Imbalance: buy lighter side when pm_light + VWAP(heavy) < this (0.97 = 97¢ pair proxy vs heavy leg).
    imbalance_repair_max_pair_sum: float = 0.97

    # Seconds after a completed pair before the next layer‑2 *add* may fire (default 1; min 1 replay second).
    layer2_cooldown_sec: float = 1.0
    # Seconds after a completed pair before a new Binance spike first leg when balanced (default 1).
    pair_cooldown_sec: float = 1.0


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

    # --- Pending hedge (second leg on *other* side) ---
    # Same cheap / forced logic for hedges after *any* first leg (spike, -5c layer, -20c layer).
    if runner.pending_second is not None:
        side_o, sh_need, avg_first, t0 = runner.pending_second
        px_o = pm_u if side_o == "up" else pm_d
        age = float(t) - float(t0)
        forced = age + 1e-9 >= float(p.hedge_timeout_seconds)

        # Non-forced: held first-leg VWAP + conservative opposite quote (mid + slip buffer).
        # FAK fills can print above the mid used as the limit anchor; buffer aligns gate with live VWAP.
        slip = max(0.0, float(p.cheap_hedge_slip_buffer))
        pair_held_quote_sum = float(avg_first) + float(px_o) + slip
        cap = _nonforced_pair_cap(p)
        min_cheap_age = max(0.0, float(p.cheap_hedge_min_delay_sec))
        ok_cheap = (age + 1e-9 >= min_cheap_age) and (pair_held_quote_sum + 1e-9 <= cap)

        # Forced must run on timeout even when pm_u+pm_d > forced_hedge_max_book_sum (wide/late-window mids);
        # otherwise cheap can fail forever and inventory stays one-sided.
        ok_forced = forced

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

    bal_tol = max(0.0, float(p.balance_share_tolerance))
    min_sz_gate = max(0.0, min_sh - bal_tol)
    su, sd = float(st.size_up), float(st.size_down)
    balanced = abs(su - sd) <= bal_tol + 1e-9
    flat = su <= 1e-9 and sd <= 1e-9
    both = min(su, sd) + 1e-9 >= min_sz_gate

    # --- Imbalance repair: top up lighter side when pm_light + avg(heavy) < cap ---
    if not balanced and not flat:
        su, sd = float(st.size_up), float(st.size_down)
        cap_rep = max(0.5, min(1.0, float(p.imbalance_repair_max_pair_sum)))
        light: Side | None = None
        avg_h = 0.0
        pm_l = 0.0
        gap = 0.0
        if su > sd + 1e-9 and su > 1e-9:
            light = "down"
            avg_h = float(st.avg_up)
            pm_l = float(pm_d)
            gap = su - sd
        elif sd > su + 1e-9 and sd > 1e-9:
            light = "up"
            avg_h = float(st.avg_down)
            pm_l = float(pm_u)
            gap = sd - su
        if light is not None and gap > bal_tol + 1e-9:
            sh_rep = _clamp_shares(st, light, gap, p.max_shares_per_side, min_sh)
            if sh_rep >= min_sh - 1e-9 and pm_l + avg_h + 1e-9 < cap_rep:
                filled_ir = buy(
                    st,
                    t=t,
                    side=light,
                    shares=sh_rep,
                    px=pm_l,
                    reason="v7_imbalance_repair",
                    budget=p.budget_usdc,
                    min_notional=p.min_notional,
                    min_shares=min_sh,
                )
                if filled_ir > 1e-9:
                    return

    # --- Extra layers: higher-VWAP dip, then lower-VWAP deep dip, then Binance spike (one fill → return) ---
    l2_cd = max(1.0, float(p.layer2_cooldown_sec))

    def _try_layer_dip(side: Side, dip_below_avg: float, reason: str) -> bool:
        px_s = pm_u if side == "up" else pm_d
        avg_s = float(st.avg_up) if side == "up" else float(st.avg_down)
        sz_s = float(st.size_up) if side == "up" else float(st.size_down)
        dip_m = max(0.0, float(dip_below_avg))
        other_s: Side = "down" if side == "up" else "up"
        sh_add = _clamp_shares(st, side, base_sz, p.max_shares_per_side, min_sh)
        if (
            sz_s + 1e-9 < min_sz_gate
            or not (px_s + 1e-9 < avg_s - dip_m)
            or sh_add + 1e-9 < min_sh - 1e-9
        ):
            return False
        filled = buy(
            st,
            t=t,
            side=side,
            shares=sh_add,
            px=px_s,
            reason=reason,
            budget=p.budget_usdc,
            min_notional=p.min_notional,
            min_shares=min_sh,
        )
        if filled > 1e-9:
            leg_avg = float(st.avg_up) if side == "up" else float(st.avg_down)
            runner.pending_second = (other_s, float(filled), leg_avg, int(t))
            return True
        return False

    if balanced and both and (float(t) - float(runner.last_completed_pair_elapsed)) >= l2_cd:
        hi = _higher_vwap_side(st, pm_u, pm_d)
        if _try_layer_dip(hi, float(p.layer2_dip_below_avg), "v7_layer2_dip_lead"):
            return
        lo = _lower_vwap_side(st, pm_u, pm_d)
        if _try_layer_dip(lo, float(p.layer2_low_vwap_dip_below_avg), "v7_layer2_lowvwap_dip"):
            return
        # Dip gates passed but no fill: do not block Binance spike this second.

    # --- New first leg on Binance spike + jump ---
    can_open = flat or (balanced and both)
    if not can_open:
        return
    pair_cd = max(1.0, float(p.pair_cooldown_sec))
    if float(t) - float(runner.last_completed_pair_elapsed) < pair_cd and not flat:
        return

    if not (_volume_spike(ticks, t, p) and _price_jump(ticks, t, p)):
        return

    mom = _btc_momentum_side(ticks, t)
    if mom is None:
        return

    px_1 = pm_u if mom == "up" else pm_d
    if px_1 + 1e-9 > float(p.first_leg_max_pm):
        return

    sh1 = _clamp_shares(st, mom, base_sz, p.max_shares_per_side, min_sh)
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
