#!/usr/bin/env python3
"""BTC 15-minute single-window redeem-hold strategy."""

from __future__ import annotations

import json
import signal
import math
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from config import (
    LOGGER,
    ActiveContract,
    BotConfig,
    TokenMarket,
    append_window_balance_snapshot,
    setup_file_logger,
)
from btc_price_feed import BtcPricePoint, RealtimeBtcPriceFeed
from market_locator import GammaMarketLocator
from trader import PolymarketTrader

_REPO_ROOT = Path(__file__).resolve().parent
MIMIC_PARAMS_JSON = _REPO_ROOT / "exports" / "wallet10_mimic_search.json"
IY2_PARAMS_JSON = _REPO_ROOT / "strategy_params" / "iy2_summary.json"
IY3_PARAMS_JSON = _REPO_ROOT / "strategy_params" / "iy3_summary.json"
AA1_STRATEGY_PROFILE_ID = "AA1_deep_v1_m42_d03_cd15_ml8_c30_tp97"
STRATEGY_0_PROFILE_ID = "STRATEGY_0_current_v1"
STRATEGY_0_META_PROFILE_ID = "STRATEGY_0_meta_public_v4_delay12_wr739_pnl1386"
MIMIC_STRATEGY_PROFILE_ID = "MIMIC_wallet10_fixed_lot5_v1"
IY2_STRATEGY_PROFILE_ID = "IY2_wallet_overlap_v1"
IY3_STRATEGY_PROFILE_ID = "IY3_wallet_overlap_path_v1"
IY2_MAX_IMBALANCE_SHARES = 5
WD_STRATEGY_PROFILE_ID = "WD_wallet_strict_v1"
VOLUME_T10_STRATEGY_PROFILE_ID = "BTC_VOLUME_T10_dual_v1"
VOLUME_T10_HYBRID_STRATEGY_PROFILE_ID = "BTC_VOLUME_T10_hybrid_v2"
VOLUME_SCALP_UP_PROFILE_ID = "BTC_VOLUME_SCALP_UP_v2"
BTC_PERP15_PROFILE_ID = "BTC_PERP15_UP_LADDER_v3"
STRATEGY_PROFILE_ID = BTC_PERP15_PROFILE_ID


def _is_fak_no_match_order_error(exc: BaseException) -> bool:
    """True when CLOB rejected a FAK buy because nothing crossed (retry / GTC fallback)."""
    msg = str(exc).lower()
    if "no orders found to match" in msg and "fak" in msg:
        return True
    if "fak orders are partially filled or killed" in msg:
        return True
    return False


def _load_mimic_search_params(path: Path) -> dict[str, float] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("best", {}).get("params")
        if isinstance(raw, dict) and raw:
            return raw  # type: ignore[return-value]
    except (OSError, json.JSONDecodeError, TypeError, KeyError):
        pass
    return None


def _load_best_params_json(path: Path) -> dict[str, float] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("best_params")
        if isinstance(raw, dict) and raw:
            merged = dict(raw)
            for key in (
                "bucket_targets",
                "freeze_roi",
                "improvement_min",
                "cheap_buffer",
                "candidate_share_cap",
                "same_side_dedupe_seconds",
                "same_side_dedupe_price_band",
                "enable_target_prop_override",
                "path_spend_scale",
                "extreme_cheap_max_price",
            ):
                if key in data:
                    merged[key] = data[key]
            return merged  # type: ignore[return-value]
    except (OSError, json.JSONDecodeError, TypeError, KeyError):
        pass
    return None


PRIMARY_PRICE_MIN = 0.01
PRIMARY_PRICE_MAX = 0.98
PRIMARY_PRICE_SOFT_MAX = 0.98
PRIMARY_PRICE_HARD_MAX = 0.99
HEDGE_MAX_PRICE = 0.99
LATE_HEDGE_MAX_PRICE = 0.99
AA1_CHEAP_ENTRY_MAX = 0.42
AA1_CHEAP_REPEAT_DROP = 0.03
AA1_CHEAP_COOLDOWN_SECONDS = 15
AA1_MAX_CHEAP_LOTS_PER_SIDE = 8
AA1_MAX_OPEN_IMBALANCE_ORDERS = 2
# Named profile "strategy_0" / simulate_current_strategy_price_history primary+hedge path.
S0_PRIMARY_PRICE_MIN = 0.45
S0_PRIMARY_PRICE_MAX = 0.75
S0_PRIMARY_PRICE_SOFT_MAX = 0.82
S0_PRIMARY_PRICE_HARD_MAX = 0.85
S0_HEDGE_MAX_PRICE = 0.35
S0_LATE_HEDGE_MAX_PRICE = 0.45
S0_PRIMARY_TARGET_SHARE = 0.67
S0_HEDGE_TARGET_SHARE = 0.33
S0_TARGET_DIRECTIONAL_RATIO = 1.10
S0_TARGET_GUARANTEE_RATIO = 1.00
S0_LATE_REPAIR_SECONDS = 120
S0_LATE_TREND_START_SECONDS = 420
S0_LATE_TREND_TARGET_SHARE = 0.58
S0_LATE_TREND_HEDGE_MAX_PRICE = 0.32
S0_LATE_TREND_MIN_WIN_PNL = 2.0
S0_META_DECISION_DELAY_SECONDS = 180
S0_META_BASELINE = {
    "label": "baseline",
    "primary_price_min": S0_PRIMARY_PRICE_MIN,
    "primary_price_max": S0_PRIMARY_PRICE_MAX,
    "primary_price_soft_max": S0_PRIMARY_PRICE_SOFT_MAX,
    "primary_price_hard_max": S0_PRIMARY_PRICE_HARD_MAX,
    "hedge_max_price": S0_HEDGE_MAX_PRICE,
    "late_hedge_max_price": S0_LATE_HEDGE_MAX_PRICE,
    "primary_target_share": S0_PRIMARY_TARGET_SHARE,
    "hedge_target_share": S0_HEDGE_TARGET_SHARE,
    "target_directional_ratio": S0_TARGET_DIRECTIONAL_RATIO,
    "target_guarantee_ratio": S0_TARGET_GUARANTEE_RATIO,
    "late_repair_seconds": S0_LATE_REPAIR_SECONDS,
    "late_trend_start_seconds": S0_LATE_TREND_START_SECONDS,
    "late_trend_target_share": S0_LATE_TREND_TARGET_SHARE,
    "late_trend_hedge_max_price": S0_LATE_TREND_HEDGE_MAX_PRICE,
    "late_trend_min_win_pnl": S0_LATE_TREND_MIN_WIN_PNL,
    "primary_flip_threshold": 0.03,
}
S0_META_LOCAL_172 = {
    "label": "local_172",
    "primary_price_min": 0.42,
    "primary_price_max": 0.75,
    "primary_price_soft_max": 0.78,
    "primary_price_hard_max": 0.88,
    "hedge_max_price": 0.22,
    "late_hedge_max_price": 0.45,
    "primary_target_share": 0.67,
    "hedge_target_share": 0.36,
    "target_directional_ratio": 1.00,
    "target_guarantee_ratio": 0.84,
    "late_repair_seconds": 120,
    "late_trend_start_seconds": 360,
    "late_trend_target_share": 0.64,
    "late_trend_hedge_max_price": 0.22,
    "late_trend_min_win_pnl": 2.0,
    "primary_flip_threshold": 0.10,
}
S0_META_LOCAL_153 = {
    "label": "local_153",
    "primary_price_min": 0.45,
    "primary_price_max": 0.72,
    "primary_price_soft_max": 0.82,
    "primary_price_hard_max": 0.84,
    "hedge_max_price": 0.22,
    "late_hedge_max_price": 0.40,
    "primary_target_share": 0.64,
    "hedge_target_share": 0.28,
    "target_directional_ratio": 1.04,
    "target_guarantee_ratio": 0.84,
    "late_repair_seconds": 120,
    "late_trend_start_seconds": 330,
    "late_trend_target_share": 0.64,
    "late_trend_hedge_max_price": 0.26,
    "late_trend_min_win_pnl": 1.0,
    "primary_flip_threshold": 0.06,
}
S0_META_FILTERED = {
    "label": "filtered",
    "primary_price_min": 0.45,
    "primary_price_max": 0.70,
    "primary_price_soft_max": 0.78,
    "primary_price_hard_max": 0.84,
    "hedge_max_price": 0.22,
    "late_hedge_max_price": 0.40,
    "primary_target_share": 0.70,
    "hedge_target_share": 0.30,
    "target_directional_ratio": 1.00,
    "target_guarantee_ratio": 0.92,
    "late_repair_seconds": 150,
    "late_trend_start_seconds": 450,
    "late_trend_target_share": 0.68,
    "late_trend_hedge_max_price": 0.24,
    "late_trend_min_win_pnl": 1.5,
    "primary_flip_threshold": 0.08,
}
S0_META_H5 = {
    "label": "H5",
    "primary_price_min": 0.48,
    "primary_price_max": 0.68,
    "primary_price_soft_max": 0.78,
    "primary_price_hard_max": 0.88,
    "hedge_max_price": 0.26,
    "late_hedge_max_price": 0.45,
    "primary_target_share": 0.72,
    "hedge_target_share": 0.30,
    "target_directional_ratio": 1.14,
    "target_guarantee_ratio": 0.96,
    "late_repair_seconds": 120,
    "late_trend_start_seconds": 360,
    "late_trend_target_share": 0.68,
    "late_trend_hedge_max_price": 0.32,
    "late_trend_min_win_pnl": 2.0,
    "primary_flip_threshold": 0.03,
}
S0_META_ROBUST = {
    "label": "robust",
    "primary_price_min": 0.42,
    "primary_price_max": 0.70,
    "primary_price_soft_max": 0.78,
    "primary_price_hard_max": 0.82,
    "hedge_max_price": 0.28,
    "late_hedge_max_price": 0.40,
    "primary_target_share": 0.64,
    "hedge_target_share": 0.36,
    "target_directional_ratio": 1.08,
    "target_guarantee_ratio": 0.88,
    "late_repair_seconds": 150,
    "late_trend_start_seconds": 360,
    "late_trend_target_share": 0.64,
    "late_trend_hedge_max_price": 0.26,
    "late_trend_min_win_pnl": 3.0,
    "primary_flip_threshold": 0.10,
}
S0_META_DEEP064 = {
    "label": "deep064",
    "primary_price_min": 0.42,
    "primary_price_max": 0.70,
    "primary_price_soft_max": 0.78,
    "primary_price_hard_max": 0.82,
    "hedge_max_price": 0.28,
    "late_hedge_max_price": 0.38,
    "primary_target_share": 0.64,
    "hedge_target_share": 0.33,
    "target_directional_ratio": 1.08,
    "target_guarantee_ratio": 0.92,
    "late_repair_seconds": 180,
    "late_trend_start_seconds": 450,
    "late_trend_target_share": 0.68,
    "late_trend_hedge_max_price": 0.24,
    "late_trend_min_win_pnl": 3.0,
    "primary_flip_threshold": 0.10,
}
S0_META_H2 = {
    "label": "H2",
    "primary_price_min": 0.40,
    "primary_price_max": 0.75,
    "primary_price_soft_max": 0.78,
    "primary_price_hard_max": 0.88,
    "hedge_max_price": 0.24,
    "late_hedge_max_price": 0.36,
    "primary_target_share": 0.62,
    "hedge_target_share": 0.40,
    "target_directional_ratio": 1.18,
    "target_guarantee_ratio": 0.88,
    "late_repair_seconds": 90,
    "late_trend_start_seconds": 360,
    "late_trend_target_share": 0.68,
    "late_trend_hedge_max_price": 0.34,
    "late_trend_min_win_pnl": 2.0,
    "primary_flip_threshold": 0.05,
}
S0_META_H1 = {
    "label": "H1",
    "primary_price_min": 0.48,
    "primary_price_max": 0.75,
    "primary_price_soft_max": 0.84,
    "primary_price_hard_max": 0.84,
    "hedge_max_price": 0.35,
    "late_hedge_max_price": 0.45,
    "primary_target_share": 0.62,
    "hedge_target_share": 0.30,
    "target_directional_ratio": 1.18,
    "target_guarantee_ratio": 0.88,
    "late_repair_seconds": 60,
    "late_trend_start_seconds": 330,
    "late_trend_target_share": 0.56,
    "late_trend_hedge_max_price": 0.34,
    "late_trend_min_win_pnl": 1.5,
    "primary_flip_threshold": 0.03,
}
WD_DECISION_DELAY_SECONDS = 180
WD_FILTER_MAX_EARLY_FLIPS_180 = 3
WD_FILTER_MAX_EARLY_LEAD_MAX_180 = 0.28
WD_PROFILE = {
    "phase_caps_name": "wallet_late",
    "entry_delay_seconds": 35,
    "late_trend_start_seconds": 390,
    "late_trend_clear_edge": 0.10,
    "primary_price_min": 0.40,
    "primary_price_max": 0.68,
    "primary_price_soft_max": 0.80,
    "primary_price_hard_max": 0.84,
    "hedge_max_price": 0.22,
    "late_hedge_max_price": 0.36,
    "primary_target_share": 0.60,
    "hedge_target_share": 0.28,
    "target_directional_ratio": 1.10,
    "target_guarantee_ratio": 0.80,
    "late_repair_seconds": 180,
    "late_trend_target_share": 0.72,
    "late_trend_hedge_max_price": 0.30,
    "late_trend_min_win_pnl": 0.50,
    "primary_flip_threshold": 0.05,
}
VOLUME_ENTRY_MIN_ELAPSED_SECONDS = 60.0
VOLUME_ENTRY_MAX_ELAPSED_SECONDS = 600.0
VOLUME_AVG_LOOKBACK_SECONDS = 30
VOLUME_RATIO_THRESHOLD = 2.5
VOLUME_ENTRY_MIN_PRICE = 0.05
VOLUME_ENTRY_MAX_PRICE = 0.90
# Treat smaller deltas as flat so dust after TP does not block the next cycle forever.
VOLUME_T10_SERIAL_INVENTORY_EPS = 0.02
VOLUME_SCALP_ENTRY_MAX_PRICE = 0.80
T10_ENTRY_START_SECONDS_REMAINING = 20.0
T10_ENTRY_END_SECONDS_REMAINING = 3.0
T10_MIN_WINDOW_ELAPSED_SECONDS = 120.0
T10_MIN_BTC_DELTA = 0.0005
T10_PAIR_SUM_TARGET = 0.98
T10_ENTRY_MIN_PRICE = 0.05
T10_ENTRY_MAX_PRICE = 0.95
POSITION_MAX_PCT = 0.20
POLY_MIN_LIMIT_SHARES = 5
MAKER_FEE_RATE_BPS = 50
MAKER_PRICE_TICK = 0.01
VOLUME_T10_TP_START_SECONDS_REMAINING = 60.0
LATE_REPAIR_SECONDS = 120
LATE_TREND_START_SECONDS = 300
LATE_TREND_CLEAR_PRICE = 0.55
LATE_TREND_CLEAR_EDGE = 0.12
LATE_TREND_LOCK_REVERSAL_PRICE = 0.60
LATE_TREND_LOCK_REVERSAL_EDGE = 0.14
LATE_TREND_HEDGE_MAX_PRICE = 0.28
LATE_TREND_TARGET_SHARE = 0.66
LATE_TREND_MIN_WIN_PNL = 1.5
PRICE_HISTORY_RETENTION_SECONDS = 120
BTC_PRICE_HISTORY_RETENTION_SECONDS = 960
TP_PRICE = 0.99
EARLY_EXIT_WIN_PRICE = 0.99
EARLY_EXIT_LOSE_PRICE = 0.01
HEDGE_SALVAGE_MIN_PRICE = 0.03
HEDGE_SALVAGE_BUFFER = 0.02
MIN_MARKETABLE_BUY_NOTIONAL = 1.00
TOKEN_SHARE_RAW_UNIT = 1_000_000
LEFTOVER_CLEANUP_PRICE = 0.98
LEFTOVER_CLEANUP_BALANCE_MAX = 5.0
LEFTOVER_CLEANUP_START_SECONDS_REMAINING = 300.0
LEFTOVER_CLEANUP_INTERVAL_SECONDS = 5.0
# Flatten odd-lot window position: 0 < token_delta vs window baseline < 2 sh → FAK sell (per-side throttle below).
SUB_TWO_SHARE_MAX_EXCLUSIVE = 2.0
SUB_TWO_SHARE_MARKET_CHECK_SECONDS = 5.0
# btc_perp15: min seconds between end-window market dump attempts per side (avoid spam).
BTC_PERP15_END_DUMP_THROTTLE_SECONDS = 2.0
EXIT_RECONCILE_INTERVAL_SECONDS = 30.0
HOLD_TO_REDEEM_RELEASE_SECONDS = 30.0
# "current" phase ramp (same as run_named_profiles strategy_0 / price-history replay).
PHASE_SPEND_CAPS = (
    (60, 0.05),
    (180, 0.15),
    (420, 0.30),
    (720, 0.65),
    (840, 1.00),
    (900, 1.00),
)

# Twin-style box: two-sided, balance, stop when UP-win and DOWN-win PnL both positive; slow 5-share scaling.
BOX_STRATEGY_PROFILE_ID = "BOX_balance_both_ways_v1"
CHAMP4_6S_PROFILE_ID = "CHAMP4_6S_wallet_dual_live_v1"
BOX_BOTH_WAYS_MIN_PNL_USDC = 0.25
BOX_MAX_LOTS_PER_SIDE = 10
BOX_MAX_OPEN_IMBALANCE_ORDERS = 2
BOX_SIDE_COOLDOWN_SECONDS = 18
BOX_BALANCE_COOLDOWN_SECONDS = 5
BOX_PAIR_MIN = 0.97
BOX_PAIR_MAX = 1.03
BOX_PAIR_SPREAD_MAX = 0.18
# Wider band for first leg (empty book) so we actually participate most windows.
BOX_OPEN_PAIR_MIN = 0.88
BOX_OPEN_PAIR_MAX = 1.12
BOX_OPEN_SPREAD_MAX = 0.52
# If the market stays choppy vs tight pair, still open before half the window is gone.
BOX_OPEN_FALLBACK_ELAPSED_SECONDS = 40.0
BOX_OPEN_FALLBACK_PAIR_MAX = 1.18
BOX_OPEN_FALLBACK_SPREAD_MAX = 0.62
BOX_OPEN_PATIENCE_ELAPSED_SECONDS = 90.0
BOX_OPEN_PATIENCE_PAIR_MAX = 1.35
BOX_OPEN_PATIENCE_SPREAD_MAX = 0.75
BOX_SECOND_LEG_PAIR_MAX = 1.14
BOX_SECOND_LEG_SPREAD_MAX = 0.50
BOX_WINNER_SPREAD_MIN = 0.10
BOX_WINNER_PRICE_MIN = 0.40
BOX_WINNER_PRICE_MAX = 0.90
BOX_LOSER_PRICE_MAX = 0.44
BOX_LOSER_SPREAD_MIN = 0.08
S0_PAIR_AVG_MAX = 0.95

CHAMP4_ENTRY_DELAY_SECONDS = 24
CHAMP4_MID1_DELAY_SECONDS = 90
CHAMP4_MID2_DELAY_SECONDS = 540
CHAMP4_LATE_DELAY_SECONDS = 600
CHAMP4_LATE_SPREAD_MIN = 0.12


@dataclass(slots=True)
class ManagedOrder:
    order_id: str
    side_label: str
    kind: str
    price: float
    shares: int
    placed_at: float
    reason: str

    @property
    def notional(self) -> float:
        """Planned outlay at order limit price × shares — not venue fill proceeds (do not use as fill avg)."""
        return self.price * self.shares


@dataclass(slots=True)
class ManagedExitOrder:
    order_id: str
    side_label: str
    purpose: str
    price: float
    shares: int
    placed_at: float
    cancel_requested_at: float | None = None


@dataclass(slots=True)
class PricePoint:
    ts: float
    up_price: float
    down_price: float


@dataclass(slots=True)
class AA1CheapLot:
    cheap_side: str
    cheap_price: float
    elapsed_sec: float


@dataclass(slots=True)
class OrderCandidate:
    side_label: str
    kind: str
    reference_price: float
    limit_ceiling: float
    reason: str
    shares: int
    min_shares: int = 1
    post_only: bool = False
    fee_rate_bps: int | None = None
    strategy_tag: str = ""
    execution_style: str = "normal"


@dataclass(slots=True)
class BookSnapshot:
    up_shares: int
    down_shares: int
    up_spend: float
    down_spend: float
    pending_notional: float
    total_spend: float
    committed_notional: float
    up_avg_price: float
    down_avg_price: float
    pair_avg_sum: float
    guarantee_ratio: float
    directional_ratio: float
    primary_side: str
    primary_price: float
    primary_score: float
    hedge_side: str
    hedge_price: float
    hedge_score: float
    score_edge: float
    primary_spend_share: float
    hedge_spend_share: float
    primary_coverage: float
    hedge_coverage: float
    up_pnl_if_win: float
    down_pnl_if_win: float
    best_case_pnl: float
    worst_case_loss: float


