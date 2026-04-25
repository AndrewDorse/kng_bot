#!/usr/bin/env python3
"""
Synthetic PALADIN v7 checks: uneven legs (favorite vs underdog sizes) and forced hedge timeout.

Run from repo root:
  python PALADIN/test_paladin_v7_skew_hedge_timeout.py

Notes:
- v7 only arms ``pending_second`` when |up−down| > min_shares − 1 (default 5 → need gap ≥ 5).
  A 6 vs 5 book (gap 1) is intentionally *below* that gate, so no hedge path runs.
- For forced-at-timeout we seed 10 vs 5 (up favorite), block cheap fills in ``try_buy_fn``,
  and assert a ``v7_hedge_forced`` fill on the expected second (timeout 30).
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
_PAL = Path(__file__).resolve().parent
if str(_PAL) not in sys.path:
    sys.path.insert(0, str(_PAL))

from paladin_engine import apply_buy_fill  # noqa: E402
from paladin_v7 import PaladinV7Params, PaladinV7Runner, WindowTick, paladin_v7_step  # noqa: E402
from simulate_paladin_window import SimState, Trade, try_buy  # noqa: E402


def _seed_two_leg(
    st: SimState,
    *,
    up_sh: float,
    dn_sh: float,
    pm_u: float,
    pm_d: float,
    reason_up: str,
    reason_dn: str,
) -> None:
    """Fill winning-side then losing-side notionals at the given mids (synthetic test book)."""
    su, au, sd, ad = apply_buy_fill(0.0, 0.0, 0.0, 0.0, side="up", add_shares=up_sh, fill_price=pm_u)
    su, au, sd, ad = apply_buy_fill(su, au, sd, ad, side="down", add_shares=dn_sh, fill_price=pm_d)
    st.size_up, st.avg_up, st.size_down, st.avg_down = su, au, sd, ad
    n1 = up_sh * pm_u
    n2 = dn_sh * pm_d
    st.spent_usdc = n1 + n2
    st.trades.append(Trade(0, "up", up_sh, pm_u, n1, reason_up))
    st.trades.append(Trade(0, "down", dn_sh, pm_d, n2, reason_dn))


def _flat_ticks(pm_u: float, pm_d: float, n: int = 900) -> list[WindowTick]:
    return [WindowTick(pm_u=pm_u, pm_d=pm_d, btc_px=50_000.0 + float(t), btc_vol=1.0) for t in range(n)]


def test_skew_6_vs_5_no_hedge_when_below_material_gap() -> None:
    """6 up / 5 down with up favorite: gap 1 < min_sh(5)−1 → strategy never arms pending_second."""
    pm_u, pm_d = 0.62, 0.38
    p = PaladinV7Params(hedge_timeout_seconds=30.0, budget_usdc=500.0)
    ticks = _flat_ticks(pm_u, pm_d)
    runner = PaladinV7Runner()
    _seed_two_leg(
        runner.st,
        up_sh=6.0,
        dn_sh=5.0,
        pm_u=pm_u,
        pm_d=pm_d,
        reason_up="test_seed_win",
        reason_dn="test_seed_lose",
    )
    n_tr0 = len(runner.st.trades)
    for t in range(120):
        paladin_v7_step(runner, t, ticks, params=p, try_buy_fn=try_buy)
    assert runner.pending_second is None
    assert len(runner.st.trades) == n_tr0
    assert all(tr.reason not in ("v7_hedge_cheap", "v7_hedge_forced") for tr in runner.st.trades)


def test_forced_hedge_at_timeout_skew_10_vs_5() -> None:
    """10 up / 5 down: gap arms hedge; cheap blocked → forced at t0 + timeout."""
    pm_u, pm_d = 0.62, 0.38
    p = PaladinV7Params(hedge_timeout_seconds=30.0, budget_usdc=500.0)
    ticks = _flat_ticks(pm_u, pm_d)
    runner = PaladinV7Runner()
    _seed_two_leg(
        runner.st,
        up_sh=10.0,
        dn_sh=5.0,
        pm_u=pm_u,
        pm_d=pm_d,
        reason_up="test_seed_win",
        reason_dn="test_seed_lose",
    )

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
        if reason == "v7_hedge_cheap":
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

    forced_elapsed: int | None = None
    for t in range(120):
        paladin_v7_step(runner, t, ticks, params=p, try_buy_fn=try_buy_fn)
        if any(tr.reason == "v7_hedge_forced" for tr in runner.st.trades):
            forced_elapsed = t
            break
    assert forced_elapsed == 30, f"expected forced hedge at elapsed=30, got {forced_elapsed}"
    assert abs(float(runner.st.size_up) - float(runner.st.size_down)) <= 1e-6 + p.balance_share_tolerance


def main() -> None:
    test_skew_6_vs_5_no_hedge_when_below_material_gap()
    test_forced_hedge_at_timeout_skew_10_vs_5()
    print("PALADIN v7 skew + hedge timeout tests: OK")


if __name__ == "__main__":
    main()
