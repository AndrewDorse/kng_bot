#!/usr/bin/env python3
"""
Pattern-based signal analyzer -- LIVE ORDER PLACEMENT.

Runs as a background thread alongside the main bot engine.
Monitors live UP/DOWN prices, detects backtested patterns,
places 5-share buy orders on signals, and sets TP sell at 0.99
after each fill to free cash.

Set BOT_STRATEGY_MODE=signal_only to let this module handle all orders
while the engine still polls prices, heartbeats, and detects fills.

21 active patterns.
Per-window buy cap: floor(wallet_USDC / 15) signal buys per side (snapshot at window start).
Pruned live set to 90%+ WR signals on the latest full public replay.

All prob/EV values below are ACTUAL TESTED (not claimed).
EV = average net profit per fire (5 shares).
Win$ = avg profit on correct pick.  Loss$ = avg loss on wrong pick.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

SIGLOG = logging.getLogger("polymarket_btc_ladder")

CLIP = 5
TP_PRICE = 0.99
SIGNAL_BUY_PRICE_PAD = 0.03
MAX_SIGNAL_TRIGGER_PRICE = 0.90
# Per window: max signal buys on UP = floor(balance / this), same for DOWN (set at window start).
SIGNAL_USDC_PER_DEAL_PER_SIDE = 15.0
BTC_LAYER_PATTERN_COUNT = 10
LEFTOVER_CLEANUP_PRICE = 0.98
LEFTOVER_CLEANUP_START_ELAPSED = 600.0
LEFTOVER_CLEANUP_INTERVAL_SECONDS = 5.0
SIGNAL_WINDOW_CLOSE_PRICE = 0.98
EARLY_WINDOW_SKIP_CHECK_ELAPSED = 120.0
EARLY_WINDOW_SKIP_PM_RANGE = 0.60
BTC_VOLUME_OK_LOOKBACK_POINTS = 30
HEDGE_PAIR_SUM_MAX = 0.90
HEDGE_PAIR_SUM_MAX_RICH_ENTRY = 0.95
HEDGE_MIN_LIMIT_PRICE = 0.01
HEDGE_REBALANCE_INTERVAL_SECONDS = 5.0

LIVE_READY_SIGNAL_NAMES: set[str] = {
    "btcagree_t525_lb180_m0.001",
    "btcagree_t525_lb180_m0.001_l0.05",
    "btcsqz_t525_lb90_r0.0006_l0.4",
    "btcrev_t510_lb180_r0.002",
    "lbounce_t585_r30_f30_rm003_fm006",
    "ratio_t720_ge4",
    "rn_grindtrend_t495_b15_n2_dr0.05_lf-0.01_bc0.0016_ra1.4",
    "rn_ratioexpand_t480_lb60_r1.25_rg0.1_bc0.0016_dc0.84",
    "rddrecov_t360_dd0.15_r0.75",
    "rddrecov_t360_dd0.2_r0.75",
    "spread_squeeze_t720_drop20",
    "spread_t720_ge06",
}

W1_SIGNAL_NAMES: set[str] = set(LIVE_READY_SIGNAL_NAMES)

WALLET_PRINCIPLES_SIGNAL_NAMES: set[str] = {
    "rn_grindtrend_t495_b15_n2_dr0.05_lf-0.01_bc0.0016_ra1.4",
    "rn_ratioexpand_t480_lb60_r1.25_rg0.1_bc0.0016_dc0.84",
}

SIGNAL_PRESETS: dict[str, set[str]] = {
    "w1": W1_SIGNAL_NAMES,
    "live_ready": LIVE_READY_SIGNAL_NAMES,
    "wallet_principles": WALLET_PRINCIPLES_SIGNAL_NAMES,
}

ACTIVE_SIGNAL_NAMES: set[str] = set(W1_SIGNAL_NAMES)

CANDIDATE_SIGNAL_NAMES: set[str] = {
    "btcsqz_t645_lb90_r0.0006_l0.4",
    "nearpeak_t645_g001",
}

# Best blended set from BTC overlay search.
# If BTC data is available, these classic signals require the listed BTC confirmation
# filters before firing. If BTC is unavailable, the classic signal still fires.
CLASSIC_BTC_CONFIRMATION_FILTERS: dict[str, tuple[str, ...]] = {
    "vshape_t330_lb120_b0.08_c0.85": ("moveabs_le_30_0.0002317888",),
    "vshape_t600_lb240_b0.12": ("range_le_120_0.0016",),
    "rdiv_t600_w180_r0.08_f0.01": ("range_le_120_0.0016", "range_le_30_0.0004"),
    "vshape_t600_lb240_b0.08": ("range_le_120_0.0016",),
    "diverge_t345_w60_r005": ("range_le_30_0.0004",),
    "reversal_300_to_600": ("range_le_120_0.0012",),
    "dom_t720_lead30": ("range_le_45_0.0012", "range_le_60_0.0012"),
    "spread_t720_ge06": ("range_le_45_0.0012",),
    "spread_squeeze_t720_drop20": ("range_le_45_0.0012",),
    "crossover_t585_k45": ("moveabs_ge_90_0.0002", "range_le_60_0.0008"),
    "crossover_t585_k60": ("moveabs_ge_90_0.0002", "range_le_60_0.0008"),
    "crossover_t600_k60": ("range_le_90_0.0016",),
    "ratio_t720_ge4": ("range_le_45_0.0012",),
    "rddrecov_t360_dd0.2_r0.75": ("range_le_30_0.001",),
    "rddrecov_t360_dd0.15_r0.75": ("base_ratio_ge_5_30_0.2352320764",),
    "accum_t615_b20_n3": ("range_le_75_0.0016", "rebound_ge_120_0.0004"),
    "low_vol_t720_flip2": ("range_le_45_0.0008",),
    "loserfloor_t495": ("range_le_15_0.0012", "range_le_30_0.0012"),
    "lbounce_t240_r60_f15_rm005_fm006": ("range_le_30_0.0008", "rebound_ge_180_0.0004"),
    "crossover_t600_k30": ("range_le_60_0.0008",),
    "flipband_t720_0to1": ("range_le_45_0.0008",),
    "velocity_t720_w60": ("range_le_90_0.0025",),
    "low_vol_t600_flip2": ("range_le_45_0.0005",),
    "vel_t693_w60_v004": ("range_le_60_0.0016",),
    "vel_t645_w90_v003": ("range_le_60_0.0012", "rebound_ge_120_0.0004"),
    "mix_loserdrop_t690_w30_v0.002_br60_0.0008": ("rebound_ge_120_0.0004",),
    "loserdrop_t585_w45_v002": ("range_le_60_0.0016", "range_le_60_0.0012", "range_le_45_0.0008"),
    "lbounce_t585_r30_f30_rm003_fm006": ("moveabs_ge_90_0.0002",),
    "vel_t315_w30_v004": ("range_le_45_0.0016",),
    "vshape_t585_lb240_b0.15_c0.95": ("rebound_ge_120_0.0004", "rebound_ge_60_0.0002"),
}

# Pattern-specific risk blockers derived from late-reversal stress tests.
# If a blocker condition is met, the signal is skipped. BTC-based blockers are
# ignored when BTC data is unavailable so classic fallback behavior still works.
PATTERN_ENTRY_RISK_BLOCKERS: dict[str, tuple[str, ...]] = {
    "dom_t720_lead30": ("elapsed_ge_720", "btc_move_abs_lt_90_0.0002"),
    "btcsqz_t720_lb45_r0.0012_l0.3": ("elapsed_ge_720", "btc_move_abs_lt_90_0.0002", "btcrange60_ge_0.0012", "baseratio1_5_gt_3.0", "loser_lt_0.03"),
    "btcsqz_t720_lb75_r0.0016_l0.2": ("elapsed_ge_720", "btc_move_abs_lt_90_0.0002", "btcrange45_ge_0.0008", "btcrange30_ge_0.0006", "baseratio1_5_gt_3.0"),
    "btcsqz_t645_lb90_r0.0006_l0.18": (
        "baseratio15_60_gt_3.0",
        "trades1_ge_2.0",
        "btcmove90_le_-0.0001136109",
    ),
    "btcsqz_t645_lb90_r0.0006_l0.4": ("baseratio15_60_gt_3.0", "quote1_lt_6.2000892004", "losermove60_lt_-0.4782608696"),
    "btcsqz_t525_lb90_r0.0006_l0.4": ("baseratio15_60_gt_2.5", "flips_gt_8.0", "tradesratio15_60_gt_1.4110953058", "btcprice_lt_68810.57"),
    "vel_t315_w30_v004": ("flips_ge_5",),
    "vshape_t600_lb240_b0.12": ("elapsed_ge_600",),
    "vshape_t600_lb240_b0.08": ("ratio_ge_5.0", "elapsed_ge_600"),
    "vshape_t600_lb240_b0.12_btcm240dn0002": ("btc_range_ge_90_0.0008", "sidemove30_lt_-0.0571428571"),
    "vshape_t585_lb240_b0.15_c0.95": ("btc_range_ge_90_0.0008", "losermove30_gt_-0.1176470588", "sidemove30_lt_0.0819672131"),
    "mix_vshape_t585_lb240_b0.12_br120_0.0016": ("btc_range_ge_90_0.0008",),
    "mix_loserdrop_t750_w20_v0.0015_br60_0.0005": ("elapsed_ge_750", "base1_lt_0.00026", "price_gt_0.85"),
    "spread_squeeze_t720_drop20": ("btc_range_ge_45_0.0008", "btc_range_ge_60_0.001", "btcmoveabs90_lt_0.0002", "btcmoveabs180_gt_0.0004", "tradesratio15_60_gt_3.0", "btcmoveabs30_gt_0.0004399364", "losermove30_gt_0.9"),
    "twapgap_t585_lb300_g005": ("btc_range_ge_120_0.0016", "quote5_lt_13236.7510394007"),
    "btcrev_t585_lb180_r0.0005": ("btc_range_ge_120_0.0016", "trades15_lt_498"),
    "btcsqz_t690_lb30_r0.0006_l0.12": ("price_ge_0.75",),
    "crossover_t600_k60": ("elapsed_ge_600", "losermove60_lt_-0.328358209"),
    "crossover_t600_k30": ("elapsed_ge_599",),
    "rddrecov_t360_dd0.15_r0.75": ("elapsed_ge_360", "domprice_gt_0.71", "btcmoveabs30_gt_0.0008", "sidemove60_gt_0.5"),
    "rddrecov_t360_dd0.2_r0.75": ("elapsed_ge_360", "baseratio15_60_gt_2.5", "btcmove30_lt_-0.0005758616"),
    "ddrecov_t615_dd01_r075": ("price_ge_0.75",),
    "nearpeak_t645_g001": ("btc_range_ge_60_0.0006", "btcrange120_gt_0.0006", "price_gt_0.85"),
    "loserdrop_t840_w60_v0.0015": ("price_ge_0.8", "base1_lt_0.00109"),
    "ratio_t720_ge4": ("elapsed_ge_720", "loser_lt_0.05", "btcrange60_gt_0.0012", "btcmoveabs60_lt_1.52508e-05", "losermove30_lt_-0.5"),
    "spread_t720_ge06": ("elapsed_ge_720", "loser_lt_0.05", "btcrange60_gt_0.0012", "btcmoveabs60_lt_1.52508e-05", "losermove30_lt_-0.5"),
    "low_vol_t600_flip2": ("btc_range_ge_60_0.0008", "trades1_gt_3"),
    "low_vol_t720_flip2": ("btc_range_ge_30_0.0004", "baseratio5_15_gt_1.2"),
    "btcbreak_t600_sq30_mv45_r0.0006_m0.0004": ("elapsed_ge_600",),
    "vel_t693_w60_v004": ("loser_lt_0.12", "base15_gt_1.03821"),
    "diverge_t345_w60_r005": ("ratio_ge_5.0",),
    "retrace_t585_r085": ("loser_lt_0.1",),
    "reversal_300_to_600": ("loserdrop30_lt_0.02",),
    "loserfloor_t495": ("btcrebound120_lt_0.0008100599",),
    "lbounce_t240_r60_f15_rm005_fm006": ("btcrange60_gt_0.0009086287",),
    "lbounce_t585_r30_f30_rm003_fm006": ("btcmoveabs60_gt_0.0005183479", "losermove60_lt_-0.2592592593"),
    "loserdrop_t585_w45_v002": ("flips_lt_6", "base5_lt_0.24631"),
    "btcagree_t525_lb180_m0.001": ("losermove30_lt_0.04", "btcprice_gt_72930.27"),
    "btcagree_t525_lb180_m0.001_l0.05": ("losermove30_lt_0.04", "btcprice_gt_72930.27"),
    "btcagree_t795_lb120_m0.0005": ("tradesratio5_15_gt_2.0", "domprice_ge_0.66", "btcmove120_gt_-0.0006204365"),
    "btcrev_t510_lb180_r0.002": ("btcmoveabs30_gt_0.0008", "sidemove60_gt_0.8235294118"),
    "vel_t645_w90_v003": ("base5_lt_0.07386",),
    "accum_t615_b20_n3": ("btcmoveabs120_gt_0.0012", "btcmoveabs90_gt_0.0012", "price_gt_0.85", "elapsed_ge_613.0"),
    "nf_quietlead_t630_lb60_r0.0006_l0.22_d0.62": ("elapsed_ge_629.0", "losermove60_ge_-0.3684210526"),
    "nf_breakquiet_t630_pre45_post60_pr0.02_mv0.06_r0.0006": ("tradesratio5_30_lt_0.1061946903", "losermove60_lt_-0.5333333333", "base60_gt_9.4374", "tradesratio5_30_gt_2.1823834197"),
    "rn_ratioexpand_t480_lb60_r1.25_rg0.1_bc0.0016_dc0.84": ("btcmove30_lt_-0.0002250055", "btcmove90_lt_-0.0003842727"),
    "rn_grindtrend_t495_b15_n2_dr0.05_lf-0.01_bc0.0016_ra1.4": ("btcmove120_lt_-0.0006421542", "base15_lt_0.29407"),
    "mix_loserdrop_t690_w30_v0.002_br60_0.0008": ("rebound_ge_120_0.0004", "tradesratio1_5_gt_3.0"),
    "flipband_t720_0to1": ("btcmoveabs180_gt_0.0004", "sidemove30_gt_0.1904761905"),
}


@dataclass
class _PriceSnap:
    ts: float
    elapsed: float
    up: float
    down: float


class SignalAnalyzer:
    """Observes engine state, fires live buy orders + TP sells on pattern signals."""

    def __init__(self, signal_preset: str | None = None) -> None:
        self._engine = None
        self._trader = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._signal_preset = (signal_preset or os.getenv("BOT_SIGNAL_PRESET", "w1")).strip().lower()
        self._active_signal_names = self._resolve_signal_preset(self._signal_preset)

        self._window_slug: str | None = None
        self._window_start_ts: float = 0.0
        self._history: list[_PriceSnap] = []
        self._dom_flips: int = 0
        self._flip_times: list[float] = []
        self._last_dom: str | None = None
        self._signals_fired: set[str] = set()
        self._loser_at_60: float | None = None
        self._dom_at_60: str | None = None
        self._last_order_ts: float = 0.0
        self._pending_tp: list[dict[str, float | int | str]] = []
        self._pending_hedges: list[dict[str, float | int | str]] = []
        self._active_hedge_orders: list[dict[str, float | int | str | bool]] = []
        self._desired_hedge_shares: dict[str, int] = {"Up": 0, "Down": 0}
        self._hedge_limit_by_side: dict[str, float] = {"Up": HEDGE_MIN_LIMIT_PRICE, "Down": HEDGE_MIN_LIMIT_PRICE}
        self._hedge_tp_covered_shares: dict[str, int] = {"Up": 0, "Down": 0}
        self._buys_placed_up: int = 0
        self._buys_placed_down: int = 0
        self._max_buys_per_side: int = 0
        self._balance_snapshot_usdc: float = 0.0
        # If set (e.g. by tests/replay), used instead of wallet read for window caps.
        self._window_balance_override: float | None = None
        self._last_leftover_cleanup_ts: float = 0.0
        self._last_hedge_rebalance_ts: float = 0.0
        self._signal_window_closed: bool = False
        self._early_pm_min: float | None = None
        self._early_pm_max: float | None = None
        self._early_window_blocked: bool = False
        self._btc_missing_logged: bool = False
        self._btc_volume_missing_logged: bool = False

    def _resolve_signal_preset(self, preset_name: str) -> set[str]:
        return set(SIGNAL_PRESETS.get(preset_name, LIVE_READY_SIGNAL_NAMES))

    def attach(self, engine) -> None:
        self._engine = engine
        self._trader = engine.trader
        self._live = engine.config.strategy_mode == "signal_only"
        self._thread = threading.Thread(target=self._run, daemon=True, name="signal_analyzer")
        self._thread.start()
        mode_label = "LIVE -- orders enabled" if self._live else "log-only"
        SIGLOG.info(
            "[SIGNAL] analyzer thread started (%s) | preset=%s | %d patterns active",
            mode_label,
            self._signal_preset,
            len(self._active_signal_names),
        )
        SIGLOG.info("[SIGNAL] active signals: %s", ", ".join(sorted(self._active_signal_names)))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                SIGLOG.exception("[SIGNAL] tick error")
            time.sleep(1.0)

    def _tick(self) -> None:
        eng = self._engine
        if eng is None:
            return
        slug = eng._current_window_slug
        if slug is None:
            return

        if slug != self._window_slug:
            self._reset_window(slug, eng._window_start_ts)

        up = eng._last_up_price
        down = eng._last_down_price
        if up is None or down is None or up <= 0 or down <= 0:
            return

        now = time.time()
        elapsed = now - self._window_start_ts
        if elapsed < 0:
            return

        snap = _PriceSnap(ts=now, elapsed=elapsed, up=up, down=down)
        self._history.append(snap)

        pm_min = min(up, down)
        pm_max = max(up, down)
        if self._early_pm_min is None or pm_min < self._early_pm_min:
            self._early_pm_min = pm_min
        if self._early_pm_max is None or pm_max > self._early_pm_max:
            self._early_pm_max = pm_max

        cur_dom = "Up" if up >= down else "Down"
        if self._last_dom is not None and cur_dom != self._last_dom:
            self._dom_flips += 1
            self._flip_times.append(elapsed)
        self._last_dom = cur_dom

        if self._loser_at_60 is None and elapsed >= 60:
            self._loser_at_60 = min(up, down)
            self._dom_at_60 = cur_dom

        if self._live:
            self._check_pending_hedges()
            self._manage_active_hedges()
            self._rebalance_hedges(now)
            self._check_pending_tp()
            self._cleanup_small_leftovers(elapsed, now)
        if (
            not self._early_window_blocked
            and elapsed <= EARLY_WINDOW_SKIP_CHECK_ELAPSED
            and self._early_pm_min is not None
            and self._early_pm_max is not None
            and (self._early_pm_max - self._early_pm_min) >= EARLY_WINDOW_SKIP_PM_RANGE
        ):
            self._early_window_blocked = True
            self._signal_window_closed = True
            SIGLOG.info(
                "[SIGNAL] window closed early | pm_range_0_120 >= %.2f | range=%.2f | window=%s",
                EARLY_WINDOW_SKIP_PM_RANGE,
                self._early_pm_max - self._early_pm_min,
                self._window_slug,
            )
        if max(up, down) >= SIGNAL_WINDOW_CLOSE_PRICE:
            self._signal_window_closed = True
        self._eval_patterns(snap, elapsed, cur_dom)

    def _reset_window(self, slug: str, start_ts: float) -> None:
        self._cancel_active_hedge_orders()
        self._window_slug = slug
        self._window_start_ts = start_ts
        self._history.clear()
        self._dom_flips = 0
        self._flip_times.clear()
        self._last_dom = None
        self._signals_fired.clear()
        self._loser_at_60 = None
        self._dom_at_60 = None
        self._pending_tp.clear()
        self._pending_hedges.clear()
        self._active_hedge_orders.clear()
        self._desired_hedge_shares = {"Up": 0, "Down": 0}
        self._hedge_limit_by_side = {"Up": HEDGE_MIN_LIMIT_PRICE, "Down": HEDGE_MIN_LIMIT_PRICE}
        self._hedge_tp_covered_shares = {"Up": 0, "Down": 0}
        self._buys_placed_up = 0
        self._buys_placed_down = 0
        self._last_leftover_cleanup_ts = 0.0
        self._last_hedge_rebalance_ts = 0.0
        self._signal_window_closed = False
        self._early_pm_min = None
        self._early_pm_max = None
        self._early_window_blocked = False
        self._btc_missing_logged = False
        self._btc_volume_missing_logged = False

        if self._window_balance_override is not None:
            balance_usdc = float(self._window_balance_override)
        elif self._trader is not None:
            try:
                balance_usdc = self._trader.wallet_balance_usdc()
            except Exception:
                balance_usdc = 0.0
        else:
            balance_usdc = 0.0
        self._balance_snapshot_usdc = balance_usdc
        self._max_buys_per_side = int(balance_usdc // SIGNAL_USDC_PER_DEAL_PER_SIDE)

        SIGLOG.info(
            "[SIGNAL] new window %s | balance=$%.2f | max_signal_buys per_side=%d (1 per $%.0f/side)",
            slug,
            balance_usdc,
            self._max_buys_per_side,
            SIGNAL_USDC_PER_DEAL_PER_SIDE,
        )

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------
    def _get_token(self, side: str):
        eng = self._engine
        if eng is None or eng._last_contract is None:
            return None
        return eng._last_contract.up if side == "Up" else eng._last_contract.down

    def _hedge_pair_sum_cap(self, primary_buy_limit: float) -> float:
        if primary_buy_limit >= 0.85 - 1e-9:
            return HEDGE_PAIR_SUM_MAX_RICH_ENTRY
        return HEDGE_PAIR_SUM_MAX

    def _place_buy(self, name: str, side: str, price: float, prob: str, ev: str, extra: str = "") -> bool:
        placed_side = self._buys_placed_up if side == "Up" else self._buys_placed_down
        if placed_side >= self._max_buys_per_side:
            SIGLOG.info(
                "[SIGNAL] BUY SKIPPED %s | max per-side reached (%d/%d on %s) | window=%s",
                name,
                placed_side,
                self._max_buys_per_side,
                side,
                self._window_slug,
            )
            return False
        if price > MAX_SIGNAL_TRIGGER_PRICE:
            SIGLOG.info(
                "[SIGNAL] BUY SKIPPED %s | %s current price above %.2f (%.2f) | window=%s",
                name, side, MAX_SIGNAL_TRIGGER_PRICE, price, self._window_slug,
            )
            return False
        token = self._get_token(side)
        if token is None:
            SIGLOG.warning("[SIGNAL] no token for %s -- skipping %s", side, name)
            return False
        limit = round(min(price + SIGNAL_BUY_PRICE_PAD, MAX_SIGNAL_TRIGGER_PRICE), 2)
        notional = limit * CLIP
        try:
            resp = self._trader.place_limit_buy(token, limit, CLIP)
            order_id = resp.get("orderID") or resp.get("id") or "?"
            self._last_order_ts = time.time()
            if side == "Up":
                self._buys_placed_up += 1
            else:
                self._buys_placed_down += 1
            SIGLOG.info(
                "[SIGNAL] *** BUY PLACED %s | %s @ %.2f x%d ($%.2f) | prob=%s ev(5sh)=%s %s| order=%s | up=%d down=%d cap/side=%d | window=%s",
                name, side, limit, CLIP, notional, prob, ev,
                f"| {extra} " if extra else "",
                order_id,
                self._buys_placed_up,
                self._buys_placed_down,
                self._max_buys_per_side,
                self._window_slug,
            )
            self._pending_tp.append(
                {
                    "side": side,
                    "shares": CLIP,
                    "min_balance": float(CLIP),
                }
            )
            hedge_side = "Down" if side == "Up" else "Up"
            hedge_pair_sum_cap = self._hedge_pair_sum_cap(limit)
            hedge_limit = round(hedge_pair_sum_cap - limit, 2)
            if hedge_limit >= HEDGE_MIN_LIMIT_PRICE:
                hedge_limit = float(max(HEDGE_MIN_LIMIT_PRICE, hedge_limit))
                self._desired_hedge_shares[hedge_side] += CLIP
                self._hedge_limit_by_side[hedge_side] = max(self._hedge_limit_by_side[hedge_side], hedge_limit)
                self._pending_hedges.append(
                    {
                        "pattern": name,
                        "primary_side": side,
                        "hedge_side": hedge_side,
                        "shares": CLIP,
                        "hedge_limit": hedge_limit,
                    }
                )
                self._check_pending_hedges()
                SIGLOG.info(
                    "[SIGNAL] hedge queued %s | primary=%s @ %.2f | hedge=%s bid @ %.2f x%d | pair_cap=%.2f | window=%s",
                    name,
                    side,
                    limit,
                    hedge_side,
                    hedge_limit,
                    CLIP,
                    hedge_pair_sum_cap,
                    self._window_slug,
                )
            return True
        except Exception as exc:
            SIGLOG.error("[SIGNAL] BUY FAILED %s | %s @ %.2f | %s", name, side, limit, exc)
            return False

    def _check_pending_hedges(self) -> None:
        if not self._pending_hedges:
            return
        remaining: list[dict[str, float | int | str]] = []
        for hedge in self._pending_hedges:
            hedge_side = str(hedge["hedge_side"])
            shares = int(hedge["shares"])
            hedge_token = self._get_token(hedge_side)
            if hedge_token is None:
                remaining.append(hedge)
                continue
            try:
                resp = self._trader.place_limit_buy(hedge_token, float(hedge["hedge_limit"]), shares)
                order_id = resp.get("orderID") or resp.get("id") or "?"
                self._active_hedge_orders.append(
                    {
                        "order_id": str(order_id),
                        "pattern": str(hedge["pattern"]),
                        "side": hedge_side,
                        "shares": shares,
                        "limit": float(hedge["hedge_limit"]),
                    }
                )
                SIGLOG.info(
                    "[SIGNAL] HEDGE BUY placed %s | %s bid @ %.2f x%d | order=%s | window=%s",
                    hedge["pattern"],
                    hedge_side,
                    float(hedge["hedge_limit"]),
                    shares,
                    order_id,
                    self._window_slug,
                )
            except Exception as exc:
                SIGLOG.debug(
                    "[SIGNAL] HEDGE BUY failed %s | %s @ %.2f x%d: %s -- will retry",
                    hedge["pattern"],
                    hedge_side,
                    float(hedge["hedge_limit"]),
                    shares,
                    exc,
                )
                remaining.append(hedge)
        self._pending_hedges = remaining

    def _manage_active_hedges(self) -> None:
        for side in ("Up", "Down"):
            token = self._get_token(side)
            if token is None:
                continue
            balance = self._trader.token_balance(token.token_id)
            covered = self._hedge_tp_covered_shares[side]
            hedge_balance_shares = int(balance // CLIP) * CLIP
            new_cover = hedge_balance_shares - covered
            if new_cover >= CLIP:
                self._pending_tp.append(
                    {
                        "side": side,
                        "shares": int(new_cover),
                        "min_balance": float(covered + new_cover),
                    }
                )
                self._hedge_tp_covered_shares[side] += int(new_cover)
                SIGLOG.info(
                    "[SIGNAL] HEDGE balance detected | %s balance=%.4f | TP queued x%d | window=%s",
                    side,
                    balance,
                    int(new_cover),
                    self._window_slug,
                )

        remaining: list[dict[str, float | int | str | bool]] = []
        for hedge in self._active_hedge_orders:
            side = str(hedge["side"])
            token = self._get_token(side)
            if token is None:
                remaining.append(hedge)
                continue
            balance = self._trader.token_balance(token.token_id)
            desired = self._desired_hedge_shares.get(side, 0)
            if balance + 1e-9 >= desired and desired > 0:
                order_id = str(hedge.get("order_id") or "")
                if order_id and order_id != "?":
                    self._trader.cancel_order(order_id)
                continue
            remaining.append(hedge)
        self._active_hedge_orders = remaining

    def _pending_hedge_shares(self, side: str) -> int:
        total = 0
        for hedge in self._pending_hedges:
            if str(hedge["hedge_side"]) == side:
                total += int(hedge["shares"])
        for hedge in self._active_hedge_orders:
            if str(hedge["side"]) == side:
                total += int(hedge["shares"])
        return total

    def _rebalance_hedges(self, now: float) -> None:
        if now - self._last_hedge_rebalance_ts < HEDGE_REBALANCE_INTERVAL_SECONDS:
            return
        self._last_hedge_rebalance_ts = now
        for side in ("Up", "Down"):
            desired = self._desired_hedge_shares.get(side, 0)
            if desired <= 0:
                continue
            token = self._get_token(side)
            if token is None:
                continue
            balance = self._trader.token_balance(token.token_id)
            covered = int(balance // CLIP) * CLIP + self._pending_hedge_shares(side)
            gap = desired - covered
            if gap < CLIP:
                continue
            hedge_limit = max(HEDGE_MIN_LIMIT_PRICE, float(self._hedge_limit_by_side.get(side, HEDGE_MIN_LIMIT_PRICE)))
            while gap >= CLIP:
                self._pending_hedges.append(
                    {
                        "pattern": "rebalance",
                        "primary_side": "Down" if side == "Up" else "Up",
                        "hedge_side": side,
                        "shares": CLIP,
                        "hedge_limit": hedge_limit,
                    }
                )
                gap -= CLIP
            self._check_pending_hedges()

    def _check_pending_tp(self) -> None:
        if not self._pending_tp:
            return
        remaining: list[dict[str, float | int | str]] = []
        for item in self._pending_tp:
            side = str(item["side"])
            shares = int(item["shares"])
            token = self._get_token(side)
            if token is None:
                remaining.append(item)
                continue
            balance = self._trader.token_balance(token.token_id)
            if balance + 1e-9 < float(item["min_balance"]):
                remaining.append(item)
                continue
            try:
                resp = self._trader.place_limit_sell(token, TP_PRICE, shares)
                order_id = resp.get("orderID") or resp.get("id") or "?"
                SIGLOG.info(
                    "[SIGNAL] TP SELL placed %s @ %.2f x%d | order=%s | window=%s",
                    side, TP_PRICE, shares, order_id, self._window_slug,
                )
            except Exception as exc:
                SIGLOG.debug("[SIGNAL] TP SELL failed %s x%d: %s -- will retry", side, shares, exc)
                remaining.append(item)
        self._pending_tp = remaining

    def _cancel_active_hedge_orders(self) -> None:
        if not self._active_hedge_orders or self._trader is None:
            return
        for hedge in self._active_hedge_orders:
            order_id = str(hedge.get("order_id") or "")
            if order_id and order_id != "?":
                self._trader.cancel_order(order_id)

    def _btc_data_ok(self) -> bool:
        eng = self._engine
        if eng is None:
            return False
        if not eng._btc_price_history:
            return False
        last = eng._btc_price_history[-1]
        return last is not None and getattr(last, "price", None) is not None and last.price > 0

    def _btc_volume_ok(self) -> bool:
        eng = self._engine
        if eng is None or not eng._btc_price_history:
            return False
        history = eng._btc_price_history[-BTC_VOLUME_OK_LOOKBACK_POINTS:]
        for point in history:
            if point is None:
                continue
            if (getattr(point, "base_volume", 0.0) or 0.0) > 0:
                return True
            if (getattr(point, "quote_volume", 0.0) or 0.0) > 0:
                return True
            if (getattr(point, "trade_count", 0) or 0) > 0:
                return True
        return False

    def _signal_requires_btc(self, name: str) -> bool:
        if name.startswith("btc") or name.startswith("mix_"):
            return True
        if name in CLASSIC_BTC_CONFIRMATION_FILTERS:
            return True
        blockers = PATTERN_ENTRY_RISK_BLOCKERS.get(name, ())
        for blocker in blockers:
            if any(token in blocker for token in ("btc", "btcrange", "btcmove", "btcrebound", "btcmoveabs")):
                return True
            if any(token in blocker for token in ("base", "quote", "trades", "baseratio", "quoteratio", "tradesratio")):
                return True
        return False

    def _signal_requires_volume(self, name: str) -> bool:
        blockers = PATTERN_ENTRY_RISK_BLOCKERS.get(name, ())
        for blocker in blockers:
            if any(token in blocker for token in ("base", "quote", "trades", "baseratio", "quoteratio", "tradesratio")):
                return True
        return False

    def _cleanup_small_leftovers(self, elapsed: float, now: float) -> None:
        if elapsed < LEFTOVER_CLEANUP_START_ELAPSED:
            return
        if now - self._last_leftover_cleanup_ts < LEFTOVER_CLEANUP_INTERVAL_SECONDS:
            return
        self._last_leftover_cleanup_ts = now

        for side in ("Up", "Down"):
            token = self._get_token(side)
            if token is None:
                continue
            current_price = self._engine._last_up_price if side == "Up" else self._engine._last_down_price
            if current_price is None or current_price < LEFTOVER_CLEANUP_PRICE:
                continue
            balance = self._trader.token_balance(token.token_id)
            if balance <= 0.0 or balance >= 5.0:
                continue
            try:
                resp = self._trader.place_marketable_sell(token, LEFTOVER_CLEANUP_PRICE, round(balance, 4))
                order_id = resp.get("orderID") or resp.get("id") or "?"
                SIGLOG.info(
                    "[SIGNAL] LEFTOVER CLEANUP SELL %s @ %.2f x%.4f | order=%s | window=%s",
                    side, LEFTOVER_CLEANUP_PRICE, balance, order_id, self._window_slug,
                )
            except Exception as exc:
                SIGLOG.debug(
                    "[SIGNAL] LEFTOVER CLEANUP failed %s x%.4f @ %.2f: %s",
                    side, balance, LEFTOVER_CLEANUP_PRICE, exc,
                )

    # ------------------------------------------------------------------
    # Signal fire (with live order)
    # ------------------------------------------------------------------
    def _fire(self, name: str, side: str, price: float, prob: str, ev: str, extra: str = "") -> None:
        if name not in self._active_signal_names:
            return
        if self._signal_window_closed:
            return
        if self._signal_requires_btc(name) and not self._btc_data_ok():
            if not self._btc_missing_logged:
                self._btc_missing_logged = True
                SIGLOG.warning(
                    "[SIGNAL] BTC data missing or invalid | skipping BTC-dependent signals | window=%s",
                    self._window_slug,
                )
            return
        if self._signal_requires_volume(name) and not self._btc_volume_ok():
            if not self._btc_volume_missing_logged:
                self._btc_volume_missing_logged = True
                SIGLOG.warning(
                    "[SIGNAL] BTC volume data missing | skipping volume-dependent signals | window=%s",
                    self._window_slug,
                )
            return
        if not self._btc_overlay_allows(name, side):
            return
        if self._entry_risk_blocked(name, side, price):
            return
        if name in self._signals_fired:
            return
        self._signals_fired.add(name)
        SIGLOG.info(
            "[SIGNAL] DETECTED %s | side=%s price=%.2f | prob=%s ev(5sh)=%s %s| window=%s",
            name, side, price, prob, ev,
            f"| {extra} " if extra else "",
            self._window_slug,
        )
        if self._live:
            self._place_buy(name, side, price, prob, ev, extra)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _dom_side(self, snap: _PriceSnap) -> str:
        return "Up" if snap.up >= snap.down else "Down"

    def _dom_price(self, snap: _PriceSnap) -> float:
        return max(snap.up, snap.down)

    def _loser_price(self, snap: _PriceSnap) -> float:
        return min(snap.up, snap.down)

    def _snap_near(self, target_elapsed: float, tolerance: float = 30.0) -> _PriceSnap | None:
        best: _PriceSnap | None = None
        best_dist = 999.0
        for s in self._history:
            d = abs(s.elapsed - target_elapsed)
            if d < best_dist:
                best_dist = d
                best = s
        if best is None or best_dist > tolerance:
            return None
        return best

    def _dom_at_elapsed(self, target_elapsed: float) -> str | None:
        s = self._snap_near(target_elapsed)
        return self._dom_side(s) if s is not None else None

    def _flips_before(self, elapsed: float) -> int:
        return sum(1 for t in self._flip_times if t <= elapsed)

    def _side_price(self, snap: _PriceSnap, side: str) -> float:
        return snap.up if side == "Up" else snap.down

    def _btc_ready(self) -> bool:
        eng = self._engine
        return eng is not None and getattr(eng, "_last_btc_price", None) is not None

    def _btc_price_near(self, target_elapsed: float, tolerance: float = 15.0) -> float | None:
        eng = self._engine
        if eng is None:
            return None
        history = getattr(eng, "_btc_price_history", None)
        if not history:
            return None
        target_ts = self._window_start_ts + target_elapsed
        best = None
        best_dist = 999.0
        for point in history:
            dist = abs(point.ts - target_ts)
            if dist < best_dist:
                best_dist = dist
                best = point
        if best is None or best_dist > tolerance:
            return None
        return float(best.price)

    def _btc_volume_sum(self, end_elapsed: float, lookback_seconds: float, field: str) -> float | None:
        eng = self._engine
        if eng is None:
            return None
        history = getattr(eng, "_btc_price_history", None)
        if not history:
            return None
        start_ts = self._window_start_ts + max(0.0, end_elapsed - lookback_seconds)
        end_ts = self._window_start_ts + max(0.0, end_elapsed)
        total = 0.0
        found = False
        for point in history:
            if point.ts < start_ts or point.ts > end_ts:
                continue
            value = getattr(point, field, None)
            if value is None:
                continue
            total += float(value)
            found = True
        return total if found else None

    def _btc_base_ratio(self, end_elapsed: float, short_lookback: float, long_lookback: float) -> float | None:
        short_sum = self._btc_volume_sum(end_elapsed, short_lookback, "base_volume")
        long_sum = self._btc_volume_sum(end_elapsed, long_lookback, "base_volume")
        if short_sum is None or long_sum is None or long_lookback <= 0 or short_lookback <= 0:
            return None
        baseline = long_sum * (short_lookback / long_lookback)
        if baseline <= 0:
            return None
        return short_sum / baseline

    def _btc_quote_ratio(self, end_elapsed: float, short_lookback: float, long_lookback: float) -> float | None:
        short_sum = self._btc_volume_sum(end_elapsed, short_lookback, "quote_volume")
        long_sum = self._btc_volume_sum(end_elapsed, long_lookback, "quote_volume")
        if short_sum is None or long_sum is None or long_lookback <= 0 or short_lookback <= 0:
            return None
        baseline = long_sum * (short_lookback / long_lookback)
        if baseline <= 0:
            return None
        return short_sum / baseline

    def _btc_trade_ratio(self, end_elapsed: float, short_lookback: float, long_lookback: float) -> float | None:
        short_sum = self._btc_volume_sum(end_elapsed, short_lookback, "trade_count")
        long_sum = self._btc_volume_sum(end_elapsed, long_lookback, "trade_count")
        if short_sum is None or long_sum is None or long_lookback <= 0 or short_lookback <= 0:
            return None
        baseline = long_sum * (short_lookback / long_lookback)
        if baseline <= 0:
            return None
        return short_sum / baseline

    def _signal_metric_value(self, metric: str, side: str, elapsed: float, snap: _PriceSnap) -> float | None:
        if metric == "elapsed":
            return float(elapsed)
        if metric in {"btcprice", "btc_price"}:
            return self._btc_price_near(elapsed)
        if metric == "price" or metric == "domprice":
            return float(self._dom_price(snap))
        if metric == "loser" or metric == "loserprice":
            return float(self._loser_price(snap))
        if metric == "lead":
            return float(self._dom_price(snap) - self._loser_price(snap))
        if metric == "ratio":
            loser = self._loser_price(snap)
            return float(self._dom_price(snap) / loser) if loser > 0.01 else 999.0
        if metric == "flips":
            return float(self._dom_flips)
        if metric.startswith("sidemove"):
            lookback = float(metric.removeprefix("sidemove"))
            old = self._snap_near(max(0.0, elapsed - lookback), tolerance=max(5.0, lookback * 0.35))
            if old is None:
                return None
            old_px = self._side_price(old, side)
            cur_px = self._side_price(snap, side)
            return ((cur_px - old_px) / old_px) if old_px > 0 else None
        if metric.startswith("losermove"):
            lookback = float(metric.removeprefix("losermove"))
            loser_side = "Down" if side == "Up" else "Up"
            old = self._snap_near(max(0.0, elapsed - lookback), tolerance=max(5.0, lookback * 0.35))
            if old is None:
                return None
            old_px = self._side_price(old, loser_side)
            cur_px = self._side_price(snap, loser_side)
            return ((cur_px - old_px) / old_px) if old_px > 0 else None
        if metric.startswith("base") and metric[4:].isdigit():
            return self._btc_volume_sum(elapsed, float(metric[4:]), "base_volume")
        if metric.startswith("quote") and metric[5:].isdigit():
            return self._btc_volume_sum(elapsed, float(metric[5:]), "quote_volume")
        if metric.startswith("trades") and metric[6:].isdigit():
            return self._btc_volume_sum(elapsed, float(metric[6:]), "trade_count")
        if metric.startswith("btcrange") and metric[8:].isdigit():
            return self._btc_range(elapsed, float(metric[8:]))
        if metric.startswith("btcmoveabs") and metric[10:].isdigit():
            value = self._btc_move(elapsed, float(metric[10:]))
            return abs(value) if value is not None else None
        if metric.startswith("btcmove") and metric[7:].isdigit():
            return self._btc_move(elapsed, float(metric[7:]))
        if metric.startswith("btcrebound") and metric[10:].isdigit():
            return self._btc_rebound(elapsed, float(metric[10:]), side)
        if metric.startswith("baseratio") and "_" in metric:
            raw = metric.removeprefix("baseratio")
            short_s, long_s = raw.split("_", 1)
            return self._btc_base_ratio(elapsed, float(short_s), float(long_s))
        if metric.startswith("quoteratio") and "_" in metric:
            raw = metric.removeprefix("quoteratio")
            short_s, long_s = raw.split("_", 1)
            return self._btc_quote_ratio(elapsed, float(short_s), float(long_s))
        if metric.startswith("tradesratio") and "_" in metric:
            raw = metric.removeprefix("tradesratio")
            short_s, long_s = raw.split("_", 1)
            return self._btc_trade_ratio(elapsed, float(short_s), float(long_s))
        return None

    def _btc_move(self, end_elapsed: float, lookback_seconds: float) -> float | None:
        end_elapsed = max(0.0, end_elapsed)
        now_px = self._btc_price_near(end_elapsed)
        old_px = self._btc_price_near(max(0.0, end_elapsed - lookback_seconds))
        if now_px is None or old_px is None or old_px <= 0:
            return None
        return (now_px - old_px) / old_px

    def _btc_range(self, end_elapsed: float, lookback_seconds: float, step_seconds: float = 5.0) -> float | None:
        start_elapsed = max(0.0, end_elapsed - lookback_seconds)
        prices: list[float] = []
        probe = start_elapsed
        while probe <= end_elapsed:
            px = self._btc_price_near(probe)
            if px is not None:
                prices.append(px)
            probe += step_seconds
        if len(prices) < 2:
            return None
        lo = min(prices)
        hi = max(prices)
        if lo <= 0:
            return None
        return (hi - lo) / lo

    def _btc_rebound(self, end_elapsed: float, lookback_seconds: float, side: str) -> float | None:
        start_elapsed = max(0.0, end_elapsed - lookback_seconds)
        prices: list[float] = []
        probe = start_elapsed
        while probe <= end_elapsed:
            px = self._btc_price_near(probe)
            if px is not None:
                prices.append(px)
            probe += 5.0
        if len(prices) < 2:
            return None
        last = prices[-1]
        if side == "Up":
            lo = min(prices)
            if lo <= 0:
                return None
            return (last - lo) / lo
        hi = max(prices)
        if hi <= 0:
            return None
        return (hi - last) / hi

    def _dom_move(self, start_elapsed: float, end_elapsed: float) -> float | None:
        start = self._snap_near(max(0.0, start_elapsed), tolerance=max(5.0, abs(end_elapsed - start_elapsed) * 0.35))
        end = self._snap_near(max(0.0, end_elapsed), tolerance=5.0)
        if start is None or end is None:
            return None
        return self._dom_price(end) - self._dom_price(start)

    def _dom_streak_seconds(self, current_side: str) -> float:
        streak = 0.0
        for snap in reversed(self._history):
            if self._dom_side(snap) != current_side:
                break
            streak += 1.0
        return streak

    def _btc_accel(self, end_elapsed: float, lookback_seconds: float) -> float | None:
        if end_elapsed < 2 * lookback_seconds:
            return None
        prev_move = self._btc_move(end_elapsed - lookback_seconds, lookback_seconds)
        cur_move = self._btc_move(end_elapsed, lookback_seconds)
        if prev_move is None or cur_move is None:
            return None
        return abs(cur_move) - abs(prev_move)

    def _btc_filter_passes(self, filter_spec: str, side: str, elapsed: float) -> bool:
        parts = filter_spec.split("_")
        if len(parts) < 3:
            return True
        kind = "_".join(parts[:-2])
        lookback = float(parts[-2])
        threshold = float(parts[-1])
        if kind == "range_le":
            value = self._btc_range(elapsed, lookback)
            return value is not None and value <= threshold
        if kind == "rebound_ge":
            value = self._btc_rebound(elapsed, lookback, side)
            return value is not None and value >= threshold
        if kind == "move_up":
            value = self._btc_move(elapsed, lookback)
            return value is not None and value >= threshold
        if kind == "move_dn":
            value = self._btc_move(elapsed, lookback)
            return value is not None and value <= -threshold
        if kind == "moveabs_ge":
            value = self._btc_move(elapsed, lookback)
            return value is not None and abs(value) >= threshold
        if kind == "moveabs_le":
            value = self._btc_move(elapsed, lookback)
            return value is not None and abs(value) <= threshold
        if kind == "accel_ge":
            value = self._btc_accel(elapsed, lookback)
            return value is not None and value >= threshold
        if kind.startswith("base_ratio_ge_"):
            short_raw = kind.removeprefix("base_ratio_ge_")
            if not short_raw:
                return True
            short_lookback = float(short_raw)
            long_lookback = lookback
            value = self._btc_base_ratio(elapsed, short_lookback, long_lookback)
            return value is not None and value >= threshold
        return True

    def _btc_overlay_allows(self, name: str, side: str) -> bool:
        filters = CLASSIC_BTC_CONFIRMATION_FILTERS.get(name)
        if not filters:
            return True
        # Fallback behavior: if BTC feed/history is unavailable, keep the classic signal live.
        if not self._btc_ready():
            return True
        elapsed = self._history[-1].elapsed if self._history else 0.0
        return all(self._btc_filter_passes(spec, side, elapsed) for spec in filters)

    def _entry_risk_blocked(self, name: str, side: str, price: float) -> bool:
        specs = PATTERN_ENTRY_RISK_BLOCKERS.get(name)
        if not specs or not self._history:
            return False
        snap = self._history[-1]
        d_px = self._dom_price(snap)
        l_px = self._loser_price(snap)
        ratio = d_px / l_px if l_px > 0.01 else 999.0
        elapsed = snap.elapsed
        for spec in specs:
            parts = spec.split("_")
            if len(parts) >= 3 and parts[-2] in {"ge", "gt", "le", "lt"}:
                metric = "_".join(parts[:-2])
                cmp = parts[-2]
                threshold = float(parts[-1])
                value = self._signal_metric_value(metric, side, elapsed, snap)
                if value is None:
                    continue
                if cmp == "ge" and value >= threshold:
                    return True
                if cmp == "gt" and value > threshold:
                    return True
                if cmp == "le" and value <= threshold:
                    return True
                if cmp == "lt" and value < threshold:
                    return True
                continue
            if len(parts) == 3 and parts[0] == "elapsed" and parts[1] == "ge":
                if elapsed >= float(parts[2]):
                    return True
                continue
            if len(parts) == 3 and parts[0] == "price" and parts[1] == "ge":
                if price >= float(parts[2]):
                    return True
                continue
            if len(parts) == 3 and parts[0] == "ratio" and parts[1] == "ge":
                if ratio >= float(parts[2]):
                    return True
                continue
            if len(parts) == 3 and parts[0] == "flips" and parts[1] == "ge":
                if self._dom_flips >= int(float(parts[2])):
                    return True
                continue
            if len(parts) == 3 and parts[0] == "loser" and parts[1] == "lt":
                if l_px < float(parts[2]):
                    return True
                continue
            if len(parts) == 5 and parts[0] == "btc" and parts[1] == "range" and parts[2] == "ge":
                btc_range = self._btc_range(elapsed, float(parts[3]))
                if btc_range is not None and btc_range >= float(parts[4]):
                    return True
                continue
            if len(parts) == 6 and parts[0] == "btc" and parts[1] == "move" and parts[2] == "abs" and parts[3] == "lt":
                btc_move = self._btc_move(elapsed, float(parts[4]))
                if btc_move is not None and abs(btc_move) < float(parts[5]):
                    return True
                continue
        return False

    # ------------------------------------------------------------------
    # Pattern evaluators  (44 active -- tested on 178 windows)
    # ------------------------------------------------------------------
    # REMOVED PATTERNS (kept for reference, no longer active):
    #   - consistent_dom_300_720    (v2, removed: WR degraded 93%->87%)
    #   - flipband_t540_0to1        (v4, removed: PnL<$10 & WR<100%)
    #   - squeeze_t135_d02          (v4, removed: PnL<$10 & WR<100%)
    #   - flipband_t465_0to0        (v4, removed: PnL<$10 & WR<100%)
    #   - late_cert_t780_dp95       (v2, removed: low EV +$0.09)
    #   - compound_t720_f3_s03      (v3, removed: low EV +$0.20)
    # ------------------------------------------------------------------
    def _eval_patterns(self, snap: _PriceSnap, elapsed: float, dom: str) -> None:
        d_px = self._dom_price(snap)
        l_px = self._loser_price(snap)
        lead = d_px - l_px
        spread = lead

        # ==============================================================
        # v2 PATTERNS (1-6)
        # ==============================================================

        # 1. low_vol_t600_flip2  (96% | n=47 | win +$0.60 | loss -$3.58 | ev +$0.42)
        if 598 <= elapsed <= 605 and self._dom_flips <= 2:
            self._fire("low_vol_t600_flip2", dom, d_px, "96%", "+$0.42",
                       f"flips={self._dom_flips}")

        # 2. low_vol_t720_flip2  (100% | n=46 | win +$0.45 | loss $0 | ev +$0.45)
        if 718 <= elapsed <= 725 and self._dom_flips <= 2:
            self._fire("low_vol_t720_flip2", dom, d_px, "100%", "+$0.45",
                       f"flips={self._dom_flips}")

        # 3. spread_squeeze_t720_drop20  (98% | n=103 | win +$0.30 | loss -$4.68 | ev +$0.20)
        if 718 <= elapsed <= 730 and self._loser_at_60 is not None:
            drop = self._loser_at_60 - l_px
            if drop >= 0.20:
                self._fire("spread_squeeze_t720_drop20", dom, d_px, "98%", "+$0.20",
                           f"loser_drop={drop:.3f}")

        # 4. dom_t720_lead30  (96% | n=121 | win +$0.42 | loss -$4.17 | ev +$0.23)
        if 718 <= elapsed <= 725 and lead >= 0.30:
            self._fire("dom_t720_lead30", dom, d_px, "96%", "+$0.23",
                       f"lead={lead:.3f}")

        # ==============================================================
        # v3 PATTERNS (6-14)
        # ==============================================================

        # 6. crossover_t600_k60  (93% | n=14 | win +$2.07 | loss -$2.70 | ev +$1.73)
        if 598 <= elapsed <= 605:
            dom_540 = self._dom_at_elapsed(540)
            if dom_540 is not None and dom_540 != dom:
                self._fire("crossover_t600_k60", dom, d_px, "93%", "+$1.73",
                           f"dom_540={dom_540} dom_600={dom}")

        # 7. crossover_t600_k30  (89% | n=9 | win +$2.04 | loss -$2.70 | ev +$1.51)
        if 598 <= elapsed <= 605:
            dom_570 = self._dom_at_elapsed(570)
            if dom_570 is not None and dom_570 != dom:
                self._fire("crossover_t600_k30", dom, d_px, "89%", "+$1.51",
                           f"dom_570={dom_570} dom_600={dom}")

        # 8. velocity_t720_w60  (92% | n=13 | win +$1.29 | loss -$1.65 | ev +$1.06)
        if 718 <= elapsed <= 725:
            snap_660 = self._snap_near(660)
            if snap_660 is not None:
                vel_up = (snap.up - snap_660.up) / 60.0
                vel_dn = (snap.down - snap_660.down) / 60.0
                if vel_up >= 0.003 and vel_up > vel_dn:
                    self._fire("velocity_t720_w60", "Up", snap.up, "92%", "+$1.06",
                               f"vel_up={vel_up:.4f}/s")
                elif vel_dn >= 0.003 and vel_dn > vel_up:
                    self._fire("velocity_t720_w60", "Down", snap.down, "92%", "+$1.06",
                               f"vel_dn={vel_dn:.4f}/s")

        # 9. reversal_300_to_600  (94% | n=32 | win +$1.11 | loss -$4.03 | ev +$0.79)
        if 598 <= elapsed <= 605:
            dom_300 = self._dom_at_elapsed(300)
            if dom_300 is not None and dom_300 != dom:
                self._fire("reversal_300_to_600", dom, d_px, "94%", "+$0.79",
                           f"was={dom_300} now={dom}")

        # 10. flipband_t720_0to1  (100% | n=34 | win +$0.44 | loss $0 | ev +$0.44)
        if 718 <= elapsed <= 725 and self._dom_flips <= 1:
            self._fire("flipband_t720_0to1", dom, d_px, "100%", "+$0.44",
                       f"flips={self._dom_flips}")

        # 11. ratio_t720_ge4  (95% | n=58 | loss -$4.50 | ev +$0.19)
        if 718 <= elapsed <= 725:
            if l_px > 0.01:
                ratio = d_px / l_px
                if ratio >= 4.0:
                    self._fire("ratio_t720_ge4", dom, d_px, "95%", "+$0.19",
                               f"ratio={ratio:.1f}")

        # 12. spread_t720_ge06  (97% | n=104 | win +$0.27 | loss -$4.50 | ev +$0.13)
        if 718 <= elapsed <= 725 and spread >= 0.60:
            self._fire("spread_t720_ge06", dom, d_px, "97%", "+$0.13",
                       f"spread={spread:.3f}")

        # ==============================================================
        # v4 PATTERNS (20-29)
        # ==============================================================

        # 21. crossover_t585_k45  (100% | n=10 | win +$2.12 | loss $0 | ev +$2.12)
        if 583 <= elapsed <= 590:
            dom_540 = self._dom_at_elapsed(540)
            if dom_540 is not None and dom_540 != dom:
                self._fire("crossover_t585_k45", dom, d_px, "100%", "+$2.12",
                           f"dom_540={dom_540} now={dom}")

        # 22. crossover_t585_k60  (100% | n=12 | win +$1.97 | loss $0 | ev +$1.97)
        if 583 <= elapsed <= 590:
            dom_525 = self._dom_at_elapsed(525)
            if dom_525 is not None and dom_525 != dom:
                self._fire("crossover_t585_k60", dom, d_px, "100%", "+$1.97",
                           f"dom_525={dom_525} now={dom}")

        # 23. vel_t315_w30_v004  (89% | n=18 | win +$1.87 | loss -$3.12 | ev +$1.32)
        if 313 <= elapsed <= 320:
            s285 = self._snap_near(285)
            if s285 is not None:
                vu = (snap.up - s285.up) / 30.0
                vd = (snap.down - s285.down) / 30.0
                if vu >= 0.004 and vu > vd:
                    self._fire("vel_t315_w30_v004", "Up", snap.up, "89%", "+$1.32",
                               f"vel={vu:.4f}/s")
                elif vd >= 0.004 and vd > vu:
                    self._fire("vel_t315_w30_v004", "Down", snap.down, "89%", "+$1.32",
                               f"vel={vd:.4f}/s")

        # 24. vel_t693_w60_v004  (100% | n=14 | win +$1.28 | loss $0 | ev +$1.28)
        if 691 <= elapsed <= 698:
            s633 = self._snap_near(633)
            if s633 is not None:
                vu = (snap.up - s633.up) / 60.0
                vd = (snap.down - s633.down) / 60.0
                if vu >= 0.004 and vu > vd:
                    self._fire("vel_t693_w60_v004", "Up", snap.up, "100%", "+$1.28",
                               f"vel={vu:.4f}/s")
                elif vd >= 0.004 and vd > vu:
                    self._fire("vel_t693_w60_v004", "Down", snap.down, "100%", "+$1.28",
                               f"vel={vd:.4f}/s")

        # 25. vel_t645_w90_v003  (100% | n=16 | win +$1.07 | loss $0 | ev +$1.07)
        if 643 <= elapsed <= 650:
            s555 = self._snap_near(555)
            if s555 is not None:
                vu = (snap.up - s555.up) / 90.0
                vd = (snap.down - s555.down) / 90.0
                if vu >= 0.003 and vu > vd:
                    self._fire("vel_t645_w90_v003", "Up", snap.up, "100%", "+$1.07",
                               f"vel={vu:.4f}/s")
                elif vd >= 0.003 and vd > vu:
                    self._fire("vel_t645_w90_v003", "Down", snap.down, "100%", "+$1.07",
                               f"vel={vd:.4f}/s")

        # ==============================================================
        # v5 PATTERNS (29-32)
        # ==============================================================

        # 29. loserdrop_t585_w45_v002  (93% | n=43 | win +$1.23 | loss -$3.58 | ev +$0.89)
        if 583 <= elapsed <= 590:
            s540 = self._snap_near(540)
            if s540 is not None:
                loser_side = "Down" if dom == "Up" else "Up"
                l_540 = s540.down if loser_side == "Down" else s540.up
                l_now = snap.down if loser_side == "Down" else snap.up
                drop_vel = (l_540 - l_now) / 45.0
                if drop_vel >= 0.002:
                    self._fire("loserdrop_t585_w45_v002", dom, d_px, "93%", "+$0.89",
                               f"drop_vel={drop_vel:.4f}/s")

        # 30. diverge_t345_w60_r005  (82% | n=77 | win +$1.30 | loss -$3.30 | ev +$0.46)
        if 343 <= elapsed <= 350:
            s285 = self._snap_near(285)
            if s285 is not None:
                d_change = self._side_price(snap, dom) - self._side_price(s285, dom)
                loser_side = "Down" if dom == "Up" else "Up"
                l_change = self._side_price(snap, loser_side) - self._side_price(s285, loser_side)
                if d_change >= 0.05 and l_change <= -0.01:
                    self._fire("diverge_t345_w60_r005", dom, d_px, "82%", "+$0.46",
                               f"dom_rise={d_change:.3f} loser_drop={l_change:.3f}")

        # 31. ddrecov_t615_dd01_r075  (100% | n=10 | win +$1.42 | loss $0 | ev +$1.42)
        if 613 <= elapsed <= 620:
            dom_prices = [self._side_price(s, dom) for s in self._history if s.elapsed <= elapsed]
            if len(dom_prices) >= 30:
                peak = max(dom_prices)
                trough = peak
                peak_hit = False
                for px in dom_prices:
                    if px == peak:
                        peak_hit = True
                    if peak_hit:
                        trough = min(trough, px)
                dd = peak - trough
                if dd >= 0.10:
                    recovery = (d_px - trough) / dd if dd > 0 else 0
                    if recovery >= 0.75:
                        self._fire("ddrecov_t615_dd01_r075", dom, d_px, "100%", "+$1.42",
                                   f"dd={dd:.3f} recov={recovery:.0%}")

        # ==============================================================
        # v6 PATTERNS (33-41)
        # ==============================================================

        # 33. twapgap_t585_lb300_g005  (92% | n=112 | win +$0.77 | loss -$3.60 | ev +$0.42)
        if 583 <= elapsed <= 590:
            dom_prices_300 = [self._side_price(s, dom) for s in self._history if 285 <= s.elapsed <= elapsed]
            if len(dom_prices_300) >= 30:
                twap = sum(dom_prices_300) / len(dom_prices_300)
                gap = d_px - twap
                if gap >= 0.05:
                    self._fire("twapgap_t585_lb300_g005", dom, d_px, "92%", "+$0.42",
                               f"twap={twap:.3f} gap={gap:.3f}")

        # 34. retrace_t585_r085  (95% | n=119 | win +$0.58 | loss -$3.95 | ev +$0.36)
        if 583 <= elapsed <= 590:
            dom_all = [self._side_price(s, dom) for s in self._history if s.elapsed <= elapsed]
            if len(dom_all) >= 30:
                hi, lo = max(dom_all), min(dom_all)
                rng = hi - lo
                if rng >= 0.05:
                    retrace = (d_px - lo) / rng
                    if retrace >= 0.85:
                        self._fire("retrace_t585_r085", dom, d_px, "95%", "+$0.36",
                                   f"hi={hi:.3f} lo={lo:.3f} retrace={retrace:.2f}")

        # 35. lbounce_t585_r30_f45_rm008_fm002  (100% | n=11 | win +$1.59 | loss $0 | ev +$1.59)
        if 583 <= elapsed <= 590:
            s510 = self._snap_near(510)
            s540 = self._snap_near(540)
            if s510 is not None and s540 is not None:
                loser_side = "Down" if dom == "Up" else "Up"
                l_start = self._side_price(s510, loser_side)
                l_peak = self._side_price(s540, loser_side)
                l_now = self._side_price(snap, loser_side)
                rise = l_peak - l_start
                fall = l_peak - l_now
                if rise >= 0.08 and fall >= 0.02:
                    self._fire("lbounce_t585_r30_f45_rm008_fm002", dom, d_px, "100%", "+$1.59",
                               f"rise={rise:.3f} fall={fall:.3f}")

        # 36. lbounce_t240_r60_f15_rm005_fm006  (100% | n=10 | win +$1.63 | loss $0 | ev +$1.64)
        if 238 <= elapsed <= 245:
            s165 = self._snap_near(165)
            s225 = self._snap_near(225)
            if s165 is not None and s225 is not None:
                loser_side = "Down" if dom == "Up" else "Up"
                l_start = self._side_price(s165, loser_side)
                l_peak = self._side_price(s225, loser_side)
                l_now = self._side_price(snap, loser_side)
                rise = l_peak - l_start
                fall = l_peak - l_now
                if rise >= 0.05 and fall >= 0.06:
                    self._fire("lbounce_t240_r60_f15_rm005_fm006", dom, d_px, "100%", "+$1.64",
                               f"rise={rise:.3f} fall={fall:.3f}")

        # 37. accum_t615_b20_n3  (100% | n=30 | win +$0.82 | loss $0 | ev +$0.82)
        if 613 <= elapsed <= 620:
            s555 = self._snap_near(555)
            s575 = self._snap_near(575)
            s595 = self._snap_near(595)
            if s555 is not None and s575 is not None and s595 is not None:
                g1 = self._side_price(s575, dom) - self._side_price(s555, dom)
                g2 = self._side_price(s595, dom) - self._side_price(s575, dom)
                g3 = self._side_price(snap, dom) - self._side_price(s595, dom)
                if g1 > 0 and g2 > 0 and g3 > 0:
                    self._fire("accum_t615_b20_n3", dom, d_px, "100%", "+$0.82",
                               f"g1={g1:.3f} g2={g2:.3f} g3={g3:.3f}")

        # 38. lbounce_t585_r30_f30_rm003_fm006  (100% | n=17 | win +$1.19 | loss $0 | ev +$1.19)
        if 583 <= elapsed <= 590:
            s525 = self._snap_near(525)
            s555 = self._snap_near(555)
            if s525 is not None and s555 is not None:
                loser_side = "Down" if dom == "Up" else "Up"
                l_start = self._side_price(s525, loser_side)
                l_peak = self._side_price(s555, loser_side)
                l_now = self._side_price(snap, loser_side)
                rise = l_peak - l_start
                fall = l_peak - l_now
                if rise >= 0.03 and fall >= 0.06:
                    self._fire("lbounce_t585_r30_f30_rm003_fm006", dom, d_px, "100%", "+$1.19",
                               f"rise={rise:.3f} fall={fall:.3f}")

        # 39. nearpeak_t645_g001  (100% | n=66 | win +$0.30 | loss $0 | ev +$0.30)
        if 643 <= elapsed <= 650:
            dom_all = [self._side_price(s, dom) for s in self._history if s.elapsed <= elapsed]
            if len(dom_all) >= 30:
                peak = max(dom_all)
                if peak >= 0.55 and d_px >= peak - 0.01:
                    self._fire("nearpeak_t645_g001", dom, d_px, "100%", "+$0.30",
                               f"peak={peak:.3f} current={d_px:.3f}")

        # ==============================================================
        # v7 ADDED FROM LATEST SEARCH (42-49)
        # ==============================================================

        # 42. vshape_t600_lb240_b0.12  (84% | n=140 | win +$0.96 | loss -$2.78 | ev +$0.35)
        if 598 <= elapsed <= 605:
            for side in ("Up", "Down"):
                px_min = min(self._side_price(s, side) for s in self._history if 360 <= s.elapsed <= 480)
                px_now = self._side_price(snap, side)
                if px_now - px_min >= 0.12:
                    self._fire("vshape_t600_lb240_b0.12", side, px_now, "84%", "+$0.35",
                               f"v_bounce={px_now - px_min:.3f}")
                    break

        # 43. vshape_t600_lb240_b0.08  (83% | n=148 | win +$0.96 | loss -$2.90 | ev +$0.31)
        if 598 <= elapsed <= 605:
            for side in ("Up", "Down"):
                px_min = min(self._side_price(s, side) for s in self._history if 360 <= s.elapsed <= 480)
                px_now = self._side_price(snap, side)
                if px_now - px_min >= 0.08:
                    self._fire("vshape_t600_lb240_b0.08", side, px_now, "83%", "+$0.31",
                               f"v_bounce={px_now - px_min:.3f}")
                    break

        # 44. rdiv_t600_w180_r0.08_f0.01  (91% | n=98 | win +$0.86 | loss -$3.74 | ev +$0.44)
        if 598 <= elapsed <= 605:
            s420 = self._snap_near(420)
            if s420 is not None:
                loser_side = "Down" if dom == "Up" else "Up"
                dchg = self._side_price(snap, dom) - self._side_price(s420, dom)
                lchg = self._side_price(snap, loser_side) - self._side_price(s420, loser_side)
                if dchg >= 0.08 and lchg <= -0.01:
                    self._fire("rdiv_t600_w180_r0.08_f0.01", dom, d_px, "91%", "+$0.44",
                               f"dom_rise={dchg:.3f} loser_drop={lchg:.3f}")

        # 45. rddrecov_t360_dd0.15_r0.75  (100% | n=14 | win +$1.91 | loss $0 | ev +$1.91)
        if 358 <= elapsed <= 365:
            dom_prices = [self._side_price(s, dom) for s in self._history if s.elapsed <= elapsed]
            if len(dom_prices) >= 30:
                peak = max(dom_prices)
                trough = peak
                peak_hit = False
                for px in dom_prices:
                    if px == peak:
                        peak_hit = True
                    if peak_hit:
                        trough = min(trough, px)
                dd = peak - trough
                if dd >= 0.15:
                    recovery = (d_px - trough) / dd if dd > 0 else 0
                    if recovery >= 0.75:
                        self._fire("rddrecov_t360_dd0.15_r0.75", dom, d_px, "100%", "+$1.91",
                                   f"dd={dd:.3f} recov={recovery:.0%}")

        # 46. rddrecov_t360_dd0.2_r0.75  (100% | n=13 | win +$1.92 | loss $0 | ev +$1.92)
        if 358 <= elapsed <= 365:
            dom_prices = [self._side_price(s, dom) for s in self._history if s.elapsed <= elapsed]
            if len(dom_prices) >= 30:
                peak = max(dom_prices)
                trough = peak
                peak_hit = False
                for px in dom_prices:
                    if px == peak:
                        peak_hit = True
                    if peak_hit:
                        trough = min(trough, px)
                dd = peak - trough
                if dd >= 0.20:
                    recovery = (d_px - trough) / dd if dd > 0 else 0
                    if recovery >= 0.75:
                        self._fire("rddrecov_t360_dd0.2_r0.75", dom, d_px, "100%", "+$1.92",
                                   f"dd={dd:.3f} recov={recovery:.0%}")

        # 47. loserfloor_t495  (100% | n=45 | win +$0.46 | loss $0 | ev +$0.46)
        if 493 <= elapsed <= 500:
            loser_side = "Down" if dom == "Up" else "Up"
            loser_now = self._side_price(snap, loser_side)
            loser_min = min(self._side_price(s, loser_side) for s in self._history if s.elapsed <= elapsed)
            if abs(loser_now - loser_min) < 0.005:
                self._fire("loserfloor_t495", dom, d_px, "100%", "+$0.46",
                           f"loser_now={loser_now:.3f} loser_min={loser_min:.3f}")

        # 48. vshape_t330_lb120_b0.08_c0.85
        if 329 <= elapsed <= 331:
            for side in ("Up", "Down"):
                start = 210
                mid = 270
                segment = [self._side_price(s, side) for s in self._history if start <= s.elapsed <= mid]
                if not segment:
                    continue
                px_min = min(segment)
                px_now = self._side_price(snap, side)
                if px_now <= 0.85 and px_now - px_min >= 0.08:
                    self._fire("vshape_t330_lb120_b0.08_c0.85", side, px_now, "69%", "+$0.39",
                               f"v_bounce={px_now - px_min:.3f}")
                    break

        # 49. vshape_t585_lb240_b0.15_c0.95
        if 583 <= elapsed <= 590:
            for side in ("Up", "Down"):
                start = 345
                mid = 465
                segment = [self._side_price(s, side) for s in self._history if start <= s.elapsed <= mid]
                if not segment:
                    continue
                px_min = min(segment)
                px_now = self._side_price(snap, side)
                if px_now <= 0.95 and px_now - px_min >= 0.15:
                    self._fire("vshape_t585_lb240_b0.15_c0.95", side, px_now, "78%", "+$0.44",
                               f"v_bounce={px_now - px_min:.3f}")
                    break

        # 51. loserdrop_t840_w60_v0.0015
        if 838 <= elapsed <= 845:
            s780 = self._snap_near(780)
            if s780 is not None:
                loser_side = "Down" if dom == "Up" else "Up"
                l_780 = self._side_price(s780, loser_side)
                l_now = self._side_price(snap, loser_side)
                drop_vel = (l_780 - l_now) / 60.0
                if drop_vel >= 0.0015:
                    self._fire("loserdrop_t840_w60_v0.0015", dom, d_px, "100%", "+$0.69",
                               f"drop_vel={drop_vel:.4f}/s")

        # ==============================================================
        # v8 NEW DISCOVERY PATTERNS
        # ==============================================================
        # 67. nf_quietlead_t630_lb60_r0.0006_l0.22_d0.62
        if 628 <= elapsed <= 635:
            btc_rng60 = self._btc_range(elapsed, 60)
            if btc_rng60 is not None and btc_rng60 <= 0.0006 and d_px >= 0.62 and lead >= 0.22:
                self._fire(
                    "nf_quietlead_t630_lb60_r0.0006_l0.22_d0.62",
                    dom,
                    d_px,
                    "90.2%",
                    "+$0.25",
                    f"lead={lead:.3f} btc_rng60={btc_rng60:.4%} d_px={d_px:.3f}",
                )

        # 68. nf_stairhold_t720_b15_n3_m0.03_c0.0006
        if 718 <= elapsed <= 725:
            s705 = self._snap_near(705)
            s690 = self._snap_near(690)
            s675 = self._snap_near(675)
            btc_move45 = self._btc_move(elapsed, 45)
            if s705 is not None and s690 is not None and s675 is not None and btc_move45 is not None:
                if (
                    self._dom_side(s705) == dom
                    and self._dom_side(s690) == dom
                    and self._dom_side(s675) == dom
                    and abs(btc_move45) <= 0.0006
                ):
                    rise = d_px - self._side_price(s675, dom)
                    if rise >= 0.03:
                        self._fire(
                            "nf_stairhold_t720_b15_n3_m0.03_c0.0006",
                            dom,
                            d_px,
                            "96.8%",
                            "+$0.49",
                            f"rise={rise:.3f} btc_move45={btc_move45:.4%}",
                        )

        # 69. nf_breakquiet_t630_pre45_post60_pr0.02_mv0.06_r0.0006
        if 628 <= elapsed <= 635:
            btc_rng60 = self._btc_range(elapsed, 60)
            if btc_rng60 is not None and btc_rng60 <= 0.0006:
                pre_end = self._history[-1].elapsed - 60.0
                pre_start = pre_end - 45.0
                if pre_start >= 0:
                    btc_rng_pre = self._btc_range(pre_end, 45)
                    s570 = self._snap_near(570)
                    if btc_rng_pre is not None and btc_rng_pre <= 0.02 and s570 is not None:
                        rise = d_px - self._side_price(s570, dom)
                        if rise >= 0.06:
                            self._fire(
                                "nf_breakquiet_t630_pre45_post60_pr0.02_mv0.06_r0.0006",
                                dom,
                                d_px,
                                "94.7%",
                                "+$0.59",
                                f"rise={rise:.3f} btc_rng60={btc_rng60:.4%} btc_rng_pre={btc_rng_pre:.4%}",
                            )

        # ==============================================================
        # BTC LAYER PATTERNS (52-60) -- optional, require live BTC feed
        # ==============================================================
        if not self._btc_ready():
            return

        # 52. vshape_t600_lb240_b0.12_btcm240dn0002
        # Mixed layer: strong v-shape works better when BTC has been weak over prior 240s.
        if 598 <= elapsed <= 605:
            btc_m240 = self._btc_move(elapsed, 240)
            if btc_m240 is not None and btc_m240 <= -0.000202:
                for side in ("Up", "Down"):
                    px_min = min(self._side_price(s, side) for s in self._history if 360 <= s.elapsed <= 480)
                    px_now = self._side_price(snap, side)
                    if px_now - px_min >= 0.12:
                        self._fire(
                            "vshape_t600_lb240_b0.12_btcm240dn0002",
                            side,
                            px_now,
                            "91%",
                            "+$0.78",
                            f"v_bounce={px_now - px_min:.3f} btc_m240={btc_m240:.4%}",
                        )
                        break

        # 53. btcagree_t525_lb180_m0.001
        if 523 <= elapsed <= 530 and lead >= 0.05:
            btc_m180 = self._btc_move(elapsed, 180)
            if btc_m180 is not None and abs(btc_m180) >= 0.001:
                btc_side = "Up" if btc_m180 > 0 else "Down"
                if btc_side == dom:
                    self._fire(
                        "btcagree_t525_lb180_m0.001",
                        dom,
                        d_px,
                        "100%",
                        "+$0.55",
                        f"lead={lead:.3f} btc_m180={btc_m180:.4%}",
                    )

        # 54. btcagree_t525_lb180_m0.001_l0.05
        if 523 <= elapsed <= 530 and lead >= 0.05:
            btc_m180 = self._btc_move(elapsed, 180)
            if btc_m180 is not None and abs(btc_m180) >= 0.001:
                btc_side = "Up" if btc_m180 > 0 else "Down"
                if btc_side == dom:
                    self._fire(
                        "btcagree_t525_lb180_m0.001_l0.05",
                        dom,
                        d_px,
                        "100%",
                        "+$0.79",
                        f"lead={lead:.3f} btc_m180={btc_m180:.4%}",
                    )

        # 55. btcsqz_t525_lb90_r0.0006_l0.4
        if 523 <= elapsed <= 530 and lead >= 0.40:
            btc_rng90 = self._btc_range(elapsed, 90)
            if btc_rng90 is not None and btc_rng90 <= 0.0006:
                self._fire(
                    "btcsqz_t525_lb90_r0.0006_l0.4",
                    dom,
                    d_px,
                    "92%",
                    "+$0.42",
                    f"lead={lead:.3f} btc_rng90={btc_rng90:.4%}",
                )

        # 55. btcagree_t525_lb60_m0.0004
        if 523 <= elapsed <= 530:
            btc_m60 = self._btc_move(elapsed, 60)
            if btc_m60 is not None:
                if (dom == "Up" and btc_m60 >= 0.0004) or (dom == "Down" and btc_m60 <= -0.0004):
                    self._fire(
                        "btcagree_t525_lb60_m0.0004",
                        dom,
                        d_px,
                        "90%",
                        "+$0.44",
                        f"lead={lead:.3f} btc_m60={btc_m60:.4%}",
                    )

        # 56. btcbreak_t525_sq60_mv60_r0.0012_m0.0004
        if 523 <= elapsed <= 530:
            btc_m60 = self._btc_move(elapsed, 60)
            if btc_m60 is not None:
                if (dom == "Up" and btc_m60 >= 0.0004) or (dom == "Down" and btc_m60 <= -0.0004):
                    self._fire(
                        "btcbreak_t525_sq60_mv60_r0.0012_m0.0004",
                        dom,
                        d_px,
                        "90%",
                        "+$0.44",
                        f"lead={lead:.3f} btc_m60={btc_m60:.4%}",
                    )

        # 56. btcagree_t795_lb120_m0.0005
        if 793 <= elapsed <= 800 and lead >= 0.05:
            btc_m120 = self._btc_move(elapsed, 120)
            if btc_m120 is not None and abs(btc_m120) >= 0.0005:
                btc_side = "Up" if btc_m120 > 0 else "Down"
                if btc_side == dom:
                    self._fire(
                        "btcagree_t795_lb120_m0.0005",
                        dom,
                        d_px,
                        "100%",
                        "+$0.30",
                        f"lead={lead:.3f} btc_m120={btc_m120:.4%}",
                    )

        # 57. btcbreak_t135_sq90_mv30_r0.0008_m0.0006
        if 133 <= elapsed <= 140:
            btc_rng_pre90 = self._btc_range(elapsed - 30.0, 60.0)
            btc_m30 = self._btc_move(elapsed, 30)
            if (
                btc_rng_pre90 is not None
                and btc_m30 is not None
                and btc_rng_pre90 <= 0.0008
                and ((dom == "Up" and btc_m30 >= 0.0006) or (dom == "Down" and btc_m30 <= -0.0006))
            ):
                self._fire(
                    "btcbreak_t135_sq90_mv30_r0.0008_m0.0006",
                    dom,
                    d_px,
                    "90%",
                    "+$1.17",
                    f"lead={lead:.3f} pre_rng90={btc_rng_pre90:.4%} btc_m30={btc_m30:.4%}",
                )

        # 57. btcbreak_t330_sq120_mv60_r0.0005_m0.0004
        if 328 <= elapsed <= 335:
            btc_m60 = self._btc_move(elapsed, 60)
            btc_rng120 = self._btc_range(elapsed - 60.0, 60.0)
            if (
                btc_m60 is not None
                and btc_rng120 is not None
                and btc_rng120 <= 0.0005
                and ((dom == "Up" and btc_m60 >= 0.0004) or (dom == "Down" and btc_m60 <= -0.0004))
            ):
                self._fire(
                    "btcbreak_t330_sq120_mv60_r0.0005_m0.0004",
                    dom,
                    d_px,
                    "91%",
                    "+$0.70",
                    f"lead={lead:.3f} pre_rng120={btc_rng120:.4%} btc_m60={btc_m60:.4%}",
                )

        # 58. btcbreak_t600_sq30_mv45_r0.0006_m0.0004
        if 598 <= elapsed <= 605 and lead >= 0.05:
            btc_rng30 = self._btc_range(elapsed, 30)
            btc_m45 = self._btc_move(elapsed, 45)
            if (
                btc_rng30 is not None
                and btc_m45 is not None
                and btc_rng30 <= 0.0006
                and abs(btc_m45) >= 0.0004
            ):
                btc_side = "Up" if btc_m45 > 0 else "Down"
                if btc_side == dom:
                    self._fire(
                        "btcbreak_t600_sq30_mv45_r0.0006_m0.0004",
                        dom,
                        d_px,
                        "100%",
                        "+$0.66",
                        f"lead={lead:.3f} btc_rng30={btc_rng30:.4%} btc_m45={btc_m45:.4%}",
                    )

        # 58. btcbreak_t645_sq90_mv30_r0.0005_m0.0003
        if 643 <= elapsed <= 650:
            btc_rng_pre90 = self._btc_range(elapsed - 30.0, 60.0)
            btc_m30 = self._btc_move(elapsed, 30)
            if (
                btc_rng_pre90 is not None
                and btc_m30 is not None
                and btc_rng_pre90 <= 0.0005
                and ((dom == "Up" and btc_m30 >= 0.0003) or (dom == "Down" and btc_m30 <= -0.0003))
            ):
                self._fire(
                    "btcbreak_t645_sq90_mv30_r0.0005_m0.0003",
                    dom,
                    d_px,
                    "100%",
                    "+$0.86",
                    f"lead={lead:.3f} pre_rng90={btc_rng_pre90:.4%} btc_m30={btc_m30:.4%}",
                )

        # 59. btcsqz_t690_lb30_r0.0006_l0.12
        if 688 <= elapsed <= 695 and lead >= 0.12:
            btc_rng30 = self._btc_range(elapsed, 30)
            if btc_rng30 is not None and btc_rng30 <= 0.0006:
                self._fire(
                    "btcsqz_t690_lb30_r0.0006_l0.12",
                    dom,
                    d_px,
                    "94%",
                    "+$0.30",
                    f"lead={lead:.3f} btc_rng30={btc_rng30:.4%}",
                )

        # 60. btcrev_t375_lb180_r0.0016
        if 374 <= elapsed <= 376 and lead >= 0.05:
            btc_rebound180 = self._btc_rebound(elapsed, 180, dom)
            if btc_rebound180 is not None and btc_rebound180 >= 0.0016:
                self._fire(
                    "btcrev_t375_lb180_r0.0016",
                    dom,
                    d_px,
                    "90%",
                    "+$0.57",
                    f"lead={lead:.3f} btc_rebound180={btc_rebound180:.4%}",
                )

        # 60. btcrev_t510_lb180_r0.002
        if 508 <= elapsed <= 515 and lead >= 0.05:
            btc_rebound180 = self._btc_rebound(elapsed, 180, dom)
            if btc_rebound180 is not None and btc_rebound180 >= 0.002:
                self._fire(
                    "btcrev_t510_lb180_r0.002",
                    dom,
                    d_px,
                    "100%",
                    "+$0.55",
                    f"lead={lead:.3f} btc_rebound180={btc_rebound180:.4%}",
                )

        # 61. btcrev_t570_lb180_r0.0008
        if 568 <= elapsed <= 575 and lead >= 0.05:
            btc_rebound180 = self._btc_rebound(elapsed, 180, dom)
            if btc_rebound180 is not None and btc_rebound180 >= 0.0008:
                self._fire(
                    "btcrev_t570_lb180_r0.0008",
                    dom,
                    d_px,
                    "90%",
                    "+$0.28",
                    f"lead={lead:.3f} btc_rebound180={btc_rebound180:.4%}",
                )

        # 62. btcrev_t585_lb180_r0.0005
        if 583 <= elapsed <= 590 and lead >= 0.05:
            btc_rebound180 = self._btc_rebound(elapsed, 180, dom)
            if btc_rebound180 is not None and btc_rebound180 >= 0.0005:
                self._fire(
                    "btcrev_t585_lb180_r0.0005",
                    dom,
                    d_px,
                    "92%",
                    "+$0.40",
                    f"lead={lead:.3f} btc_rebound180={btc_rebound180:.4%}",
                )

        # 63. btcsqz_t495_lb60_r0.0005_l0.4
        if 494 <= elapsed <= 496 and lead >= 0.40:
            btc_rng60 = self._btc_range(elapsed, 60)
            if btc_rng60 is not None and btc_rng60 <= 0.0005:
                self._fire(
                    "btcsqz_t495_lb60_r0.0005_l0.4",
                    dom,
                    d_px,
                    "90%",
                    "+$0.32",
                    f"lead={lead:.3f} btc_rng60={btc_rng60:.4%}",
                )

        # 64. btcsqz_t510_lb120_r0.0006_l0.4
        if 508 <= elapsed <= 515 and lead >= 0.40:
            btc_rng120 = self._btc_range(elapsed, 120)
            if btc_rng120 is not None and btc_rng120 <= 0.0006:
                self._fire(
                    "btcsqz_t510_lb120_r0.0006_l0.4",
                    dom,
                    d_px,
                    "94%",
                    "+$0.49",
                    f"lead={lead:.3f} btc_rng120={btc_rng120:.4%}",
                )

        # 65. btcsqz_t630_lb75_r0.0005_l0.35
        if 628 <= elapsed <= 635 and lead >= 0.35:
            btc_rng75 = self._btc_range(elapsed, 75)
            if btc_rng75 is not None and btc_rng75 <= 0.0005:
                self._fire(
                    "btcsqz_t630_lb75_r0.0005_l0.35",
                    dom,
                    d_px,
                    "90%",
                    "+$0.43",
                    f"lead={lead:.3f} btc_rng75={btc_rng75:.4%}",
                )

        # 64. btcsqz_t645_lb45_r0.0006_l0.12
        if 643 <= elapsed <= 650 and lead >= 0.12:
            btc_rng45 = self._btc_range(elapsed, 45)
            if btc_rng45 is not None and btc_rng45 <= 0.0006:
                self._fire(
                    "btcsqz_t645_lb45_r0.0006_l0.12",
                    dom,
                    d_px,
                    "90%",
                    "+$0.28",
                    f"lead={lead:.3f} btc_rng45={btc_rng45:.4%}",
                )

        # 65. btcsqz_t645_lb90_r0.0006_l0.18
        if 643 <= elapsed <= 650 and lead >= 0.18:
            btc_rng90 = self._btc_range(elapsed, 90)
            if btc_rng90 is not None and btc_rng90 <= 0.0006:
                self._fire(
                    "btcsqz_t645_lb90_r0.0006_l0.18",
                    dom,
                    d_px,
                    "90%",
                    "+$0.47",
                    f"lead={lead:.3f} btc_rng90={btc_rng90:.4%}",
                )

        # 66. btcsqz_t645_lb90_r0.0006_l0.4
        if 643 <= elapsed <= 650 and lead >= 0.40:
            btc_rng90 = self._btc_range(elapsed, 90)
            if btc_rng90 is not None and btc_rng90 <= 0.0006:
                self._fire(
                    "btcsqz_t645_lb90_r0.0006_l0.4",
                    dom,
                    d_px,
                    "92%",
                    "+$0.39",
                    f"lead={lead:.3f} btc_rng90={btc_rng90:.4%}",
                )

        # 66. btcsqz_t720_lb45_r0.0012_l0.3
        if 718 <= elapsed <= 725 and lead >= 0.30:
            btc_rng45 = self._btc_range(elapsed, 45)
            if btc_rng45 is not None and btc_rng45 <= 0.0012:
                self._fire(
                    "btcsqz_t720_lb45_r0.0012_l0.3",
                    dom,
                    d_px,
                    "96%",
                    "+$0.25",
                    f"lead={lead:.3f} btc_rng45={btc_rng45:.4%}",
                )

        # 67. btcsqz_t720_lb75_r0.0016_l0.2
        if 718 <= elapsed <= 725 and lead >= 0.20:
            btc_rng75 = self._btc_range(elapsed, 75)
            if btc_rng75 is not None and btc_rng75 <= 0.0016:
                self._fire(
                    "btcsqz_t720_lb75_r0.0016_l0.2",
                    dom,
                    d_px,
                    "94%",
                    "+$0.24",
                    f"lead={lead:.3f} btc_rng75={btc_rng75:.4%}",
                )

        # 68. btcsqz_t720_lb60_r0.0008_l0.35
        if 718 <= elapsed <= 725 and lead >= 0.35:
            btc_rng60 = self._btc_range(elapsed, 60)
            if btc_rng60 is not None and btc_rng60 <= 0.0008:
                self._fire(
                    "btcsqz_t720_lb60_r0.0008_l0.35",
                    dom,
                    d_px,
                    "91%",
                    "+$0.36",
                    f"lead={lead:.3f} btc_rng60={btc_rng60:.4%}",
                )

        # 69. rn_ratioexpand_t480_lb60_r1.25_rg0.1_bc0.0016_dc0.84
        if 478 <= elapsed <= 485 and d_px <= 0.84 and l_px > 0.01:
            old = self._snap_near(420, tolerance=25.0)
            btc_rng60 = self._btc_range(elapsed, 60)
            ratio_now = d_px / l_px
            if old is not None and btc_rng60 is not None and btc_rng60 <= 0.0016:
                old_loser = self._loser_price(old)
                if old_loser > 0.01:
                    old_ratio = self._dom_price(old) / old_loser
                    if ratio_now >= 1.25 and (ratio_now - old_ratio) >= 0.10:
                        self._fire(
                            "rn_ratioexpand_t480_lb60_r1.25_rg0.1_bc0.0016_dc0.84",
                            dom,
                            d_px,
                            "88%",
                            "+$0.88",
                            f"ratio={ratio_now:.2f} gain={ratio_now - old_ratio:.2f} btc_rng60={btc_rng60:.4%}",
                        )

        # 70. rn_grindtrend_t495_b15_n2_dr0.05_lf-0.01_bc0.0016_ra1.4
        if 493 <= elapsed <= 500 and l_px > 0.01:
            streak = self._dom_streak_seconds(dom)
            btc_m30 = self._btc_move(elapsed, 30)
            dom_m30 = self._dom_move(elapsed - 30.0, elapsed)
            old = self._snap_near(elapsed - 30.0, tolerance=15.0)
            ratio_now = d_px / l_px
            if (
                streak >= 30.0
                and btc_m30 is not None
                and abs(btc_m30) <= 0.0016
                and dom_m30 is not None
                and dom_m30 >= 0.05
                and ratio_now >= 1.4
                and old is not None
            ):
                loser_side = "Down" if dom == "Up" else "Up"
                loser_start = self._side_price(old, loser_side)
                loser_end = self._side_price(snap, loser_side)
                if loser_start > 0:
                    loser_fade = (loser_end - loser_start) / loser_start
                    if loser_fade <= -0.01:
                        self._fire(
                            "rn_grindtrend_t495_b15_n2_dr0.05_lf-0.01_bc0.0016_ra1.4",
                            dom,
                            d_px,
                            "93%",
                            "+$0.75",
                            f"streak={streak:.0f}s dom_m30={dom_m30:.3f} loser_fade={loser_fade:.2%} ratio={ratio_now:.2f}",
                        )

        # 71. btcacc_t600_lb20_a0.0002_d0.004
        if 598 <= elapsed <= 605:
            btc_m1 = self._btc_move(elapsed - 20.0, 20)
            btc_m2 = self._btc_move(elapsed, 20)
            dom_m20 = self._dom_move(elapsed - 20.0, elapsed)
            if (
                btc_m1 is not None
                and btc_m2 is not None
                and dom_m20 is not None
                and abs(btc_m2) - abs(btc_m1) >= 0.0002
                and abs(dom_m20) / 20.0 >= 0.004
            ):
                self._fire(
                    "btcacc_t600_lb20_a0.0002_d0.004",
                    dom,
                    d_px,
                    "93%",
                    "+$1.22",
                    f"lead={lead:.3f} btc_acc={(abs(btc_m2) - abs(btc_m1)):.4%} dom_v={abs(dom_m20)/20.0:.4f}",
                )

        # 69. btcdiv_t480_lb90_b0.0008_c0.8
        if 478 <= elapsed <= 485 and d_px <= 0.80:
            btc_m90 = self._btc_move(elapsed, 90)
            if btc_m90 is not None:
                if (dom == "Up" and btc_m90 >= 0.0008) or (dom == "Down" and btc_m90 <= -0.0008):
                    self._fire(
                        "btcdiv_t480_lb90_b0.0008_c0.8",
                        dom,
                        d_px,
                        "95%",
                        "+$1.33",
                        f"lead={lead:.3f} btc_m90={btc_m90:.4%}",
                    )

        # 70. btcreversal_t345_e120_l45_m0.0006_r0.0003
        if 343 <= elapsed <= 350:
            early = self._btc_move(elapsed - 45.0, 120)
            late = self._btc_move(elapsed, 45)
            if early is not None and late is not None:
                if (dom == "Up" and early <= -0.0006 and late >= 0.0003) or (
                    dom == "Down" and early >= 0.0006 and late <= -0.0003
                ):
                    self._fire(
                        "btcreversal_t345_e120_l45_m0.0006_r0.0003",
                        dom,
                        d_px,
                        "92%",
                        "+$1.18",
                        f"lead={lead:.3f} btc_early={early:.4%} btc_late={late:.4%}",
                    )

        # 71. mix_vshape_t585_lb240_b0.12_br120_0.0016
        if 583 <= elapsed <= 590:
            btc_rng120 = self._btc_range(elapsed, 120)
            if btc_rng120 is not None and btc_rng120 <= 0.0016:
                for side in ("Up", "Down"):
                    segment = [self._side_price(s, side) for s in self._history if 345 <= s.elapsed <= 465]
                    if not segment:
                        continue
                    px_min = min(segment)
                    px_now = self._side_price(snap, side)
                    if px_now - px_min >= 0.12:
                        self._fire(
                            "mix_vshape_t585_lb240_b0.12_br120_0.0016",
                            side,
                            px_now,
                            "79%",
                            "+$0.32",
                            f"v_bounce={px_now - px_min:.3f} btc_rng120={btc_rng120:.4%}",
                        )
                        break

        # 61. mix_loserdrop_t750_w20_v0.0015_br60_0.0005
        if 748 <= elapsed <= 755:
            btc_rng60 = self._btc_range(elapsed, 60)
            s730 = self._snap_near(730)
            if btc_rng60 is not None and btc_rng60 <= 0.0005 and s730 is not None:
                loser_side = "Down" if dom == "Up" else "Up"
                l_730 = self._side_price(s730, loser_side)
                l_now = self._side_price(snap, loser_side)
                drop_vel = (l_730 - l_now) / 20.0
                if drop_vel >= 0.0015:
                    self._fire(
                        "mix_loserdrop_t750_w20_v0.0015_br60_0.0005",
                        dom,
                        d_px,
                        "100%",
                        "+$1.01",
                        f"drop_vel={drop_vel:.4f}/s btc_rng60={btc_rng60:.4%}",
                    )

        # 62. mix_loserdrop_t690_w30_v0.002_br60_0.0008
        if 688 <= elapsed <= 695:
            btc_rng60 = self._btc_range(elapsed, 60)
            s660 = self._snap_near(660)
            if btc_rng60 is not None and btc_rng60 <= 0.0008 and s660 is not None:
                loser_side = "Down" if dom == "Up" else "Up"
                l_660 = self._side_price(s660, loser_side)
                l_now = self._side_price(snap, loser_side)
                drop_vel = (l_660 - l_now) / 30.0
                if drop_vel >= 0.002:
                    self._fire(
                        "mix_loserdrop_t690_w30_v0.002_br60_0.0008",
                        dom,
                        d_px,
                        "100%",
                        "+$0.99",
                        f"drop_vel={drop_vel:.4f}/s btc_rng60={btc_rng60:.4%}",
                    )