class Btc15RedeemEngine:
    """One-window BTC 15m strategy: buy, hold, and stop before expiry."""

    def __init__(
        self,
        config: BotConfig,
        locator: GammaMarketLocator,
        trader: PolymarketTrader,
    ):
        self.config = config
        self.locator = locator
        self.trader = trader

        self._shutdown = False
        self._current_window_slug: str | None = None
        self._window_start_ts: float = 0.0
        self._window_budget_usdc: float = 0.0
        self._session_balance_usdc: float = 0.0
        self._window_started = False
        self._window_finished = False
        self._pre_window_logged = False
        self._no_signal_reason = ""
        self._last_heartbeat = 0.0
        self._last_order_time = 0.0
        self._current_elapsed = 0.0
        self._strategy_primary_side: str | None = None
        self._late_trend_locked_side: str | None = None
        self._s0_meta_action: str | None = None
        self._s0_meta_profile: dict[str, float | str] | None = None
        self._wd_window_action: str | None = None
        self._primary_reversals = 0
        self._last_primary_switch_time = 0.0
        self._tp_phase_started = False
        self._exit_mode = False
        self._exit_mode_winner_side: str | None = None
        self._exit_orders_by_side: dict[str, ManagedExitOrder] = {}
        self._fully_exited_sides: set[str] = set()
        self._last_leftover_cleanup_ts: dict[str, float] = {"UP": 0.0, "DOWN": 0.0}
        self._last_sub_two_market_attempt_ts: dict[str, float] = {"UP": 0.0, "DOWN": 0.0}

        self._last_contract: ActiveContract | None = None
        self._last_up_price: float | None = None
        self._last_down_price: float | None = None
        self._last_price_time: float = 0.0
        self._price_history: list[PricePoint] = []
        self._btc_feed = RealtimeBtcPriceFeed(config) if config.btc_feed_enabled else None
        self._last_btc_price: float | None = None
        self._last_btc_price_time: float = 0.0
        self._last_btc_base_volume: float | None = None
        self._last_btc_quote_volume: float | None = None
        self._last_btc_trade_count: int = 0
        self._btc_price_history: list[BtcPricePoint] = []
        self._last_btc_feed_error_log: float = 0.0
        self._window_open_btc_price: float | None = None
        self._volume_scalp_entry_count: dict[str, int] = {"UP": 0, "DOWN": 0}
        # Scalp entry hint per side (place/fill); TP anchor prefers hint, then ledger avg, then live px — never min() with mkt (dip would sell below entry).
        self._volume_scalp_entry_reference_px: dict[str, float | None] = {"UP": None, "DOWN": None}

        self._perp15_samples: list[tuple[float, float, float]] = []
        self._perp15_last_sample_ts: float = 0.0
        self._perp15_monitor_complete: bool = False
        self._perp15_gate_passed: bool = False
        self._perp15_entry_placed: bool = False
        self._perp15_chosen_side: str | None = None
        self._perp15_entry_px: float | None = None
        self._perp15_target_shares: int = 0
        self._perp15_ladder_submitted: bool = False
        self._perp15_entry_window_closed: bool = False
        self._perp15_attempted_entry_prices: set[float] = set()
        self._perp15_last_end_dump_ts: dict[str, float] = {"UP": 0.0, "DOWN": 0.0}

        self._order_map: dict[str, ManagedOrder] = {}
        self._orders_placed = 0
        self._fills = 0
        self._cancels = 0
        self._primary_orders = 0
        self._hedge_orders = 0

        self._up_shares = 0
        self._down_shares = 0
        self._up_spend = 0.0
        self._down_spend = 0.0

        self._baseline_up_balance = 0.0
        self._baseline_down_balance = 0.0
        self._window_finalized = False
        self._balance_retry_attempts_remaining = 0
        self._next_balance_retry_ts = 0.0
        self._aa1_unmatched_lots: dict[str, list[AA1CheapLot]] = {"UP": [], "DOWN": []}
        self._aa1_cheap_lot_counts: dict[str, int] = {"UP": 0, "DOWN": 0}
        self._aa1_last_cheap_buy_elapsed: dict[str, float | None] = {"UP": None, "DOWN": None}
        self._aa1_last_cheap_buy_price: dict[str, float | None] = {"UP": None, "DOWN": None}

        self._mimic_params: dict[str, float] = {}
        self._mimic_action_queue: deque[OrderCandidate] = deque()
        self._mimic_last_pair_elapsed = -10_000.0
        self._mimic_last_winner_elapsed = -10_000.0
        self._mimic_last_loser_elapsed = -10_000.0
        self._mimic_last_reversal_elapsed = -10_000.0
        self._mimic_last_lottery_elapsed = -10_000.0
        self._mimic_prev_winner: str | None = None
        self._mimic_consecutive_same_winner = 0
        if self.config.strategy_mode == "mimic_lot":
            loaded = _load_mimic_search_params(MIMIC_PARAMS_JSON)
            if loaded:
                self._mimic_params = loaded
            else:
                LOGGER.error(
                    "strategy_mode=mimic_lot but could not load params from %s — falling back to aa1",
                    MIMIC_PARAMS_JSON,
                )

        self._iy2_params: dict[str, float] = {}
        self._iy2_action_queue: deque[OrderCandidate] = deque()
        self._iy2_last_base_elapsed = -10_000.0
        self._iy2_last_winner_elapsed = -10_000.0
        self._iy2_last_hedge_elapsed = -10_000.0
        self._iy2_last_repair_elapsed = -10_000.0
        self._iy2_last_late_elapsed = -10_000.0
        self._iy2_last_maint_elapsed = -10_000.0
        self._iy2_last_rebalance_elapsed = -10_000.0
        self._iy2_last_safety_elapsed = -10_000.0
        self._iy2_last_value_elapsed = -10_000.0
        self._iy2_last_deep_elapsed = -10_000.0
        self._iy2_base_locked = False
        self._iy2_last_fill_elapsed: dict[str, float] = {"UP": -10_000.0, "DOWN": -10_000.0}
        self._iy2_last_fill_price: dict[str, float | None] = {"UP": None, "DOWN": None}
        self._iy2_last_filled_side: str | None = None
        self._iy2_last_fail_elapsed: dict[str, float] = {"UP": -10_000.0, "DOWN": -10_000.0}
        if self.config.strategy_mode in {"iy2", "iy3"}:
            iy_params_path = IY3_PARAMS_JSON if self.config.strategy_mode == "iy3" else IY2_PARAMS_JSON
            loaded = _load_best_params_json(iy_params_path)
            if loaded:
                self._iy2_params = loaded
            else:
                LOGGER.error(
                    "strategy_mode=%s but could not load params from %s, falling back to aa1",
                    self.config.strategy_mode,
                    iy_params_path,
                )

        self._box_lot_counts: dict[str, int] = {"UP": 0, "DOWN": 0}
        self._box_last_side_elapsed: dict[str, float | None] = {"UP": None, "DOWN": None}
        self._champ4_action_queue: deque[OrderCandidate] = deque()
        self._champ4_stage_done: dict[str, bool] = {
            "entry": False,
            "mid1": False,
            "mid2": False,
            "late": False,
        }

    def _strategy_mode_mimic(self) -> bool:
        return self.config.strategy_mode == "mimic_lot" and bool(self._mimic_params)

    def _strategy_mode_champ4_6s(self) -> bool:
        return self.config.strategy_mode == "champ4_6s"

    def _strategy_mode_iy2(self) -> bool:
        return self.config.strategy_mode in {"iy2", "iy3"} and bool(self._iy2_params)

    def _strategy_mode_iy3(self) -> bool:
        return self.config.strategy_mode == "iy3" and bool(self._iy2_params)

    def _strategy_mode_box_balance(self) -> bool:
        return self.config.strategy_mode == "box_balance"

    def _strategy_mode_strategy_0(self) -> bool:
        return self.config.strategy_mode == "strategy_0"

    def _strategy_mode_wd(self) -> bool:
        return self.config.strategy_mode == "wd"

    def _strategy_mode_volume_t10(self) -> bool:
        return self.config.strategy_mode in {"volume_t10", "volume_t10_hybrid"}

    def _strategy_mode_volume_scalp_up(self) -> bool:
        m = (self.config.strategy_mode or "").strip().lower().replace("-", "_")
        if m in ("volume_scalp_up", "volume_scalp", "vol_scalp_up"):
            return True
        if "t10" in m:
            return False
        if "scalp" in m and "volume" in m:
            return True
        if m in ("scalp_up", "btc_volume_scalp", "vol_scalp"):
            return True
        return False

    def _strategy_mode_btc_perp15(self) -> bool:
        return self.config.strategy_mode == "btc_perp15"

    def _strategy_mode_signal_only(self) -> bool:
        return self.config.strategy_mode == "signal_only"

    def _strategy_mode_hold_to_redeem(self) -> bool:
        return (
            self._strategy_mode_champ4_6s()
            or self._strategy_mode_iy2()
            or self._strategy_mode_mimic()
            or self._strategy_mode_box_balance()
            or self._strategy_mode_volume_t10()
            or self._strategy_mode_volume_scalp_up()
        )

    def _profile_label(self) -> str:
        if self._strategy_mode_champ4_6s():
            return CHAMP4_6S_PROFILE_ID
        if self._strategy_mode_iy2():
            return IY2_STRATEGY_PROFILE_ID
        if self._strategy_mode_box_balance():
            return BOX_STRATEGY_PROFILE_ID
        if self._strategy_mode_mimic():
            return MIMIC_STRATEGY_PROFILE_ID
        if self._strategy_mode_wd():
            return WD_STRATEGY_PROFILE_ID
        if self._strategy_mode_volume_t10():
            if self.config.strategy_mode == "volume_t10_hybrid":
                return VOLUME_T10_HYBRID_STRATEGY_PROFILE_ID
            return VOLUME_T10_STRATEGY_PROFILE_ID
        if self._strategy_mode_volume_scalp_up():
            return VOLUME_SCALP_UP_PROFILE_ID
        if self._strategy_mode_btc_perp15():
            return BTC_PERP15_PROFILE_ID
        if self._strategy_mode_signal_only():
            return "SIGNAL_ANALYZER_v1"
        if self._strategy_mode_strategy_0():
            return STRATEGY_0_META_PROFILE_ID
        return AA1_STRATEGY_PROFILE_ID

    def shutdown(self, *_: Any) -> None:
        LOGGER.info("Shutdown requested. Cancelling live orders and stopping.")
        self._shutdown = True

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

        self._session_balance_usdc = self.trader.wallet_balance_usdc()
        self._window_budget_usdc = self._effective_budget(self._session_balance_usdc)

        LOGGER.info(
            "BTC 15m redeem engine | profile=%s | version=%s | mode=%s | dry_run=%s | market=%s | continuous=%s",
            self._profile_label(),
            self.config.bot_version,
            self.config.strategy_mode,
            self.config.dry_run,
            self.config.market_slug_prefix,
            not self.config.trade_one_window,
        )
        LOGGER.info(
            "Balance plan | wallet=$%.2f | budget_cap=$%.2f | reserve=$%.2f | effective_budget=$%.2f",
            self._session_balance_usdc,
            self.config.strategy_budget_cap_usdc,
            self.config.strategy_wallet_reserve_usdc,
            self._window_budget_usdc,
        )
        LOGGER.info(
            "Execution plan | shares/order=%d | entry_delay=%ds | new_order_cutoff=%ds | stale_order=%ss | live_order_cap=%d",
            self.config.shares_per_level,
            self.config.strategy_entry_delay_seconds,
            self.config.strategy_new_order_cutoff_seconds,
            self.config.strategy_stale_order_seconds,
            self.config.strategy_max_live_orders,
        )
        LOGGER.info(
            "BTC feed plan | enabled=%s | symbol=%s | poll=%.1fs",
            self._btc_feed is not None,
            self.config.btc_feed_symbol,
            self.config.btc_feed_poll_seconds,
        )

        if self._window_budget_usdc < self.config.strategy_min_budget_usdc:
            LOGGER.warning(
                "Usable budget below target minimum at startup: have $%.2f after reserve, target is $%.2f. "
                "Bot will keep running; trading can still proceed once budget is at least $%.2f.",
                self._window_budget_usdc,
                self.config.strategy_min_budget_usdc,
                self._minimum_tradable_budget_usdc(),
            )

        while not self._shutdown:
            tick_started = time.time()
            try:
                self._loop_once()
            except Exception:
                LOGGER.exception("Loop error")
            elapsed = time.time() - tick_started
            time.sleep(max(0.0, self.config.poll_interval_seconds - elapsed))

        self._graceful_shutdown()

    def _graceful_shutdown(self) -> None:
        if self._last_contract is not None and not self._window_finalized:
            self._finalize_window(self._last_contract, reason="shutdown")

    def _loop_once(self) -> None:
        contract = self.locator.get_active_contract()
        if contract is None:
            return

        self._last_contract = contract
        now = time.time()
        window_end_ts = float(contract.end_time.timestamp())
        window_start_ts = window_end_ts - self.config.window_size_seconds
        seconds_to_start = window_start_ts - now
        seconds_remaining = max(0.0, window_end_ts - now)
        elapsed = max(0.0, now - window_start_ts)
        self._current_elapsed = elapsed

        if contract.slug != self._current_window_slug:
            self._handle_new_window(contract, window_start_ts)

        if seconds_to_start > 0:
            if not self._pre_window_logged or seconds_to_start <= 10:
                LOGGER.info(
                    "[PRE-WINDOW] %s | starts_in=%ds | ends=%s",
                    contract.slug,
                    int(seconds_to_start),
                    contract.end_time.strftime("%H:%M:%S"),
                )
                self._pre_window_logged = True
            return

        if not self._window_started:
            self._window_started = True
            self._pre_window_logged = False
            if elapsed > 5:
                LOGGER.info(
                    "[WINDOW JOIN] %s | elapsed=%ds | budget=$%.2f | entry_delay=%ds",
                    contract.slug,
                    int(elapsed),
                    self._window_budget_usdc,
                    self.config.strategy_entry_delay_seconds,
                )
            else:
                LOGGER.info(
                    "[WINDOW START] %s | budget=$%.2f | entry_delay=%ds",
                    contract.slug,
                    self._window_budget_usdc,
                    self.config.strategy_entry_delay_seconds,
                )

        open_orders = self._get_contract_orders(contract)
        live_ids = self._extract_live_ids(open_orders)
        self._poll_btc_price()
        self._poll_prices(contract)
        self._detect_fills(contract, live_ids)
        self._maybe_market_sell_sub_two_share_inventory(contract)
        self._maybe_volume_scalp_risk_exit(contract, seconds_remaining)
        self._maybe_volume_scalp_arm_take_profit(contract)
        self._cancel_stale_orders(contract, seconds_remaining)
        self._maybe_retry_window_balance(contract, elapsed, seconds_remaining)

        open_orders = self._get_contract_orders(contract)
        self._maybe_enter_early_exit_mode(contract)
        allow_hold_release = self._strategy_mode_hold_to_redeem() and seconds_remaining <= HOLD_TO_REDEEM_RELEASE_SECONDS
        if (
            not self._strategy_mode_hold_to_redeem() and not self._strategy_mode_signal_only()
        ) or self._strategy_mode_volume_t10() or self._strategy_mode_volume_scalp_up() or self._strategy_mode_btc_perp15() or allow_hold_release:
            self._manage_exit_orders(contract, open_orders)
            self._maybe_btc_perp15_end_window_market_dump(contract, seconds_remaining)
            self._cleanup_small_leftovers(contract, seconds_remaining, time.time())

        if (
            not self._strategy_mode_hold_to_redeem()
            and not self._strategy_mode_signal_only()
            and not self._strategy_mode_btc_perp15()
            and seconds_remaining <= self.config.strategy_new_order_cutoff_seconds
        ) or (
            self._strategy_mode_volume_t10()
            and seconds_remaining <= VOLUME_T10_TP_START_SECONDS_REMAINING
        ):
            self._run_take_profit_phase(contract)

        snapshot = self._build_snapshot()
        self._maybe_record_price_snapshot(contract, snapshot, elapsed, seconds_remaining)
        if self._strategy_mode_btc_perp15():
            self._btc_perp15_tick(contract, snapshot, elapsed, seconds_remaining)
        self._maintain_passive_maker_orders(contract, snapshot, elapsed, seconds_remaining)

        if not self._has_tradable_budget():
            self._no_signal_reason = (
                f"waiting for balance settlement: budget ${self._window_budget_usdc:.2f} "
                f"< tradable ${self._minimum_tradable_budget_usdc():.2f}"
            )
        elif self._tp_phase_started and not self._strategy_mode_hold_to_redeem() and not self._strategy_mode_volume_t10() and not self._strategy_mode_volume_scalp_up() and not self._strategy_mode_btc_perp15():
            self._no_signal_reason = "exit mode active"
        elif elapsed >= (
            int(self._iy2_params.get("entry_delay", self.config.strategy_entry_delay_seconds))
            if self._strategy_mode_iy2()
            else self.config.strategy_entry_delay_seconds
        ):
            if self._strategy_mode_champ4_6s():
                self._champ4_evaluate_tick(elapsed)
                snapshot = self._process_candidate_queue(contract, snapshot, elapsed, self._champ4_action_queue)
                if not self._champ4_action_queue and self._no_signal_reason:
                    LOGGER.debug("[WAIT] %s | %s", contract.slug, self._no_signal_reason)
            elif self._strategy_mode_iy2():
                self._iy2_evaluate_tick(snapshot, elapsed, seconds_remaining)
                snapshot = self._process_candidate_queue(contract, snapshot, elapsed, self._iy2_action_queue)
                if not self._iy2_action_queue and self._no_signal_reason:
                    LOGGER.debug("[WAIT] %s | %s", contract.slug, self._no_signal_reason)
            elif self._strategy_mode_mimic():
                self._mimic_evaluate_tick(elapsed)
                snapshot = self._process_candidate_queue(contract, snapshot, elapsed, self._mimic_action_queue)
                if not self._mimic_action_queue and self._no_signal_reason:
                    LOGGER.debug("[WAIT] %s | %s", contract.slug, self._no_signal_reason)
            elif self._strategy_mode_box_balance():
                candidate = self._choose_box_balance_candidate(snapshot, elapsed, seconds_remaining)
                if candidate is not None:
                    open_orders = self._get_contract_orders(contract)
                    if self._can_place_candidate(snapshot, candidate, open_orders, elapsed):
                        if self._place_candidate(contract, candidate, snapshot, elapsed):
                            self._box_lot_counts[candidate.side_label] += 1
                            self._box_last_side_elapsed[candidate.side_label] = elapsed
                elif self._no_signal_reason:
                    LOGGER.debug("[WAIT] %s | %s", contract.slug, self._no_signal_reason)
            elif self._strategy_mode_volume_t10():
                candidate = self._choose_volume_t10_candidate(contract, snapshot, elapsed, seconds_remaining)
                if candidate is not None:
                    open_orders = self._get_contract_orders(contract)
                    if self._can_place_candidate(snapshot, candidate, open_orders, elapsed):
                        self._place_candidate(contract, candidate, snapshot, elapsed)
                elif self._no_signal_reason:
                    LOGGER.debug("[WAIT] %s | %s", contract.slug, self._no_signal_reason)
            elif self._strategy_mode_volume_scalp_up():
                candidate = self._choose_volume_scalp_candidate(contract, snapshot, elapsed)
                if candidate is not None:
                    open_orders = self._get_contract_orders(contract)
                    if self._can_place_candidate(snapshot, candidate, open_orders, elapsed):
                        self._place_candidate(contract, candidate, snapshot, elapsed)
                elif self._no_signal_reason:
                    LOGGER.debug("[WAIT] %s | %s", contract.slug, self._no_signal_reason)
            elif self._strategy_mode_btc_perp15():
                pass  # entry + TP handled in _btc_perp15_tick / _manage_exit_orders
            elif self._strategy_mode_signal_only():
                pass  # signal_analyzer thread handles orders
            elif self._strategy_mode_wd():
                candidate = self._choose_wd_candidate(snapshot, elapsed, seconds_remaining)
                if candidate is not None:
                    open_orders = self._get_contract_orders(contract)
                    if self._can_place_candidate(snapshot, candidate, open_orders, elapsed):
                        self._place_candidate(contract, candidate, snapshot, elapsed)
                elif self._no_signal_reason:
                    LOGGER.debug("[WAIT] %s | %s", contract.slug, self._no_signal_reason)
            elif self._strategy_mode_strategy_0():
                candidate = self._choose_strategy_0_candidate(snapshot, elapsed, seconds_remaining)
                if candidate is not None:
                    open_orders = self._get_contract_orders(contract)
                    if self._can_place_candidate(snapshot, candidate, open_orders, elapsed):
                        self._place_candidate(contract, candidate, snapshot, elapsed)
                elif self._no_signal_reason:
                    LOGGER.debug("[WAIT] %s | %s", contract.slug, self._no_signal_reason)
            else:
                candidate = self._choose_aa1_candidate(snapshot, elapsed, seconds_remaining)
                if candidate is not None:
                    open_orders = self._get_contract_orders(contract)
                    if self._can_place_candidate(snapshot, candidate, open_orders, elapsed):
                        self._place_candidate(contract, candidate, snapshot, elapsed)
                elif self._no_signal_reason:
                    LOGGER.debug("[WAIT] %s | %s", contract.slug, self._no_signal_reason)

        if now - self._last_heartbeat >= self.config.strategy_heartbeat_interval_seconds:
            heartbeat_orders = self._get_contract_orders(contract)
            self._log_heartbeat(contract, snapshot, heartbeat_orders, elapsed, seconds_remaining)
            self._last_heartbeat = now

    def _handle_new_window(self, contract: ActiveContract, window_start_ts: float) -> None:
        if self._current_window_slug and not self._window_finalized and self._last_contract is not None:
            self._finalize_window(self._last_contract, reason="rollover")

        self._session_balance_usdc = self.trader.wallet_balance_usdc()
        self._window_budget_usdc = self._effective_budget(self._session_balance_usdc)
        self._current_window_slug = contract.slug
        self._window_start_ts = window_start_ts
        self._window_started = False
        self._window_finished = False
        self._window_finalized = False
        self._pre_window_logged = False
        self._last_heartbeat = 0.0
        self._last_order_time = 0.0
        self._current_elapsed = 0.0
        self._strategy_primary_side = None
        self._late_trend_locked_side = None
        self._s0_meta_action = None
        self._s0_meta_profile = None
        self._wd_window_action = None
        self._primary_reversals = 0
        self._last_primary_switch_time = 0.0
        self._tp_phase_started = False
        self._exit_mode = False
        self._exit_mode_winner_side = None
        self._exit_orders_by_side.clear()
        self._fully_exited_sides.clear()
        self._last_leftover_cleanup_ts = {"UP": 0.0, "DOWN": 0.0}
        self._last_sub_two_market_attempt_ts = {"UP": 0.0, "DOWN": 0.0}
        self._no_signal_reason = ""
        self._balance_retry_attempts_remaining = 0
        self._next_balance_retry_ts = 0.0

        self._order_map.clear()
        self._orders_placed = 0
        self._fills = 0
        self._cancels = 0
        self._primary_orders = 0
        self._hedge_orders = 0

        self._up_shares = 0
        self._down_shares = 0
        self._up_spend = 0.0
        self._down_spend = 0.0

        self._last_up_price = None
        self._last_down_price = None
        self._last_price_time = 0.0
        self._price_history.clear()
        self._last_btc_price = None
        self._last_btc_price_time = 0.0
        self._last_btc_base_volume = None
        self._last_btc_quote_volume = None
        self._last_btc_trade_count = 0
        self._btc_price_history.clear()
        self._window_open_btc_price = None
        self._volume_scalp_entry_count = {"UP": 0, "DOWN": 0}
        self._volume_scalp_entry_reference_px = {"UP": None, "DOWN": None}
        self._perp15_samples = []
        self._perp15_last_sample_ts = 0.0
        self._perp15_monitor_complete = False
        self._perp15_gate_passed = False
        self._perp15_entry_placed = False
        self._perp15_chosen_side = None
        self._perp15_entry_px = None
        self._perp15_target_shares = 0
        self._perp15_ladder_submitted = False
        self._perp15_entry_window_closed = False
        self._perp15_attempted_entry_prices = set()
        self._perp15_last_end_dump_ts = {"UP": 0.0, "DOWN": 0.0}

        self._baseline_up_balance = self.trader.token_balance(contract.up.token_id)
        self._baseline_down_balance = self.trader.token_balance(contract.down.token_id)
        self._aa1_unmatched_lots = {"UP": [], "DOWN": []}
        self._aa1_cheap_lot_counts = {"UP": 0, "DOWN": 0}
        self._aa1_last_cheap_buy_elapsed = {"UP": None, "DOWN": None}
        self._aa1_last_cheap_buy_price = {"UP": None, "DOWN": None}

        self._mimic_action_queue.clear()
        self._mimic_last_pair_elapsed = -10_000.0
        self._mimic_last_winner_elapsed = -10_000.0
        self._mimic_last_loser_elapsed = -10_000.0
        self._mimic_last_reversal_elapsed = -10_000.0
        self._mimic_last_lottery_elapsed = -10_000.0
        self._mimic_prev_winner = None
        self._mimic_consecutive_same_winner = 0

        self._iy2_action_queue.clear()
        self._iy2_last_base_elapsed = -10_000.0
        self._iy2_last_winner_elapsed = -10_000.0
        self._iy2_last_hedge_elapsed = -10_000.0
        self._iy2_last_repair_elapsed = -10_000.0
        self._iy2_last_late_elapsed = -10_000.0
        self._iy2_last_maint_elapsed = -10_000.0
        self._iy2_last_rebalance_elapsed = -10_000.0
        self._iy2_last_safety_elapsed = -10_000.0
        self._iy2_last_value_elapsed = -10_000.0
        self._iy2_last_deep_elapsed = -10_000.0
        self._iy2_base_locked = False
        self._iy2_last_fill_elapsed = {"UP": -10_000.0, "DOWN": -10_000.0}
        self._iy2_last_fill_price = {"UP": None, "DOWN": None}
        self._iy2_last_filled_side = None
        self._iy2_last_fail_elapsed = {"UP": -10_000.0, "DOWN": -10_000.0}

        self._box_lot_counts = {"UP": 0, "DOWN": 0}
        self._box_last_side_elapsed = {"UP": None, "DOWN": None}
        self._champ4_action_queue.clear()
        self._champ4_stage_done = {"entry": False, "mid1": False, "mid2": False, "late": False}

        setup_file_logger(contract.slug)
        log_file = next(
            (
                str(Path(getattr(handler, "baseFilename", "")).absolute())
                for handler in LOGGER.handlers
                if hasattr(handler, "baseFilename")
            ),
            "",
        )
        LOGGER.info(
            "[NEW WINDOW] %s | question=%s | ends=%s | wallet=$%.2f | budget=$%.2f | baseline_up=%.4f | baseline_down=%.4f",
            contract.slug,
            contract.question,
            contract.end_time.strftime("%H:%M:%S"),
            self._session_balance_usdc,
            self._window_budget_usdc,
            self._baseline_up_balance,
            self._baseline_down_balance,
        )
        append_window_balance_snapshot(
            fetched_at=datetime.now(),
            log_file=log_file,
            slug=contract.slug,
            question=contract.question,
            ends_at=contract.end_time.strftime("%H:%M:%S"),
            wallet_usdc=self._session_balance_usdc,
            budget_usdc=self._window_budget_usdc,
            baseline_up=self._baseline_up_balance,
            baseline_down=self._baseline_down_balance,
            dry_run=self.config.dry_run,
        )
        LOGGER.info(
            "[BALANCE SNAPSHOT] %s | wallet=$%.2f | budget=$%.2f | baseline_up=%.4f | baseline_down=%.4f",
            contract.slug,
            self._session_balance_usdc,
            self._window_budget_usdc,
            self._baseline_up_balance,
            self._baseline_down_balance,
        )
        LOGGER.info(
            "[WINDOW MODE] %s | dry_run=%s | shares_per_order=%d | trade_one_window=%s",
            contract.slug,
            self.config.dry_run,
            self.config.shares_per_level,
            self.config.trade_one_window,
        )
        if self._strategy_mode_champ4_6s():
            LOGGER.info(
                "[STRATEGY PARAMS] %s | profile=%s | 6-share dual-side hedge | entry=%ds mid1=%ds mid2=%ds late=%ds | "
                "open=24/12 shares BTC-dir/opposite | mid1=6/12 shares | mid2=opp-heavy hedge | late_confirm=6 shares | hold_to_redeem",
                contract.slug,
                self._profile_label(),
                CHAMP4_ENTRY_DELAY_SECONDS,
                CHAMP4_MID1_DELAY_SECONDS,
                CHAMP4_MID2_DELAY_SECONDS,
                CHAMP4_LATE_DELAY_SECONDS,
            )
        elif self._strategy_mode_iy2():
            p = self._iy2_params
            LOGGER.info(
                "[STRATEGY PARAMS] %s | profile=%s | iy2_json=%s | wallet_path buckets=%d | entry=%ss cutoff=%ss | "
                "freeze=%.2f improve=%.3f cheap_buf=%.2f share_cap=%s",
                contract.slug,
                self._profile_label(),
                IY2_PARAMS_JSON.name,
                len(p.get("bucket_targets", []) or []),
                p.get("entry_delay"),
                p.get("cutoff"),
                float(p.get("freeze_roi", 0.05) or 0.05),
                float(p.get("improvement_min", 0.015) or 0.015),
                float(p.get("cheap_buffer", 0.03) or 0.03),
                p.get("candidate_share_cap", 25),
            )
        elif self._strategy_mode_mimic():
            mp = self._mimic_params
            LOGGER.info(
                "[STRATEGY PARAMS] %s | profile=%s | mimic_json=%s | entry_delay=%s | cutoff_elapsed=%s | "
                "pair=[%.2f,%.2f] winner_spread_min=%.2f lottery_start=%s | hold_to_redeem (no late TP)",
                contract.slug,
                self._profile_label(),
                MIMIC_PARAMS_JSON.name,
                mp.get("entry_delay"),
                mp.get("cutoff"),
                float(mp.get("pair_min", 0)),
                float(mp.get("pair_max", 0)),
                float(mp.get("winner_spread_min", 0)),
                mp.get("lottery_start"),
            )
        elif self._strategy_mode_box_balance():
            LOGGER.info(
                "[STRATEGY PARAMS] %s | profile=%s | shares/order=%d | "
                "stop_new_buys_only_if_both_outcomes_pnl>$%.2f | max_lots/side=%d imbalance_orders<=%d | "
                "pair[%.2f,%.2f] spread<=%.2f | hold_to_redeem (no late TP)",
                contract.slug,
                self._profile_label(),
                self.config.shares_per_level,
                BOX_BOTH_WAYS_MIN_PNL_USDC,
                BOX_MAX_LOTS_PER_SIDE,
                BOX_MAX_OPEN_IMBALANCE_ORDERS,
                BOX_PAIR_MIN,
                BOX_PAIR_MAX,
                BOX_PAIR_SPREAD_MAX,
            )
        elif self._strategy_mode_signal_only():
            LOGGER.info(
                "[STRATEGY PARAMS] %s | profile=%s | signal_only mode — orders via signal_analyzer thread",
                contract.slug, self._profile_label(),
            )
            LOGGER.info(
                "[STRATEGY VERSION] %s | profile=%s | version=%s",
                contract.slug,
                self._profile_label(),
                self.config.bot_version,
            )
        elif self._strategy_mode_strategy_0():
            LOGGER.info(
                "[STRATEGY PARAMS] %s | profile=%s | env_entry_delay=%ds | meta_decision_delay=%ds | new_cutoff=%ds | "
                "meta_profiles=baseline,filtered,local_172,local_153,H5,robust,deep064,H2,H1,skip | selector=v4_delay12_wr739_pnl1386 | "
                "signals=unchanged(log-only) | tp=$%.2f",
                contract.slug,
                self._profile_label(),
                self.config.strategy_entry_delay_seconds,
                S0_META_DECISION_DELAY_SECONDS,
                self.config.strategy_new_order_cutoff_seconds,
                TP_PRICE,
            )
        elif self._strategy_mode_wd():
            LOGGER.info(
                "[STRATEGY PARAMS] %s | profile=%s | decision_delay=%ds | early_filters: flips_180<=%d lead_max_180<=%.2f | "
                "entry_delay=%ds | target_primary=%.2f target_hedge=%.2f | directional=%.2f guarantee=%.2f | tp=$%.2f",
                contract.slug,
                self._profile_label(),
                WD_DECISION_DELAY_SECONDS,
                WD_FILTER_MAX_EARLY_FLIPS_180,
                WD_FILTER_MAX_EARLY_LEAD_MAX_180,
                self.config.strategy_entry_delay_seconds,
                float(WD_PROFILE["primary_target_share"]),
                float(WD_PROFILE["hedge_target_share"]),
                float(WD_PROFILE["target_directional_ratio"]),
                float(WD_PROFILE["target_guarantee_ratio"]),
                TP_PRICE,
            )
        elif self._strategy_mode_volume_t10():
            LOGGER.info(
                "[STRATEGY PARAMS] %s | profile=%s | volume_first=%ds-%ds ratio>%.2f | "
                "volume_side=BTC_direction | t10_window=T-%ds..T-%ds | hybrid_exec=maker>=10s,maker>=5s,taker<5s | "
                "tp_last_minute=$%.2f | min_btc_delta=%.4f | pair_sum<=%.2f | max_pos_pct=%.2f | min_shares=%d",
                contract.slug,
                self._profile_label(),
                int(VOLUME_ENTRY_MIN_ELAPSED_SECONDS),
                int(VOLUME_ENTRY_MAX_ELAPSED_SECONDS),
                VOLUME_RATIO_THRESHOLD,
                int(T10_ENTRY_START_SECONDS_REMAINING),
                int(T10_ENTRY_END_SECONDS_REMAINING),
                TP_PRICE,
                T10_MIN_BTC_DELTA,
                T10_PAIR_SUM_TARGET,
                POSITION_MAX_PCT,
                POLY_MIN_LIMIT_SHARES,
            )
        elif self._strategy_mode_volume_scalp_up():
            LOGGER.info(
                "[STRATEGY PARAMS] %s | profile=%s | UP+DOWN | max entry $%.2f | elapsed=%ds-%ds | vol_ratio>%.2f | "
                "shares=%d | max_orders/side=%d | tp=avg+%.2f | stop=avg-%.2f | time_exit<=%.0fs",
                contract.slug,
                self._profile_label(),
                VOLUME_SCALP_ENTRY_MAX_PRICE,
                self.config.volume_scalp_entry_min_elapsed,
                self.config.volume_scalp_entry_max_elapsed,
                self.config.volume_scalp_volume_ratio,
                self.config.volume_scalp_shares,
                self.config.volume_scalp_tp_offset,
                self.config.volume_scalp_max_orders_per_side,
                self.config.volume_scalp_stop_offset,
                self.config.volume_scalp_time_exit_seconds_remaining,
            )
        elif self._strategy_mode_btc_perp15():
            LOGGER.info(
                "[STRATEGY PARAMS] %s | profile=%s | gate=%ds sample=%.1fs btc_trend>=%.4f | up_only ladder=%s | "
                "entry_window<=%ds | shares/order=%d | TP-after-cutoff=$%.2f | dump<=%.0fs",
                contract.slug,
                self._profile_label(),
                self.config.btc_perp15_monitor_seconds,
                self.config.btc_perp15_sample_interval_seconds,
                self.config.btc_perp15_btc_trend_threshold,
                ",".join(f"{p:.2f}" for p in self.config.btc_perp15_ladder_prices),
                self.config.btc_perp15_entry_window_seconds,
                self.config.btc_perp15_min_shares,
                self.config.btc_perp15_tp_price,
                self.config.btc_perp15_end_dump_seconds_remaining,
            )
        else:
            LOGGER.info(
                "[STRATEGY PARAMS] %s | profile=%s | entry_delay=%ds | new_cutoff=%ds | "
                "cheap_max=%.2f | repeat_drop=%.2f | cheap_cooldown=%ds | max_cheap_lots=%d | max_imbalance_orders=%d | tp=$%.2f",
                contract.slug,
                self._profile_label(),
                self.config.strategy_entry_delay_seconds,
                self.config.strategy_new_order_cutoff_seconds,
                AA1_CHEAP_ENTRY_MAX,
                AA1_CHEAP_REPEAT_DROP,
                AA1_CHEAP_COOLDOWN_SECONDS,
                AA1_MAX_CHEAP_LOTS_PER_SIDE,
                AA1_MAX_OPEN_IMBALANCE_ORDERS,
                TP_PRICE,
            )
        if not self._has_tradable_budget():
            self._balance_retry_attempts_remaining = self.config.strategy_balance_retry_attempts
            self._next_balance_retry_ts = time.time() + self.config.strategy_balance_retry_seconds
            LOGGER.warning(
                "[BALANCE WAIT] %s | budget=$%.2f below tradable=$%.2f | will retry %d times every %ds",
                contract.slug,
                self._window_budget_usdc,
                self._minimum_tradable_budget_usdc(),
                self._balance_retry_attempts_remaining,
                self.config.strategy_balance_retry_seconds,
            )

    def _finalize_window(self, contract: ActiveContract, reason: str) -> None:
        if self._window_finalized:
            return

        for order_id in list(self._order_map):
            self._cancel_order_safe(order_id, reason=reason)
        open_orders = self._get_contract_orders(contract)
        if open_orders:
            cancelled = self.trader.cancel_all_orders(open_orders) if not self.config.dry_run else len(open_orders)
            self._cancels += cancelled
            LOGGER.info(
                "[WINDOW STOP] %s | reason=%s | cancelled=%d live orders",
                contract.slug,
                reason,
                cancelled,
            )

        time.sleep(1.0)

        up_delta = self._token_delta(contract.up, self._baseline_up_balance)
        down_delta = self._token_delta(contract.down, self._baseline_down_balance)
        total_spend = self._up_spend + self._down_spend
        up_avg_price = (self._up_spend / self._up_shares) if self._up_shares else 0.0
        down_avg_price = (self._down_spend / self._down_shares) if self._down_shares else 0.0
        guarantee_ratio = (min(self._up_shares, self._down_shares) / total_spend) if total_spend else 0.0
        directional_ratio = (max(self._up_shares, self._down_shares) / total_spend) if total_spend else 0.0
        up_pnl_if_win = self._up_shares - total_spend
        down_pnl_if_win = self._down_shares - total_spend

        LOGGER.info(
            "[WINDOW SUMMARY] %s | reason=%s | spend=$%.2f | shares_up=%d shares_down=%d | avg_up=%.3f avg_down=%.3f pair=%.3f | guarantee=%.3f directional=%.3f",
            contract.slug,
            reason,
            total_spend,
            self._up_shares,
            self._down_shares,
            up_avg_price,
            down_avg_price,
            up_avg_price + down_avg_price,
            guarantee_ratio,
            directional_ratio,
        )
        LOGGER.info(
            "[WINDOW PAYOUT] %s | if_UP_wins=$%.2f | if_DOWN_wins=$%.2f",
            contract.slug,
            up_pnl_if_win,
            down_pnl_if_win,
        )
        LOGGER.info(
            "[WINDOW DELTA] %s | wallet_up_delta=%.4f | wallet_down_delta=%.4f",
            contract.slug,
            up_delta,
            down_delta,
        )
        LOGGER.info(
            "[WINDOW OPS] %s | orders=%d | fills=%d | cancels=%d | primary_orders=%d | hedge_orders=%d",
            contract.slug,
            self._orders_placed,
            self._fills,
            self._cancels,
            self._primary_orders,
            self._hedge_orders,
        )
        LOGGER.info("Window finalized. Remaining positions, if any, stay open after late TP handling.")

        self._window_finished = True
        self._window_finalized = True

    def _poll_prices(self, contract: ActiveContract) -> None:
        up_price = self._resolve_trade_price(contract.up)
        down_price = self._resolve_trade_price(contract.down)
        if up_price is None or down_price is None:
            LOGGER.debug("[PRICE WAIT] %s | up=%s | down=%s", contract.slug, up_price, down_price)
            return

        self._last_up_price = up_price
        self._last_down_price = down_price
        self._last_price_time = time.time()

        self._price_history.append(
            PricePoint(ts=self._last_price_time, up_price=up_price, down_price=down_price)
        )
        cutoff = self._last_price_time - PRICE_HISTORY_RETENTION_SECONDS
        self._price_history = [p for p in self._price_history if p.ts >= cutoff]

    def _poll_btc_price(self) -> None:
        if self._btc_feed is None:
            return
        try:
            point = self._btc_feed.poll()
        except Exception as exc:
            now = time.time()
            if now - self._last_btc_feed_error_log >= 30.0:
                LOGGER.warning("[BTC FEED] poll failed, classic signals remain active: %s", exc)
                self._last_btc_feed_error_log = now
            return

        now = time.time()
        self._last_btc_price = point.price
        self._last_btc_price_time = now
        self._last_btc_base_volume = point.base_volume
        self._last_btc_quote_volume = point.quote_volume
        self._last_btc_trade_count = point.trade_count
        self._btc_price_history.append(
            BtcPricePoint(
                ts=now,
                price=point.price,
                base_volume=point.base_volume,
                quote_volume=point.quote_volume,
                trade_count=point.trade_count,
            )
        )
        if self._window_open_btc_price is None and now >= self._window_start_ts and point.price > 0:
            self._window_open_btc_price = point.price
        cutoff = now - BTC_PRICE_HISTORY_RETENTION_SECONDS
        self._btc_price_history = [p for p in self._btc_price_history if p.ts >= cutoff]

    def _maybe_retry_window_balance(
        self,
        contract: ActiveContract,
        elapsed: float,
        seconds_remaining: float,
    ) -> None:
        if self.config.dry_run:
            return
        if self._has_tradable_budget():
            return
        if self._balance_retry_attempts_remaining <= 0:
            return
        if time.time() < self._next_balance_retry_ts:
            return
        if seconds_remaining <= self.config.strategy_new_order_cutoff_seconds:
            return

        attempt = self.config.strategy_balance_retry_attempts - self._balance_retry_attempts_remaining + 1
        self._balance_retry_attempts_remaining -= 1
        self._next_balance_retry_ts = time.time() + self.config.strategy_balance_retry_seconds

        refreshed_wallet = self.trader.wallet_balance_usdc()
        refreshed_budget = self._effective_budget(refreshed_wallet)
        self._session_balance_usdc = refreshed_wallet
        self._window_budget_usdc = refreshed_budget

        LOGGER.info(
            "[BALANCE RETRY] %s | attempt=%d/%d | elapsed=%ds | wallet=$%.2f | budget=$%.2f",
            contract.slug,
            attempt,
            self.config.strategy_balance_retry_attempts,
            int(elapsed),
            refreshed_wallet,
            refreshed_budget,
        )

        if refreshed_budget >= self._minimum_tradable_budget_usdc():
            LOGGER.info(
                "[BALANCE READY] %s | refreshed budget reached $%.2f and trading is enabled for this window",
                contract.slug,
                refreshed_budget,
            )
            return

        if self._balance_retry_attempts_remaining <= 0:
            LOGGER.warning(
                "[BALANCE GIVEUP] %s | budget still $%.2f after retries; skipping new entries this window",
                contract.slug,
                refreshed_budget,
            )

    def _resolve_trade_price(self, token: TokenMarket) -> float | None:
        market_price = self.trader.get_market_price(token.token_id)
        if market_price is not None and market_price > 0:
            return round(market_price, 4)
        midpoint = self.trader.get_midpoint(token.token_id)
        if midpoint is not None and midpoint > 0:
            return round(midpoint, 4)
        return None

    def _detect_fills(self, contract: ActiveContract, live_ids: set[str]) -> None:
        now = time.time()
        filled_ids: list[str] = []
        for order_id, order in self._order_map.items():
            if now - order.placed_at < self.config.strategy_fill_grace_seconds:
                continue
            if order_id in live_ids:
                continue
            self._mark_fill(contract, order)
            filled_ids.append(order_id)

        for order_id in filled_ids:
            self._order_map.pop(order_id, None)

    def _mark_fill(self, contract: ActiveContract, order: ManagedOrder) -> None:
        self._fills += 1
        notional = order.notional
        if order.side_label == "UP":
            self._up_shares += order.shares
            self._up_spend += notional
        else:
            self._down_shares += order.shares
            self._down_spend += notional
        if order.reason == "aa1_buy_cheap":
            self._aa1_record_cheap_fill(order.side_label, order.price, self._current_elapsed)
        elif order.reason.startswith("iy2|"):
            self._iy2_last_fill_elapsed[order.side_label] = self._current_elapsed
            self._iy2_last_fill_price[order.side_label] = float(order.price)
            self._iy2_last_filled_side = order.side_label
            if self._up_shares > 0 and self._down_shares > 0:
                self._iy2_base_locked = True
        elif order.reason.startswith("aa1_balance|"):
            self._aa1_mark_balance_fill(order.reason)
        elif (
            self._strategy_mode_volume_scalp_up()
            and order.side_label in ("UP", "DOWN")
            and order.reason.startswith("volume_scalp|")
        ):
            self._volume_scalp_entry_count[order.side_label] = self._volume_scalp_entry_count.get(order.side_label, 0) + 1
            self._fully_exited_sides.discard(order.side_label)
            # notional is price×shares (cap), NOT true fill — use last side quote vs limit for anchor.
            lim = float(order.price)
            mkt = self._side_price(order.side_label)
            parts = [lim] if lim > 0 else []
            if mkt > 0 and mkt < 1:
                parts.append(mkt)
            if parts:
                anchor = round(min(parts), 2)
                if 0 < anchor < 1:
                    self._volume_scalp_entry_reference_px[order.side_label] = anchor

        LOGGER.info(
            "[FILL] %s | kind=%s | side=%s | price=$%.2f | shares=%d | spend=$%.2f | reason=%s",
            contract.slug,
            order.kind,
            order.side_label,
            order.price,
            order.shares,
            notional,
            order.reason,
        )

    def _cancel_stale_orders(self, contract: ActiveContract, seconds_remaining: float) -> None:
        if not self._order_map:
            return

        now = time.time()
        stale_seconds = self.config.strategy_stale_order_seconds
        stale_ids: list[tuple[str, str]] = []
        for order_id, order in self._order_map.items():
            if self._strategy_mode_btc_perp15() and order.reason.startswith("btc_perp15|entry"):
                continue
            age = now - order.placed_at
            if age < stale_seconds:
                continue

            current_price = self._last_up_price if order.side_label == "UP" else self._last_down_price
            too_far = current_price is not None and current_price > order.price + 0.05
            if too_far or seconds_remaining <= LATE_REPAIR_SECONDS:
                stale_ids.append((order_id, "stale"))

        for order_id, reason in stale_ids:
            self._cancel_order_safe(order_id, reason=reason)
            LOGGER.info("[CANCEL] %s | order=%s | reason=%s", contract.slug, order_id[:16], reason)

    def _run_take_profit_phase(self, contract: ActiveContract) -> None:
        if self._strategy_mode_volume_scalp_up() or self._strategy_mode_btc_perp15():
            return
        raw = (self.config.strategy_mode or "").lower()
        if "scalp" in raw and "volume" in raw and "t10" not in raw:
            return
        self._enter_exit_mode(contract, reason=f"late tp phase at ${TP_PRICE:.2f}")
        if self._exit_mode_winner_side is None:
            self._ensure_exit_sell(contract, "UP", TP_PRICE, purpose="tp")
            self._ensure_exit_sell(contract, "DOWN", TP_PRICE, purpose="tp")

    def _maybe_enter_early_exit_mode(self, contract: ActiveContract) -> None:
        # L1 simulation holds inventory through the window and does not use
        # early winner-take-all exits.
        return

    def _btc_perp15_compute_shares(self, balance: float, entry: float) -> int | None:
        """Fixed-lot ladder sizing for btc_perp15."""
        c = self.config
        if entry <= 0 or balance <= 0:
            return None
        if balance < entry * float(c.btc_perp15_min_shares):
            return None
        shares = int(c.btc_perp15_min_shares)
        if shares < 1:
            return None
        if balance < entry * shares:
            return None
        return shares

    def _btc_perp15_tick(
        self,
        contract: ActiveContract,
        snapshot: BookSnapshot,
        elapsed: float,
        seconds_remaining: float,
    ) -> None:
        """Gate on early BTC trend, place fixed UP ladder orders, then convert filled inventory to TP after cutoff."""
        now = time.time()
        gate_seconds = float(self.config.btc_perp15_monitor_seconds)
        entry_window_seconds = float(self.config.btc_perp15_entry_window_seconds)

        if elapsed < gate_seconds:
            if (
                self._last_up_price is not None
                and self._last_down_price is not None
                and self._last_btc_price is not None
                and self._last_up_price > 0
                and self._last_down_price > 0
                and self._last_btc_price > 0
            ):
                if (
                    not self._perp15_samples
                    or now - self._perp15_last_sample_ts >= self.config.btc_perp15_sample_interval_seconds
                ):
                    self._perp15_samples.append(
                        (float(self._last_up_price), float(self._last_down_price), float(self._last_btc_price))
                    )
                    self._perp15_last_sample_ts = now
            return

        if not self._perp15_monitor_complete:
            self._perp15_monitor_complete = True
            if not self._perp15_samples:
                LOGGER.warning("[PERP15] %s | no samples in gate phase; skip window", contract.slug)
                return
            b0 = float(self._perp15_samples[0][2])
            b1 = float(self._perp15_samples[-1][2])
            btc_trend = ((b1 - b0) / b0) if b0 > 0 else 0.0
            thr = float(self.config.btc_perp15_btc_trend_threshold)
            if btc_trend < thr:
                LOGGER.info(
                    "[PERP15] %s | skip no UP trend confirmation btc_trend=%+.5f (need >= %.5f)",
                    contract.slug,
                    btc_trend,
                    thr,
                )
                return
            self._perp15_gate_passed = True
            self._perp15_chosen_side = "UP"
            LOGGER.info(
                "[PERP15] %s | gate passed btc_trend=%+.5f | ladder=%s",
                contract.slug,
                btc_trend,
                ",".join(f"{p:.2f}" for p in self.config.btc_perp15_ladder_prices),
            )

        if not self._perp15_gate_passed:
            return

        if elapsed >= entry_window_seconds:
            if not self._perp15_entry_window_closed:
                self._perp15_entry_window_closed = True
                cancelled = 0
                for order_id, order in list(self._order_map.items()):
                    if order.reason.startswith("btc_perp15|entry|side=UP"):
                        self._cancel_order_safe(order_id, reason="perp15-entry-window-ended")
                        cancelled += 1
                LOGGER.info("[PERP15] %s | entry window ended | cancelled_up_buys=%d", contract.slug, cancelled)
            self._perp15_entry_placed = True
            return

        if seconds_remaining <= float(self.config.strategy_new_order_cutoff_seconds):
            LOGGER.warning("[PERP15] %s | skip past new-order cutoff (remaining=%.0fs)", contract.slug, seconds_remaining)
            return

        open_orders = self._get_contract_orders(contract)
        live_buy_prices = {
            round(order.price, 2)
            for order in self._order_map.values()
            if order.reason.startswith("btc_perp15|entry|side=UP")
        }
        for entry in self.config.btc_perp15_ladder_prices:
            px = round(float(entry), 2)
            if px in self._perp15_attempted_entry_prices or px in live_buy_prices:
                continue
            shares = int(self.config.btc_perp15_min_shares)
            cost = px * float(shares)
            if self._window_budget_usdc < cost:
                LOGGER.info("[PERP15] %s | skip ladder level $%.2f cost $%.2f > budget $%.2f", contract.slug, px, cost, self._window_budget_usdc)
                self._perp15_attempted_entry_prices.add(px)
                continue
            candidate = OrderCandidate(
                side_label="UP",
                kind="primary",
                reference_price=px,
                limit_ceiling=px,
                reason="btc_perp15|entry|side=UP",
                shares=shares,
                min_shares=shares,
                execution_style="normal",
            )
            if not self._can_place_candidate(snapshot, candidate, open_orders, elapsed):
                LOGGER.warning("[PERP15] %s | cannot place ladder level $%.2f: %s", contract.slug, px, self._no_signal_reason)
                return
            if self._place_candidate(contract, candidate, snapshot, elapsed):
                self._perp15_attempted_entry_prices.add(px)
                self._perp15_ladder_submitted = True
                self._perp15_entry_placed = True
            return

    def _btc_perp15_ensure_tp_limit(self, contract: ActiveContract) -> None:
        if not self._perp15_entry_window_closed or self._perp15_chosen_side is None:
            return
        sr = max(0.0, float(contract.end_time.timestamp()) - time.time())
        if sr <= float(self.config.btc_perp15_end_dump_seconds_remaining):
            return
        side = self._perp15_chosen_side
        if any(o.reason.startswith("btc_perp15|entry|side=UP") for o in self._order_map.values()):
            return
        token = contract.up if side == "UP" else contract.down
        bal = float(self.trader.token_balance(token.token_id))
        if bal < 1.0:
            return
        tp = round(float(self.config.btc_perp15_tp_price), 2)
        self._ensure_exit_sell(contract, side, tp, purpose="tp")

    def _maybe_btc_perp15_end_window_market_dump(
        self, contract: ActiveContract, seconds_remaining: float
    ) -> None:
        """Last N seconds: if window position > 0 on a side, cancel resting exit and FAK-sell full position size."""
        if not self._strategy_mode_btc_perp15():
            return
        if seconds_remaining > float(self.config.btc_perp15_end_dump_seconds_remaining):
            return
        now = time.time()
        for side_label, token in (("UP", contract.up), ("DOWN", contract.down)):
            if now - self._perp15_last_end_dump_ts[side_label] < BTC_PERP15_END_DUMP_THROTTLE_SECONDS:
                continue
            baseline = self._baseline_up_balance if side_label == "UP" else self._baseline_down_balance
            position_shares = float(self._token_delta(token, baseline))
            if position_shares <= 0.0:
                continue

            self._perp15_last_end_dump_ts[side_label] = now

            active = self._exit_orders_by_side.get(side_label)
            if active is not None:
                oid = active.order_id
                if oid in self._order_map:
                    self._cancel_order_safe(oid, reason="perp15-end-dump")
                else:
                    self._cancel_venue_order(oid, reason="perp15-end-dump")
                self._exit_orders_by_side.pop(side_label, None)
                self._fully_exited_sides.discard(side_label)
                LOGGER.info(
                    "[PERP15 END DUMP] %s | side=%s | cancelled exit %s before market flatten",
                    contract.slug,
                    side_label,
                    oid[:16] if oid else "n/a",
                )

            free_wallet = float(self.trader.token_balance(token.token_id))
            sell_qty = min(max(0.0, free_wallet), position_shares)
            if sell_qty <= 0.0:
                LOGGER.debug(
                    "[PERP15 END DUMP SKIP] %s | side=%s | position=%.4f free_wallet=%.4f",
                    contract.slug,
                    side_label,
                    position_shares,
                    free_wallet,
                )
                continue

            px = self._sub_two_marketable_sell_limit_price(token, side_label)
            if self.config.dry_run:
                LOGGER.info(
                    "[DRY PERP15 END DUMP] %s | side=%s | T-%.0fs | sell=%.4f @ limit=$%.2f",
                    contract.slug,
                    side_label,
                    seconds_remaining,
                    sell_qty,
                    px,
                )
                continue
            try:
                resp = self.trader.place_marketable_sell(token, px, round(sell_qty, 4))
                order_id = str(resp.get("orderID") or resp.get("id") or "")
                LOGGER.info(
                    "[PERP15 END DUMP] %s | side=%s | T-%.0fs | marketable sell=%.4f @ limit=$%.2f | order=%s",
                    contract.slug,
                    side_label,
                    seconds_remaining,
                    sell_qty,
                    px,
                    order_id[:16] if order_id else "n/a",
                )
            except Exception as exc:
                LOGGER.warning(
                    "[PERP15 END DUMP FAILED] %s | side=%s | T-%.0fs | sell=%.4f | %s",
                    contract.slug,
                    side_label,
                    seconds_remaining,
                    sell_qty,
                    exc,
                )

    def _maintain_passive_maker_orders(
        self,
        contract: ActiveContract,
        snapshot: BookSnapshot,
        elapsed: float,
        seconds_remaining: float,
    ) -> None:
        return

    def _dominant_extreme_side(self) -> str | None:
        if self._last_up_price is None or self._last_down_price is None:
            return None
        if self._last_up_price >= EARLY_EXIT_WIN_PRICE and self._last_down_price <= EARLY_EXIT_LOSE_PRICE:
            return "UP"
        if self._last_down_price >= EARLY_EXIT_WIN_PRICE and self._last_up_price <= EARLY_EXIT_LOSE_PRICE:
            return "DOWN"
        return None

    def _enter_exit_mode(self, contract: ActiveContract, reason: str, winner_side: str | None = None) -> None:
        if not self._tp_phase_started:
            self._tp_phase_started = True
            self._exit_mode = True
            for order_id in list(self._order_map):
                self._cancel_order_safe(order_id, reason="exit-mode")
            LOGGER.info("[TP PHASE] %s | entering exit mode: %s", contract.slug, reason)
        if winner_side is not None and self._exit_mode_winner_side != winner_side:
            self._exit_mode_winner_side = winner_side
            LOGGER.info("[EARLY EXIT] %s | winner_side=%s", contract.slug, winner_side)

    def _manage_exit_orders(self, contract: ActiveContract, open_orders: list[dict[str, Any]]) -> None:
        seconds_remaining = max(0.0, contract.end_time.timestamp() - time.time())
        hold_release_active = self._strategy_mode_hold_to_redeem() and seconds_remaining <= HOLD_TO_REDEEM_RELEASE_SECONDS
        if not self._tp_phase_started and not self._strategy_mode_volume_scalp_up() and not self._strategy_mode_btc_perp15() and not hold_release_active:
            return

        open_ids = self._extract_live_ids(open_orders)
        self._refresh_exit_orders(contract, open_ids)
        self._reconcile_exit_orders(contract, open_ids)

        if self._strategy_mode_btc_perp15():
            self._btc_perp15_ensure_tp_limit(contract)
            return

        if hold_release_active:
            if seconds_remaining <= self.config.force_exit_before_end_seconds:
                self._force_late_exit_cleanup(contract)
                return
            for side_label in ("UP", "DOWN"):
                self._ensure_exit_sell(
                    contract,
                    side_label,
                    self._desired_exit_price(contract, side_label, "tp"),
                    purpose="tp",
                )
            self._force_late_exit_cleanup(contract)
            return

        if self._exit_mode_winner_side is not None:
            ws = self._exit_mode_winner_side
            # volume_scalp manages exits via purpose=scalp_tp only; generic winner TP at $0.99 must not run here.
            if not self._strategy_mode_volume_scalp_up():
                self._ensure_exit_sell(
                    contract,
                    ws,
                    self._desired_exit_price(contract, ws, "tp"),
                    purpose="tp",
                )
            loser_side = "DOWN" if ws == "UP" else "UP"
            if ws in self._fully_exited_sides and not self._strategy_mode_volume_scalp_up():
                salvage_price = self._desired_exit_price(contract, loser_side, "salvage")
                self._ensure_exit_sell(contract, loser_side, salvage_price, purpose="salvage")
        self._force_late_exit_cleanup(contract)

    def _cleanup_small_leftovers(self, contract: ActiveContract, seconds_remaining: float, now: float) -> None:
        if self._strategy_mode_volume_scalp_up() or self._strategy_mode_btc_perp15():
            return
        if seconds_remaining > LEFTOVER_CLEANUP_START_SECONDS_REMAINING:
            return

        for side_label, token, current_price in (
            ("UP", contract.up, self._last_up_price),
            ("DOWN", contract.down, self._last_down_price),
        ):
            if current_price is None or current_price < LEFTOVER_CLEANUP_PRICE:
                continue
            if now - self._last_leftover_cleanup_ts[side_label] < LEFTOVER_CLEANUP_INTERVAL_SECONDS:
                continue
            if side_label in self._exit_orders_by_side:
                continue
            balance = self.trader.token_balance(token.token_id)
            if balance <= 0.0 or balance >= LEFTOVER_CLEANUP_BALANCE_MAX:
                continue
            self._last_leftover_cleanup_ts[side_label] = now
            try:
                resp = self.trader.place_marketable_sell(token, LEFTOVER_CLEANUP_PRICE, round(balance, 4))
                order_id = str(resp.get("orderID") or resp.get("id") or "")
                LOGGER.info(
                    "[LEFTOVER CLEANUP] %s | side=%s | price=$%.2f | shares=%.4f | order=%s",
                    contract.slug,
                    side_label,
                    LEFTOVER_CLEANUP_PRICE,
                    balance,
                    order_id[:16] if order_id else "n/a",
                )
            except Exception as exc:
                LOGGER.debug(
                    "[LEFTOVER CLEANUP FAILED] %s | side=%s | price=$%.2f | shares=%.4f | %s",
                    contract.slug,
                    side_label,
                    LEFTOVER_CLEANUP_PRICE,
                    balance,
                    exc,
                )

    def _sub_two_marketable_sell_limit_price(self, token: TokenMarket, side_label: str) -> float:
        """FAK sell limit: slightly below best bid / last market so the order clears like a market sell."""
        tid = token.token_id
        bid = self.trader.get_best_bid(tid)
        if bid is not None and bid > 0:
            return round(max(0.01, min(0.99, bid - 0.02)), 2)
        mpx = self.trader.get_market_price(tid)
        if mpx is not None and mpx > 0:
            return round(max(0.01, min(0.99, mpx - 0.05)), 2)
        last = self._last_up_price if side_label == "UP" else self._last_down_price
        if last is not None and last > 0:
            return round(max(0.01, min(0.99, last - 0.05)), 2)
        return 0.01

    def _maybe_market_sell_sub_two_share_inventory(self, contract: ActiveContract) -> None:
        """If window *position* (token delta vs baseline) is >0 and <2 sh, FAK-sell that size (capped by free wallet after cancel)."""
        if self._strategy_mode_hold_to_redeem():
            return
        now = time.time()
        for side_label, token in (("UP", contract.up), ("DOWN", contract.down)):
            if now - self._last_sub_two_market_attempt_ts[side_label] < SUB_TWO_SHARE_MARKET_CHECK_SECONDS:
                continue
            baseline = self._baseline_up_balance if side_label == "UP" else self._baseline_down_balance
            position_shares = float(self._token_delta(token, baseline))
            if position_shares <= 0.0 or position_shares >= SUB_TWO_SHARE_MAX_EXCLUSIVE:
                continue

            self._last_sub_two_market_attempt_ts[side_label] = now

            active = self._exit_orders_by_side.get(side_label)
            if active is not None:
                oid = active.order_id
                if oid in self._order_map:
                    self._cancel_order_safe(oid, reason="sub2-market-dump")
                else:
                    self._cancel_venue_order(oid, reason="sub2-market-dump")
                self._exit_orders_by_side.pop(side_label, None)
                self._fully_exited_sides.discard(side_label)
                LOGGER.info(
                    "[SUB2 DUMP] %s | side=%s | cancelled resting exit %s before market dump",
                    contract.slug,
                    side_label,
                    oid[:16] if oid else "n/a",
                )

            free_wallet = float(self.trader.token_balance(token.token_id))
            sell_qty = min(max(0.0, free_wallet), position_shares)
            if sell_qty <= 0.0:
                LOGGER.debug(
                    "[SUB2 DUMP SKIP] %s | side=%s | position=%.4f sh but free_wallet=%.4f after cancel",
                    contract.slug,
                    side_label,
                    position_shares,
                    free_wallet,
                )
                continue

            px = self._sub_two_marketable_sell_limit_price(token, side_label)
            if self.config.dry_run:
                LOGGER.info(
                    "[DRY SUB2 DUMP] %s | side=%s | position=%.4f | sell=%.4f | limit=$%.2f (would FAK sell)",
                    contract.slug,
                    side_label,
                    position_shares,
                    sell_qty,
                    px,
                )
                continue
            try:
                resp = self.trader.place_marketable_sell(token, px, round(sell_qty, 4))
                order_id = str(resp.get("orderID") or resp.get("id") or "")
                LOGGER.info(
                    "[SUB2 DUMP] %s | side=%s | position=%.4f | sell=%.4f | limit=$%.2f | order=%s",
                    contract.slug,
                    side_label,
                    position_shares,
                    sell_qty,
                    px,
                    order_id[:16] if order_id else "n/a",
                )
            except Exception as exc:
                LOGGER.warning(
                    "[SUB2 DUMP FAILED] %s | side=%s | position=%.4f | sell=%.4f | limit=$%.2f | %s",
                    contract.slug,
                    side_label,
                    position_shares,
                    sell_qty,
                    px,
                    exc,
                )

    def _refresh_exit_orders(self, contract: ActiveContract, open_ids: set[str]) -> None:
        for side_label, managed in list(self._exit_orders_by_side.items()):
            if managed.order_id in open_ids:
                continue
            balance = self._safe_exit_position_shares(contract, side_label)
            if balance < 1:
                self._fully_exited_sides.add(side_label)
                LOGGER.info(
                    "[EXIT FILLED] %s | side=%s | purpose=%s | price=$%.2f | remaining=%.4f",
                    contract.slug,
                    side_label,
                    managed.purpose,
                    managed.price,
                    balance,
                )
            else:
                if managed.cancel_requested_at is not None:
                    LOGGER.info(
                        "[EXIT CLEARED] %s | side=%s | purpose=%s | balance=%.4f remains after cancel",
                        contract.slug,
                        side_label,
                        managed.purpose,
                        balance,
                    )
                else:
                    LOGGER.info(
                        "[EXIT CLEAR] %s | side=%s | purpose=%s | order gone but balance=%.4f remains",
                        contract.slug,
                        side_label,
                        managed.purpose,
                        balance,
                    )
            self._exit_orders_by_side.pop(side_label, None)

    def _salvage_price_for(self, side_label: str) -> float:
        current = self._last_up_price if side_label == "UP" else self._last_down_price
        current = current or 0.0
        target = max(HEDGE_SALVAGE_MIN_PRICE, min(0.25, current + HEDGE_SALVAGE_BUFFER))
        return round(target, 2)

    def _desired_exit_price(self, contract: ActiveContract, side_label: str, purpose: str) -> float:
        token = contract.up if side_label == "UP" else contract.down
        current_price = self._side_price(side_label)
        best_bid = self.trader.get_best_bid(token.token_id)

        if purpose == "tp":
            if self._strategy_mode_btc_perp15():
                return round(float(self.config.btc_perp15_tp_price), 2)
            return TP_PRICE

        if best_bid is not None and best_bid > 0:
            return round(max(HEDGE_SALVAGE_MIN_PRICE, min(0.25, best_bid)), 2)
        return self._salvage_price_for(side_label)

    def _reconcile_exit_orders(self, contract: ActiveContract, open_ids: set[str]) -> None:
        now = time.time()
        for side_label, managed in list(self._exit_orders_by_side.items()):
            if managed.order_id not in open_ids:
                continue
            if managed.cancel_requested_at is not None:
                continue
            age = now - managed.placed_at
            if age < EXIT_RECONCILE_INTERVAL_SECONDS:
                continue
            live_balance = int(self._safe_exit_position_shares(contract, side_label))
            if managed.purpose == "scalp_tp":
                desired_price = managed.price
            else:
                desired_price = self._desired_exit_price(contract, side_label, managed.purpose)
            if live_balance < 1:
                continue
            if managed.shares == live_balance and abs(managed.price - desired_price) < 0.001:
                managed.placed_at = now
                continue
            if managed.order_id in self._order_map:
                self._cancel_order_safe(managed.order_id, reason=f"exit-reconcile-{managed.purpose}")
            else:
                self._cancel_venue_order(managed.order_id, reason=f"exit-reconcile-{managed.purpose}")
            managed.cancel_requested_at = now
            LOGGER.info(
                "[EXIT RECONCILE] %s | side=%s | purpose=%s | old_shares=%d | new_shares=%d | old=$%.2f | new=$%.2f | age=%.1fs",
                contract.slug,
                side_label,
                managed.purpose,
                managed.shares,
                live_balance,
                managed.price,
                desired_price,
                age,
            )

    def _force_late_exit_cleanup(self, contract: ActiveContract) -> None:
        if self._strategy_mode_volume_scalp_up() or self._strategy_mode_btc_perp15():
            return
        seconds_remaining = max(0.0, contract.end_time.timestamp() - time.time())
        if seconds_remaining > self.config.force_exit_before_end_seconds:
            return
        for side_label, token in (("UP", contract.up), ("DOWN", contract.down)):
            if side_label in self._fully_exited_sides:
                continue
            balance = self._safe_exit_position_shares(contract, side_label)
            if balance < 1:
                continue
            active = self._exit_orders_by_side.get(side_label)
            if active is not None:
                if active.cancel_requested_at is None:
                    if active.order_id in self._order_map:
                        self._cancel_order_safe(active.order_id, reason="force-exit-cleanup")
                    else:
                        self._cancel_venue_order(active.order_id, reason="force-exit-cleanup")
                    active.cancel_requested_at = time.time()
                    LOGGER.info(
                        "[FORCE EXIT WAIT] %s | side=%s | waiting for tp cancel before cleanup",
                        contract.slug,
                        side_label,
                    )
                continue
            emergency_price = self._desired_exit_price(contract, side_label, "salvage")
            try:
                resp = self.trader.place_marketable_sell(token, emergency_price, round(balance, 4))
                order_id = str(resp.get("orderID") or resp.get("id") or "")
                LOGGER.info(
                    "[FORCE EXIT] %s | side=%s | price=$%.2f | shares=%.4f | order=%s",
                    contract.slug,
                    side_label,
                    emergency_price,
                    balance,
                    order_id[:16] if order_id else "n/a",
                )
            except Exception as exc:
                LOGGER.warning(
                    "[FORCE EXIT FAILED] %s | side=%s | price=$%.2f | shares=%.4f | %s",
                    contract.slug,
                    side_label,
                    emergency_price,
                    balance,
                    exc,
                )

    def _ensure_exit_sell(self, contract: ActiveContract, side_label: str, price: float, purpose: str) -> None:
        if side_label in self._fully_exited_sides:
            return
        if self._strategy_mode_volume_scalp_up() and purpose == "tp":
            LOGGER.warning(
                "[EXIT SKIP] %s | volume_scalp forbids generic tp (use scalp_tp only)",
                contract.slug,
            )
            return
        if self._strategy_mode_volume_scalp_up() and purpose == "salvage":
            LOGGER.info(
                "[EXIT SKIP] %s | volume_scalp forbids hedge salvage on %s (use scalp_tp only)",
                contract.slug,
                side_label,
            )
            return
        if purpose == "scalp_tp" and self._strategy_mode_volume_scalp_up():
            limit_tp = self._volume_scalp_tp_from_buy_limit(side_label)
            if limit_tp is not None:
                if abs(limit_tp - price) > 0.005:
                    LOGGER.warning(
                        "[EXIT TP ANCHOR] %s | side=%s | overriding requested $%.4f with entry_anchor+offset $%.2f",
                        contract.slug,
                        side_label,
                        price,
                        limit_tp,
                    )
                price = limit_tp
        active = self._exit_orders_by_side.get(side_label)
        if active is not None:
            return

        token = contract.up if side_label == "UP" else contract.down
        balance = self._safe_exit_position_shares(contract, side_label)
        size = int(balance)
        min_exit_shares = 1 if purpose != "tp" else max(1, self.config.shares_per_level)
        if size < min_exit_shares:
            LOGGER.info(
                "[EXIT SKIP] %s | side=%s | purpose=%s | balance=%.4f < %d share%s",
                contract.slug,
                side_label,
                purpose,
                balance,
                min_exit_shares,
                "" if min_exit_shares == 1 else "s",
            )
            if balance < 1:
                self._fully_exited_sides.add(side_label)
            return

        order_id = self._place_take_profit_sell(contract, token, side_label, size, price, purpose)
        if order_id is not None:
            self._exit_orders_by_side[side_label] = ManagedExitOrder(
                order_id=order_id,
                side_label=side_label,
                purpose=purpose,
                price=price,
                shares=size,
                placed_at=time.time(),
            )

    def _place_take_profit_sell(
        self,
        contract: ActiveContract,
        token: TokenMarket,
        side_label: str,
        size: int,
        price: float,
        purpose: str,
    ) -> str | None:
        if self._strategy_mode_volume_scalp_up() and purpose == "tp":
            LOGGER.warning(
                "[EXIT BLOCK] %s | refuse generic tp limit sell in volume_scalp (would be $%.2f)",
                contract.slug,
                price,
            )
            return None
        min_exit_shares = 1 if purpose != "tp" else max(1, self.config.shares_per_level)
        attempt_size = size
        attempts = 0
        while attempt_size >= min_exit_shares and attempts < 8:
            attempts += 1
            try:
                resp = self.trader.place_limit_sell(token, price, attempt_size)
                order_id = str(resp.get("orderID") or resp.get("id") or "")
                LOGGER.info(
                    "[EXIT PLACE] %s | side=%s | purpose=%s | price=$%.2f | shares=%d | order=%s",
                    contract.slug,
                    side_label,
                    purpose,
                    price,
                    attempt_size,
                    order_id[:16] if order_id else "n/a",
                )
                return order_id
            except Exception as exc:
                error_text = str(exc)
                share_state = self._tp_share_state_from_error(error_text, attempt_size)
                next_size = self._next_tp_attempt_size(error_text, attempt_size)
                LOGGER.warning(
                    "[EXIT FAILED] %s | side=%s | purpose=%s | price=$%.2f | shares=%d | next=%s | %s%s",
                    contract.slug,
                    side_label,
                    purpose,
                    price,
                    attempt_size,
                    next_size,
                    exc,
                    (
                        " | decoded balance=%.4f matched=%.4f available=%.4f order=%.4f"
                        % (
                            share_state["balance_shares"],
                            share_state["matched_shares"],
                            share_state["available_shares"],
                            share_state["order_shares"],
                        )
                    )
                    if share_state is not None
                    else "",
                )
                if share_state is not None and share_state["available_whole_shares"] < 1:
                    LOGGER.info(
                        "[EXIT HOLD] %s | side=%s | purpose=%s | only %.4f shares available outside matched orders; skipping re-place",
                        contract.slug,
                        side_label,
                        purpose,
                        share_state["available_shares"],
                    )
                    return None
                if next_size is None or next_size >= attempt_size:
                    next_size = attempt_size - 1
                if next_size is not None and next_size < min_exit_shares:
                    next_size = None
                attempt_size = next_size

        LOGGER.warning(
            "[EXIT GIVEUP] %s | side=%s | purpose=%s | unable to place exit sell after retries",
            contract.slug,
            side_label,
            purpose,
        )
        return None

    def _next_tp_attempt_size(self, error_text: str, current_size: int) -> int | None:
        available_size = self._available_tp_size_from_error(error_text, current_size)
        if available_size is not None and available_size >= 1:
            return available_size

        numbers = [int(match) for match in re.findall(r"(?<![\d.])(\d+)(?![\d.])", error_text)]
        lower_candidates = [value for value in numbers if 5 <= value < current_size]
        if lower_candidates:
            return max(lower_candidates)
        if "size" in error_text.lower() or "amount" in error_text.lower() or "balance" in error_text.lower():
            return current_size - 1
        return None

    def _tp_share_state_from_error(self, error_text: str, current_size: int) -> dict[str, float | int] | None:
        balance_match = re.search(r"balance:\s*(\d+)", error_text)
        amount_match = re.search(r"order amount:\s*(\d+)", error_text)
        if balance_match is None or amount_match is None or current_size <= 0:
            return None

        matched_match = re.search(r"sum of matched orders:\s*(\d+)", error_text)
        balance_raw = int(balance_match.group(1))
        matched_raw = int(matched_match.group(1)) if matched_match is not None else 0
        amount_raw = int(amount_match.group(1))
        if amount_raw <= 0:
            return None

        unit_per_share = amount_raw / current_size
        if unit_per_share <= 0:
            return None

        available_raw = max(0, balance_raw - matched_raw)
        balance_shares = balance_raw / unit_per_share
        matched_shares = matched_raw / unit_per_share
        available_shares = available_raw / unit_per_share
        order_shares = amount_raw / unit_per_share
        available_whole_shares = int(available_raw // unit_per_share)
        return {
            "balance_shares": balance_shares,
            "matched_shares": matched_shares,
            "available_shares": available_shares,
            "order_shares": order_shares,
            "available_whole_shares": available_whole_shares,
        }

    def _available_tp_size_from_error(self, error_text: str, current_size: int) -> int | None:
        share_state = self._tp_share_state_from_error(error_text, current_size)
        if share_state is None:
            return None
        available_size = int(share_state["available_whole_shares"])
        if 0 <= available_size < current_size:
            return available_size
        return None

    def _build_snapshot(self) -> BookSnapshot:
        primary_side, up_score, down_score = self._choose_primary_side()
        primary_price = self._last_up_price if primary_side == "UP" else self._last_down_price
        hedge_side = "DOWN" if primary_side == "UP" else "UP"
        hedge_price = self._last_down_price if hedge_side == "DOWN" else self._last_up_price

        pending_notional = round(sum(order.notional for order in self._order_map.values()), 2)
        total_spend_raw = self._up_spend + self._down_spend
        total_spend = round(total_spend_raw, 2)
        committed_notional = round(total_spend + pending_notional, 2)
        up_avg_price = (self._up_spend / self._up_shares) if self._up_shares else 0.0
        down_avg_price = (self._down_spend / self._down_shares) if self._down_shares else 0.0
        pair_avg_sum = up_avg_price + down_avg_price
        guarantee_ratio = (min(self._up_shares, self._down_shares) / total_spend) if total_spend else 0.0
        directional_ratio = (max(self._up_shares, self._down_shares) / total_spend) if total_spend else 0.0

        primary_spend = self._up_spend if primary_side == "UP" else self._down_spend
        hedge_spend = self._down_spend if primary_side == "UP" else self._up_spend
        primary_spend_share = (primary_spend / total_spend) if total_spend else 0.0
        hedge_spend_share = (hedge_spend / total_spend) if total_spend else 0.0
        up_coverage = (self._up_shares / total_spend_raw) if total_spend_raw else 0.0
        down_coverage = (self._down_shares / total_spend_raw) if total_spend_raw else 0.0
        primary_coverage = up_coverage if primary_side == "UP" else down_coverage
        hedge_coverage = down_coverage if primary_side == "UP" else up_coverage
        up_pnl_if_win = self._up_shares - total_spend
        down_pnl_if_win = self._down_shares - total_spend
        best_case_pnl = max(up_pnl_if_win, down_pnl_if_win)
        worst_case_loss = max(0.0, -up_pnl_if_win, -down_pnl_if_win)

        return BookSnapshot(
            up_shares=self._up_shares,
            down_shares=self._down_shares,
            up_spend=round(self._up_spend, 2),
            down_spend=round(self._down_spend, 2),
            pending_notional=pending_notional,
            total_spend=total_spend,
            committed_notional=committed_notional,
            up_avg_price=up_avg_price,
            down_avg_price=down_avg_price,
            pair_avg_sum=pair_avg_sum,
            guarantee_ratio=guarantee_ratio,
            directional_ratio=directional_ratio,
            primary_side=primary_side,
            primary_price=primary_price or 0.0,
            primary_score=up_score if primary_side == "UP" else down_score,
            hedge_side=hedge_side,
            hedge_price=hedge_price or 0.0,
            hedge_score=down_score if primary_side == "UP" else up_score,
            score_edge=abs(up_score - down_score),
            primary_spend_share=primary_spend_share,
            hedge_spend_share=hedge_spend_share,
            primary_coverage=primary_coverage,
            hedge_coverage=hedge_coverage,
            up_pnl_if_win=up_pnl_if_win,
            down_pnl_if_win=down_pnl_if_win,
            best_case_pnl=best_case_pnl,
            worst_case_loss=worst_case_loss,
        )

    def _choose_primary_side(self) -> tuple[str, float, float]:
        if self._strategy_mode_strategy_0():
            return self._choose_primary_side_scored()
        up_score = self._last_up_price if self._last_up_price is not None else -1.0
        down_score = self._last_down_price if self._last_down_price is not None else -1.0
        primary_side = "UP" if up_score >= down_score else "DOWN"
        self._strategy_primary_side = primary_side
        return primary_side, up_score, down_score

    def _choose_primary_side_scored(self) -> tuple[str, float, float]:
        up_score = self._score_side("UP")
        down_score = self._score_side("DOWN")
        flip = (
            self._s0_profile_value("primary_flip_threshold")
            if self._strategy_mode_strategy_0()
            else self.config.strategy_primary_flip_threshold
        )
        if self._strategy_primary_side is not None:
            current = up_score if self._strategy_primary_side == "UP" else down_score
            other = down_score if self._strategy_primary_side == "UP" else up_score
            if other - current < flip:
                return self._strategy_primary_side, up_score, down_score
        self._strategy_primary_side = "UP" if up_score >= down_score else "DOWN"
        return self._strategy_primary_side, up_score, down_score

    def _s0_profile_value(self, key: str) -> float:
        profile = self._s0_meta_profile or S0_META_BASELINE
        value = profile.get(key)
        return float(value) if value is not None else 0.0

    def _score_side(self, side_label: str) -> float:
        current = self._last_up_price if side_label == "UP" else self._last_down_price
        if current is None:
            return -1.0

        delta_30 = self._price_delta(side_label, 30)
        delta_60 = self._price_delta(side_label, 60)
        delta_90 = self._price_delta(side_label, 90)
        return current + (0.8 * delta_30) + (0.5 * delta_60) + (0.3 * delta_90)

    def _price_delta(self, side_label: str, lookback_seconds: int) -> float:
        if not self._price_history:
            return 0.0
        current = self._last_up_price if side_label == "UP" else self._last_down_price
        if current is None:
            return 0.0
        target_ts = self._last_price_time - lookback_seconds
        for point in reversed(self._price_history):
            if point.ts <= target_ts:
                old_price = point.up_price if side_label == "UP" else point.down_price
                return current - old_price
        oldest = self._price_history[0]
        old_price = oldest.up_price if side_label == "UP" else oldest.down_price
        return current - old_price

    def _winner_streak(self) -> tuple[str | None, int]:
        if self._last_up_price is None or self._last_down_price is None:
            return None, 0
        current_winner = "UP" if self._last_up_price >= self._last_down_price else "DOWN"
        streak = 0
        for point in reversed(self._price_history):
            winner = "UP" if point.up_price >= point.down_price else "DOWN"
            if winner != current_winner:
                break
            streak += 1
        return current_winner, streak

    def _project_book(self, snapshot: BookSnapshot, side_label: str, price: float, shares: int) -> dict[str, float]:
        up_shares = snapshot.up_shares + (shares if side_label == "UP" else 0)
        down_shares = snapshot.down_shares + (shares if side_label == "DOWN" else 0)
        up_spend = self._up_spend + (price * shares if side_label == "UP" else 0.0)
        down_spend = self._down_spend + (price * shares if side_label == "DOWN" else 0.0)
        total_spend = up_spend + down_spend
        up_avg_price = (up_spend / up_shares) if up_shares else 0.0
        down_avg_price = (down_spend / down_shares) if down_shares else 0.0
        up_pnl_if_win = up_shares - total_spend
        down_pnl_if_win = down_shares - total_spend
        return {
            "up_shares": up_shares,
            "down_shares": down_shares,
            "pair_sum": up_avg_price + down_avg_price,
            "up_pnl_if_win": up_pnl_if_win,
            "down_pnl_if_win": down_pnl_if_win,
            "best_case_pnl": max(up_pnl_if_win, down_pnl_if_win),
            "worst_case_loss": max(0.0, -up_pnl_if_win, -down_pnl_if_win),
            "guarantee_ratio": (min(up_shares, down_shares) / total_spend) if total_spend else 0.0,
            "directional_ratio": (max(up_shares, down_shares) / total_spend) if total_spend else 0.0,
            "up_coverage": (up_shares / total_spend) if total_spend else 0.0,
            "down_coverage": (down_shares / total_spend) if total_spend else 0.0,
        }

    def _side_price(self, side_label: str) -> float:
        if side_label == "UP":
            return self._last_up_price or 0.0
        return self._last_down_price or 0.0

    def _late_trend_side(self) -> str | None:
        up_price = self._last_up_price
        down_price = self._last_down_price
        if up_price is None or down_price is None:
            return None
        if max(up_price, down_price) < LATE_TREND_CLEAR_PRICE:
            return None
        if abs(up_price - down_price) < LATE_TREND_CLEAR_EDGE:
            return None
        return "UP" if up_price > down_price else "DOWN"

    def _s0_elapsed_price_rows(self, max_elapsed: int) -> list[PricePoint]:
        if self._window_start_ts <= 0:
            return []
        cutoff_ts = self._window_start_ts + max_elapsed
        return [point for point in self._price_history if point.ts <= cutoff_ts]

    def _s0_elapsed_btc_rows(self, max_elapsed: int) -> list[BtcPricePoint]:
        if self._window_start_ts <= 0:
            return []
        cutoff_ts = self._window_start_ts + max_elapsed
        return [point for point in self._btc_price_history if point.ts <= cutoff_ts]

    def _s0_sum_quote_volume(self, max_elapsed: int) -> float:
        return sum(float(point.quote_volume or 0.0) for point in self._s0_elapsed_btc_rows(max_elapsed))

    def _s0_peak_quote_ratio_5_180(self) -> float:
        quote_vals = [float(point.quote_volume or 0.0) for point in self._s0_elapsed_btc_rows(180)]
        if not quote_vals:
            return 0.0
        width = 5
        running = 0.0
        best = 0.0
        for idx, value in enumerate(quote_vals):
            running += value
            if idx >= width:
                running -= quote_vals[idx - width]
            if idx >= width - 1 and running > best:
                best = running
        avg = sum(quote_vals) / max(1.0, len(quote_vals) / width)
        return best / max(1e-9, avg)

    def _s0_btc_range_120(self) -> float:
        rows = [point.price for point in self._s0_elapsed_btc_rows(120) if point.price > 0]
        if len(rows) < 2 or rows[0] <= 0:
            return 0.0
        return (max(rows) - min(rows)) / rows[0]

    def _s0_early_features(self) -> dict[str, float]:
        price_rows_180 = self._s0_elapsed_price_rows(180)
        early_leads = [abs(point.up_price - point.down_price) for point in price_rows_180]
        early_mid_balance_secs = sum(1 for point in price_rows_180 if abs(point.up_price - point.down_price) <= 0.06)
        return {
            "early_lead_avg": (sum(early_leads) / len(early_leads)) if early_leads else 0.0,
            "quote_total_120": self._s0_sum_quote_volume(120),
            "peak_quote_ratio_5_180": self._s0_peak_quote_ratio_5_180(),
            "btc_range_120": self._s0_btc_range_120(),
            "early_mid_balance_secs": float(early_mid_balance_secs),
        }

    def _btc_rows_in_window(self) -> list[BtcPricePoint]:
        if self._window_start_ts <= 0:
            return []
        return [point for point in self._btc_price_history if point.ts >= self._window_start_ts]

    def _volume_t10_btc_return(self) -> float | None:
        if self._window_open_btc_price is None or self._window_open_btc_price <= 0:
            return None
        if self._last_btc_price is None or self._last_btc_price <= 0:
            return None
        return (self._last_btc_price - self._window_open_btc_price) / self._window_open_btc_price

    def _champ4_btc_direction_side(self) -> str | None:
        ret = self._volume_t10_btc_return()
        if ret is None:
            return None
        return "UP" if ret >= 0 else "DOWN"

    def _champ4_queue_clip(self, side_label: str, reason: str) -> None:
        reference_price = self._side_price(side_label)
        if reference_price <= 0:
            return
        self._champ4_action_queue.append(
            OrderCandidate(
                side_label=side_label,
                kind="primary",
                reference_price=reference_price,
                limit_ceiling=min(PRIMARY_PRICE_HARD_MAX, reference_price + 0.05),
                reason=reason,
                shares=self.config.shares_per_level,
            )
        )

    def _champ4_enqueue_clips(self, main_side: str, main_clips: int, hedge_clips: int, reason_prefix: str) -> None:
        hedge_side = "DOWN" if main_side == "UP" else "UP"
        max_clips = max(main_clips, hedge_clips)
        for idx in range(max_clips):
            if idx < main_clips:
                self._champ4_queue_clip(main_side, f"{reason_prefix}|main|{idx + 1}")
            if idx < hedge_clips:
                self._champ4_queue_clip(hedge_side, f"{reason_prefix}|hedge|{idx + 1}")

    def _champ4_evaluate_tick(self, elapsed: float) -> None:
        self._no_signal_reason = ""
        if not self._strategy_mode_champ4_6s():
            return
        up_price = self._side_price("UP")
        down_price = self._side_price("DOWN")
        if up_price <= 0 or down_price <= 0:
            self._no_signal_reason = "waiting for both side prices"
            return
        btc_side = self._champ4_btc_direction_side()
        if btc_side is None:
            self._no_signal_reason = "waiting for BTC direction"
            return
        leader_side = "UP" if up_price >= down_price else "DOWN"
        spread = abs(up_price - down_price)

        if elapsed >= CHAMP4_ENTRY_DELAY_SECONDS and not self._champ4_stage_done["entry"]:
            self._champ4_enqueue_clips(btc_side, 4, 2, "champ4|entry")
            self._champ4_stage_done["entry"] = True
        if elapsed >= CHAMP4_MID1_DELAY_SECONDS and not self._champ4_stage_done["mid1"]:
            self._champ4_enqueue_clips(btc_side, 1, 2, "champ4|mid1")
            self._champ4_stage_done["mid1"] = True
        if elapsed >= CHAMP4_MID2_DELAY_SECONDS and not self._champ4_stage_done["mid2"]:
            if btc_side == leader_side:
                hedge_side = "DOWN" if btc_side == "UP" else "UP"
                self._champ4_queue_clip(hedge_side, "champ4|mid2|agree_hedge")
            else:
                self._champ4_enqueue_clips(btc_side, 1, 3, "champ4|mid2|disagree")
            self._champ4_stage_done["mid2"] = True
        if elapsed >= CHAMP4_LATE_DELAY_SECONDS and not self._champ4_stage_done["late"]:
            if btc_side == leader_side and spread >= CHAMP4_LATE_SPREAD_MIN:
                self._champ4_queue_clip(leader_side, "champ4|late_confirm")
            self._champ4_stage_done["late"] = True

        if not self._champ4_action_queue and not self._no_signal_reason:
            self._no_signal_reason = "champ4: no stage fired this tick"

    def _process_candidate_queue(
        self,
        contract: ActiveContract,
        snapshot: BookSnapshot,
        elapsed: float,
        queue: deque[OrderCandidate],
    ) -> BookSnapshot:
        rounds = len(queue)
        if rounds <= 0:
            return snapshot
        placed_any = False
        for _ in range(rounds):
            candidate = queue.popleft()
            if placed_any and self._strategy_mode_iy2():
                queue.appendleft(candidate)
                break
            open_orders = self._get_contract_orders(contract)
            if self._can_place_candidate(snapshot, candidate, open_orders, elapsed):
                if self._place_candidate(contract, candidate, snapshot, elapsed):
                    snapshot = self._build_snapshot()
                    placed_any = True
                    continue
            queue.append(candidate)
        return snapshot

    def _volume_t10_latest_volume_ratio(self) -> float | None:
        rows = self._btc_rows_in_window()
        if len(rows) <= VOLUME_AVG_LOOKBACK_SECONDS:
            return None
        current = float(rows[-1].quote_volume or rows[-1].base_volume or 0.0)
        if current <= 0:
            return None
        prev = [
            float(point.quote_volume or point.base_volume or 0.0)
            for point in rows[-(VOLUME_AVG_LOOKBACK_SECONDS + 1):-1]
        ]
        if not prev:
            return None
        avg_prev = sum(prev) / len(prev)
        if avg_prev <= 0:
            return None
        return current / avg_prev

    def _volume_t10_target_shares(self, entry_price: float) -> int:
        safe_price = max(0.01, entry_price)
        position_notional = self._window_budget_usdc * POSITION_MAX_PCT
        target = int(position_notional / safe_price)
        return max(POLY_MIN_LIMIT_SHARES, target)

    def _wd_early_features(self) -> dict[str, float]:
        price_rows_180 = self._s0_elapsed_price_rows(WD_DECISION_DELAY_SECONDS)
        early_flips_180 = 0
        prev_dom: str | None = None
        early_lead_max = 0.0
        for point in price_rows_180:
            dom = "UP" if point.up_price >= point.down_price else "DOWN"
            if prev_dom is not None and dom != prev_dom:
                early_flips_180 += 1
            prev_dom = dom
            early_lead_max = max(early_lead_max, abs(point.up_price - point.down_price))
        return {
            "early_flips_180": float(early_flips_180),
            "early_lead_max": float(early_lead_max),
        }

    def _wd_choose_action(self, elapsed: float) -> str | None:
        if self._wd_window_action is not None:
            return self._wd_window_action
        if elapsed < WD_DECISION_DELAY_SECONDS:
            return None

        features = self._wd_early_features()
        early_flips_180 = int(features["early_flips_180"])
        early_lead_max = float(features["early_lead_max"])
        action = (
            "trade"
            if early_flips_180 <= WD_FILTER_MAX_EARLY_FLIPS_180
            and early_lead_max <= WD_FILTER_MAX_EARLY_LEAD_MAX_180
            else "skip"
        )
        self._wd_window_action = action
        LOGGER.info(
            "[WD FILTER] %s | action=%s | early_flips_180=%d | early_lead_max=%.4f",
            self._current_window_slug,
            action,
            early_flips_180,
            early_lead_max,
        )
        return self._wd_window_action

    def _wd_profile_value(self, key: str) -> float:
        value = WD_PROFILE.get(key)
        if value is None:
            raise KeyError(f"Missing WD profile key: {key}")
        return float(value)

    def _volume_t10_serial_scalp_blocks_entry(self, contract: ActiveContract) -> str | None:
        """Block a new entry until TP/settlement cleared wallet vs window baseline and no buy is in flight."""
        up_d = self._token_delta(contract.up, self._baseline_up_balance)
        down_d = self._token_delta(contract.down, self._baseline_down_balance)
        if max(abs(up_d), abs(down_d)) > VOLUME_T10_SERIAL_INVENTORY_EPS:
            return f"still holding inventory (upΔ={up_d:.3f} downΔ={down_d:.3f})"
        if any(order.kind == "primary" for order in self._order_map.values()):
            return "primary entry order still open"
        return None

    def _volume_scalp_blocks_new_entry(self, contract: ActiveContract, side_label: str) -> str | None:
        """Allow repeat scalp entries up to a side cap; only block duplicate live buys or cap exhaustion."""
        if self._volume_scalp_entry_count.get(side_label, 0) >= int(self.config.volume_scalp_max_orders_per_side):
            return f"{side_label}: max scalp entries reached"
        if any(
            o.kind == "primary"
            and o.side_label == side_label
            and o.reason.startswith("volume_scalp|")
            for o in self._order_map.values()
        ):
            return f"{side_label}: primary entry order still open"
        return None

    @staticmethod
    def _volume_scalp_strategy_side(strategy_tag: str | None) -> str | None:
        if strategy_tag == "scalp_up":
            return "UP"
        if strategy_tag == "scalp_down":
            return "DOWN"
        return None

    def _volume_scalp_tp_offset_dollars(self) -> float:
        """TP step in share price units (0–1). Env typo `12` meaning 12¢ is normalized to `0.12`."""
        raw = float(self.config.volume_scalp_tp_offset)
        if raw > 1.0:
            raw = raw / 100.0
        return max(0.01, min(raw, 0.50))

    def _volume_scalp_effective_entry_anchor(self, side_label: str) -> float | None:
        """$/sh for TP: prefer place/fill hint, else ledger avg, else last side px.

        Do not combine with min() against live mkt: a transient low quote would drag
        anchor+offset below true entry (e.g. buy ~0.54, TP wrongly ~0.51 instead of ~0.66).
        """
        shares = self._up_shares if side_label == "UP" else self._down_shares
        spend = self._up_spend if side_label == "UP" else self._down_spend
        if shares >= 1 and spend > 0:
            avg = spend / float(shares)
            if 0 < avg < 1:
                return float(avg)
        stored = self._volume_scalp_entry_reference_px.get(side_label)
        if stored is not None and 0 < float(stored) < 1:
            return float(stored)
        mkt = self._side_price(side_label)
        if mkt > 0 and mkt < 1:
            return float(mkt)
        return None

    def _volume_scalp_tp_from_buy_limit(self, side_label: str) -> float | None:
        """TP = effective entry anchor + offset (anchor from hint, then ledger, then mkt)."""
        if not self._strategy_mode_volume_scalp_up():
            return None
        anchor = self._volume_scalp_effective_entry_anchor(side_label)
        if anchor is None or not (0 < anchor < 1):
            return None
        off = self._volume_scalp_tp_offset_dollars()
        return round(min(0.99, anchor + off), 2)

    def _maybe_volume_scalp_arm_take_profit(self, contract: ActiveContract) -> None:
        if not self._strategy_mode_volume_scalp_up():
            return
        for side_label in ("UP", "DOWN"):
            self._volume_scalp_arm_take_profit_for_side(contract, side_label)

    def _maybe_volume_scalp_risk_exit(self, contract: ActiveContract, seconds_remaining: float) -> None:
        if not self._strategy_mode_volume_scalp_up():
            return
        for side_label, token in (("UP", contract.up), ("DOWN", contract.down)):
            baseline = self._baseline_up_balance if side_label == "UP" else self._baseline_down_balance
            position_shares = float(self._token_delta(token, baseline))
            if position_shares <= VOLUME_T10_SERIAL_INVENTORY_EPS:
                continue
            anchor = self._volume_scalp_effective_entry_anchor(side_label)
            current_price = self._side_price(side_label)
            if anchor is None or current_price <= 0:
                continue
            stop_price = round(max(0.01, anchor - float(self.config.volume_scalp_stop_offset)), 2)
            stop_hit = current_price <= stop_price
            time_exit = seconds_remaining <= float(self.config.volume_scalp_time_exit_seconds_remaining)
            if not stop_hit and not time_exit:
                continue

            active = self._exit_orders_by_side.get(side_label)
            if active is not None:
                oid = active.order_id
                if oid in self._order_map:
                    self._cancel_order_safe(oid, reason="volume-scalp-risk-exit")
                else:
                    self._cancel_venue_order(oid, reason="volume-scalp-risk-exit")
                self._exit_orders_by_side.pop(side_label, None)
                self._fully_exited_sides.discard(side_label)

            free_wallet = float(self.trader.token_balance(token.token_id))
            sell_qty = min(max(0.0, free_wallet), position_shares)
            if sell_qty <= 0.0:
                continue
            sell_limit = self._sub_two_marketable_sell_limit_price(token, side_label)
            reason = "stop" if stop_hit else "time_exit"
            if self.config.dry_run:
                LOGGER.info(
                    "[DRY SCALP %s] %s | side=%s | current=$%.2f | anchor=$%.4f | stop=$%.2f | sell=%.4f | limit=$%.2f",
                    reason.upper(),
                    contract.slug,
                    side_label,
                    current_price,
                    anchor,
                    stop_price,
                    sell_qty,
                    sell_limit,
                )
                continue
            try:
                resp = self.trader.place_marketable_sell(token, sell_limit, round(sell_qty, 4))
                order_id = str(resp.get("orderID") or resp.get("id") or "")
                LOGGER.info(
                    "[SCALP %s] %s | side=%s | current=$%.2f | anchor=$%.4f | stop=$%.2f | sell=%.4f | limit=$%.2f | order=%s",
                    reason.upper(),
                    contract.slug,
                    side_label,
                    current_price,
                    anchor,
                    stop_price,
                    sell_qty,
                    sell_limit,
                    order_id[:16] if order_id else "n/a",
                )
            except Exception as exc:
                LOGGER.warning(
                    "[SCALP %s FAILED] %s | side=%s | current=$%.2f | anchor=$%.4f | stop=$%.2f | sell=%.4f | limit=$%.2f | %s",
                    reason.upper(),
                    contract.slug,
                    side_label,
                    current_price,
                    anchor,
                    stop_price,
                    sell_qty,
                    sell_limit,
                    exc,
                )

    def _volume_scalp_arm_take_profit_for_side(self, contract: ActiveContract, side_label: str) -> None:
        token = contract.up if side_label == "UP" else contract.down
        baseline = self._baseline_up_balance if side_label == "UP" else self._baseline_down_balance
        d = self._token_delta(token, baseline)
        if d <= VOLUME_T10_SERIAL_INVENTORY_EPS:
            return
        shares = self._up_shares if side_label == "UP" else self._down_shares
        if shares < 1:
            return
        eff_anchor = self._volume_scalp_effective_entry_anchor(side_label)
        want_tp = self._volume_scalp_tp_from_buy_limit(side_label)
        if want_tp is None:
            return

        managed = self._exit_orders_by_side.get(side_label)
        if managed is not None:
            ok = managed.purpose == "scalp_tp" and abs(managed.price - want_tp) <= 0.02
            if ok:
                return
            LOGGER.warning(
                "[SCALP TP REPLACE] %s | cancel %s exit purpose=%s price=$%.2f (want scalp_tp $%.2f)",
                contract.slug,
                side_label,
                managed.purpose,
                managed.price,
                want_tp,
            )
            self._cancel_venue_order(managed.order_id, reason="scalp-tp-replace")
            self._exit_orders_by_side.pop(side_label, None)
            self._fully_exited_sides.discard(side_label)

        off = self._volume_scalp_tp_offset_dollars()
        LOGGER.info(
            "[SCALP TP ARM] %s | side=%s | entry_anchor=$%.4f | offset=$%.4f | tp=$%.2f (anchor+offset)",
            contract.slug,
            side_label,
            float(eff_anchor) if eff_anchor is not None else 0.0,
            off,
            want_tp,
        )
        self._ensure_exit_sell(contract, side_label, want_tp, purpose="scalp_tp")

    def _choose_volume_scalp_candidate(
        self,
        contract: ActiveContract,
        snapshot: BookSnapshot,
        elapsed: float,
    ) -> OrderCandidate | None:
        last_reason = ""
        for side_label in ("UP", "DOWN"):
            self._no_signal_reason = ""
            cand = self._choose_volume_scalp_candidate_for_side(contract, snapshot, elapsed, side_label)
            if cand is not None:
                return cand
            if self._no_signal_reason:
                last_reason = self._no_signal_reason
        self._no_signal_reason = last_reason or "volume_scalp: no UP or DOWN entry"
        return None

    def _choose_volume_scalp_candidate_for_side(
        self,
        contract: ActiveContract,
        snapshot: BookSnapshot,
        elapsed: float,
        side_label: str,
    ) -> OrderCandidate | None:
        block = self._volume_scalp_blocks_new_entry(contract, side_label)
        if block is not None:
            self._no_signal_reason = block
            return None
        emin = float(self.config.volume_scalp_entry_min_elapsed)
        emax = float(min(self.config.volume_scalp_entry_max_elapsed, self.config.window_size_seconds - 30))
        if not (emin <= elapsed <= emax):
            self._no_signal_reason = f"outside entry window ({int(emin)}s–{int(emax)}s elapsed)"
            return None
        if side_label == "UP":
            if snapshot.primary_price <= 0 or self._last_up_price is None or self._last_up_price <= 0:
                self._no_signal_reason = "waiting for UP price"
                return None
        elif self._last_down_price is None or self._last_down_price <= 0:
            self._no_signal_reason = "waiting for DOWN price"
            return None
        btc_return = self._volume_t10_btc_return()
        if btc_return is None:
            self._no_signal_reason = "waiting for btc open/current price"
            return None
        if side_label == "UP":
            if btc_return <= 0:
                self._no_signal_reason = "volume_scalp UP requires BTC up from window open"
                return None
        elif btc_return >= 0:
            self._no_signal_reason = "volume_scalp DOWN requires BTC down from window open"
            return None
        volume_ratio = self._volume_t10_latest_volume_ratio()
        if volume_ratio is None:
            self._no_signal_reason = "waiting for 30s BTC volume baseline"
            return None
        if volume_ratio <= self.config.volume_scalp_volume_ratio:
            self._no_signal_reason = (
                f"volume ratio {volume_ratio:.2f} <= {self.config.volume_scalp_volume_ratio:.2f}"
            )
            return None
        side_price = self._side_price(side_label)
        if not (VOLUME_ENTRY_MIN_PRICE < side_price <= VOLUME_SCALP_ENTRY_MAX_PRICE):
            self._no_signal_reason = (
                f"volume scalp {side_label} price outside entry band (max ${VOLUME_SCALP_ENTRY_MAX_PRICE:.2f})"
            )
            return None
        shares = max(POLY_MIN_LIMIT_SHARES, int(self.config.volume_scalp_shares))
        tag = "scalp_up" if side_label == "UP" else "scalp_down"
        return OrderCandidate(
            side_label=side_label,
            kind="primary",
            reference_price=side_price,
            limit_ceiling=VOLUME_SCALP_ENTRY_MAX_PRICE,
            reason=f"volume_scalp|side={side_label}|ratio={volume_ratio:.2f}|btc_ret={btc_return:.5f}",
            shares=shares,
            min_shares=POLY_MIN_LIMIT_SHARES,
            strategy_tag=tag,
            execution_style="taker_best_ask",
        )

    def _choose_volume_t10_candidate(
        self,
        contract: ActiveContract,
        snapshot: BookSnapshot,
        elapsed: float,
        seconds_remaining: float,
    ) -> OrderCandidate | None:
        self._no_signal_reason = ""
        block = self._volume_t10_serial_scalp_blocks_entry(contract)
        if block is not None:
            self._no_signal_reason = f"serial scalp: {block}"
            return None
        if snapshot.primary_price <= 0 or snapshot.hedge_price <= 0:
            self._no_signal_reason = "waiting for both side prices"
            return None
        btc_return = self._volume_t10_btc_return()
        if btc_return is None:
            self._no_signal_reason = "waiting for btc open/current price"
            return None

        if VOLUME_ENTRY_MIN_ELAPSED_SECONDS <= elapsed <= VOLUME_ENTRY_MAX_ELAPSED_SECONDS:
            volume_ratio = self._volume_t10_latest_volume_ratio()
            if volume_ratio is None:
                self._no_signal_reason = "waiting for 30s BTC volume baseline"
                return None
            if volume_ratio > VOLUME_RATIO_THRESHOLD:
                if btc_return == 0:
                    self._no_signal_reason = "volume spike but BTC direction is flat"
                    return None
                side_label = "UP" if btc_return > 0 else "DOWN"
                side_price = self._side_price(side_label)
                if not (VOLUME_ENTRY_MIN_PRICE < side_price <= VOLUME_ENTRY_MAX_PRICE):
                    self._no_signal_reason = f"volume {side_label} price outside entry band"
                    return None
                return OrderCandidate(
                    side_label=side_label,
                    kind="primary",
                    reference_price=side_price,
                    limit_ceiling=VOLUME_ENTRY_MAX_PRICE,
                    reason=f"volume|ratio={volume_ratio:.2f}|btc_ret={btc_return:.5f}",
                    shares=self._volume_t10_target_shares(side_price),
                    min_shares=POLY_MIN_LIMIT_SHARES,
                    strategy_tag="volume",
                    execution_style="taker_best_ask",
                )

        if elapsed < T10_MIN_WINDOW_ELAPSED_SECONDS:
            self._no_signal_reason = f"waiting for T10 regime (elapsed<{int(T10_MIN_WINDOW_ELAPSED_SECONDS)})"
            return None
        if not (T10_ENTRY_END_SECONDS_REMAINING <= seconds_remaining <= T10_ENTRY_START_SECONDS_REMAINING):
            self._no_signal_reason = "outside T10 entry window"
            return None
        if abs(btc_return) < T10_MIN_BTC_DELTA:
            self._no_signal_reason = "btc delta below T10 minimum"
            return None

        side_label = "UP" if btc_return > 0 else "DOWN"
        entry_price = self._side_price(side_label)
        pair_sum = self._side_price("UP") + self._side_price("DOWN")
        if pair_sum > T10_PAIR_SUM_TARGET:
            self._no_signal_reason = "pair sum too rich for T10 maker"
            return None
        if not (T10_ENTRY_MIN_PRICE < entry_price <= T10_ENTRY_MAX_PRICE):
            self._no_signal_reason = "T10 price outside entry band"
            return None
        return OrderCandidate(
            side_label=side_label,
            kind="primary",
            reference_price=entry_price,
            limit_ceiling=T10_ENTRY_MAX_PRICE,
            reason=f"t10_hybrid|pair={pair_sum:.3f}|btc_ret={btc_return:.5f}",
            shares=self._volume_t10_target_shares(entry_price),
            min_shares=POLY_MIN_LIMIT_SHARES,
            strategy_tag="t10",
            post_only=seconds_remaining >= 5.0,
            fee_rate_bps=MAKER_FEE_RATE_BPS,
            execution_style=(
                "maker_signal"
                if seconds_remaining >= 10.0
                else "maker_best_bid"
                if seconds_remaining >= 5.0
                else "taker_best_ask"
            ),
        )

    def _s0_choose_meta_action(self, elapsed: float) -> str | None:
        if self._s0_meta_action is not None:
            return self._s0_meta_action
        if elapsed < S0_META_DECISION_DELAY_SECONDS:
            return None

        features = self._s0_early_features()
        early_lead_avg = float(features["early_lead_avg"])
        quote_total_120 = float(features["quote_total_120"])
        peak_quote_ratio_5_180 = float(features["peak_quote_ratio_5_180"])
        btc_range_120 = float(features["btc_range_120"])
        early_mid_balance_secs = int(features["early_mid_balance_secs"])

        if early_lead_avg <= 0.23320441988950277:
            if quote_total_120 <= 1079767.5516235:
                if quote_total_120 <= 325108.6770675:
                    if peak_quote_ratio_5_180 <= 4.27854932672317:
                        action = "H5"
                    elif peak_quote_ratio_5_180 <= 9.182846333983301:
                        action = "local_172"
                    else:
                        action = "filtered"
                else:
                    if btc_range_120 <= 0.00037842668751906497:
                        if peak_quote_ratio_5_180 <= 10.600512119638067:
                            action = "H2"
                        else:
                            action = "H1"
                    elif early_lead_avg <= 0.20038674033149173:
                        action = "local_153"
                    else:
                        action = "local_172"
            else:
                if early_lead_avg <= 0.0958011049723757:
                    action = "H2" if peak_quote_ratio_5_180 <= 9.182846333983301 else "H5"
                elif early_lead_avg <= 0.20038674033149173:
                    action = "skip" if btc_range_120 <= 0.001094429907128999 else "local_172"
                elif btc_range_120 <= 0.001338485459039942:
                    action = "H5"
                else:
                    action = "filtered"
        else:
            if peak_quote_ratio_5_180 <= 5.816636653896287:
                if early_lead_avg <= 0.2854696132596685:
                    action = "H2"
                elif early_mid_balance_secs <= 0:
                    action = "local_153"
                else:
                    action = "filtered"
            else:
                if quote_total_120 <= 818659.4879733:
                    action = "robust" if quote_total_120 <= 545077.0388798 else "H5"
                elif early_mid_balance_secs <= 14:
                    action = "skip" if quote_total_120 <= 3596079.4533483 else "H5"
                elif quote_total_120 <= 2611394.6330247:
                    action = "filtered"
                else:
                    action = "baseline"

        self._s0_meta_action = action
        profiles: dict[str, dict[str, float | str]] = {
            "baseline": S0_META_BASELINE,
            "local_172": S0_META_LOCAL_172,
            "local_153": S0_META_LOCAL_153,
            "filtered": S0_META_FILTERED,
            "H5": S0_META_H5,
            "robust": S0_META_ROBUST,
            "deep064": S0_META_DEEP064,
            "H2": S0_META_H2,
            "H1": S0_META_H1,
        }
        self._s0_meta_profile = profiles.get(action)
        LOGGER.info(
            "[S0 META] %s | action=%s | early_lead_avg=%.4f | quote_total_120=%.2f | peak_quote_ratio_5_180=%.3f | "
            "btc_range_120=%.6f | early_mid_balance_secs=%d",
            self._current_window_slug,
            action,
            early_lead_avg,
            quote_total_120,
            peak_quote_ratio_5_180,
            btc_range_120,
            early_mid_balance_secs,
        )
        return self._s0_meta_action

    def _choose_wd_candidate(
        self,
        snapshot: BookSnapshot,
        elapsed: float,
        seconds_remaining: float,
    ) -> OrderCandidate | None:
        self._no_signal_reason = ""
        lot = self.config.shares_per_level
        if elapsed < WD_DECISION_DELAY_SECONDS:
            self._no_signal_reason = f"wd waiting for {WD_DECISION_DELAY_SECONDS}s filter window"
            return None

        action = self._wd_choose_action(elapsed)
        if action is None:
            self._no_signal_reason = "wd decision pending"
            return None
        if action == "skip":
            self._no_signal_reason = "wd skipped window"
            return None

        target_directional_ratio = self._wd_profile_value("target_directional_ratio")
        target_guarantee_ratio = self._wd_profile_value("target_guarantee_ratio")
        late_trend_start_seconds = self._wd_profile_value("late_trend_start_seconds")
        primary_price_min = self._wd_profile_value("primary_price_min")
        primary_price_max = self._wd_profile_value("primary_price_max")
        primary_price_soft_max = self._wd_profile_value("primary_price_soft_max")
        primary_price_hard_max = self._wd_profile_value("primary_price_hard_max")
        hedge_max_price = self._wd_profile_value("hedge_max_price")
        late_hedge_max_price = self._wd_profile_value("late_hedge_max_price")
        primary_target_share = self._wd_profile_value("primary_target_share")
        hedge_target_share = self._wd_profile_value("hedge_target_share")
        late_repair_seconds = self._wd_profile_value("late_repair_seconds")
        late_trend_target_share = self._wd_profile_value("late_trend_target_share")
        late_trend_hedge_max_price = self._wd_profile_value("late_trend_hedge_max_price")
        late_trend_min_win_pnl = self._wd_profile_value("late_trend_min_win_pnl")

        if snapshot.primary_price <= 0 or snapshot.hedge_price <= 0:
            self._no_signal_reason = "waiting for both side prices"
            return None
        if seconds_remaining <= self.config.strategy_new_order_cutoff_seconds:
            self._no_signal_reason = "past new-order cutoff"
            return None
        if (
            snapshot.total_spend > 0
            and snapshot.directional_ratio >= target_directional_ratio
            and snapshot.guarantee_ratio >= target_guarantee_ratio
        ):
            self._no_signal_reason = "coverage targets already met"
            return None

        if elapsed >= late_trend_start_seconds:
            trend_side = self._late_trend_side()
            if trend_side is not None:
                trend_price = self._side_price(trend_side)
                hedge_side = "DOWN" if trend_side == "UP" else "UP"
                hedge_price = self._side_price(hedge_side)
                trend_shares = snapshot.up_shares if trend_side == "UP" else snapshot.down_shares
                hedge_shares = snapshot.down_shares if trend_side == "UP" else snapshot.up_shares
                total_sh = trend_shares + hedge_shares
                trend_share_ratio = (trend_shares / total_sh) if total_sh else 0.0
                trend_if_win = snapshot.up_pnl_if_win if trend_side == "UP" else snapshot.down_pnl_if_win
                pair_ok = snapshot.pair_avg_sum <= S0_PAIR_AVG_MAX or snapshot.guarantee_ratio >= 0.95
                if (
                    trend_price <= primary_price_soft_max
                    and pair_ok
                    and (
                        trend_shares <= hedge_shares
                        or trend_share_ratio < late_trend_target_share
                        or trend_if_win < late_trend_min_win_pnl
                    )
                ):
                    return OrderCandidate(
                        side_label=trend_side,
                        kind="primary",
                        reference_price=trend_price,
                        limit_ceiling=min(primary_price_hard_max, trend_price + 0.05),
                        reason="wd_late_winner_press",
                        shares=lot,
                    )
                if hedge_price <= late_trend_hedge_max_price and snapshot.guarantee_ratio < 0.92:
                    return OrderCandidate(
                        side_label=hedge_side,
                        kind="hedge",
                        reference_price=hedge_price,
                        limit_ceiling=min(late_trend_hedge_max_price, hedge_price + 0.05),
                        reason="wd_late_trend_hedge",
                        shares=lot,
                    )
                if trend_side != snapshot.primary_side:
                    self._no_signal_reason = "late trend lock (winner vs primary)"
                    return None

        if snapshot.total_spend == 0:
            if primary_price_min <= snapshot.primary_price <= primary_price_soft_max:
                return OrderCandidate(
                    side_label=snapshot.primary_side,
                    kind="primary",
                    reference_price=snapshot.primary_price,
                    limit_ceiling=min(primary_price_hard_max, snapshot.primary_price + 0.05),
                    reason="wd_initial_primary",
                    shares=lot,
                )
            self._no_signal_reason = "initial primary outside entry band"
            return None

        if (
            seconds_remaining <= late_repair_seconds
            and snapshot.guarantee_ratio < 0.90
            and snapshot.hedge_price <= late_hedge_max_price
        ):
            return OrderCandidate(
                side_label=snapshot.hedge_side,
                kind="hedge",
                reference_price=snapshot.hedge_price,
                limit_ceiling=min(late_hedge_max_price, snapshot.hedge_price + 0.05),
                reason="wd_late_repair",
                shares=lot,
            )

        if (
            snapshot.primary_spend_share < primary_target_share
            and primary_price_min <= snapshot.primary_price <= primary_price_max
        ):
            return OrderCandidate(
                side_label=snapshot.primary_side,
                kind="primary",
                reference_price=snapshot.primary_price,
                limit_ceiling=min(primary_price_hard_max, snapshot.primary_price + 0.05),
                reason="wd_build_primary",
                shares=lot,
            )

        if (
            snapshot.hedge_spend_share < hedge_target_share
            and snapshot.hedge_price <= hedge_max_price
            and snapshot.guarantee_ratio < 0.95
        ):
            return OrderCandidate(
                side_label=snapshot.hedge_side,
                kind="hedge",
                reference_price=snapshot.hedge_price,
                limit_ceiling=min(hedge_max_price, snapshot.hedge_price + 0.05),
                reason="wd_cheap_hedge",
                shares=lot,
            )

        if snapshot.directional_ratio < target_directional_ratio and snapshot.primary_price <= primary_price_soft_max:
            return OrderCandidate(
                side_label=snapshot.primary_side,
                kind="primary",
                reference_price=snapshot.primary_price,
                limit_ceiling=min(primary_price_hard_max, snapshot.primary_price + 0.05),
                reason="wd_coverage_primary",
                shares=lot,
            )

        if snapshot.guarantee_ratio < 0.85 and snapshot.hedge_price <= late_hedge_max_price:
            return OrderCandidate(
                side_label=snapshot.hedge_side,
                kind="hedge",
                reference_price=snapshot.hedge_price,
                limit_ceiling=min(late_hedge_max_price, snapshot.hedge_price + 0.05),
                reason="wd_repair_hedge",
                shares=lot,
            )

        self._no_signal_reason = "wd: no entry rule fired"
        return None

    def _choose_strategy_0_candidate(
        self,
        snapshot: BookSnapshot,
        elapsed: float,
        seconds_remaining: float,
    ) -> OrderCandidate | None:
        """Primary/hedge redeem logic aligned with run_named_profiles ``strategy_0`` / price-history replay."""
        self._no_signal_reason = ""
        lot = self.config.shares_per_level
        if elapsed < S0_META_DECISION_DELAY_SECONDS:
            self._no_signal_reason = f"strategy_0 meta waiting for {S0_META_DECISION_DELAY_SECONDS}s decision window"
            return None

        action = self._s0_choose_meta_action(elapsed)
        if action is None:
            self._no_signal_reason = "strategy_0 meta decision pending"
            return None
        if action == "skip":
            self._no_signal_reason = "strategy_0 meta skipped window"
            return None

        target_directional_ratio = self._s0_profile_value("target_directional_ratio")
        target_guarantee_ratio = self._s0_profile_value("target_guarantee_ratio")
        late_trend_start_seconds = self._s0_profile_value("late_trend_start_seconds")
        primary_price_min = self._s0_profile_value("primary_price_min")
        primary_price_max = self._s0_profile_value("primary_price_max")
        primary_price_soft_max = self._s0_profile_value("primary_price_soft_max")
        primary_price_hard_max = self._s0_profile_value("primary_price_hard_max")
        hedge_max_price = self._s0_profile_value("hedge_max_price")
        late_hedge_max_price = self._s0_profile_value("late_hedge_max_price")
        primary_target_share = self._s0_profile_value("primary_target_share")
        hedge_target_share = self._s0_profile_value("hedge_target_share")
        late_repair_seconds = self._s0_profile_value("late_repair_seconds")
        late_trend_target_share = self._s0_profile_value("late_trend_target_share")
        late_trend_hedge_max_price = self._s0_profile_value("late_trend_hedge_max_price")
        late_trend_min_win_pnl = self._s0_profile_value("late_trend_min_win_pnl")

        if snapshot.primary_price <= 0 or snapshot.hedge_price <= 0:
            self._no_signal_reason = "waiting for both side prices"
            return None
        if seconds_remaining <= self.config.strategy_new_order_cutoff_seconds:
            self._no_signal_reason = "past new-order cutoff"
            return None
        if (
            snapshot.total_spend > 0
            and snapshot.directional_ratio >= target_directional_ratio
            and snapshot.guarantee_ratio >= target_guarantee_ratio
        ):
            self._no_signal_reason = "coverage targets already met"
            return None

        if elapsed >= late_trend_start_seconds:
            trend_side = self._late_trend_side()
            if trend_side is not None:
                trend_price = self._side_price(trend_side)
                hedge_side = "DOWN" if trend_side == "UP" else "UP"
                hedge_price = self._side_price(hedge_side)
                trend_shares = snapshot.up_shares if trend_side == "UP" else snapshot.down_shares
                hedge_shares = snapshot.down_shares if trend_side == "UP" else snapshot.up_shares
                total_sh = trend_shares + hedge_shares
                trend_share_ratio = (trend_shares / total_sh) if total_sh else 0.0
                trend_if_win = snapshot.up_pnl_if_win if trend_side == "UP" else snapshot.down_pnl_if_win
                pair_ok = snapshot.pair_avg_sum <= S0_PAIR_AVG_MAX or snapshot.guarantee_ratio >= 0.95
                if (
                    trend_price <= primary_price_soft_max
                    and pair_ok
                    and (
                        trend_shares <= hedge_shares
                        or trend_share_ratio < late_trend_target_share
                        or trend_if_win < late_trend_min_win_pnl
                    )
                ):
                    return OrderCandidate(
                        side_label=trend_side,
                        kind="primary",
                        reference_price=trend_price,
                        limit_ceiling=min(primary_price_hard_max, trend_price + 0.05),
                        reason="s0_late_winner_press",
                        shares=lot,
                    )
                if hedge_price <= late_trend_hedge_max_price and snapshot.guarantee_ratio < 0.92:
                    return OrderCandidate(
                        side_label=hedge_side,
                        kind="hedge",
                        reference_price=hedge_price,
                        limit_ceiling=min(late_trend_hedge_max_price, hedge_price + 0.05),
                        reason="s0_late_trend_hedge",
                        shares=lot,
                    )
                if trend_side != snapshot.primary_side:
                    self._no_signal_reason = "late trend lock (winner vs primary)"
                    return None

        if snapshot.total_spend == 0:
            if primary_price_min <= snapshot.primary_price <= primary_price_soft_max:
                return OrderCandidate(
                    side_label=snapshot.primary_side,
                    kind="primary",
                    reference_price=snapshot.primary_price,
                    limit_ceiling=min(primary_price_hard_max, snapshot.primary_price + 0.05),
                    reason="s0_initial_primary",
                    shares=lot,
                )
            self._no_signal_reason = "initial primary outside entry band"
            return None

        if (
            seconds_remaining <= late_repair_seconds
            and snapshot.guarantee_ratio < 0.90
            and snapshot.hedge_price <= late_hedge_max_price
        ):
            return OrderCandidate(
                side_label=snapshot.hedge_side,
                kind="hedge",
                reference_price=snapshot.hedge_price,
                limit_ceiling=min(late_hedge_max_price, snapshot.hedge_price + 0.05),
                reason="s0_late_repair",
                shares=lot,
            )

        if (
            snapshot.primary_spend_share < primary_target_share
            and primary_price_min <= snapshot.primary_price <= primary_price_max
        ):
            return OrderCandidate(
                side_label=snapshot.primary_side,
                kind="primary",
                reference_price=snapshot.primary_price,
                limit_ceiling=min(primary_price_hard_max, snapshot.primary_price + 0.05),
                reason="s0_build_primary",
                shares=lot,
            )

        if (
            snapshot.hedge_spend_share < hedge_target_share
            and snapshot.hedge_price <= hedge_max_price
            and snapshot.guarantee_ratio < 0.95
        ):
            return OrderCandidate(
                side_label=snapshot.hedge_side,
                kind="hedge",
                reference_price=snapshot.hedge_price,
                limit_ceiling=min(hedge_max_price, snapshot.hedge_price + 0.05),
                reason="s0_cheap_hedge",
                shares=lot,
            )

        if snapshot.directional_ratio < target_directional_ratio and snapshot.primary_price <= primary_price_soft_max:
            return OrderCandidate(
                side_label=snapshot.primary_side,
                kind="primary",
                reference_price=snapshot.primary_price,
                limit_ceiling=min(primary_price_hard_max, snapshot.primary_price + 0.05),
                reason="s0_coverage_primary",
                shares=lot,
            )

        if snapshot.guarantee_ratio < 0.85 and snapshot.hedge_price <= late_hedge_max_price:
            return OrderCandidate(
                side_label=snapshot.hedge_side,
                kind="hedge",
                reference_price=snapshot.hedge_price,
                limit_ceiling=min(late_hedge_max_price, snapshot.hedge_price + 0.05),
                reason="s0_repair_hedge",
                shares=lot,
            )

        self._no_signal_reason = "strategy_0: no entry rule fired"
        return None

    def _aa1_pair_cap_for_lead(self, lead_shares: int) -> float:
        lead_orders = max(0, int(lead_shares // self.config.shares_per_level))
        if lead_orders <= 1:
            return 0.90
        if lead_orders == 2:
            return 0.92
        if lead_orders == 3:
            return 0.94
        return 0.97

    def _aa1_record_cheap_fill(self, side_label: str, price: float, elapsed: float) -> None:
        self._aa1_cheap_lot_counts[side_label] += 1
        self._aa1_last_cheap_buy_elapsed[side_label] = elapsed
        self._aa1_last_cheap_buy_price[side_label] = price
        self._aa1_unmatched_lots[side_label].append(
            AA1CheapLot(
                cheap_side=side_label,
                cheap_price=price,
                elapsed_sec=elapsed,
            )
        )

    def _aa1_mark_balance_fill(self, reason: str) -> None:
        parts = reason.split("|")
        if len(parts) < 3:
            return
        cheap_side = parts[1]
        try:
            cheap_price = float(parts[2])
        except ValueError:
            return
        lots = self._aa1_unmatched_lots.get(cheap_side, [])
        for idx, lot in enumerate(lots):
            if abs(lot.cheap_price - cheap_price) < 0.011:
                lots.pop(idx)
                return

    def _aa1_choose_balance_candidate(self) -> OrderCandidate | None:
        candidates: list[tuple[float, OrderCandidate]] = []
        for cheap_side in ("UP", "DOWN"):
            lots = self._aa1_unmatched_lots[cheap_side]
            if not lots:
                continue
            balance_side = "DOWN" if cheap_side == "UP" else "UP"
            balance_price = self._side_price(balance_side)
            if balance_price <= 0:
                continue
            lead = (self._up_shares - self._down_shares) if cheap_side == "UP" else (self._down_shares - self._up_shares)
            pair_cap = self._aa1_pair_cap_for_lead(lead)
            balanceable = [(idx, lot) for idx, lot in enumerate(lots) if balance_price <= round(pair_cap - lot.cheap_price, 4)]
            if not balanceable:
                continue
            _, target_lot = min(balanceable, key=lambda item: item[1].cheap_price)
            candidates.append(
                (
                    target_lot.cheap_price,
                    OrderCandidate(
                        side_label=balance_side,
                        kind="primary",
                        reference_price=balance_price,
                        limit_ceiling=min(PRIMARY_PRICE_HARD_MAX, balance_price + 0.05),
                        reason=f"aa1_balance|{cheap_side}|{target_lot.cheap_price:.2f}|{pair_cap:.2f}",
                        shares=self.config.shares_per_level,
                    ),
                )
            )
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[0])[1]

    def _choose_box_balance_candidate(
        self,
        snapshot: BookSnapshot,
        elapsed: float,
        _seconds_remaining: float,
    ) -> OrderCandidate | None:
        """Two-sided book, slow adds; stop new buys only when both settlement PnLs are above threshold."""
        self._no_signal_reason = ""
        lot = self.config.shares_per_level
        up_px = self._last_up_price or 0.0
        down_px = self._last_down_price or 0.0
        if up_px <= 0 or down_px <= 0:
            self._no_signal_reason = "waiting for both side prices"
            return None

        pu, pd = snapshot.up_pnl_if_win, snapshot.down_pnl_if_win
        if pu > BOX_BOTH_WAYS_MIN_PNL_USDC and pd > BOX_BOTH_WAYS_MIN_PNL_USDC:
            self._no_signal_reason = (
                f"box: both outcomes profitable (if_UP=${pu:.2f} if_DOWN=${pd:.2f}) — stop new risk"
            )
            return None

        u, d = int(snapshot.up_shares), int(snapshot.down_shares)
        imb = u - d
        imbalance_orders = abs(imb) // lot if lot else 0
        force_balance = imbalance_orders >= BOX_MAX_OPEN_IMBALANCE_ORDERS
        lag_side: str | None = "DOWN" if imb > 0 else "UP" if imb < 0 else None

        pair_sum = up_px + down_px
        spread = abs(up_px - down_px)
        winner = "UP" if up_px >= down_px else "DOWN"
        loser = "DOWN" if winner == "UP" else "UP"
        win_px = up_px if winner == "UP" else down_px
        lose_px = down_px if winner == "UP" else up_px

        def allow_side(side: str, balance_priority: bool) -> tuple[bool, str]:
            if self._box_lot_counts[side] >= BOX_MAX_LOTS_PER_SIDE:
                return False, f"box: max lots/side ({BOX_MAX_LOTS_PER_SIDE}) on {side}"
            prev = self._box_last_side_elapsed[side]
            cooldown = BOX_BALANCE_COOLDOWN_SECONDS if balance_priority else BOX_SIDE_COOLDOWN_SECONDS
            if prev is not None and elapsed - prev < cooldown:
                return False, f"box: {cooldown}s cooldown on {side}"
            ref = up_px if side == "UP" else down_px
            if lot * ref < MIN_MARKETABLE_BUY_NOTIONAL - 1e-9:
                return False, f"box: {side} below venue min notional"
            return True, ""

        def make(side: str, ref: float, reason: str) -> OrderCandidate | None:
            bal_pri = force_balance and side == lag_side
            ok, msg = allow_side(side, bal_pri)
            if not ok:
                self._no_signal_reason = msg
                return None
            return OrderCandidate(
                side_label=side,
                kind="primary",
                reference_price=ref,
                limit_ceiling=min(PRIMARY_PRICE_HARD_MAX, ref + 0.05),
                reason=reason,
                shares=lot,
            )

        if lag_side and force_balance:
            ref = down_px if lag_side == "DOWN" else up_px
            c = make(lag_side, ref, "box_balance|hedge_lagging")
            if c:
                return c
            return None

        if u == 0 and d == 0:
            open_ok = BOX_OPEN_PAIR_MIN <= pair_sum <= BOX_OPEN_PAIR_MAX and spread <= BOX_OPEN_SPREAD_MAX
            fallback_ok = (
                elapsed >= BOX_OPEN_FALLBACK_ELAPSED_SECONDS
                and pair_sum <= BOX_OPEN_FALLBACK_PAIR_MAX
                and spread <= BOX_OPEN_FALLBACK_SPREAD_MAX
                and min(up_px, down_px) >= 0.03
            )
            patience_ok = (
                elapsed >= BOX_OPEN_PATIENCE_ELAPSED_SECONDS
                and pair_sum <= BOX_OPEN_PATIENCE_PAIR_MAX
                and spread <= BOX_OPEN_PATIENCE_SPREAD_MAX
                and 0.02 <= min(up_px, down_px)
                and max(up_px, down_px) <= 0.97
            )
            if not open_ok and not fallback_ok and not patience_ok:
                self._no_signal_reason = "box: wait for openable pair"
                return None
            side = "UP" if up_px <= down_px else "DOWN"
            ref = up_px if side == "UP" else down_px
            if open_ok:
                tag = "box_balance|open_first_leg"
            elif fallback_ok:
                tag = "box_balance|open_first_leg_fallback"
            else:
                tag = "box_balance|open_first_leg_patience"
            return make(side, ref, tag)

        if u == 0 or d == 0:
            missing = "DOWN" if u > 0 else "UP"
            ref = down_px if missing == "DOWN" else up_px
            if pair_sum <= BOX_SECOND_LEG_PAIR_MAX and spread <= BOX_SECOND_LEG_SPREAD_MAX:
                c = make(missing, ref, "box_balance|complete_second_leg")
                if c:
                    return c
            self._no_signal_reason = "box: wait for second leg at reasonable pair"
            return None

        if BOX_PAIR_MIN <= pair_sum <= BOX_PAIR_MAX and spread <= BOX_PAIR_SPREAD_MAX:
            if u < d:
                c = make("UP", up_px, "box_balance|pair_balance_up")
                if c:
                    return c
            elif d < u:
                c = make("DOWN", down_px, "box_balance|pair_balance_down")
                if c:
                    return c
            else:
                side = "UP" if up_px <= down_px else "DOWN"
                ref = up_px if side == "UP" else down_px
                c = make(side, ref, "box_balance|pair_add_even")
                if c:
                    return c

        if lose_px <= BOX_LOSER_PRICE_MAX and spread >= BOX_LOSER_SPREAD_MIN:
            c = make(loser, lose_px, "box_balance|loser_scoop")
            if c:
                return c

        if spread >= BOX_WINNER_SPREAD_MIN and BOX_WINNER_PRICE_MIN <= win_px <= BOX_WINNER_PRICE_MAX:
            c = make(winner, win_px, "box_balance|winner_lean")
            if c:
                return c

        self._no_signal_reason = "box: no rule fired"
        return None

    def _mimic_evaluate_tick(self, elapsed: float) -> None:
        """Append mimic buy intents to the queue (same rules as fit_wallet10_mimic.simulate_window_fixed_lot)."""
        self._no_signal_reason = ""
        if not self._strategy_mode_mimic():
            return
        p = self._mimic_params
        up_price = self._last_up_price or 0.0
        down_price = self._last_down_price or 0.0
        if up_price <= 0 or down_price <= 0:
            self._no_signal_reason = "waiting for both side prices"
            return

        entry_delay = float(p["entry_delay"])
        cutoff = float(p["cutoff"])
        if elapsed < entry_delay:
            self._no_signal_reason = "before mimic entry_delay"
            return
        if elapsed > cutoff:
            self._no_signal_reason = "past mimic elapsed cutoff"
            return

        lot = self.config.shares_per_level
        pair_sum = up_price + down_price
        spread = abs(up_price - down_price)
        winner = "UP" if up_price >= down_price else "DOWN"
        loser = "DOWN" if winner == "UP" else "UP"
        win_price = up_price if winner == "UP" else down_price
        lose_price = down_price if winner == "UP" else up_price

        if winner == self._mimic_prev_winner:
            self._mimic_consecutive_same_winner += 1
        else:
            self._mimic_consecutive_same_winner = 1
        self._mimic_prev_winner = winner

        def min_notional_ok(px: float) -> bool:
            return lot * px >= MIN_MARKETABLE_BUY_NOTIONAL - 1e-9

        def ceiling(ref: float) -> float:
            return min(PRIMARY_PRICE_HARD_MAX, ref + 0.05)

        if (
            pair_sum >= p["pair_min"]
            and pair_sum <= p["pair_max"]
            and spread <= p["pair_spread_max"]
            and elapsed - self._mimic_last_pair_elapsed >= p["pair_cooldown"]
            and min_notional_ok(up_price)
            and min_notional_ok(down_price)
        ):
            self._mimic_action_queue.append(
                OrderCandidate(
                    side_label="UP",
                    kind="primary",
                    reference_price=up_price,
                    limit_ceiling=ceiling(up_price),
                    reason="mimic_pair_up",
                    shares=lot,
                )
            )
            self._mimic_action_queue.append(
                OrderCandidate(
                    side_label="DOWN",
                    kind="primary",
                    reference_price=down_price,
                    limit_ceiling=ceiling(down_price),
                    reason="mimic_pair_down",
                    shares=lot,
                )
            )
            self._mimic_last_pair_elapsed = elapsed

        if (
            spread >= p["winner_spread_min"]
            and win_price >= p["winner_price_min"]
            and win_price <= p["winner_price_max"]
            and self._mimic_consecutive_same_winner >= p["winner_confirm"]
            and elapsed - self._mimic_last_winner_elapsed >= p["winner_cooldown"]
            and min_notional_ok(win_price)
        ):
            side = winner
            ref = win_price
            self._mimic_action_queue.append(
                OrderCandidate(
                    side_label=side,
                    kind="primary",
                    reference_price=ref,
                    limit_ceiling=ceiling(ref),
                    reason="mimic_winner_lean",
                    shares=lot,
                )
            )
            self._mimic_last_winner_elapsed = elapsed

        if (
            lose_price <= p["loser_price_max"]
            and spread >= p["loser_spread_min"]
            and elapsed - self._mimic_last_loser_elapsed >= p["loser_cooldown"]
            and min_notional_ok(lose_price)
        ):
            side = loser
            ref = lose_price
            self._mimic_action_queue.append(
                OrderCandidate(
                    side_label=side,
                    kind="primary",
                    reference_price=ref,
                    limit_ceiling=ceiling(ref),
                    reason="mimic_loser_scoop",
                    shares=lot,
                )
            )
            self._mimic_last_loser_elapsed = elapsed

        if (
            elapsed >= p["reversal_start"]
            and spread >= p["reversal_spread_min"]
            and lose_price >= p["reversal_loser_min"]
            and lose_price <= p["reversal_loser_max"]
            and elapsed - self._mimic_last_reversal_elapsed >= p["reversal_cooldown"]
            and min_notional_ok(lose_price)
        ):
            side = loser
            ref = lose_price
            self._mimic_action_queue.append(
                OrderCandidate(
                    side_label=side,
                    kind="primary",
                    reference_price=ref,
                    limit_ceiling=ceiling(ref),
                    reason="mimic_reversal",
                    shares=lot,
                )
            )
            self._mimic_last_reversal_elapsed = elapsed

        if (
            elapsed >= p["lottery_start"]
            and lose_price <= p["lottery_price_max"]
            and elapsed - self._mimic_last_lottery_elapsed >= p["lottery_cooldown"]
            and min_notional_ok(lose_price)
        ):
            side = loser
            ref = lose_price
            self._mimic_action_queue.append(
                OrderCandidate(
                    side_label=side,
                    kind="primary",
                    reference_price=ref,
                    limit_ceiling=ceiling(ref),
                    reason="mimic_lottery",
                    shares=lot,
                )
            )
            self._mimic_last_lottery_elapsed = elapsed

        if not self._mimic_action_queue and not self._no_signal_reason:
            self._no_signal_reason = "mimic: no rule fired this tick"

    def _iy2_price_lookback(self, side_label: str, lookback_seconds: int) -> float | None:
        if not self._price_history:
            return None
        target_ts = self._last_price_time - lookback_seconds
        for point in reversed(self._price_history):
            if point.ts <= target_ts:
                return point.up_price if side_label == "UP" else point.down_price
        oldest = self._price_history[0]
        return oldest.up_price if side_label == "UP" else oldest.down_price

    def _iy2_btc_price_lookback(self, lookback_seconds: int) -> float | None:
        if not self._btc_price_history:
            return None
        target_ts = self._last_price_time - lookback_seconds
        for point in reversed(self._btc_price_history):
            if point.ts <= target_ts:
                return point.price
        return self._btc_price_history[0].price if self._btc_price_history else None

    def _iy2_roi_pair(self, snapshot: BookSnapshot) -> tuple[float, float]:
        total = snapshot.total_spend
        if total <= 0:
            return 0.0, 0.0
        return snapshot.up_pnl_if_win / total, snapshot.down_pnl_if_win / total

    def _iy2_shares_for_notional(self, notional: float, ref_price: float) -> tuple[int, int]:
        lot = max(1, self.config.shares_per_level)
        safe_price = max(0.01, round(ref_price, 2))
        raw_shares = max(
            self._minimum_order_shares(safe_price),
            int(math.ceil(max(notional, MIN_MARKETABLE_BUY_NOTIONAL) / safe_price)),
        )
        rounded = int(math.ceil(raw_shares / lot) * lot)
        return rounded, max(lot, self._minimum_order_shares(safe_price))

    def _iy2_max_shares_for_reason(self, reason: str) -> int:
        lot = max(1, self.config.shares_per_level)
        caps = {
            "base": 10,
            "winner": 15,
            "hedge": 10,
            "repair": 10,
            "late": 10,
            "maintenance": 10,
            "rebalance": 15,
            "safety": 15,
            "value": 10,
            "deep": 15,
        }
        for key, cap in caps.items():
            if f"|{key}|" in reason:
                return max(lot, int(math.ceil(cap / lot) * lot))
        return max(lot, 15)

    def _iy2_queue_candidate(
        self,
        queue: deque[OrderCandidate],
        side_label: str,
        notional: float,
        reason: str,
        *,
        kind: str = "primary",
    ) -> bool:
        ref = self._side_price(side_label)
        if ref <= 0:
            return False
        last_fill_elapsed = self._iy2_last_fill_elapsed.get(side_label, -10_000.0)
        last_fill_price = self._iy2_last_fill_price.get(side_label)
        if (
            last_fill_price is not None
            and (self._current_elapsed - last_fill_elapsed) <= 120.0
            and abs(ref - last_fill_price) <= 0.03
        ):
            return False
        shares, min_shares = self._iy2_shares_for_notional(notional, ref)
        max_shares = self._iy2_max_shares_for_reason(reason)
        if min_shares > max_shares:
            return False
        shares = min(shares, max_shares)
        queue.append(
            OrderCandidate(
                side_label=side_label,
                kind=kind,
                reference_price=ref,
                limit_ceiling=min(0.99, round(ref + 0.05, 2)),
                reason=reason,
                shares=shares,
                min_shares=min_shares,
            )
        )
        return True

    def _iy2_wallet_bucket_target(self, bucket_idx: int, budget_cap: float) -> dict[str, float | str]:
        rows = self._iy2_params.get("bucket_targets")
        if not isinstance(rows, list) or not rows:
            label = f"{bucket_idx*30:03d}-{bucket_idx*30+29:03d}"
            return {
                "bucket_label": label,
                "up_notional_target": 0.0,
                "down_notional_target": 0.0,
                "avg_up_target": 0.0,
                "avg_down_target": 0.0,
                "up_shares_target": 0.0,
                "down_shares_target": 0.0,
                "up_share_ratio_0_1": 0.5,
            }
        idx = min(max(int(bucket_idx), 0), len(rows) - 1)
        row = rows[idx] if isinstance(rows[idx], dict) else {}
        up_notional = budget_cap * float(row.get("cum_up_notional_frac", 0.0) or 0.0)
        down_notional = budget_cap * float(row.get("cum_down_notional_frac", 0.0) or 0.0)
        avg_up_target = float(row.get("avg_up", 0.0) or 0.0)
        avg_down_target = float(row.get("avg_down", 0.0) or 0.0)
        up_shares_target = (up_notional / avg_up_target) if avg_up_target > 0 else 0.0
        down_shares_target = (down_notional / avg_down_target) if avg_down_target > 0 else 0.0
        return {
            "bucket_label": str(row.get("bucket_label") or f"{bucket_idx*30:03d}-{bucket_idx*30+29:03d}"),
            "up_notional_target": up_notional,
            "down_notional_target": down_notional,
            "avg_up_target": avg_up_target,
            "avg_down_target": avg_down_target,
            "up_shares_target": up_shares_target,
            "down_shares_target": down_shares_target,
            "up_share_ratio_0_1": float(row.get("up_share_ratio_0_1", 0.5) or 0.5),
        }

    def _iy2_wallet_path_distance(
        self,
        up_shares: float,
        down_shares: float,
        up_cost: float,
        down_cost: float,
        target: dict[str, float | str],
        budget_cap: float,
        freeze_roi: float,
    ) -> float:
        total_cost = up_cost + down_cost
        if total_cost > 0:
            up_roi = (up_shares / total_cost) - 1.0
            down_roi = (down_shares / total_cost) - 1.0
        else:
            up_roi = down_roi = 0.0
        target_up_shares = float(target.get("up_shares_target", 0.0) or 0.0)
        target_down_shares = float(target.get("down_shares_target", 0.0) or 0.0)
        target_up_notional = float(target.get("up_notional_target", 0.0) or 0.0)
        target_down_notional = float(target.get("down_notional_target", 0.0) or 0.0)
        target_avg_up = float(target.get("avg_up_target", 0.0) or 0.0)
        target_avg_down = float(target.get("avg_down_target", 0.0) or 0.0)
        target_ratio = float(target.get("up_share_ratio_0_1", 0.5) or 0.5)
        size_gap = (
            abs(up_shares - target_up_shares) / max(target_up_shares, float(max(1, self.config.shares_per_level)))
            + abs(down_shares - target_down_shares) / max(target_down_shares, float(max(1, self.config.shares_per_level)))
        )
        notional_gap = (
            abs(up_cost - target_up_notional) / max(budget_cap, 1.0)
            + abs(down_cost - target_down_notional) / max(budget_cap, 1.0)
        )
        avg_gap = 0.0
        if target_up_shares > 0 and up_shares > 0 and target_avg_up > 0:
            avg_gap += abs((up_cost / up_shares) - target_avg_up)
        if target_down_shares > 0 and down_shares > 0 and target_avg_down > 0:
            avg_gap += abs((down_cost / down_shares) - target_avg_down)
        total_shares = up_shares + down_shares
        ratio = (up_shares / total_shares) if total_shares > 0 else 0.5
        ratio_gap = abs(ratio - target_ratio)
        risk_gap = max(0.0, freeze_roi - up_roi) + max(0.0, freeze_roi - down_roi)
        return 1.5 * size_gap + 1.1 * notional_gap + 0.9 * avg_gap + 0.8 * ratio_gap + 0.35 * risk_gap

    @staticmethod
    def _iy2_dominant_side(up_shares: float, down_shares: float) -> str:
        if up_shares == down_shares:
            return "UP"
        return "UP" if up_shares > down_shares else "DOWN"

    @staticmethod
    def _iy2_dominant_ratio(up_shares: float, down_shares: float) -> float:
        total = up_shares + down_shares
        return max(up_shares, down_shares) / total if total > 0 else 0.5

    def _iy2_required_repair_side(self, up_shares: float, down_shares: float) -> str | None:
        if up_shares > 0 and down_shares <= 0:
            return "DOWN"
        if down_shares > 0 and up_shares <= 0:
            return "UP"
        if up_shares > down_shares:
            return "DOWN"
        if down_shares > up_shares:
            return "UP"
        return None

    def _iy2_projected_worst_roi(
        self,
        up_shares: float,
        down_shares: float,
        up_cost: float,
        down_cost: float,
        side_label: str,
        fill_price: float,
        shares: int,
    ) -> float:
        next_up_shares = up_shares + (shares if side_label == "UP" else 0.0)
        next_down_shares = down_shares + (shares if side_label == "DOWN" else 0.0)
        next_up_cost = up_cost + (fill_price * shares if side_label == "UP" else 0.0)
        next_down_cost = down_cost + (fill_price * shares if side_label == "DOWN" else 0.0)
        next_total_cost = next_up_cost + next_down_cost
        if next_total_cost <= 0:
            return 0.0
        up_roi = (next_up_shares / next_total_cost) - 1.0
        down_roi = (next_down_shares / next_total_cost) - 1.0
        return min(up_roi, down_roi)

    def _iy2_weak_side_name(
        self,
        up_shares: float,
        down_shares: float,
        up_cost: float,
        down_cost: float,
    ) -> str:
        total_cost = up_cost + down_cost
        if total_cost <= 0:
            return "UP" if up_shares <= down_shares else "DOWN"
        up_roi = (up_shares / total_cost) - 1.0
        down_roi = (down_shares / total_cost) - 1.0
        if abs(up_roi - down_roi) <= 1e-9:
            return "UP" if up_shares <= down_shares else "DOWN"
        return "UP" if up_roi < down_roi else "DOWN"

    def _iy2_pair_sum(self, up_shares: float, down_shares: float, up_cost: float, down_cost: float) -> float:
        up_avg = (up_cost / up_shares) if up_shares > 0 else 0.0
        down_avg = (down_cost / down_shares) if down_shares > 0 else 0.0
        return up_avg + down_avg

    def _iy2_cheap_accumulation_override(
        self,
        *,
        current_up_shares: float,
        current_down_shares: float,
        current_up_cost: float,
        current_down_cost: float,
        side_label: str,
        fill_price: float,
        shares: int,
        target_avg: float,
        cheap_buffer: float,
        weak_roi_floor: float,
    ) -> bool:
        current_total_cost = current_up_cost + current_down_cost
        if current_total_cost <= 0:
            return False
        if (side_label == "UP" and current_up_shares <= 0) or (side_label == "DOWN" and current_down_shares <= 0):
            return False
        weak_side = self._iy2_weak_side_name(
            current_up_shares,
            current_down_shares,
            current_up_cost,
            current_down_cost,
        )
        if side_label != weak_side:
            return False
        next_up_shares = current_up_shares + (shares if side_label == "UP" else 0.0)
        next_down_shares = current_down_shares + (shares if side_label == "DOWN" else 0.0)
        if abs(next_up_shares - next_down_shares) > IY2_MAX_IMBALANCE_SHARES:
            return False
        next_up_cost = current_up_cost + (fill_price * shares if side_label == "UP" else 0.0)
        next_down_cost = current_down_cost + (fill_price * shares if side_label == "DOWN" else 0.0)
        current_worst_roi = min(
            (current_up_shares / current_total_cost) - 1.0,
            (current_down_shares / current_total_cost) - 1.0,
        )
        projected_worst_roi = self._iy2_projected_worst_roi(
            current_up_shares,
            current_down_shares,
            current_up_cost,
            current_down_cost,
            side_label,
            fill_price,
            shares,
        )
        current_side_shares = current_up_shares if side_label == "UP" else current_down_shares
        current_side_cost = current_up_cost if side_label == "UP" else current_down_cost
        current_avg = (current_side_cost / current_side_shares) if current_side_shares > 0 else 0.0
        current_pair = self._iy2_pair_sum(current_up_shares, current_down_shares, current_up_cost, current_down_cost)
        projected_pair = self._iy2_pair_sum(next_up_shares, next_down_shares, next_up_cost, next_down_cost)
        cheap_enough = (
            fill_price <= 0.20
            or (target_avg > 0 and fill_price <= (target_avg + cheap_buffer))
            or (current_avg > 0 and fill_price <= max(0.01, current_avg - 0.02))
        )
        pair_improves = projected_pair <= (current_pair - 0.02)
        allowed_floor = min(current_worst_roi - 0.03, weak_roi_floor - 0.02)
        roi_not_much_worse = projected_worst_roi >= allowed_floor
        return cheap_enough and pair_improves and roi_not_much_worse

    def _iy2_target_proportion_override(
        self,
        *,
        current_up_shares: float,
        current_down_shares: float,
        current_up_cost: float,
        current_down_cost: float,
        side_label: str,
        fill_price: float,
        shares: int,
        target: dict[str, Any],
        budget_cap: float,
    ) -> bool:
        current_total_cost = current_up_cost + current_down_cost
        current_total_shares = current_up_shares + current_down_shares
        if current_total_cost <= 0 or current_total_shares <= 0:
            return False
        next_up_shares = current_up_shares + (shares if side_label == "UP" else 0.0)
        next_down_shares = current_down_shares + (shares if side_label == "DOWN" else 0.0)
        if abs(next_up_shares - next_down_shares) > IY2_MAX_IMBALANCE_SHARES:
            return False
        next_total_cost = current_total_cost + (fill_price * shares)
        target_total_notional = float(target.get("up_notional_target", 0.0) or 0.0) + float(
            target.get("down_notional_target", 0.0) or 0.0
        )
        path_spend_scale = float(self._iy2_params.get("path_spend_scale", 0.55) or 0.55)
        path_budget = min(budget_cap, target_total_notional * path_spend_scale)
        target_ratio = float(target.get("up_share_ratio_0_1", 0.5) or 0.5)
        current_ratio = current_up_shares / current_total_shares if current_total_shares > 0 else 0.5
        next_total_shares = next_up_shares + next_down_shares
        next_ratio = next_up_shares / next_total_shares if next_total_shares > 0 else current_ratio
        ratio_gain = abs(current_ratio - target_ratio) - abs(next_ratio - target_ratio)
        weak_side = self._iy2_weak_side_name(
            current_up_shares,
            current_down_shares,
            current_up_cost,
            current_down_cost,
        )
        extreme_cheap_max = float(self._iy2_params.get("extreme_cheap_max_price", 0.12) or 0.12)
        under_path_budget = next_total_cost <= (path_budget + 1e-9)
        extreme_cheap_follow = side_label == weak_side and fill_price <= extreme_cheap_max and ratio_gain >= -0.05
        return (ratio_gain > 1e-9 and under_path_budget) or extreme_cheap_follow

    def _iy2_evaluate_tick(self, snapshot: BookSnapshot, elapsed: float, seconds_remaining: float) -> None:
        self._no_signal_reason = ""
        if not self._strategy_mode_iy2():
            return
        up_price = self._last_up_price or 0.0
        down_price = self._last_down_price or 0.0
        if up_price <= 0 or down_price <= 0:
            self._no_signal_reason = "iy2: waiting for both side prices"
            return
        if seconds_remaining <= self.config.strategy_new_order_cutoff_seconds:
            self._no_signal_reason = "iy2: past new-order cutoff"
            return

        p = self._iy2_params
        fail_cooldown = float(p.get("fail_cooldown_seconds", 8.0) or 8.0)
        queue: deque[OrderCandidate] = deque()
        local_up_shares = float(snapshot.up_shares)
        local_down_shares = float(snapshot.down_shares)
        local_up_cost = float(snapshot.up_spend)
        local_down_cost = float(snapshot.down_spend)
        freeze_roi = float(p.get("freeze_roi", 0.05) or 0.05)
        improvement_min = float(p.get("improvement_min", 0.015) or 0.015)
        cheap_buffer = float(p.get("cheap_buffer", 0.03) or 0.03)
        dedupe_seconds = float(p.get("same_side_dedupe_seconds", 30.0) or 30.0)
        dedupe_band = float(p.get("same_side_dedupe_price_band", 0.02) or 0.02)
        candidate_share_cap = max(1, self.config.shares_per_level)
        dominant_ratio_soft = float(p.get("dominant_ratio_soft", 0.62) or 0.62)
        dominant_ratio_hard = float(p.get("dominant_ratio_hard", 0.68) or 0.68)
        budget_cap = max(1.0, float(self._window_budget_usdc or self.config.strategy_budget_cap_usdc))
        total_cost = local_up_cost + local_down_cost
        up_roi = (local_up_shares / total_cost) - 1.0 if total_cost > 0 else 0.0
        down_roi = (local_down_shares / total_cost) - 1.0 if total_cost > 0 else 0.0
        if total_cost > 0 and up_roi >= freeze_roi and down_roi >= freeze_roi:
            self._no_signal_reason = "iy2: both-side freeze threshold reached"
            return

        bucket_idx = min(max(int(elapsed // 30), 0), 29)
        target = self._iy2_wallet_bucket_target(bucket_idx, budget_cap)
        current_distance = self._iy2_wallet_path_distance(
            local_up_shares,
            local_down_shares,
            local_up_cost,
            local_down_cost,
            target,
            budget_cap,
            freeze_roi,
        )
        current_min_roi = min(up_roi, down_roi) if total_cost > 0 else 0.0
        current_total_shares = local_up_shares + local_down_shares
        current_dom_ratio = self._iy2_dominant_ratio(local_up_shares, local_down_shares) if current_total_shares > 0 else 0.5
        current_dominant_side = self._iy2_dominant_side(local_up_shares, local_down_shares)
        current_weak_side = self._iy2_weak_side_name(local_up_shares, local_down_shares, local_up_cost, local_down_cost)
        best_candidate: tuple[float, float, str, int] | None = None
        required_side = self._iy2_required_repair_side(local_up_shares, local_down_shares)

        for side_label, ref in (("UP", up_price), ("DOWN", down_price)):
            if required_side is not None and side_label != required_side:
                continue
            if (elapsed - self._iy2_last_fail_elapsed.get(side_label, -10_000.0)) < fail_cooldown:
                continue
            if ref <= 0:
                continue
            last_fill_elapsed = self._iy2_last_fill_elapsed.get(side_label, -10_000.0)
            last_fill_price = self._iy2_last_fill_price.get(side_label)
            if (
                last_fill_price is not None
                and (elapsed - last_fill_elapsed) <= dedupe_seconds
                and abs(ref - float(last_fill_price)) <= dedupe_band
            ):
                continue
            target_notional = float(target["up_notional_target"] if side_label == "UP" else target["down_notional_target"])
            current_notional = local_up_cost if side_label == "UP" else local_down_cost
            target_avg = float(target["avg_up_target"] if side_label == "UP" else target["avg_down_target"])
            notional_gap = target_notional - current_notional
            for shares in range(max(1, self.config.shares_per_level), candidate_share_cap + 1, max(1, self.config.shares_per_level)):
                candidate_cost = shares * ref
                if (total_cost + candidate_cost) > (budget_cap + 1e-9):
                    continue
                next_up_shares = local_up_shares + (shares if side_label == "UP" else 0.0)
                next_down_shares = local_down_shares + (shares if side_label == "DOWN" else 0.0)
                next_up_cost = local_up_cost + (candidate_cost if side_label == "UP" else 0.0)
                next_down_cost = local_down_cost + (candidate_cost if side_label == "DOWN" else 0.0)
                next_total_cost = next_up_cost + next_down_cost
                next_up_roi = (next_up_shares / next_total_cost) - 1.0 if next_total_cost > 0 else 0.0
                next_down_roi = (next_down_shares / next_total_cost) - 1.0 if next_total_cost > 0 else 0.0
                next_total_shares = next_up_shares + next_down_shares
                next_dom_ratio = self._iy2_dominant_ratio(next_up_shares, next_down_shares) if next_total_shares > 0 else 0.5
                next_dominant_side = self._iy2_dominant_side(next_up_shares, next_down_shares)
                current_ratio = (local_up_shares / current_total_shares) if current_total_shares > 0 else 0.5
                next_ratio = (next_up_shares / next_total_shares) if next_total_shares > 0 else current_ratio
                target_ratio = float(target.get("up_share_ratio_0_1", 0.5) or 0.5)
                ratio_gain = abs(current_ratio - target_ratio) - abs(next_ratio - target_ratio)
                if abs(next_up_shares - next_down_shares) > IY2_MAX_IMBALANCE_SHARES:
                    continue
                next_distance = self._iy2_wallet_path_distance(
                    next_up_shares,
                    next_down_shares,
                    next_up_cost,
                    next_down_cost,
                    target,
                    budget_cap,
                    freeze_roi,
                )
                improvement = current_distance - next_distance
                price_ok = target_avg <= 0 or ref <= (target_avg + cheap_buffer)
                risk_improves = min(next_up_roi, next_down_roi) > (current_min_roi + 0.002)
                target_catchup = notional_gap > 0.01
                worsens_floor = total_cost > 0 and min(next_up_roi, next_down_roi) < (current_min_roi - 0.003)
                lopsided_worsens = (
                    total_cost > 0
                    and next_dom_ratio > dominant_ratio_soft
                    and next_dom_ratio > current_dom_ratio + 0.01
                    and next_dominant_side == side_label
                )
                dominant_chase = (
                    total_cost > 0
                    and side_label == current_dominant_side
                    and current_dom_ratio >= 0.55
                    and not risk_improves
                    and not price_ok
                )
                side_missing = (side_label == "UP" and local_up_shares <= 0) or (side_label == "DOWN" and local_down_shares <= 0)
                bootstrap_ok = (total_cost <= 0 and side_missing and target_catchup and price_ok) or (
                    total_cost > 0 and current_total_shares > 0 and side_missing and target_catchup and price_ok
                )
                cheap_accum_ok = (
                    total_cost > 0
                    and not side_missing
                    and side_label == current_weak_side
                    and self._iy2_cheap_accumulation_override(
                        current_up_shares=local_up_shares,
                        current_down_shares=local_down_shares,
                        current_up_cost=local_up_cost,
                        current_down_cost=local_down_cost,
                        side_label=side_label,
                        fill_price=ref,
                        shares=shares,
                        target_avg=target_avg,
                        cheap_buffer=cheap_buffer,
                        weak_roi_floor=float(target.get("weak_roi_floor", 0.0) or 0.0),
                    )
                )
                target_prop_ok = self._strategy_mode_iy3() and self._iy2_target_proportion_override(
                    current_up_shares=local_up_shares,
                    current_down_shares=local_down_shares,
                    current_up_cost=local_up_cost,
                    current_down_cost=local_down_cost,
                    side_label=side_label,
                    fill_price=ref,
                    shares=shares,
                    target=target,
                    budget_cap=budget_cap,
                )
                if total_cost > 0 and not side_missing and min(next_up_roi, next_down_roi) <= current_min_roi + 1e-9 and not cheap_accum_ok and not target_prop_ok:
                    continue
                if next_dom_ratio > dominant_ratio_hard and not bootstrap_ok and not cheap_accum_ok and not target_prop_ok:
                    continue
                if ((worsens_floor or lopsided_worsens) and not cheap_accum_ok and not target_prop_ok) or dominant_chase:
                    continue
                situation_gain = (
                    (min(next_up_roi, next_down_roi) - current_min_roi)
                    - max(0.0, next_dom_ratio - current_dom_ratio)
                    + max(0.0, ratio_gain) * 3.0
                )
                if (
                    improvement >= improvement_min and (price_ok or risk_improves or target_catchup or bootstrap_ok)
                ) or cheap_accum_ok or target_prop_ok:
                    if best_candidate is None or (situation_gain, improvement) > (best_candidate[0], best_candidate[1]):
                        best_candidate = (situation_gain, improvement, side_label, shares)

        if best_candidate is None:
            self._no_signal_reason = "iy2: no wallet-path improvement buy this tick"
            return

        _, _, side_label, shares = best_candidate
        ref = self._side_price(side_label)
        reason = f"iy2|wallet_path|side={side_label}|bucket={target['bucket_label']}|alt=1"
        queue.append(
            OrderCandidate(
                side_label=side_label,
                kind="primary",
                reference_price=ref,
                limit_ceiling=min(0.99, round(ref + 0.05, 2)),
                reason=reason,
                shares=shares,
                min_shares=shares,
                execution_style="normal",
            )
        )
        self._iy2_action_queue.extend(queue)

    def _choose_aa1_candidate(
        self,
        snapshot: BookSnapshot,
        elapsed: float,
        seconds_remaining: float,
    ) -> OrderCandidate | None:
        self._no_signal_reason = ""

        if snapshot.primary_price <= 0 or snapshot.hedge_price <= 0:
            self._no_signal_reason = "waiting for both side prices"
            return None

        if seconds_remaining <= self.config.strategy_new_order_cutoff_seconds:
            self._no_signal_reason = "past new-order cutoff"
            return None

        balance_candidate = self._aa1_choose_balance_candidate()
        if balance_candidate is not None:
            return balance_candidate

        up_price = self._side_price("UP")
        down_price = self._side_price("DOWN")
        cheap_side = "UP" if up_price < down_price else "DOWN"
        cheap_price = up_price if cheap_side == "UP" else down_price
        if cheap_price > AA1_CHEAP_ENTRY_MAX:
            self._no_signal_reason = "cheap side above entry max"
            return None
        imbalance_orders = abs(snapshot.up_shares - snapshot.down_shares) // self.config.shares_per_level
        if imbalance_orders >= AA1_MAX_OPEN_IMBALANCE_ORDERS:
            self._no_signal_reason = "book too imbalanced; waiting for balance"
            return None
        if self._aa1_cheap_lot_counts[cheap_side] >= AA1_MAX_CHEAP_LOTS_PER_SIDE:
            self._no_signal_reason = f"max cheap lots reached on {cheap_side}"
            return None

        prev_elapsed = self._aa1_last_cheap_buy_elapsed[cheap_side]
        prev_price = self._aa1_last_cheap_buy_price[cheap_side]
        if prev_elapsed is not None and elapsed - prev_elapsed < AA1_CHEAP_COOLDOWN_SECONDS:
            self._no_signal_reason = f"cheap-buy cooldown active on {cheap_side}"
            return None
        if prev_price is not None and cheap_price > round(prev_price - AA1_CHEAP_REPEAT_DROP, 4):
            self._no_signal_reason = f"cheap price has not improved enough on {cheap_side}"
            return None

        return OrderCandidate(
            side_label=cheap_side,
            kind="primary",
            reference_price=cheap_price,
            limit_ceiling=min(PRIMARY_PRICE_HARD_MAX, cheap_price + 0.05),
            reason="aa1_buy_cheap",
            shares=self.config.shares_per_level,
        )

    def _can_place_candidate(
        self,
        snapshot: BookSnapshot,
        candidate: OrderCandidate,
        open_orders: list[dict[str, Any]],
        elapsed: float,
    ) -> bool:
        if self._strategy_mode_iy2() and self._order_map:
            self._no_signal_reason = "iy2 waiting for prior buy order resolution"
            return False
        if self._strategy_mode_iy2():
            fail_cooldown = float(self._iy2_params.get("fail_cooldown_seconds", 8.0) or 8.0)
            if (elapsed - self._iy2_last_fail_elapsed.get(candidate.side_label, -10_000.0)) < fail_cooldown:
                self._no_signal_reason = f"iy2 fail cooldown active for {candidate.side_label}"
                return False

        if time.time() - self._last_order_time < self.config.order_cooldown_seconds:
            self._no_signal_reason = "order cooldown active"
            return False

        if len(open_orders) >= self.config.strategy_max_live_orders:
            self._no_signal_reason = "live order cap reached"
            return False

        allow_same_side = self._strategy_mode_btc_perp15() and candidate.reason.startswith("btc_perp15|entry|side=UP")
        if (not allow_same_side) and any(order.side_label == candidate.side_label for order in self._order_map.values()):
            self._no_signal_reason = f"pending {candidate.side_label} order already live"
            return False

        phase_cap = self._phase_cap_usdc(elapsed)
        estimated_limit = round(
            min(candidate.limit_ceiling, candidate.reference_price + self.config.strategy_price_buffer),
            2,
        )
        scaled_shares = self._scaled_order_shares(
            candidate.reference_price,
            candidate.shares,
            elapsed,
            min_shares=candidate.min_shares,
        )
        if self._strategy_mode_iy2():
            scaled_shares = min(scaled_shares, max(1, self.config.shares_per_level))
        if scaled_shares <= 0:
            self._no_signal_reason = "insufficient remaining budget for venue-min order"
            return False
        candidate.shares = scaled_shares
        reference_notional = estimated_limit * candidate.shares

        committed = self._up_spend + self._down_spend + sum(order.notional for order in self._order_map.values())
        if committed + reference_notional > phase_cap:
            self._no_signal_reason = "phase budget exhausted"
            return False

        if committed + reference_notional > self._window_budget_usdc:
            self._no_signal_reason = "window budget exhausted"
            return False

        return True

    def _place_candidate(
        self,
        contract: ActiveContract,
        candidate: OrderCandidate,
        snapshot: BookSnapshot,
        elapsed: float,
    ) -> bool:
        token = contract.up if candidate.side_label == "UP" else contract.down
        limit_price, order_type = self._resolve_entry_order(
            token,
            candidate.reference_price,
            candidate.limit_ceiling,
            execution_style=candidate.execution_style,
            post_only=candidate.post_only,
        )
        if limit_price is None:
            self._no_signal_reason = "no executable entry price available"
            return False
        shares = candidate.shares
        order_kind = candidate.kind.upper()
        actual_notional = limit_price * shares
        if actual_notional < MIN_MARKETABLE_BUY_NOTIONAL:
            self._no_signal_reason = (
                f"buy notional ${actual_notional:.2f} below ${MIN_MARKETABLE_BUY_NOTIONAL:.2f} venue minimum"
            )
            return False

        phase_cap = self._phase_cap_usdc(elapsed)
        committed = self._up_spend + self._down_spend + sum(order.notional for order in self._order_map.values())
        if committed + actual_notional > phase_cap:
            self._no_signal_reason = "phase budget exhausted by actual limit"
            return False
        if committed + actual_notional > self._window_budget_usdc:
            self._no_signal_reason = "window budget exhausted by actual limit"
            return False

        projection = self._project_book(snapshot, candidate.side_label, limit_price, shares)
        if self._strategy_mode_iy2():
            required_side = self._iy2_required_repair_side(float(snapshot.up_shares), float(snapshot.down_shares))
            if required_side is not None and candidate.side_label != required_side:
                self._no_signal_reason = f"iy2 can only buy weaker side {required_side}"
                return False
            current_up_shares = float(self._up_shares)
            current_down_shares = float(self._down_shares)
            current_up_cost = float(self._up_spend)
            current_down_cost = float(self._down_spend)
            current_total_cost = current_up_cost + current_down_cost
            current_worst_roi = (
                min((current_up_shares / current_total_cost) - 1.0, (current_down_shares / current_total_cost) - 1.0)
                if current_total_cost > 0
                else 0.0
            )
            next_up = snapshot.up_shares + (shares if candidate.side_label == "UP" else 0)
            next_down = snapshot.down_shares + (shares if candidate.side_label == "DOWN" else 0)
            if abs(next_up - next_down) > IY2_MAX_IMBALANCE_SHARES:
                self._no_signal_reason = "iy2 projected imbalance exceeds 5 shares"
                return False
            side_missing = (candidate.side_label == "UP" and current_up_shares <= 0) or (
                candidate.side_label == "DOWN" and current_down_shares <= 0
            )
            projected_worst_roi = self._iy2_projected_worst_roi(
                current_up_shares,
                current_down_shares,
                current_up_cost,
                current_down_cost,
                candidate.side_label,
                limit_price,
                shares,
            )
            target = self._iy2_wallet_bucket_target(
                min(max(int(elapsed // 30), 0), 29),
                max(1.0, float(self._window_budget_usdc or self.config.strategy_budget_cap_usdc)),
            )
            cheap_buffer = float(self._iy2_params.get("cheap_buffer", 0.03) or 0.03)
            target_avg = float(target["avg_up_target"] if candidate.side_label == "UP" else target["avg_down_target"])
            cheap_accum_ok = self._iy2_cheap_accumulation_override(
                current_up_shares=current_up_shares,
                current_down_shares=current_down_shares,
                current_up_cost=current_up_cost,
                current_down_cost=current_down_cost,
                side_label=candidate.side_label,
                fill_price=limit_price,
                shares=shares,
                target_avg=target_avg,
                cheap_buffer=cheap_buffer,
                weak_roi_floor=float(target.get("weak_roi_floor", 0.0) or 0.0),
            )
            target_prop_ok = self._strategy_mode_iy3() and self._iy2_target_proportion_override(
                current_up_shares=current_up_shares,
                current_down_shares=current_down_shares,
                current_up_cost=current_up_cost,
                current_down_cost=current_down_cost,
                side_label=candidate.side_label,
                fill_price=limit_price,
                shares=shares,
                target=target,
                budget_cap=max(1.0, float(self._window_budget_usdc or self.config.strategy_budget_cap_usdc)),
            )
            if current_total_cost > 0 and not side_missing and projected_worst_roi <= current_worst_roi + 1e-9 and not cheap_accum_ok and not target_prop_ok:
                self._no_signal_reason = "iy2 buy does not improve worst-case roi"
                return False

        if self.config.dry_run:
            order_id = "dry_%d_%s" % (int(time.time() * 1000), candidate.side_label.lower())
            self._order_map[order_id] = ManagedOrder(
                order_id=order_id,
                side_label=candidate.side_label,
                kind=candidate.kind,
                price=limit_price,
                shares=shares,
                placed_at=time.time(),
                reason=candidate.reason,
            )
            self._record_order_metrics(candidate.kind)
            sl = self._volume_scalp_strategy_side(candidate.strategy_tag)
            if sl is not None and self._strategy_mode_volume_scalp_up():
                px_parts = [float(limit_price), float(candidate.reference_price)]
                mx = self._side_price(candidate.side_label)
                if mx > 0 and mx < 1:
                    px_parts.append(mx)
                self._volume_scalp_entry_reference_px[sl] = round(min(px_parts), 2)
            LOGGER.info(
                "DRY [%s] %s | side=%s | ref=$%.4f | limit=$%.2f | shares=%d | pair=%.3f | post_only=%s | reason=%s",
                order_kind,
                contract.slug,
                candidate.side_label,
                candidate.reference_price,
                limit_price,
                shares,
                projection["pair_sum"],
                candidate.post_only,
                candidate.reason,
            )
            return True

        try:
            if order_type == "taker" and not self._strategy_mode_iy2():
                resp: dict[str, Any] | None = None
                last_taker_exc: BaseException | None = None
                for fak_attempt in range(2):
                    try:
                        resp = self.trader.place_marketable_buy(
                            token,
                            limit_price,
                            shares,
                            fee_rate_bps=candidate.fee_rate_bps,
                        )
                        break
                    except Exception as taker_exc:
                        last_taker_exc = taker_exc
                        if _is_fak_no_match_order_error(taker_exc) and fak_attempt == 0:
                            LOGGER.warning(
                                "[ORDER FAK RETRY] %s | kind=%s | side=%s | limit=$%.2f | %s",
                                contract.slug,
                                candidate.kind,
                                candidate.side_label,
                                limit_price,
                                taker_exc,
                            )
                            time.sleep(0.25)
                            continue
                        if _is_fak_no_match_order_error(taker_exc):
                            LOGGER.warning(
                                "[ORDER FAK→GTC] %s | kind=%s | side=%s | limit=$%.2f | resting limit: %s",
                                contract.slug,
                                candidate.kind,
                                candidate.side_label,
                                limit_price,
                                taker_exc,
                            )
                            resp = self.trader.place_limit_buy(
                                token,
                                limit_price,
                                shares,
                                fee_rate_bps=candidate.fee_rate_bps,
                                post_only=False,
                            )
                            order_type = "gtc"
                            break
                        raise
                if resp is None:
                    raise last_taker_exc if last_taker_exc else RuntimeError("FAK buy returned no response")
            elif order_type == "taker":
                resp = self.trader.place_marketable_buy(
                    token,
                    limit_price,
                    shares,
                    fee_rate_bps=candidate.fee_rate_bps,
                )
            else:
                resp = self.trader.place_limit_buy(
                    token,
                    limit_price,
                    shares,
                    fee_rate_bps=candidate.fee_rate_bps,
                    post_only=candidate.post_only,
                )
            order_id = str(resp.get("orderID") or resp.get("id") or "")
        except Exception as exc:
            if self._strategy_mode_iy2():
                self._iy2_last_fail_elapsed[candidate.side_label] = elapsed
            self._no_signal_reason = f"order placement failed: {exc}"
            LOGGER.error(
                "[ORDER FAILED] %s | kind=%s | side=%s | limit=$%.2f | %s",
                contract.slug,
                candidate.kind,
                candidate.side_label,
                limit_price,
                exc,
            )
            return False

        if not order_id:
            self._no_signal_reason = "order response missing id"
            LOGGER.warning(
                "[ORDER MISSING ID] %s | kind=%s | side=%s | resp=%s",
                contract.slug,
                candidate.kind,
                candidate.side_label,
                resp,
            )
            return False

        self._order_map[order_id] = ManagedOrder(
            order_id=order_id,
            side_label=candidate.side_label,
            kind=candidate.kind,
            price=limit_price,
            shares=shares,
            placed_at=time.time(),
            reason=candidate.reason,
        )
        self._record_order_metrics(candidate.kind)
        sl = self._volume_scalp_strategy_side(candidate.strategy_tag)
        if sl is not None and self._strategy_mode_volume_scalp_up():
            px_parts = [float(limit_price), float(candidate.reference_price)]
            mx = self._side_price(candidate.side_label)
            if mx > 0 and mx < 1:
                px_parts.append(mx)
            self._volume_scalp_entry_reference_px[sl] = round(min(px_parts), 2)
        LOGGER.info(
            "[ORDER %s] %s | side=%s | ref=$%.4f | limit=$%.2f | shares=%d | pair=%.3f | order=%s | exec=%s | post_only=%s | reason=%s",
            order_kind,
            contract.slug,
            candidate.side_label,
            candidate.reference_price,
            limit_price,
            shares,
            projection["pair_sum"],
            order_id[:16],
            order_type,
            candidate.post_only,
            candidate.reason,
        )
        return True

    def _record_order_metrics(self, kind: str) -> None:
        self._orders_placed += 1
        self._last_order_time = time.time()
        if kind == "primary":
            self._primary_orders += 1
        else:
            self._hedge_orders += 1

    def _resolve_limit_price(
        self,
        token: TokenMarket,
        reference_price: float,
        limit_ceiling: float,
        *,
        post_only: bool = False,
    ) -> float | None:
        proposed = round(min(limit_ceiling, reference_price + self.config.strategy_price_buffer), 2)
        if post_only:
            spread = self.trader.get_spread(token.token_id)
            best_bid = spread.get("best_bid")
            best_ask = spread.get("best_ask")
            if best_bid is None or best_bid <= 0:
                return None
            maker_price = round(min(limit_ceiling, best_bid + MAKER_PRICE_TICK), 2)
            if best_ask is not None and maker_price >= best_ask:
                maker_price = round(best_bid, 2)
            if maker_price < 0.01 or maker_price > limit_ceiling:
                return None
            return max(0.01, min(0.99, maker_price))
        best_ask = self.trader.get_best_ask(token.token_id)
        if best_ask is not None and 0.01 <= best_ask <= limit_ceiling:
            return round(best_ask, 2)
        return max(0.01, min(0.99, proposed))

    def _resolve_entry_order(
        self,
        token: TokenMarket,
        reference_price: float,
        limit_ceiling: float,
        *,
        execution_style: str,
        post_only: bool,
    ) -> tuple[float | None, str]:
        if execution_style == "maker_signal":
            maker_price = round(max(0.01, min(limit_ceiling, reference_price)), 2)
            best_ask = self.trader.get_best_ask(token.token_id)
            if best_ask is not None and maker_price >= best_ask:
                best_bid = self.trader.get_best_bid(token.token_id)
                if best_bid is None or best_bid <= 0:
                    return None, "maker"
                maker_price = round(min(limit_ceiling, best_bid), 2)
            return maker_price, "maker"
        if execution_style == "maker_best_bid":
            best_bid = self.trader.get_best_bid(token.token_id)
            if best_bid is None or best_bid <= 0:
                return None, "maker"
            maker_price = round(max(0.01, min(limit_ceiling, best_bid)), 2)
            return maker_price, "maker"
        if self._strategy_mode_iy2() and execution_style == "taker_best_ask":
            return self._resolve_limit_price(
                token,
                reference_price,
                limit_ceiling,
                post_only=False,
            ), "maker"
        if execution_style == "taker_best_ask":
            best_ask = self.trader.get_best_ask(token.token_id)
            if best_ask is not None and best_ask > 0:
                return round(min(limit_ceiling, best_ask), 2), "taker"
            fallback = round(max(0.01, min(limit_ceiling, reference_price)), 2)
            return fallback, "taker"
        return self._resolve_limit_price(
            token,
            reference_price,
            limit_ceiling,
            post_only=post_only,
        ), "maker"

    def _phase_cap_usdc(self, elapsed: float) -> float:
        if self._strategy_mode_volume_t10() or self._strategy_mode_volume_scalp_up() or self._strategy_mode_btc_perp15():
            return self._window_budget_usdc
        for upper_bound, share in PHASE_SPEND_CAPS:
            if elapsed <= upper_bound:
                return round(self._window_budget_usdc * share, 2)
        return self._window_budget_usdc

    def _minimum_tradable_budget_usdc(self) -> float:
        return MIN_MARKETABLE_BUY_NOTIONAL

    def _has_tradable_budget(self) -> bool:
        return self._window_budget_usdc >= self._minimum_tradable_budget_usdc()

    def _minimum_order_shares(self, price: float) -> int:
        safe_price = max(0.01, round(price, 2))
        return max(1, int(math.ceil((MIN_MARKETABLE_BUY_NOTIONAL - 1e-9) / safe_price)))

    def _scaled_order_shares(
        self,
        reference_price: float,
        requested_shares: int,
        elapsed: float,
        *,
        min_shares: int = 1,
    ) -> int:
        estimated_limit = round(
            min(0.99, max(0.01, reference_price + self.config.strategy_price_buffer)),
            2,
        )
        required_min_shares = max(min_shares, self._minimum_order_shares(estimated_limit))
        committed = self._up_spend + self._down_spend + sum(order.notional for order in self._order_map.values())
        remaining_budget = min(self._phase_cap_usdc(elapsed), self._window_budget_usdc) - committed
        if remaining_budget <= 0:
            return 0
        lot = max(1, self.config.shares_per_level)
        affordable_shares = int((remaining_budget + 1e-9) // estimated_limit)
        affordable_shares = int(affordable_shares // lot) * lot
        if affordable_shares < required_min_shares:
            return 0
        desired_shares = min(max(lot, requested_shares), affordable_shares)
        desired_shares = int(math.ceil(desired_shares / lot) * lot)
        desired_shares = min(desired_shares, affordable_shares)
        if desired_shares < required_min_shares:
            return 0
        return desired_shares

    def _effective_budget(self, wallet_balance_usdc: float) -> float:
        if self.config.dry_run and wallet_balance_usdc <= 0:
            return self.config.strategy_budget_cap_usdc
        return max(
            0.0,
            min(
                self.config.strategy_budget_cap_usdc,
                wallet_balance_usdc - self.config.strategy_wallet_reserve_usdc,
            ),
        )

    def _get_contract_orders(self, contract: ActiveContract) -> list[dict[str, Any]]:
        token_ids = {contract.up.token_id, contract.down.token_id}
        all_orders = self.trader.get_open_orders()
        return [
            order
            for order in all_orders
            if str(
                order.get("asset_id")
                or order.get("assetId")
                or order.get("token_id")
                or order.get("tokenId")
                or ""
            ) in token_ids
        ]

    def _extract_live_ids(self, open_orders: list[dict[str, Any]]) -> set[str]:
        live_ids = set()
        for order in open_orders:
            order_id = str(order.get("id") or order.get("orderID") or "")
            if order_id:
                live_ids.add(order_id)
        return live_ids

    def _cancel_venue_order(self, order_id: str, reason: str) -> None:
        """Cancel by exchange id. Exit sells are not in `_order_map`, so this is used for those ids."""
        if not order_id:
            return
        if self.config.dry_run:
            LOGGER.info("[DRY VENUE CANCEL] order=%s | reason=%s", order_id[:16], reason)
            return
        try:
            self.trader.cancel_order(order_id)
        except Exception as exc:
            LOGGER.warning("[VENUE CANCEL FAILED] order=%s | reason=%s | %s", order_id[:16], reason, exc)

    def _cancel_order_safe(self, order_id: str, reason: str) -> None:
        order = self._order_map.get(order_id)
        if order is None:
            return

        if not self.config.dry_run:
            try:
                self.trader.cancel_order(order_id)
            except Exception as exc:
                LOGGER.warning("[CANCEL FAILED] order=%s | reason=%s | %s", order_id[:16], reason, exc)
                return
        self._order_map.pop(order_id, None)
        self._cancels += 1

    def _tracked_side_shares(self, side_label: str) -> float:
        return float(self._up_shares if side_label == "UP" else self._down_shares)

    def _safe_exit_position_shares(self, contract: ActiveContract, side_label: str) -> float:
        token = contract.up if side_label == "UP" else contract.down
        baseline = self._baseline_up_balance if side_label == "UP" else self._baseline_down_balance
        tracked = max(0.0, self._tracked_side_shares(side_label))
        delta = max(0.0, float(self._token_delta(token, baseline)))
        if tracked <= 0.0:
            return delta
        # Some venue/API balance reads can come back in a scaled/raw unit and massively exceed
        # the actual window inventory. Exit sizing must never trust that larger number.
        if delta <= 0.0 or delta > max(tracked + 5.0, tracked * 2.0):
            return tracked
        return min(tracked, delta)

    def _token_delta(self, token: TokenMarket, baseline: float) -> float:
        return round(self.trader.token_balance(token.token_id) - baseline, 4)

    def _log_heartbeat(
        self,
        contract: ActiveContract,
        snapshot: BookSnapshot,
        open_orders: list[dict[str, Any]],
        elapsed: float,
        seconds_remaining: float,
    ) -> None:
        phase_cap = self._phase_cap_usdc(elapsed)
        LOGGER.info(
            "[HEARTBEAT] %s | elapsed=%ds remaining=%ds | UP=$%.4f DOWN=$%.4f | primary=%s | spend=$%.2f committed=$%.2f cap=$%.2f/$%.2f | shares=%d/%d | avg=%.3f/%.3f pair=%.3f | guarantee=%.3f directional=%.3f | pnl_if_up=$%.2f pnl_if_down=$%.2f | open_orders=%d fills=%d cancels=%d",
            contract.slug,
            int(elapsed),
            int(seconds_remaining),
            self._last_up_price or 0.0,
            self._last_down_price or 0.0,
            snapshot.primary_side,
            snapshot.total_spend,
            snapshot.committed_notional,
            phase_cap,
            self._window_budget_usdc,
            snapshot.up_shares,
            snapshot.down_shares,
            snapshot.up_avg_price,
            snapshot.down_avg_price,
            snapshot.pair_avg_sum,
            snapshot.guarantee_ratio,
            snapshot.directional_ratio,
            snapshot.up_pnl_if_win,
            snapshot.down_pnl_if_win,
            len(open_orders),
            self._fills,
            self._cancels,
        )

    def _maybe_record_price_snapshot(
        self,
        contract: ActiveContract,
        snapshot: BookSnapshot,
        elapsed: float,
        seconds_remaining: float,
    ) -> None:
        return
