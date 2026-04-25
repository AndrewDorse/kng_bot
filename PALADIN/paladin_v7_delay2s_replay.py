#!/usr/bin/env python3
"""
PALADIN v7 replay with **2s execution delay** (API / matching imitation).

- **Spike entries** (``v7_first_binance_spike``, ``v7_balanced_btc_spike``): order is armed at signal
  time ``t``; after ``spike_delay_sec`` the mid on that outcome must still be at or below
  ``signal_mid + spike_price_buffer`` or the order is cancelled (no fill).
- **Cheap hedges** (``v7_hedge_cheap``): resting limit at the strategy price; fills only after the
  mid stays at or below the limit for ``hedge_touch_sec`` consecutive seconds, or trades through
  below the limit. If ``elapsed`` reaches ``t0_first_leg + hedge_timeout_seconds`` without fill,
  the resting order is cancelled so the next tick can take the **forced** path.
- **Forced hedges** and all other buys: immediate ``try_buy`` (same as the prior instant sim).

All **batch** PnL runs should use :func:`run_window_v7_delay2s` so results stay comparable to the
live-delay studies.
"""

from __future__ import annotations

from typing import Any, Callable

from paladin_v7 import PaladinV7Params, PaladinV7Runner, WindowTick, paladin_v7_step
from simulate_paladin_window import SimState, try_buy

SPIKE_REASONS = frozenset({"v7_first_binance_spike", "v7_balanced_btc_spike"})

TryBuyFn = Callable[..., float]


def run_window_v7_delay2s(
    ticks: list[WindowTick],
    *,
    params: PaladinV7Params,
    spike_delay_sec: int = 2,
    hedge_touch_sec: int = 2,
    spike_price_buffer: float = 0.02,
) -> SimState:
    """Run one 15m window with delayed spike + resting cheap hedge semantics."""
    p = params
    runner = PaladinV7Runner()
    active: dict[str, Any] | None = None

    def process_active(t_now: int) -> None:
        nonlocal active
        if active is None:
            return
        px_now = float(ticks[t_now].pm_u if active["side"] == "up" else ticks[t_now].pm_d)
        if active["kind"] == "spike":
            if t_now - int(active["submit_t"]) < int(spike_delay_sec):
                return
            if px_now <= float(active["limit_px"]) + 1e-9:
                tk = ticks[t_now]
                matched = try_buy(
                    runner.st,
                    t=t_now,
                    side=active["side"],
                    shares=float(active["shares"]),
                    px=min(float(active["limit_px"]), px_now),
                    reason=active["reason"],
                    budget=float(active["budget"]),
                    min_notional=float(active["min_notional"]),
                    min_shares=float(active["min_shares"]),
                    pm_u=float(tk.pm_u),
                    pm_d=float(tk.pm_d),
                )
                if matched > 1e-9:
                    other = "down" if active["side"] == "up" else "up"
                    leg_avg = (
                        float(runner.st.avg_up) if active["side"] == "up" else float(runner.st.avg_down)
                    )
                    runner.pending_second = (other, float(matched), leg_avg, int(t_now))
            active = None
            return

        if active["kind"] == "cheap_limit":
            lim = float(active["limit_px"])
            if px_now <= lim + 1e-9:
                active["touch_streak"] = int(active["touch_streak"]) + 1
            else:
                active["touch_streak"] = 0
            through = px_now < lim - 1e-9
            rested = int(active["touch_streak"]) >= int(hedge_touch_sec)
            if through or rested:
                tk = ticks[t_now]
                matched = try_buy(
                    runner.st,
                    t=t_now,
                    side=active["side"],
                    shares=float(active["shares"]),
                    px=min(lim, px_now),
                    reason=active["reason"],
                    budget=float(active["budget"]),
                    min_notional=float(active["min_notional"]),
                    min_shares=float(active["min_shares"]),
                    pm_u=float(tk.pm_u),
                    pm_d=float(tk.pm_d),
                )
                if matched > 1e-9:
                    su = float(runner.st.size_up)
                    sd = float(runner.st.size_down)
                    gap = abs(su - sd)
                    if gap <= max(1e-6, float(p.balance_share_tolerance)) + 1e-9 or gap <= (
                        float(p.min_shares) - 1.0
                    ) + 1e-9:
                        runner.pending_second = None
                        runner.last_completed_pair_elapsed = int(t_now)
                active = None
                return
            if t_now >= int(active["force_t"]):
                active = None
                return

    def try_buy_fn(
        st: SimState,
        *,
        t: int,
        side: str,
        shares: float,
        px: float,
        reason: str,
        budget: float,
        min_notional: float,
        min_shares: float,
        pm_u: float | None = None,
        pm_d: float | None = None,
    ) -> float:
        nonlocal active
        if active is not None:
            return 0.0
        if reason in SPIKE_REASONS:
            active = {
                "kind": "spike",
                "submit_t": int(t),
                "side": side,
                "shares": float(shares),
                "reason": reason,
                "budget": float(budget),
                "min_notional": float(min_notional),
                "min_shares": float(min_shares),
                "limit_px": min(0.99, float(px) + float(spike_price_buffer)),
            }
            return 0.0
        if reason == "v7_hedge_cheap":
            if float(shares) * float(px) + 1e-9 < float(min_notional):
                return 0.0
            force_t = int(t)
            if runner.pending_second is not None:
                force_t = int(runner.pending_second[3] + float(p.hedge_timeout_seconds))
            active = {
                "kind": "cheap_limit",
                "submit_t": int(t),
                "force_t": force_t,
                "touch_streak": 0,
                "side": side,
                "shares": float(shares),
                "reason": reason,
                "budget": float(budget),
                "min_notional": float(min_notional),
                "min_shares": float(min_shares),
                "limit_px": float(px),
            }
            return 0.0
        tk = ticks[t]
        return try_buy(
            st,
            t=t,
            side=side,
            shares=shares,
            px=px,
            reason=reason,
            budget=budget,
            min_notional=min_notional,
            min_shares=min_shares,
            pm_u=float(tk.pm_u),
            pm_d=float(tk.pm_d),
        )

    for t in range(len(ticks)):
        process_active(t)
        paladin_v7_step(runner, t, ticks, params=p, try_buy_fn=try_buy_fn)

    return runner.st


__all__ = ["SPIKE_REASONS", "run_window_v7_delay2s"]
