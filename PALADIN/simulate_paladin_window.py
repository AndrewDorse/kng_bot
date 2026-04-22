#!/usr/bin/env python3
"""
Replay PALADIN-style rules on a recorded BTC 15m window (1 Hz prices).

This is a deterministic simulation harness (not live trading). Rules implemented:
- Clip ladder: **5**-share opens until **min leg >= 20**; then **5→7→8→10** (ascending, first that
  meets ROI floor). Pair-only uses **3%** marginal floor and **10%** profit lock (with **20+ sh** per leg).
- Min notional $1 per clip where applicable; min clip 5 shares.
- Max budget USDC per window (default 100).
- Disbalance band from STRATEGY_CORE via PaladinParams.
- Profit lock: USD path unchanged; **ROI 10%+10%** only when **each leg >= 20 shares** (engine).
- Priority each second: profit lock -> rebalance smaller side -> pair inventory when pm_up+pm_down
  is tight -> single-leg buy only if fill improves that leg's average (or opens leg).
- **Cooldown** (default **2s** simulated time) after **every** fill. Pair adds are **two fills**:
  UP at second `t`, DOWN at the earliest second `>= t + cooldown` (uses prices at that second).
  While DOWN is pending, no other orders fire.

Compare output to exported wallet 30s_state for the same slug (realized fills differ).

**Stdout:** Every run prints the 30-row bucket table and related sections; output is **flushed** each line
so logs appear immediately in the IDE or when piped.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from paladin_engine import (
    PaladinParams,
    analyze_snapshot,
    apply_buy_fill,
    load_bucket_csv,
    profit_lock_triggered,
    max_disbalance_shares,
    pnl_if_down_usdc,
    pnl_if_up_usdc,
    roi_if_down,
    roi_if_up,
    share_imbalance,
    smaller_side,
)

Side = Literal["up", "down"]


def _p(*args: object, **kwargs: Any) -> None:
    """Stdout print with flush so IDE/piped runs always show output immediately."""
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)


def window_slug_from_prices_csv(path: Path) -> str:
    stem = path.stem
    key = "btc-updown-15m-"
    if key not in stem:
        return "btc-updown-15m-unknown"
    tail = stem[stem.index(key) :]
    if tail.endswith("_prices"):
        return tail[: -len("_prices")]
    return tail


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRICES = (
    REPO_ROOT
    / "exports"
    / "window_price_snapshots_public"
    / "20260406_051501_btc-updown-15m-1775441700_prices.csv"
)
DEFAULT_WALLET_30S = (
    REPO_ROOT
    / "exports"
    / "target_wallet_e1_dataset"
    / "analysis_10windows_btc"
    / "02_btc-updown-15m-1775441700"
    / "btc-updown-15m-1775441700_30s_state.csv"
)
DEFAULT_EXAMPLE_TRACE = Path(__file__).resolve().parent / "data" / "example_bucket_trace.csv"
DEFAULT_PROFIT_LOCK_CONFIG = Path(__file__).resolve().parent / "paladin_sim_config.json"

# Used only if config file is missing or keys omitted.
_FALLBACK_PROFIT_LOCK: dict[str, float] = {
    "roi_lock_min_each": 0.10,
    "profit_lock_usdc_each_scenario": 5.0,
    "profit_lock_min_shares_per_side": 20.0,
}


def load_profit_lock_config(path: Path) -> dict[str, float]:
    """Merge JSON file over fallbacks (edit paladin_sim_config.json to tune exits)."""
    cfg = dict(_FALLBACK_PROFIT_LOCK)
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        for k in _FALLBACK_PROFIT_LOCK:
            if k in raw:
                cfg[k] = float(raw[k])
    return cfg


@dataclass(slots=True)
class Trade:
    elapsed_sec: int
    side: Side
    shares: float
    price: float
    notional: float
    reason: str


@dataclass(slots=True)
class SimState:
    size_up: float = 0.0
    avg_up: float = 0.0
    size_down: float = 0.0
    avg_down: float = 0.0
    spent_usdc: float = 0.0
    locked: bool = False
    lock_reason: str = ""
    trades: list[Trade] = field(default_factory=list)

    def snapshot_metrics(self) -> dict[str, float]:
        return {
            "size_up": self.size_up,
            "size_down": self.size_down,
            "avg_up": self.avg_up,
            "avg_down": self.avg_down,
            "spent_usdc": self.spent_usdc,
            "pnl_if_up_usdc": pnl_if_up_usdc(self.size_up, self.avg_up, self.size_down, self.avg_down),
            "pnl_if_down_usdc": pnl_if_down_usdc(self.size_up, self.avg_up, self.size_down, self.avg_down),
            "roi_up": roi_if_up(self.size_up, self.avg_up, self.size_down, self.avg_down),
            "roi_dn": roi_if_down(self.size_up, self.avg_up, self.size_down, self.avg_down),
        }


@dataclass(slots=True)
class PaladinPairRunner:
    """One-window mutable state for PALADIN pair-only stepping (replay or live)."""

    st: SimState = field(default_factory=SimState)
    last_buy_elapsed: float = -1_000_000.0
    # (side to buy next, shares, ready_at_elapsed) — completes a pair after first leg + cooldown.
    pending_second_leg: tuple[Side, float, float] | None = None
    # Causal “ladder”: when balanced, next stagger first leg must be this side; flip after each full pair.
    alternate_next_first: Side = "up"
    last_stagger_pair_start_elapsed: float = -1_000_000.0
    # (pm_u, pm_d) each simulated second — for trailing-min entry filters (no lookahead).
    pm_history: list[tuple[float, float]] = field(default_factory=list)


def load_prices_by_elapsed(path: Path) -> dict[int, tuple[float, float]]:
    """elapsed_sec -> (up_price, down_price). Last row wins if duplicates."""
    out: dict[int, tuple[float, float]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            e = int(float(row["elapsed_sec"]))
            out[e] = (float(row["up_price"]), float(row["down_price"]))
    return out


def forward_fill_prices(by_elapsed: dict[int, tuple[float, float]], window_sec: int = 900) -> list[tuple[float, float]]:
    """One price pair per second [0, window_sec)."""
    last_u, last_d = 0.5, 0.5
    series: list[tuple[float, float]] = []
    for t in range(window_sec):
        if t in by_elapsed:
            last_u, last_d = by_elapsed[t]
        series.append((last_u, last_d))
    return series


def resolve_winner_from_last_prices(
    series: list[tuple[float, float]],
    *,
    tie_eps: float = 1e-9,
) -> tuple[Literal["up", "down", "tie"], float, float]:
    """
    Proxy winner from the final replayed second: whichever of (up_price, down_price)
    is higher matches how these windows resolve in backtests on exported snapshots.
    """
    if not series:
        return "tie", 0.5, 0.5
    last_u, last_d = series[-1]
    if last_u > last_d + tie_eps:
        return "up", last_u, last_d
    if last_d > last_u + tie_eps:
        return "down", last_u, last_d
    return "tie", last_u, last_d


def settled_pnl_usdc(metrics: dict[str, float], winner: Literal["up", "down", "tie"]) -> float:
    """Settlement PnL given resolved side (tie → average of both scenarios)."""
    pu = float(metrics["pnl_if_up_usdc"])
    pd = float(metrics["pnl_if_down_usdc"])
    if winner == "up":
        return pu
    if winner == "down":
        return pd
    return 0.5 * (pu + pd)


def improves_leg(size: float, avg: float, px: float, qty: float) -> bool:
    if qty <= 0:
        return False
    if size <= 0:
        return True
    new_avg = (size * avg + qty * px) / (size + qty)
    return new_avg <= avg + 1e-12


def can_afford(spent: float, add: float, budget: float) -> bool:
    return spent + add <= budget + 1e-9


def clip_for_inventory(size_up: float, size_down: float, *, min_clip: float = 5.0, max_clip: float = 10.0) -> float:
    """Open with min_clip (5); scale to 7, 8, … once both legs exist (STRATEGY_CORE)."""
    if size_up <= 1e-9 or size_down <= 1e-9:
        return min_clip
    m = min(size_up, size_down)
    if m < 20.0:
        return min_clip
    if m < 55.0:
        return 7.0
    if m < 120.0:
        return 8.0
    return min(max_clip, 10.0)


def clip_for_inventory_wide(
    size_up: float,
    size_down: float,
    *,
    min_clip: float = 5.0,
    max_clip: float = 15.0,
) -> float:
    """Dynamic clip: always min_clip until min leg >= 20; then allow up to max_clip (e.g. 5–15)."""
    if size_up <= 1e-9 or size_down <= 1e-9:
        return min_clip
    if min(size_up, size_down) < 20.0:
        return min_clip
    return float(max_clip)


def pair_clip_candidates_dynamic(
    min_leg: float,
    *,
    min_sh: float,
    max_sh: float,
) -> list[float]:
    """Integer share counts from min_sh..max_sh inclusive after both legs >= 20; else only min_sh."""
    if min_leg < 20.0 - 1e-9:
        return [float(min_sh)]
    lo = int(round(min_sh))
    hi = int(round(max_sh))
    return [float(i) for i in range(lo, hi + 1)]


def shares_affordable(px: float, budget_left: float, desired: float, *, min_sh: float = 5.0) -> float:
    """Largest fill size <= desired and >= min_sh that fits budget and $1 notional."""
    px = max(px, 1e-9)
    min_for_notional = max(min_sh, float(math.ceil(1.0 / px)))
    max_by_budget = budget_left / px
    s = min(desired, max_by_budget)
    if s < min_for_notional - 1e-9:
        return 0.0
    return s


def try_buy(
    st: SimState,
    *,
    t: int,
    side: Side,
    shares: float,
    px: float,
    reason: str,
    budget: float,
    min_notional: float,
    min_shares: float = 5.0,
) -> float:
    """Simulated full fill. Returns matched shares, or 0.0 if rejected."""
    px = round(px, 4)
    notion = shares * px
    if shares < min_shares - 1e-9:
        return 0.0
    if notion < min_notional - 1e-9:
        return 0.0
    if not can_afford(st.spent_usdc, notion, budget):
        return 0.0
    su, au, sd, ad = apply_buy_fill(st.size_up, st.avg_up, st.size_down, st.avg_down, side=side, add_shares=shares, fill_price=px)
    st.size_up, st.avg_up, st.size_down, st.avg_down = su, au, sd, ad
    st.spent_usdc += notion
    st.trades.append(Trade(t, side, shares, px, notion, reason))
    return float(shares)


def min_roi_after_symmetric_pair(
    size_up: float,
    avg_up: float,
    size_down: float,
    avg_down: float,
    pu: float,
    pd: float,
    sh: float,
) -> float:
    """Min of settlement ROIs after adding `sh` to both legs at (pu, pd)."""
    su, au, sd, ad = apply_buy_fill(size_up, avg_up, size_down, avg_down, side="up", add_shares=sh, fill_price=pu)
    su, au, sd, ad = apply_buy_fill(su, au, sd, ad, side="down", add_shares=sh, fill_price=pd)
    return min(roi_if_up(su, au, sd, ad), roi_if_down(su, au, sd, ad))


def min_roi_after_buy_leg(
    size_up: float,
    avg_up: float,
    size_down: float,
    avg_down: float,
    *,
    side: Side,
    sh: float,
    px: float,
) -> float:
    """Min settlement ROI after one additional buy on `side` at `px` (for staggered second leg)."""
    su, au, sd, ad = apply_buy_fill(
        size_up, avg_up, size_down, avg_down, side=side, add_shares=sh, fill_price=px
    )
    return min(roi_if_up(su, au, sd, ad), roi_if_down(su, au, sd, ad))


def stagger_first_leg_candidates(
    pm_u: float, pm_d: float, first_leg_max_px: float
) -> list[tuple[Side, float]]:
    """Sides at/below first-leg max, sorted cheaper first (for inventory-aware stagger opens)."""
    opts: list[tuple[Side, float]] = []
    if pm_u <= first_leg_max_px + 1e-12:
        opts.append(("up", pm_u))
    if pm_d <= first_leg_max_px + 1e-12:
        opts.append(("down", pm_d))
    opts.sort(key=lambda x: x[1])
    return opts


def stagger_first_leg_candidates_winning_side_when_position(
    pm_u: float,
    pm_d: float,
    first_leg_max_px: float,
    *,
    size_up: float,
    size_down: float,
    tie_eps: float = 1e-9,
) -> list[tuple[Side, float]]:
    """
    Like stagger_first_leg_candidates, but once we already have inventory on either leg,
    only open a new stagger *first* leg on the side with the higher mid (market "winner").
    If that side is above first_leg_max_px, return [] (no new stagger first leg this second;
    avoids opening the cheap "loser" leg first and then force-hedging at a bad price).
    """
    base = stagger_first_leg_candidates(pm_u, pm_d, first_leg_max_px)
    if not base:
        return base
    if size_up <= 1e-9 and size_down <= 1e-9:
        return base
    if pm_u > pm_d + tie_eps:
        win: Side = "up"
    elif pm_d > pm_u + tie_eps:
        win = "down"
    else:
        return base
    return [(s, p) for s, p in base if s == win]


def _clamp_symmetric_clip_for_cap(
    sh: float,
    st: SimState,
    *,
    max_shares_per_side: float | None,
    min_clip: float,
) -> float:
    """Max equal add to both legs such that neither side exceeds cap."""
    if max_shares_per_side is None or max_shares_per_side <= 0:
        return sh
    room_u = float(max_shares_per_side) - st.size_up
    room_d = float(max_shares_per_side) - st.size_down
    room = min(room_u, room_d)
    if room < min_clip - 1e-9:
        return 0.0
    return min(float(sh), room)


def _clamp_one_leg_for_cap(
    sh: float,
    st: SimState,
    *,
    side: Side,
    max_shares_per_side: float | None,
    min_clip: float,
) -> float:
    if max_shares_per_side is None or max_shares_per_side <= 0:
        return sh
    cur = st.size_up if side == "up" else st.size_down
    room = float(max_shares_per_side) - cur
    if room < min_clip - 1e-9:
        return 0.0
    return min(float(sh), room)


TryBuy = Callable[..., float]


def _effective_pair_sum_cap(
    base_max: float,
    *,
    tighten_per_fill: float,
    min_floor: float,
    n_fills: int,
) -> float:
    """Lower the pair sum cap as fills accrue (wait for better mids on later clips)."""
    if tighten_per_fill <= 0:
        return float(base_max)
    return max(float(min_floor), float(base_max) - float(tighten_per_fill) * int(n_fills))


def _effective_target_roi(
    base_roi: float,
    *,
    per_fill: float,
    n_fills: int,
) -> float:
    """Optional stricter marginal ROI after each fill."""
    if per_fill <= 0:
        return float(base_roi)
    return float(base_roi) + float(per_fill) * int(n_fills)


def _violates_blended_avg_sum_cap(
    size_up: float,
    avg_up: float,
    size_down: float,
    avg_down: float,
    *,
    side: Side,
    sh: float,
    px: float,
    cap: float | None,
) -> bool:
    """After a one-leg buy: if both legs exist, require avg_up + avg_down <= cap."""
    if cap is None or float(cap) <= 0 or sh <= 1e-9:
        return False
    nsu, nau, nsd, nad = apply_buy_fill(
        size_up, avg_up, size_down, avg_down, side=side, add_shares=sh, fill_price=px
    )
    if nsu <= 1e-9 or nsd <= 1e-9:
        return False
    return nau + nad > float(cap) + 1e-9


def _clamp_shares_for_blended_pair_avg_sum(
    size_up: float,
    avg_up: float,
    size_down: float,
    avg_down: float,
    *,
    side: Side,
    px: float,
    cap: float | None,
    sh_hi: float,
    min_clip: float,
) -> float:
    """Largest whole-share clip <= sh_hi (and >= min_clip) with post-fill avg_up+avg_down <= cap."""
    if cap is None or float(cap) <= 0:
        return float(sh_hi)
    if sh_hi < min_clip - 1e-9:
        return 0.0
    s_hi = int(math.floor(float(sh_hi) + 1e-9))
    s_lo = int(math.ceil(float(min_clip) - 1e-9))
    for s in range(s_hi, s_lo - 1, -1):
        if not _violates_blended_avg_sum_cap(
            size_up,
            avg_up,
            size_down,
            avg_down,
            side=side,
            sh=float(s),
            px=px,
            cap=cap,
        ):
            return float(s)
    return 0.0


def _passes_trailing_leg_low(
    hist: list[tuple[float, float]],
    window: int,
    side: Side,
    pm_u: float,
    pm_d: float,
    slip: float,
) -> bool:
    """Causal dip filter: leg price is near the trailing minimum over the last `window` samples."""
    if window <= 0 or not hist:
        return True
    chunk = hist[-window:] if len(hist) >= window else hist
    if len(chunk) < 3:
        return True
    if side == "up":
        lo = min(u for u, _ in chunk)
        return pm_u <= lo + slip + 1e-12
    lo = min(d for _, d in chunk)
    return pm_d <= lo + slip + 1e-12


def balanced_symmetric_pair_start(
    runner: PaladinPairRunner,
    st: SimState,
    *,
    buy: TryBuy,
    t: int,
    pm_u: float,
    pm_d: float,
    first_side: Side,
    pair_px: float,
    pair_sum_max: float,
    cands: list[float],
    budget_usdc: float,
    budget_left: float,
    min_clip: float,
    min_notional: float,
    cooldown_seconds: float,
    max_shares_per_side: float | None,
    max_blended_pair_avg_sum: float | None,
    skip_first_leg_blend: bool,
    sym_roi_floor: float,
) -> bool:
    """Start UP+DOWN pair with `first_side` filling first (causal symmetric fallback)."""
    if pair_px > float(pair_sum_max) + 1e-9:
        return False
    sh_pair_fb = 0.0
    for cand in cands:
        if cand < min_clip - 1e-9:
            continue
        sh_try = _clamp_symmetric_clip_for_cap(
            cand, st, max_shares_per_side=max_shares_per_side, min_clip=min_clip
        )
        if sh_try < min_clip - 1e-9:
            continue
        pc = sh_try * pair_px
        if pc > budget_left + 1e-9 or not can_afford(st.spent_usdc, pc, budget_usdc):
            continue
        if sh_try * pm_u < 1.0 - 1e-9 or sh_try * pm_d < 1.0 - 1e-9:
            continue
        mr = min_roi_after_symmetric_pair(
            st.size_up, st.avg_up, st.size_down, st.avg_down, pm_u, pm_d, sh_try
        )
        cur_m = min(
            roi_if_up(st.size_up, st.avg_up, st.size_down, st.avg_down),
            roi_if_down(st.size_up, st.avg_up, st.size_down, st.avg_down),
        )
        cheap_pair = pair_px + 1e-9 <= min(0.995, float(pair_sum_max))
        improves_book = mr + 1e-9 >= cur_m
        if mr + 1e-9 >= sym_roi_floor or (cheap_pair and improves_book):
            sh_pair_fb = sh_try
            break
    if sh_pair_fb <= 0 or not can_afford(st.spent_usdc, sh_pair_fb * pair_px, budget_usdc):
        return False
    px_first = pm_u if first_side == "up" else pm_d
    blend_viol = _violates_blended_avg_sum_cap(
        st.size_up,
        st.avg_up,
        st.size_down,
        st.avg_down,
        side=first_side,
        sh=sh_pair_fb,
        px=px_first,
        cap=max_blended_pair_avg_sum,
    )
    if skip_first_leg_blend:
        blend_viol = False
    if blend_viol:
        return False
    reason = (
        "pair_up_balanced_sym_fallback"
        if first_side == "up"
        else "pair_dn_balanced_sym_fallback"
    )
    filled = buy(
        st,
        t=t,
        side=first_side,
        shares=sh_pair_fb,
        px=px_first,
        reason=reason,
        budget=budget_usdc,
        min_notional=min_notional,
        min_shares=min_clip,
    )
    if filled <= 0:
        return False
    other: Side = "down" if first_side == "up" else "up"
    runner.pending_second_leg = (other, filled, float(t) + float(cooldown_seconds))
    runner.last_buy_elapsed = float(t)
    return True


def paladin_step(
    runner: PaladinPairRunner,
    t: int,
    pm_u: float,
    pm_d: float,
    *,
    budget_usdc: float,
    params: PaladinParams,
    pair_sum_max: float = 0.982,
    single_leg_max_px: float = 0.54,
    pair_only: bool = False,
    stagger_pair_entry: bool = False,
    stagger_hedge_force_after_seconds: float | None = None,
    target_min_roi: float = 0.03,
    cooldown_seconds: float = 2.0,
    dynamic_clip_cap: float | None = None,
    pair_size_pick: Literal["ascending", "max_feasible"] = "max_feasible",
    try_buy_fn: TryBuy | None = None,
    max_shares_per_side: float | None = None,
    pair_sum_tighten_per_fill: float = 0.0,
    pair_sum_min_floor: float = 0.88,
    force_hedge_respects_effective_sum: bool = False,
    second_leg_book_improve_eps: float = 0.0,
    target_roi_per_fill: float = 0.0,
    pending_hedge_bypass_imbalance_shares: float | None = None,
    discipline_relax_after_forced_sec: float | None = None,
    max_blended_pair_avg_sum: float | None = None,
    max_elapsed_to_start_flat: int | None = None,
    min_elapsed_for_flat_open: int | None = None,
    stagger_winning_side_first_when_position: bool = False,
    stagger_symmetric_fallback_when_balanced: bool = True,
    stagger_symmetric_fallback_roi_discount: float = 0.03,
    stagger_symmetric_fallback_skip_first_leg_blend_cap: bool = False,
    stagger_alternate_first_leg_when_balanced: bool = False,
    min_elapsed_between_pair_starts: float | None = None,
    entry_trailing_min_low_seconds: int | None = None,
    entry_trailing_low_slippage: float = 0.02,
    second_leg_must_improve_leg_avg: bool = False,
) -> bool:
    """Advance PALADIN by one simulated second. Returns True if profit-lock stopped trading."""
    st = runner.st
    buy: TryBuy = try_buy_fn if try_buy_fn is not None else try_buy
    min_notional = 1.0
    min_clip = float(params.min_clip_shares)
    runner.pm_history.append((float(pm_u), float(pm_d)))
    n_fills = len(st.trades)
    eff_sum_max = _effective_pair_sum_cap(
        pair_sum_max,
        tighten_per_fill=pair_sum_tighten_per_fill,
        min_floor=pair_sum_min_floor,
        n_fills=n_fills,
    )
    eff_target_roi = _effective_target_roi(
        target_min_roi, per_fill=target_roi_per_fill, n_fills=n_fills
    )
    sym_roi_floor = max(
        0.0, float(eff_target_roi) - float(stagger_symmetric_fallback_roi_discount)
    )
    if (
        max_shares_per_side is not None
        and max_shares_per_side > 0
        and st.size_up >= max_shares_per_side - 1e-9
        and st.size_down >= max_shares_per_side - 1e-9
        and runner.pending_second_leg is None
    ):
        return False
    locked, reason = profit_lock_triggered(st.size_up, st.avg_up, st.size_down, st.avg_down, params)
    if locked:
        st.locked = True
        st.lock_reason = reason
        return True

    mesf = max_elapsed_to_start_flat
    if mesf is not None and mesf >= 0:
        if (
            st.size_up <= 1e-9
            and st.size_down <= 1e-9
            and runner.pending_second_leg is None
            and float(t) > float(mesf) + 1e-9
        ):
            return False

    mifo = min_elapsed_for_flat_open
    if mifo is not None and mifo > 0:
        if (
            st.size_up <= 1e-9
            and st.size_down <= 1e-9
            and runner.pending_second_leg is None
            and float(t) + 1e-9 < float(mifo)
        ):
            return False

    if runner.pending_second_leg is not None:
        side_w, sh_w, ready_at = runner.pending_second_leg
        if float(t) + 1e-9 < ready_at:
            return False
        px = pm_u if side_w == "up" else pm_d
        hf_sec = float(stagger_hedge_force_after_seconds or 0.0)
        time_force_deadline = float(ready_at) + hf_sec
        force_hedge = (
            stagger_pair_entry
            and stagger_hedge_force_after_seconds is not None
            and hf_sec > 0
            and float(t) + 1e-9 >= time_force_deadline
        )
        imb_abs = abs(share_imbalance(st.size_up, st.size_down))
        bypass_thr = pending_hedge_bypass_imbalance_shares
        bypass_imb = (
            bypass_thr is not None
            and float(bypass_thr) > 0
            and imb_abs >= float(bypass_thr) - 1e-9
        )
        drel = discipline_relax_after_forced_sec
        extra_relax = (
            drel is not None
            and float(drel) > 0
            and force_hedge
            and float(t) + 1e-9 >= time_force_deadline + float(drel)
        )
        use_relaxed_pending = bypass_imb or extra_relax
        eff_sum_pending = float(pair_sum_max) if use_relaxed_pending else eff_sum_max
        book_eps_pending = 0.0 if use_relaxed_pending else second_leg_book_improve_eps
        if stagger_pair_entry and not force_hedge:
            if pm_u + pm_d > eff_sum_pending + 1e-9:
                return False
            if (
                book_eps_pending > 0
                and st.size_up > 1e-9
                and st.size_down > 1e-9
                and pm_u + pm_d > st.avg_up + st.avg_down - book_eps_pending + 1e-9
            ):
                return False
            mr = min_roi_after_buy_leg(
                st.size_up, st.avg_up, st.size_down, st.avg_down, side=side_w, sh=sh_w, px=px
            )
            if mr + 1e-9 < eff_target_roi:
                return False
        if force_hedge:
            # Old behavior: force skipped sum/ROI. When tightening, book-beat, or explicit flag is on,
            # keep waiting for a cheaper second leg instead of buying a bad forced hedge.
            discipline_force = (
                not use_relaxed_pending
                and (
                    force_hedge_respects_effective_sum
                    or pair_sum_tighten_per_fill > 0
                    or second_leg_book_improve_eps > 0
                )
            )
            if discipline_force:
                if pm_u + pm_d > eff_sum_max + 1e-9:
                    return False
                if (
                    second_leg_book_improve_eps > 0
                    and st.size_up > 1e-9
                    and st.size_down > 1e-9
                    and pm_u + pm_d > st.avg_up + st.avg_down - second_leg_book_improve_eps + 1e-9
                ):
                    return False
            reason_second = (
                "stagger_second_force_imb_relax" if use_relaxed_pending else "stagger_second_force"
            )
        elif stagger_pair_entry:
            reason_second = "stagger_second"
        else:
            reason_second = "pair_second"
        sh_exec = _clamp_one_leg_for_cap(
            sh_w, st, side=side_w, max_shares_per_side=max_shares_per_side, min_clip=min_clip
        )
        sh_exec = _clamp_shares_for_blended_pair_avg_sum(
            st.size_up,
            st.avg_up,
            st.size_down,
            st.avg_down,
            side=side_w,
            px=px,
            cap=max_blended_pair_avg_sum,
            sh_hi=sh_exec,
            min_clip=min_clip,
        )
        if sh_exec < min_clip - 1e-9:
            return False
        if (
            second_leg_must_improve_leg_avg
            and not use_relaxed_pending
            and not force_hedge
        ):
            sz_i = st.size_up if side_w == "up" else st.size_down
            av_i = st.avg_up if side_w == "up" else st.avg_down
            if sz_i > 1e-9 and not improves_leg(sz_i, av_i, px, sh_exec):
                return False
        filled_w = buy(
            st,
            t=t,
            side=side_w,
            shares=sh_exec,
            px=px,
            reason=reason_second,
            budget=budget_usdc,
            min_notional=min_notional,
            min_shares=min_clip,
        )
        if filled_w > 0:
            runner.last_buy_elapsed = float(t)
            runner.pending_second_leg = None
            if stagger_alternate_first_leg_when_balanced:
                o = runner.alternate_next_first
                runner.alternate_next_first = "down" if o == "up" else "up"
        return False

    if cooldown_seconds > 0 and float(t) < runner.last_buy_elapsed + cooldown_seconds - 1e-9:
        return False

    if dynamic_clip_cap is not None:
        clip = clip_for_inventory_wide(
            st.size_up, st.size_down, min_clip=min_clip, max_clip=float(dynamic_clip_cap)
        )
    else:
        clip = clip_for_inventory(st.size_up, st.size_down, min_clip=min_clip)
    budget_left = budget_usdc - st.spent_usdc

    max_d = max_disbalance_shares(st.size_up, st.size_down, params)
    imb = share_imbalance(st.size_up, st.size_down)

    if pair_only:
        pair_px = pm_u + pm_d
        min_leg = min(st.size_up, st.size_down)
        if dynamic_clip_cap is not None:
            cands = pair_clip_candidates_dynamic(
                min_leg, min_sh=min_clip, max_sh=float(dynamic_clip_cap)
            )
        elif min_leg < 20.0 - 1e-9:
            cands = [5.0]
        else:
            cands = [5.0, 7.0, 8.0, 10.0]
        if pair_size_pick == "max_feasible":
            cands = list(reversed(cands))

        if stagger_pair_entry:
            imb_pair = share_imbalance(st.size_up, st.size_down)
            balanced = abs(imb_pair) <= 1e-9
            trail_w = int(entry_trailing_min_low_seconds or 0)
            trail_slip = float(entry_trailing_low_slippage)

            if (
                balanced
                and min_elapsed_between_pair_starts is not None
                and float(min_elapsed_between_pair_starts) > 0
                and float(t) - runner.last_stagger_pair_start_elapsed
                < float(min_elapsed_between_pair_starts) - 1e-9
            ):
                return False

            side_opts: list[tuple[Side, float]] = []
            sym_first: Side = "up"

            if balanced and stagger_alternate_first_leg_when_balanced:
                base = stagger_first_leg_candidates(pm_u, pm_d, single_leg_max_px)
                side_opts = [(s, p) for s, p in base if s == runner.alternate_next_first]
                sym_first = runner.alternate_next_first
                if (
                    not side_opts
                    and stagger_symmetric_fallback_when_balanced
                    and balanced_symmetric_pair_start(
                        runner,
                        st,
                        buy=buy,
                        t=t,
                        pm_u=pm_u,
                        pm_d=pm_d,
                        first_side=sym_first,
                        pair_px=pair_px,
                        pair_sum_max=float(pair_sum_max),
                        cands=cands,
                        budget_usdc=budget_usdc,
                        budget_left=budget_left,
                        min_clip=min_clip,
                        min_notional=min_notional,
                        cooldown_seconds=cooldown_seconds,
                        max_shares_per_side=max_shares_per_side,
                        max_blended_pair_avg_sum=max_blended_pair_avg_sum,
                        skip_first_leg_blend=stagger_symmetric_fallback_skip_first_leg_blend_cap,
                        sym_roi_floor=sym_roi_floor,
                    )
                ):
                    runner.last_stagger_pair_start_elapsed = float(t)
                    return False
            elif stagger_winning_side_first_when_position:
                side_opts = stagger_first_leg_candidates_winning_side_when_position(
                    pm_u,
                    pm_d,
                    single_leg_max_px,
                    size_up=st.size_up,
                    size_down=st.size_down,
                )
                sym_first = "up"
                if (
                    not side_opts
                    and balanced
                    and stagger_symmetric_fallback_when_balanced
                    and balanced_symmetric_pair_start(
                        runner,
                        st,
                        buy=buy,
                        t=t,
                        pm_u=pm_u,
                        pm_d=pm_d,
                        first_side="up",
                        pair_px=pair_px,
                        pair_sum_max=float(pair_sum_max),
                        cands=cands,
                        budget_usdc=budget_usdc,
                        budget_left=budget_left,
                        min_clip=min_clip,
                        min_notional=min_notional,
                        cooldown_seconds=cooldown_seconds,
                        max_shares_per_side=max_shares_per_side,
                        max_blended_pair_avg_sum=max_blended_pair_avg_sum,
                        skip_first_leg_blend=stagger_symmetric_fallback_skip_first_leg_blend_cap,
                        sym_roi_floor=sym_roi_floor,
                    )
                ):
                    runner.last_stagger_pair_start_elapsed = float(t)
                    return False
                if not side_opts and not balanced:
                    side_opts = stagger_first_leg_candidates(pm_u, pm_d, single_leg_max_px)
            else:
                side_opts = stagger_first_leg_candidates(pm_u, pm_d, single_leg_max_px)

            if not side_opts:
                return False
            sh_use = 0.0
            for cand in cands:
                if cand < min_clip - 1e-9:
                    continue
                sh_try = _clamp_symmetric_clip_for_cap(
                    cand, st, max_shares_per_side=max_shares_per_side, min_clip=min_clip
                )
                if sh_try < min_clip - 1e-9:
                    continue
                pc = sh_try * pair_px
                if pc > budget_left + 1e-9 or not can_afford(st.spent_usdc, pc, budget_usdc):
                    continue
                if sh_try * pm_u < 1.0 - 1e-9 or sh_try * pm_d < 1.0 - 1e-9:
                    continue
                sh_use = sh_try
                break
            if sh_use <= 0:
                return False
            filled_first = 0.0
            first_side: Side | None = None
            for side_try, px_try in side_opts:
                if not _passes_trailing_leg_low(
                    runner.pm_history,
                    trail_w,
                    side_try,
                    pm_u,
                    pm_d,
                    trail_slip,
                ):
                    continue
                sz = st.size_up if side_try == "up" else st.size_down
                av = st.avg_up if side_try == "up" else st.avg_down
                if sz > 1e-9 and not improves_leg(sz, av, px_try, sh_use):
                    continue
                if _violates_blended_avg_sum_cap(
                    st.size_up,
                    st.avg_up,
                    st.size_down,
                    st.avg_down,
                    side=side_try,
                    sh=sh_use,
                    px=px_try,
                    cap=max_blended_pair_avg_sum,
                ):
                    continue
                ff = buy(
                    st,
                    t=t,
                    side=side_try,
                    shares=sh_use,
                    px=px_try,
                    reason="stagger_first",
                    budget=budget_usdc,
                    min_notional=min_notional,
                    min_shares=min_clip,
                )
                if ff > 0:
                    filled_first = ff
                    first_side = side_try
                    break
            if filled_first > 0 and first_side is not None:
                other: Side = "down" if first_side == "up" else "up"
                runner.pending_second_leg = (
                    other,
                    filled_first,
                    float(t) + float(cooldown_seconds),
                )
                runner.last_buy_elapsed = float(t)
                if balanced:
                    runner.last_stagger_pair_start_elapsed = float(t)
            return False

        if pair_px > eff_sum_max + 1e-9:
            return False
        sh_pair = 0.0
        for cand in cands:
            if cand < min_clip - 1e-9:
                continue
            sh_try = _clamp_symmetric_clip_for_cap(
                cand, st, max_shares_per_side=max_shares_per_side, min_clip=min_clip
            )
            if sh_try < min_clip - 1e-9:
                continue
            pc = sh_try * pair_px
            if pc > budget_left + 1e-9 or not can_afford(st.spent_usdc, pc, budget_usdc):
                continue
            if sh_try * pm_u < 1.0 - 1e-9 or sh_try * pm_d < 1.0 - 1e-9:
                continue
            mr = min_roi_after_symmetric_pair(
                st.size_up, st.avg_up, st.size_down, st.avg_down, pm_u, pm_d, sh_try
            )
            if mr + 1e-9 >= eff_target_roi:
                sh_pair = sh_try
                break
        if sh_pair <= 0:
            return False
        pair_cost = sh_pair * pair_px
        if sh_pair > 0 and can_afford(st.spent_usdc, pair_cost, budget_usdc):
            if not _violates_blended_avg_sum_cap(
                st.size_up,
                st.avg_up,
                st.size_down,
                st.avg_down,
                side="up",
                sh=sh_pair,
                px=pm_u,
                cap=max_blended_pair_avg_sum,
            ):
                filled_up = buy(
                    st,
                    t=t,
                    side="up",
                    shares=sh_pair,
                    px=pm_u,
                    reason="pair_up",
                    budget=budget_usdc,
                    min_notional=min_notional,
                    min_shares=min_clip,
                )
                if filled_up > 0:
                    runner.pending_second_leg = (
                        "down",
                        filled_up,
                        float(t) + float(cooldown_seconds),
                    )
                    runner.last_buy_elapsed = float(t)
        return False

    if abs(imb) > max_d + 1e-9:
        side = smaller_side(st.size_up, st.size_down)
        px = pm_u if side == "up" else pm_d
        sh = shares_affordable(px, budget_left, clip, min_sh=min_clip)
        sh = _clamp_one_leg_for_cap(sh, st, side=side, max_shares_per_side=max_shares_per_side, min_clip=min_clip)
        if sh > 0:
            if (
                buy(
                    st,
                    t=t,
                    side=side,
                    shares=sh,
                    px=px,
                    reason="rebalance",
                    budget=budget_usdc,
                    min_notional=min_notional,
                    min_shares=min_clip,
                )
                > 0
            ):
                runner.last_buy_elapsed = float(t)
        return False

    if pm_u + pm_d <= eff_sum_max:
        sh_pair = shares_affordable(pm_u, budget_left, clip, min_sh=min_clip)
        sh_pair = _clamp_symmetric_clip_for_cap(
            sh_pair, st, max_shares_per_side=max_shares_per_side, min_clip=min_clip
        )
        pair_cost = sh_pair * (pm_u + pm_d) if sh_pair > 0 else 0.0
        if sh_pair > 0 and can_afford(st.spent_usdc, pair_cost, budget_usdc):
            ok_u = improves_leg(st.size_up, st.avg_up, pm_u, sh_pair)
            ok_d = improves_leg(st.size_down, st.avg_down, pm_d, sh_pair)
            if ok_u and ok_d:
                if not _violates_blended_avg_sum_cap(
                    st.size_up,
                    st.avg_up,
                    st.size_down,
                    st.avg_down,
                    side="up",
                    sh=sh_pair,
                    px=pm_u,
                    cap=max_blended_pair_avg_sum,
                ):
                    filled_up = buy(
                        st,
                        t=t,
                        side="up",
                        shares=sh_pair,
                        px=pm_u,
                        reason="pair_up",
                        budget=budget_usdc,
                        min_notional=min_notional,
                        min_shares=min_clip,
                    )
                    if filled_up > 0:
                        runner.pending_second_leg = (
                            "down",
                            filled_up,
                            float(t) + float(cooldown_seconds),
                        )
                        runner.last_buy_elapsed = float(t)
                return False

    min_leg = min(st.size_up, st.size_down)
    min_shares_for_pair_only = float(params.profit_lock_min_shares_per_side)
    allow_single_leg = min_leg < min_shares_for_pair_only - 1e-9 or (pm_u + pm_d) > eff_sum_max + 1e-9
    if not allow_single_leg:
        return False

    candidates: list[tuple[Side, float, float, float]] = []
    for side in ("up", "down"):
        px = pm_u if side == "up" else pm_d
        sz = st.size_up if side == "up" else st.size_down
        av = st.avg_up if side == "up" else st.avg_down
        if px > single_leg_max_px:
            continue
        sh = shares_affordable(px, budget_left, clip, min_sh=min_clip)
        sh = _clamp_one_leg_for_cap(sh, st, side=side, max_shares_per_side=max_shares_per_side, min_clip=min_clip)
        if sh <= 0 or not improves_leg(sz, av, px, sh):
            continue
        leg_sz = st.size_up if side == "up" else st.size_down
        candidates.append((side, px, sh, leg_sz))
    candidates.sort(key=lambda x: (x[3], x[1]))
    for side, px, sh, _ in candidates:
        if (
            buy(
                st,
                t=t,
                side=side,
                shares=sh,
                px=px,
                reason=f"inventory_{side}",
                budget=budget_usdc,
                min_notional=min_notional,
                min_shares=min_clip,
            )
            > 0
        ):
            runner.last_buy_elapsed = float(t)
            break

    return False


def run_window(
    prices: list[tuple[float, float]],
    *,
    budget_usdc: float,
    params: PaladinParams,
    pair_sum_max: float = 0.982,
    single_leg_max_px: float = 0.54,
    pair_only: bool = False,
    stagger_pair_entry: bool = False,
    stagger_hedge_force_after_seconds: float | None = None,
    target_min_roi: float = 0.03,
    cooldown_seconds: float = 2.0,
    dynamic_clip_cap: float | None = None,
    pair_size_pick: Literal["ascending", "max_feasible"] = "max_feasible",
    try_buy_fn: TryBuy | None = None,
    max_shares_per_side: float | None = None,
    pair_sum_tighten_per_fill: float = 0.0,
    pair_sum_min_floor: float = 0.88,
    force_hedge_respects_effective_sum: bool = False,
    second_leg_book_improve_eps: float = 0.0,
    target_roi_per_fill: float = 0.0,
    pending_hedge_bypass_imbalance_shares: float | None = None,
    discipline_relax_after_forced_sec: float | None = None,
    max_blended_pair_avg_sum: float | None = None,
    max_elapsed_to_start_flat: int | None = None,
    min_elapsed_for_flat_open: int | None = None,
    stagger_winning_side_first_when_position: bool = False,
    stagger_symmetric_fallback_when_balanced: bool = True,
    stagger_symmetric_fallback_roi_discount: float = 0.03,
    stagger_symmetric_fallback_skip_first_leg_blend_cap: bool = False,
    stagger_alternate_first_leg_when_balanced: bool = False,
    min_elapsed_between_pair_starts: float | None = None,
    entry_trailing_min_low_seconds: int | None = None,
    entry_trailing_low_slippage: float = 0.02,
    second_leg_must_improve_leg_avg: bool = False,
) -> SimState:
    runner = PaladinPairRunner()
    for t, (pm_u, pm_d) in enumerate(prices):
        if paladin_step(
            runner,
            t,
            pm_u,
            pm_d,
            budget_usdc=budget_usdc,
            params=params,
            pair_sum_max=pair_sum_max,
            single_leg_max_px=single_leg_max_px,
            pair_only=pair_only,
            stagger_pair_entry=stagger_pair_entry,
            stagger_hedge_force_after_seconds=stagger_hedge_force_after_seconds,
            target_min_roi=target_min_roi,
            cooldown_seconds=cooldown_seconds,
            dynamic_clip_cap=dynamic_clip_cap,
            pair_size_pick=pair_size_pick,
            try_buy_fn=try_buy_fn,
            max_shares_per_side=max_shares_per_side,
            pair_sum_tighten_per_fill=pair_sum_tighten_per_fill,
            pair_sum_min_floor=pair_sum_min_floor,
            force_hedge_respects_effective_sum=force_hedge_respects_effective_sum,
            second_leg_book_improve_eps=second_leg_book_improve_eps,
            target_roi_per_fill=target_roi_per_fill,
            pending_hedge_bypass_imbalance_shares=pending_hedge_bypass_imbalance_shares,
            discipline_relax_after_forced_sec=discipline_relax_after_forced_sec,
            max_blended_pair_avg_sum=max_blended_pair_avg_sum,
            max_elapsed_to_start_flat=max_elapsed_to_start_flat,
            min_elapsed_for_flat_open=min_elapsed_for_flat_open,
            stagger_winning_side_first_when_position=stagger_winning_side_first_when_position,
            stagger_symmetric_fallback_when_balanced=stagger_symmetric_fallback_when_balanced,
            stagger_symmetric_fallback_roi_discount=stagger_symmetric_fallback_roi_discount,
            stagger_symmetric_fallback_skip_first_leg_blend_cap=stagger_symmetric_fallback_skip_first_leg_blend_cap,
            stagger_alternate_first_leg_when_balanced=stagger_alternate_first_leg_when_balanced,
            min_elapsed_between_pair_starts=min_elapsed_between_pair_starts,
            entry_trailing_min_low_seconds=entry_trailing_min_low_seconds,
            entry_trailing_low_slippage=entry_trailing_low_slippage,
            second_leg_must_improve_leg_avg=second_leg_must_improve_leg_avg,
        ):
            break
    return runner.st


def load_wallet_30s(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def replay_inventory_from_trades(trades: list[Trade], window_sec: int = 900) -> list[tuple[float, float, float, float]]:
    """Per-second end state: size_up, size_down, avg_up, avg_down."""
    by_t: dict[int, list[Trade]] = defaultdict(list)
    for tr in trades:
        by_t[tr.elapsed_sec].append(tr)
    su = au = sd = ad = 0.0
    out: list[tuple[float, float, float, float]] = []
    for t in range(window_sec):
        for tr in by_t.get(t, []):
            su, au, sd, ad = apply_buy_fill(su, au, sd, ad, side=tr.side, add_shares=tr.shares, fill_price=tr.price)
        out.append((su, sd, au, ad))
    return out


def each_trade_post_state(
    trades: list[Trade],
) -> Iterator[
    tuple[Trade, float, float, float, float, float, float]
]:
    """Yield each trade with book right after the fill: sizes, avgs, roi_if_up, roi_if_down."""
    su = au = sd = ad = 0.0
    for tr in trades:
        su, au, sd, ad = apply_buy_fill(su, au, sd, ad, side=tr.side, add_shares=tr.shares, fill_price=tr.price)
        yield (
            tr,
            su,
            sd,
            au,
            ad,
            roi_if_up(su, au, sd, ad),
            roi_if_down(su, au, sd, ad),
        )


def iter_bucket_trace_rows(
    prices: list[tuple[float, float]],
    sim_states: list[tuple[float, float, float, float]],
) -> list[dict[str, float | str]]:
    """30 rows: state at end of each 30s bucket (seconds 29, 59, … 899)."""
    rows: list[dict[str, float | str]] = []
    for b in range(30):
        start = b * 30
        end_sec = start + 29
        label = f"{start:03d}-{start + 29:03d}"
        pm_u, pm_d = prices[min(end_sec, len(prices) - 1)]
        su, sd, au, ad = sim_states[min(end_sec, len(sim_states) - 1)]
        ru = roi_if_up(su, au, sd, ad)
        rd = roi_if_down(su, au, sd, ad)
        pnu = pnl_if_up_usdc(su, au, sd, ad)
        pnd = pnl_if_down_usdc(su, au, sd, ad)
        rows.append(
            {
                "bucket": label,
                "pm_up": pm_u,
                "pm_down": pm_d,
                "size_up": su,
                "size_down": sd,
                "avg_up": au,
                "avg_down": ad,
                "roi_if_up": ru,
                "roi_if_down": rd,
                "pnl_if_up": pnu,
                "pnl_if_down": pnd,
            }
        )
    return rows


def print_sim_bucket_table(
    prices: list[tuple[float, float]],
    sim_states: list[tuple[float, float, float, float]],
) -> None:
    """
    Print end-of-30s-bucket snapshot: prices at bucket end second + inventory + ROIs.
    Matches the user's bucket trace layout (000-029 .. 870-899).
    """
    hdr = (
        "bucket   | pm_up  | pm_down | size_up   | size_down | avg_up | avg_down | "
        "roi_if_up | roi_if_down | pnl_if_up | pnl_if_down"
    )
    _p()
    _p(hdr)
    for row in iter_bucket_trace_rows(prices, sim_states):
        _p(
            f"{row['bucket']!s:7} | {float(row['pm_up']):6.4f} | {float(row['pm_down']):6.4f}  | "
            f"{float(row['size_up']):9.4f} | {float(row['size_down']):9.4f} | "
            f"{float(row['avg_up']):6.4f} | {float(row['avg_down']):8.4f}   | "
            f"{float(row['roi_if_up']):9.4f} | {float(row['roi_if_down']):10.4f} | "
            f"{float(row['pnl_if_up']):9.2f} | {float(row['pnl_if_down']):10.2f}"
        )


def print_bucket_compare(
    sim_states: list[tuple[float, float, float, float]],
    wallet_rows: list[dict[str, str]],
    *,
    max_rows: int = 30,
) -> None:
    """Print PALADIN vs wallet cum sizes at end of each 30s bucket (subset of rows)."""
    _p()
    _p("--- 30s bucket snapshot (end of bucket second): PALADIN sim vs wallet export ---")
    hdr = (
        f"{'bucket':<9} | {'sim_up':>8} {'sim_dn':>8} | "
        f"{'wal_up':>8} {'wal_dn':>8} | {'note'}"
    )
    _p(hdr)
    _p("-" * len(hdr))
    n = len(wallet_rows)
    show_idx: set[int] = set()
    if n <= max_rows:
        show_idx = set(range(n))
    else:
        for i in range(max_rows):
            show_idx.add(i)
        for i in range(max(0, n - 5), n):
            show_idx.add(i)
    for i in sorted(show_idx):
        wr = wallet_rows[i]
        label = wr.get("bucket_label", "")
        end_sec = int(float(wr.get("bucket_end_sec", "29")))
        su, sd, au, ad = sim_states[min(end_sec, 899)]
        wu = float(wr.get("cum_up_size", 0))
        wd = float(wr.get("cum_down_size", 0))
        _p(
            f"{label:<9} | {su:8.2f} {sd:8.2f} | {wu:8.2f} {wd:8.2f} | end_sec={end_sec}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="PALADIN window trade simulation")
    ap.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_PROFIT_LOCK_CONFIG,
        help="JSON with roi_lock_min_each, profit_lock_usdc_each_scenario, profit_lock_min_shares_per_side (CLI overrides those).",
    )
    ap.add_argument("--prices", type=Path, default=DEFAULT_PRICES)
    ap.add_argument("--budget", type=float, default=100.0, help="USDC cap per window.")
    ap.add_argument(
        "--pair-sum-max",
        type=float,
        default=0.982,
        help="Max pm_up+pm_down for a pair add. Use ~0.99 with --pair-only for more fills.",
    )
    ap.add_argument("--single-max", type=float, default=0.54)
    ap.add_argument(
        "--pair-only",
        action="store_true",
        help="Pair adds only (no standalone single-leg inventory). Use with --stagger-pair for live-style one-leg open.",
    )
    ap.add_argument(
        "--stagger-pair",
        action="store_true",
        help="With --pair-only: first leg on cheaper side under --single-max; second leg after cooldown when sum+ROI gates pass.",
    )
    ap.add_argument(
        "--stagger-hedge-force-sec",
        type=float,
        default=-1.0,
        help="Sim seconds after hedge-ready to force 2nd leg (skip ROI+sum). -1=auto 45s if --stagger-pair else off; 0=off.",
    )
    ap.add_argument(
        "--max-shares-per-side",
        type=float,
        default=0.0,
        help="Hard cap on size_up and size_down (0=no cap).",
    )
    ap.add_argument(
        "--target-min-roi",
        type=float,
        default=0.03,
        help="Minimum min(roi) after a marginal pair in pair-only (default 3%%).",
    )
    ap.add_argument(
        "--roi-lock-each",
        type=float,
        default=None,
        help="ROI on each branch to stop trading (overrides config roi_lock_min_each).",
    )
    ap.add_argument(
        "--cooldown-seconds",
        type=float,
        default=2.0,
        help="Seconds after each fill before the next fill; pair legs count as two fills (DOWN is at earliest t >= UP + this). Default 2.",
    )
    ap.add_argument(
        "--profit-lock-min-shares",
        type=float,
        default=None,
        help="Min shares on each leg before ROI profit lock can trip (overrides config).",
    )
    ap.add_argument(
        "--no-usd-profit-lock",
        action="store_true",
        help="Disable the USD P&L profit lock ($5/side by default); only ROI lock (--roi-lock-each) can stop trading.",
    )
    ap.add_argument(
        "--profit-lock-usdc-each",
        type=float,
        default=None,
        help="Override USD profit lock threshold (per scenario; overrides config). Ignored if --no-usd-profit-lock.",
    )
    ap.add_argument("--json-out", type=Path, default=None)
    ap.add_argument("--compare-wallet", type=Path, default=DEFAULT_WALLET_30S)
    ap.add_argument(
        "--compare-example-trace",
        type=Path,
        default=None,
        help=f"Optional: path to illustrative bucket CSV (default {DEFAULT_EXAMPLE_TRACE}) for final-row compare.",
    )
    ap.add_argument(
        "--dynamic-clip-max",
        type=float,
        default=None,
        help="If set (e.g. 15), opening stays 5 sh/side until min leg>=20; then pair sizes are 5..this (integers). Inventory clip cap matches.",
    )
    ap.add_argument(
        "--pair-size-pick",
        choices=["ascending", "max_feasible"],
        default="max_feasible",
        help="How to pick size among ROI-feasible clips: smallest first, or largest first (default).",
    )
    ap.add_argument(
        "--pair-sum-tighten-per-fill",
        type=float,
        default=0.0,
        help="Lower pair sum cap by this amount per completed fill (0=off). Waits for cheaper mids on later clips.",
    )
    ap.add_argument(
        "--pair-sum-min-floor",
        type=float,
        default=0.88,
        help="Minimum pair sum cap when using --pair-sum-tighten-per-fill.",
    )
    ap.add_argument(
        "--force-hedge-respect-effective-sum",
        action="store_true",
        help="On stagger force-hedge timer, still require pm_up+pm_down <= effective pair cap (and book beat if set).",
    )
    ap.add_argument(
        "--second-leg-book-improve-eps",
        type=float,
        default=0.0,
        help="When both legs already have size, second leg requires pm_up+pm_down <= avg_up+avg_down - eps (0=off).",
    )
    ap.add_argument(
        "--target-roi-per-fill",
        type=float,
        default=0.0,
        help="Add this to target_min_roi for each completed fill (stricter marginal ROI as book grows).",
    )
    ap.add_argument(
        "--pending-hedge-bypass-imbalance-shares",
        type=float,
        default=0.0,
        help="When |up-down| >= this, pending 2nd leg uses base pair_sum_max (drops tighten/book beat for that leg).",
    )
    ap.add_argument(
        "--discipline-relax-after-forced-sec",
        type=float,
        default=0.0,
        help="After force-hedge time + this many seconds, relax discipline on the pending leg (0=off).",
    )
    ap.add_argument(
        "--max-blended-pair-avg-sum",
        type=float,
        default=0.0,
        help="If >0: after any one-leg buy, if both legs exist, require avg_up+avg_down <= this (0=off).",
    )
    ap.add_argument(
        "--min-elapsed-for-flat-open",
        type=int,
        default=-1,
        help="If >=0: when flat, do not open before this elapsed second (live entry delay). -1=off.",
    )
    ap.add_argument(
        "--max-elapsed-to-start-flat",
        type=int,
        default=-1,
        help="If >=0: when still flat, do not start after this elapsed second. -1=off.",
    )
    ap.add_argument(
        "--stagger-winning-side-first-when-position",
        action="store_true",
        help="With --stagger-pair: if we already have UP or DOWN size, new stagger first leg only on the higher-mid side; when balanced and that blocks, try symmetric pair add (see --no-stagger-symmetric-fallback).",
    )
    ap.add_argument(
        "--no-stagger-symmetric-fallback",
        action="store_true",
        help="With --stagger-winning-side-first-when-position: do not use balanced symmetric pair fallback when win-side stagger has no first leg.",
    )
    ap.add_argument(
        "--stagger-symmetric-fallback-roi-discount",
        type=float,
        default=0.03,
        help="Lower marginal min-ROI floor for balanced symmetric fallback by this amount (default 0.03 vs --target-min-roi).",
    )
    ap.add_argument(
        "--stagger-symmetric-fallback-respect-blend-cap",
        action="store_true",
        help="Apply max-blended-pair-avg-sum to the first UP leg of balanced symmetric fallback (default: skip for that leg).",
    )
    ap.add_argument(
        "--stagger-alternate-first-when-balanced",
        action="store_true",
        help="With --stagger-pair: when balanced, alternate stagger first leg UP/DN/UP/DN (causal ladder); uses symmetric fallback with the same first side when the leg is over --single-max.",
    )
    ap.add_argument(
        "--min-elapsed-between-pair-starts",
        type=float,
        default=-1.0,
        help="When balanced, wait at least this many seconds after starting a pair before starting the next (causal pacing). -1=off.",
    )
    ap.add_argument(
        "--entry-trailing-min-low-seconds",
        type=int,
        default=-1,
        help="Require stagger first-leg price <= trailing min over this many past seconds (+ slippage). -1=off.",
    )
    ap.add_argument(
        "--entry-trailing-low-slippage",
        type=float,
        default=0.02,
        help="Slippage added to trailing min for --entry-trailing-min-low-seconds (default 0.02).",
    )
    args = ap.parse_args()

    pl_cfg = load_profit_lock_config(args.config)
    roi_lock = float(args.roi_lock_each) if args.roi_lock_each is not None else pl_cfg["roi_lock_min_each"]
    min_sh_lock = (
        float(args.profit_lock_min_shares)
        if args.profit_lock_min_shares is not None
        else pl_cfg["profit_lock_min_shares_per_side"]
    )
    if args.no_usd_profit_lock:
        usd_lock_thr = float("inf")
    elif args.profit_lock_usdc_each is not None:
        usd_lock_thr = float(args.profit_lock_usdc_each)
    else:
        usd_lock_thr = float(pl_cfg["profit_lock_usdc_each_scenario"])

    raw = load_prices_by_elapsed(args.prices)
    series = forward_fill_prices(raw)
    params = PaladinParams(
        profit_lock_min_shares_per_side=min_sh_lock,
        roi_lock_min_each=roi_lock,
        profit_lock_usdc_each_scenario=usd_lock_thr,
    )
    if args.stagger_hedge_force_sec < 0:
        shfs: float | None = 45.0 if args.stagger_pair else None
    elif args.stagger_hedge_force_sec == 0:
        shfs = None
    else:
        shfs = float(args.stagger_hedge_force_sec)

    mx_sh = None if args.max_shares_per_side <= 0 else float(args.max_shares_per_side)

    st = run_window(
        series,
        budget_usdc=args.budget,
        params=params,
        pair_sum_max=args.pair_sum_max,
        single_leg_max_px=args.single_max,
        pair_only=args.pair_only,
        stagger_pair_entry=args.stagger_pair,
        stagger_hedge_force_after_seconds=shfs,
        target_min_roi=args.target_min_roi,
        cooldown_seconds=args.cooldown_seconds,
        dynamic_clip_cap=args.dynamic_clip_max,
        pair_size_pick=args.pair_size_pick,  # type: ignore[arg-type]
        max_shares_per_side=mx_sh,
        pair_sum_tighten_per_fill=float(args.pair_sum_tighten_per_fill),
        pair_sum_min_floor=float(args.pair_sum_min_floor),
        force_hedge_respects_effective_sum=bool(args.force_hedge_respect_effective_sum),
        second_leg_book_improve_eps=float(args.second_leg_book_improve_eps),
        target_roi_per_fill=float(args.target_roi_per_fill),
        pending_hedge_bypass_imbalance_shares=(
            None
            if float(args.pending_hedge_bypass_imbalance_shares) <= 0
            else float(args.pending_hedge_bypass_imbalance_shares)
        ),
        discipline_relax_after_forced_sec=(
            None
            if float(args.discipline_relax_after_forced_sec) <= 0
            else float(args.discipline_relax_after_forced_sec)
        ),
        max_blended_pair_avg_sum=(
            None
            if float(args.max_blended_pair_avg_sum) <= 0
            else float(args.max_blended_pair_avg_sum)
        ),
        min_elapsed_for_flat_open=(
            None if int(args.min_elapsed_for_flat_open) < 0 else int(args.min_elapsed_for_flat_open)
        ),
        max_elapsed_to_start_flat=(
            None if int(args.max_elapsed_to_start_flat) < 0 else int(args.max_elapsed_to_start_flat)
        ),
        stagger_winning_side_first_when_position=bool(args.stagger_winning_side_first_when_position),
        stagger_symmetric_fallback_when_balanced=not bool(args.no_stagger_symmetric_fallback),
        stagger_symmetric_fallback_roi_discount=float(args.stagger_symmetric_fallback_roi_discount),
        stagger_symmetric_fallback_skip_first_leg_blend_cap=not bool(
            args.stagger_symmetric_fallback_respect_blend_cap
        ),
        stagger_alternate_first_leg_when_balanced=bool(args.stagger_alternate_first_when_balanced),
        min_elapsed_between_pair_starts=(
            None
            if float(args.min_elapsed_between_pair_starts) < 0
            else float(args.min_elapsed_between_pair_starts)
        ),
        entry_trailing_min_low_seconds=(
            None
            if int(args.entry_trailing_min_low_seconds) < 0
            else int(args.entry_trailing_min_low_seconds)
        ),
        entry_trailing_low_slippage=float(args.entry_trailing_low_slippage),
    )

    slug = window_slug_from_prices_csv(args.prices)
    out: dict[str, Any] = {
        "slug": slug,
        "prices_csv": str(args.prices),
        "sim_config_path": str(args.config),
        "profit_lock_min_shares_per_side": min_sh_lock,
        "roi_lock_min_each": roi_lock,
        "profit_lock_usdc_each_scenario": (
            None if not math.isfinite(usd_lock_thr) else usd_lock_thr
        ),
        "usd_profit_lock_disabled": args.no_usd_profit_lock,
        "pair_only": args.pair_only,
        "stagger_pair_entry": args.stagger_pair,
        "target_min_roi": args.target_min_roi,
        "cooldown_seconds": args.cooldown_seconds,
        "dynamic_clip_max": args.dynamic_clip_max,
        "pair_size_pick": args.pair_size_pick,
        "budget_usdc": args.budget,
        "spent_usdc": st.spent_usdc,
        "locked": st.locked,
        "lock_reason": st.lock_reason,
        "final": st.snapshot_metrics(),
        "trade_count": len(st.trades),
        "trades": [
            {
                "elapsed_sec": tr.elapsed_sec,
                "side": tr.side,
                "shares": tr.shares,
                "price": tr.price,
                "notional": tr.notional,
                "reason": tr.reason,
                "post_size_up": su,
                "post_size_down": sd,
                "post_avg_up": au,
                "post_avg_down": ad,
                "post_roi_if_up": ru,
                "post_roi_if_down": rd,
            }
            for tr, su, sd, au, ad, ru, rd in each_trade_post_state(st.trades)
        ],
    }

    _p(f"PALADIN sim | {slug}")
    _p(f"Prices: {args.prices}")
    _p(f"Profit-lock config: {args.config}")
    if args.no_usd_profit_lock:
        _p(
            f"Stops when: ROI >= {roi_lock:.0%} on each leg (each leg >= {min_sh_lock:.0f} sh). USD lock OFF."
        )
    else:
        usd_s = f"${usd_lock_thr:.2f}" if math.isfinite(usd_lock_thr) else "off"
        _p(
            f"Stops when: (1) ROI >= {roi_lock:.0%} each leg with >= {min_sh_lock:.0f} sh/side, "
            f"OR (2) PnL if UP >= {usd_s} AND PnL if DOWN >= {usd_s} (whichever first)."
        )
    _p(f"Budget: ${args.budget:.2f} | Spent: ${st.spent_usdc:.2f} | Trades: {len(st.trades)}")
    _p(f"Locked early: {st.locked} {st.lock_reason}")
    fm = st.snapshot_metrics()
    _p(
        f"Final inventory: up={fm['size_up']:.4f}@{fm['avg_up']:.4f} "
        f"down={fm['size_down']:.4f}@{fm['avg_down']:.4f} | "
        f"roi_if_up={fm['roi_up']:.4f} roi_if_dn={fm['roi_dn']:.4f} | "
        f"pnl_if_up=${fm['pnl_if_up_usdc']:.2f} pnl_if_dn=${fm['pnl_if_down_usdc']:.2f}"
    )
    win_side, _, _ = resolve_winner_from_last_prices(series)
    settle_usdc = settled_pnl_usdc(fm, win_side)
    roi_on_spent = settle_usdc / st.spent_usdc if st.spent_usdc > 1e-9 else 0.0
    _p(
        f"Settlement proxy (higher final mid={win_side}): pnl=${settle_usdc:.2f} | "
        f"roi_on_spent={roi_on_spent:.2%} (target check vs spend)"
    )
    _p()
    _p(f"--- Trade list (window {slug}; header once, then values only) ---")
    _p(
        "t_sec\tside\tshares\tprice\tnotional_usd\treason\t"
        "post_sz_up\tpost_sz_dn\tpost_avg_up\tpost_avg_dn\tpost_roi_if_up\tpost_roi_if_dn"
    )
    for tr, su, sd, au, ad, ru, rd in each_trade_post_state(st.trades):
        _p(
            f"{tr.elapsed_sec}\t{tr.side}\t{tr.shares:.1f}\t{tr.price:.4f}\t{tr.notional:.2f}\t{tr.reason}\t"
            f"{su:.2f}\t{sd:.2f}\t{au:.4f}\t{ad:.4f}\t{ru:.4f}\t{rd:.4f}"
        )

    sim_states = replay_inventory_from_trades(st.trades)
    bucket_trace = iter_bucket_trace_rows(series, sim_states)
    out["bucket_trace"] = bucket_trace

    print_sim_bucket_table(series, sim_states)

    if args.compare_wallet.is_file():
        _p()
        _p("--- vs exported wallet 30s_state (same window, target wallet) ---")
        _p("Reference: cum_up_size / cum_down_size at bucket end (not PALADIN).")
        wrows = load_wallet_30s(args.compare_wallet)
        last = wrows[-1] if wrows else {}
        _p(
            f"Wallet final row {last.get('bucket_label','')}: "
            f"up={float(last.get('cum_up_size',0)):.2f} down={float(last.get('cum_down_size',0)):.2f} "
            f"avg_up={float(last.get('avg_up_buy_price',0)):.4f} avg_dn={float(last.get('avg_down_buy_price',0)):.4f}"
        )
        _p(
            f"PALADIN sim final: up={st.size_up:.2f} down={st.size_down:.2f} "
            f"avg_up={st.avg_up:.4f} avg_dn={st.avg_down:.4f}"
        )
        print_bucket_compare(sim_states, wrows)

    ex_path = args.compare_example_trace
    if ex_path is None:
        ex_path = DEFAULT_EXAMPLE_TRACE
    if ex_path.is_file():
        _p()
        _p(
            f"--- vs PALADIN/data/example_bucket_trace.csv (illustrative; not chain data for {slug}) ---"
        )
        ex_rows = load_bucket_csv(ex_path)
        if ex_rows:
            last_ex = ex_rows[-1]
            m_ex = analyze_snapshot(last_ex)
            _p(
                f"Example final bucket {last_ex.bucket_label}: "
                f"up={m_ex.size_up:.2f} down={m_ex.size_down:.2f} "
                f"roi_up={m_ex.roi_if_up:+.4f} roi_dn={m_ex.roi_if_down:+.4f}"
            )
            _p(
                f"PALADIN sim final (this window): up={st.size_up:.2f} down={st.size_down:.2f} "
                f"roi_up={fm['roi_up']:+.4f} roi_dn={fm['roi_dn']:+.4f}"
            )

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(out, indent=2), encoding="utf-8")
        _p()
        _p(f"Wrote {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
