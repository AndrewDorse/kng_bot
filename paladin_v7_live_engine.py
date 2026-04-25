#!/usr/bin/env python3
"""
Live PALADIN v7: Binance agg-trade volume + spot move (via RealtimeBtcPriceFeed) + Polymarket mids.

Each poll updates the per-second PM/BTC arrays. ``paladin_v7_step`` is designed for replay: **one call
per integer market second** ``elapsed``. When ``poll_interval_seconds`` is below 1, many polls can share the
same ``elapsed``; we therefore run ``paladin_v7_step`` at most once per ``(slug, elapsed)`` so signals
are not fired twice in the same second (duplicate FAKs). A **set** of fired ``elapsed`` values (not only
``last == elapsed``) avoids re-running an older second if ``elapsed`` ever moves backward (clock skew).
``pending_second`` is re-read **after** reconcile so cutoff/entry-delay gates match post-sync state.
When rebuilding pending from inventory, **preserve** the prior hedge ``t0`` for the same hedge side so
``hedge_timeout_seconds`` is not reset every reconcile (that stranded cheap-failing hedges).
Resting ``v7_hedge_cheap`` GTC orders use a **window-second** cancel deadline aligned with ``paladin_v7_step``
(forced hedge age); the poll loop must **not** skip ``paladin_v7_step`` while that order is merely open.
Live buy POSTs stay serialized in ``PolymarketTrader`` via a lock.
"""

from __future__ import annotations

import math
import signal
import sys
import time
from pathlib import Path
from typing import Any

from btc_price_feed import RealtimeBtcPriceFeed
from config import LOGGER, ActiveContract, BotConfig, TokenMarket
from market_locator import GammaMarketLocator
from py_clob_client.exceptions import PolyApiException
from trader import PolymarketTrader

_PALADIN = Path(__file__).resolve().parent / "PALADIN"
if str(_PALADIN) not in sys.path:
    sys.path.insert(0, str(_PALADIN))

from paladin_v7 import (  # noqa: E402
    PaladinV7Params,
    PaladinV7Runner,
    WindowTick,
    paladin_v7_step,
)
from paladin_engine import apply_buy_fill  # noqa: E402
from simulate_paladin_window import SimState, Trade, try_buy as sim_try_buy  # noqa: E402

# Hard ceiling for any *single* CLOB BUY this engine posts for PALADIN v7 (spike, hedge, flatten).
# Config may mis-read; API fallbacks may spike — this value is never exceeded for order size / adds.
V7_LIVE_MAX_SHARES_PER_SINGLE_ORDER = 5


def _can_afford_live(spent: float, add: float, budget: float) -> bool:
    return spent + add <= budget + 1e-6


def _v7_params_from_config(cfg: BotConfig) -> PaladinV7Params:
    return PaladinV7Params(
        budget_usdc=float(cfg.strategy_budget_cap_usdc),
        base_order_shares=float(cfg.paladin_v7_base_order_shares),
        max_shares_per_side=float(cfg.paladin_v7_max_shares_per_side),
        min_notional=float(cfg.paladin_v7_min_notional),
        min_shares=float(cfg.paladin_v7_min_shares),
        volume_lookback_sec=int(cfg.paladin_v7_volume_lookback_sec),
        volume_spike_ratio=float(cfg.paladin_v7_volume_spike_ratio),
        volume_floor=float(cfg.paladin_v7_volume_floor),
        btc_abs_move_min_usd=float(cfg.paladin_v7_btc_abs_move_min_usd),
        first_leg_max_pm=float(cfg.paladin_v7_first_leg_max_pm),
        balanced_entry_min_pm=float(cfg.paladin_v7_balanced_entry_min_pm),
        balanced_entry_max_pm=float(cfg.paladin_v7_balanced_entry_max_pm),
        # Live-only execution buffer is handled below in try_buy_fn; sim path keeps pure strategy prices.
        cheap_other_margin=float(cfg.paladin_v7_cheap_other_margin),
        cheap_pair_sum_max=float(cfg.paladin_v7_cheap_pair_sum_max),
        cheap_pair_avg_sum_nonforced_max=float(cfg.paladin_v7_cheap_pair_avg_sum_nonforced_max),
        cheap_hedge_slip_buffer=float(cfg.paladin_v7_cheap_hedge_slip_buffer),
        cheap_hedge_min_delay_sec=float(cfg.paladin_v7_cheap_hedge_min_delay_sec),
        hedge_timeout_seconds=float(cfg.paladin_v7_hedge_timeout_seconds),
        forced_hedge_max_book_sum=float(cfg.paladin_v7_forced_hedge_max_book_sum),
        layer2_dip_below_avg=float(cfg.paladin_v7_layer2_dip_below_avg),
        cheap_balance_start_deduction=float(cfg.paladin_v7_cheap_balance_start_deduction),
        layer_level_offset_step=float(cfg.paladin_v7_layer_level_offset_step),
        layer2_low_vwap_dip_below_avg=float(cfg.paladin_v7_layer2_low_vwap_dip_below_avg),
        no_new_layers_last_seconds=float(cfg.paladin_v7_no_new_layers_last_seconds),
        balance_share_tolerance=float(cfg.paladin_v7_balance_share_tolerance),
        imbalance_repair_max_pair_sum=float(cfg.paladin_v7_imbalance_repair_max_pair_sum),
        layer2_cooldown_sec=float(cfg.paladin_v7_layer2_cooldown_sec),
        pair_cooldown_sec=float(cfg.paladin_v7_pair_cooldown_sec),
    )


def _build_ticks(
    sec_pm_u: list[float],
    sec_pm_d: list[float],
    sec_btc_px: list[float],
    sec_btc_vol: list[float],
    elapsed: int,
    window_sec: int = 900,
) -> list[WindowTick]:
    lu, ld, lbx = 0.5, 0.5, 0.0
    out: list[WindowTick] = []
    for i in range(window_sec):
        if i <= elapsed:
            lu = float(sec_pm_u[i])
            ld = float(sec_pm_d[i])
            if sec_btc_px[i] > 0.0:
                lbx = float(sec_btc_px[i])
        vb = float(sec_btc_vol[i]) if i <= elapsed else 0.0
        bpx = lbx if lbx > 0.0 else (float(out[-1].btc_px) if out else 0.0)
        out.append(WindowTick(pm_u=lu, pm_d=ld, btc_px=bpx, btc_vol=vb))
    return out


class PaladinV7LiveEngine:
    """Continuous BTC 15m PALADIN v7: Binance volume spike + BTC impulse -> timed limit-buy legs."""

    def __init__(self, config: BotConfig, locator: GammaMarketLocator, trader: PolymarketTrader) -> None:
        self.config = config
        self.locator = locator
        self.trader = trader
        # Live log lines use this prefix so PaladinV9LiveEngine can show "PALADIN v9" (same code path).
        self._paladin_live_log_prefix: str = "PALADIN v7"
        self._stop = False
        self._slug: str | None = None
        self._runner: PaladinV7Runner | None = None
        self._ws: Any = None
        self._btc: RealtimeBtcPriceFeed | None = (
            RealtimeBtcPriceFeed(config) if config.btc_feed_enabled else None
        )
        self._sec_pm_u: list[float] = [0.5] * config.window_size_seconds
        self._sec_pm_d: list[float] = [0.5] * config.window_size_seconds
        self._sec_btc_px: list[float] = [0.0] * config.window_size_seconds
        self._sec_btc_vol: list[float] = [0.0] * config.window_size_seconds
        self._last_hb_ts: float = 0.0
        self._last_missing_price_log_ts: float = 0.0
        self._last_reconcile_ts: float = 0.0
        self._reconcile_mismatch_count: int = 0
        self._last_flatten_ts: float = 0.0
        self._v7_window_reconcile_applies: int = 0
        self._v7_window_flatten_fills: int = 0
        self._live_order_serial: int = 0
        self._limit_order_busy_until_ts: float = 0.0
        self._limit_order_busy_reason: str = ""
        self._active_limit_order_id: str = ""
        self._active_limit_order_side: str = ""
        self._active_limit_order_reason: str = ""
        self._active_limit_order_req_shares: float = 0.0
        self._active_limit_order_last_check_ts: float = 0.0
        self._active_limit_order_cancel_requested: bool = False
        self._active_limit_order_absent_checks: int = 0
        self._active_limit_order_limit_px: float = 0.0
        self._active_limit_order_api_before: float = 0.0
        self._active_limit_order_force_cancel_ts: float = 0.0
        self._active_limit_order_persistent: bool = False
        # Window-second (elapsed) deadline for resting cheap hedge; wall clock alone desyncs from paladin_v7_step.
        self._active_limit_order_cancel_at_elapsed: int | None = None
        self._last_untracked_open_order_log_ts: float = 0.0
        self._api_reality_mismatch_count: int = 0
        self._api_reality_last_u: float = -1.0
        self._api_reality_last_d: float = -1.0
        self._api_reality_next_check_ts: float = 0.0
        self._pre_window_warned_slug: str | None = None
        self._force_exit_warned_slug: str | None = None
        self._entry_delay_warned_slug: str | None = None
        self._new_cutoff_warned_slug: str | None = None
        # Run paladin_v7_step at most once per integer market second (elapsed) per window. Multiple polls
        # can share the same elapsed when poll_interval < 1s. Track every fired elapsed so a backward
        # jump in int(now - start_ts) cannot replay an already-evaluated second (double FAKs).
        self._v7_steps_fired: set[int] = set()
        self._v7_params = _v7_params_from_config(config)
        if config.polymarket_ws_enabled:
            try:
                from polymarket_ws import MarketWsFeed

                self._ws = MarketWsFeed(url=config.polymarket_ws_url)
                self._ws.start()
                LOGGER.info("Polymarket market WS enabled (%s)", config.polymarket_ws_url)
            except Exception as exc:
                LOGGER.warning("Polymarket WS unavailable (fall back to REST): %s", exc)
                self._ws = None

    def run(self) -> None:
        signal.signal(signal.SIGINT, self._sig)
        signal.signal(signal.SIGTERM, self._sig)
        LOGGER.info(
            self._paladin_live_log_prefix + " live started | dry_run=%s poll=%.1fs budget=$%.2f base_order=%.1f max/side=%.0f "
            "layer2_hi_dip=%.3f layer2_lo_dip=%.3f bal_tol=%.2fsh layer2_cd=%.1fs imb_repair_pm+avg_heavy<%.3f pair_cd=%.0fs",
            self.config.dry_run,
            float(self.config.poll_interval_seconds),
            float(self.config.strategy_budget_cap_usdc),
            float(self.config.paladin_v7_base_order_shares),
            float(self.config.paladin_v7_max_shares_per_side),
            float(self.config.paladin_v7_layer2_dip_below_avg),
            float(self.config.paladin_v7_layer2_low_vwap_dip_below_avg),
            float(self.config.paladin_v7_balance_share_tolerance),
            float(self.config.paladin_v7_layer2_cooldown_sec),
            float(self.config.paladin_v7_imbalance_repair_max_pair_sum),
            float(self.config.paladin_v7_pair_cooldown_sec),
        )
        LOGGER.info(
            self._paladin_live_log_prefix + " reconcile | enabled=%s every=%.1fs tol=%.2f sh confirm_reads=%d flatten=%s",
            self.config.paladin_v7_reconcile_enabled,
            float(self.config.paladin_v7_reconcile_interval_seconds),
            float(self.config.paladin_v7_reconcile_share_tolerance),
            int(self.config.paladin_v7_reconcile_confirm_reads),
            self.config.paladin_v7_reconcile_flatten,
        )
        LOGGER.info(
            self._paladin_live_log_prefix + " API reality override | balanced_probe=%d reads every %.1fs",
            int(self.config.paladin_v7_api_reality_confirm_reads),
            float(self.config.paladin_v7_api_reality_confirm_interval_seconds),
        )
        LOGGER.info(
            self._paladin_live_log_prefix + " order mode | spike_entry=market hedge_cheap=resting_limit forced_hedge=aggressive_limit "
            "limit_cancel_after=%.1fs",
            float(self.config.paladin_v7_limit_order_cancel_seconds),
        )
        while not self._stop:
            self._loop_once()
            time.sleep(max(0.05, float(self.config.poll_interval_seconds)))
        if self._ws is not None:
            try:
                self._ws.stop()
            except Exception:
                pass

    def _sig(self, *_args: object) -> None:
        LOGGER.info(self._paladin_live_log_prefix + " live: shutdown requested")
        self._stop = True

    def _token_price(self, tm: TokenMarket) -> float | None:
        if self._ws is not None:
            mid = self._ws.mid_for(tm.token_id, max_age_sec=5.0)
            if mid is not None and mid > 0:
                return float(mid)
        # Do not use /price as a signal fallback: it is last-trade-ish and can be badly stale
        # versus the live order book, which caused repeated FAKs far from the actual ask.
        mid = self.trader.get_midpoint(tm.token_id)
        return float(mid) if mid is not None and mid > 0 else None

    def _best_ask_price(self, tm: TokenMarket) -> float | None:
        if self._ws is not None:
            ba = self._ws.best_bid_ask_for(tm.token_id, max_age_sec=5.0)
            if ba is not None:
                _bid, ask = ba
                if ask > 0:
                    return float(ask)
        ask = self.trader.get_best_ask(tm.token_id)
        return float(ask) if ask is not None and ask > 0 else None

    @staticmethod
    def _num(raw: object) -> float:
        try:
            if raw in (None, ""):
                return 0.0
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _decode_order_size(raw: object) -> float:
        if isinstance(raw, str):
            txt = raw.strip()
            if not txt:
                return 0.0
            if "." in txt:
                return PaladinV7LiveEngine._num(txt)
        val = PaladinV7LiveEngine._num(raw)
        if val <= 0.0:
            return 0.0
        # Some order payloads report decimal shares directly ("4.8236"), while others use
        # fixed-point units (e.g. 4823600). Treat large integer-like values as fixed-point.
        if val >= 1000.0 and abs(val - round(val)) <= 1e-9:
            return val / 1_000_000.0
        return val

    @classmethod
    def _raw_order_avg_price(cls, order: dict[str, Any] | None) -> float:
        if not isinstance(order, dict):
            return 0.0
        taking = cls._num(order.get("takingAmount")) or cls._num(order.get("taking_amount"))
        making = cls._num(order.get("makingAmount")) or cls._num(order.get("making_amount"))
        if taking > 1e-9 and making >= 0.0:
            avg_px = making / taking if taking > 1e-9 else 0.0
            return avg_px if avg_px > 1e-9 else 0.0
        matched = cls._decode_order_size(order.get("size_matched"))
        if matched > 1e-9:
            px_lim = cls._num(order.get("price"))
            return px_lim if px_lim > 1e-9 else 0.0
        return 0.0

    @classmethod
    def _buy_fill_from_order(cls, order: dict[str, Any] | None, limit_px: float) -> tuple[float, float, float, str]:
        if not isinstance(order, dict):
            return 0.0, 0.0, 0.0, ""
        status = str(order.get("status") or "").lower()
        taking = cls._num(order.get("takingAmount")) or cls._num(order.get("taking_amount"))
        making = cls._num(order.get("makingAmount")) or cls._num(order.get("making_amount"))
        if taking > 1e-9 and making >= 0.0:
            avg_px = cls._raw_order_avg_price(order) or float(limit_px)
            if avg_px > float(limit_px) + 1e-6:
                avg_px = float(limit_px)
                making = taking * avg_px
            return taking, making, avg_px, status
        matched = cls._decode_order_size(order.get("size_matched"))
        if matched > 1e-9:
            px_lim = cls._num(order.get("price")) or float(limit_px)
            if px_lim > float(limit_px) + 1e-6:
                px_lim = float(limit_px)
            return matched, matched * px_lim, px_lim, status
        return 0.0, 0.0, 0.0, status

    def _confirm_live_buy_avg_price(
        self,
        order_id: str,
        *,
        limit_px: float,
        filled: float,
        initial_order: dict[str, Any] | None,
    ) -> float:
        """Start with our posted limit, then upgrade only after repeated sane API confirmations."""
        fallback_px = float(limit_px)
        need = max(2, int(self.config.paladin_v7_reconcile_confirm_reads))
        share_tol = max(0.01, float(self.config.paladin_v7_reconcile_share_tolerance))
        last_good_px = 0.0
        streak = 0
        warned_bad_px = False
        warned_stale_size = False
        polls_remaining = max(need + 2, 4)
        delays = (0.0, 0.15, 0.30, 0.50, 0.75, 1.00)
        order_state = initial_order
        for idx in range(min(polls_remaining, len(delays))):
            delay = delays[idx]
            if delay > 0:
                time.sleep(delay)
                try:
                    order_state = self.trader.get_order(order_id)
                except Exception as exc:
                    LOGGER.debug(self._paladin_live_log_prefix + " get_order %s during avg confirm: %s", order_id[:18], exc)
                    continue
            raw_avg_px = self._raw_order_avg_price(order_state)
            confirmed_filled, _spent, _safe_avg_px, _status = self._buy_fill_from_order(order_state, fallback_px)
            if raw_avg_px <= 1e-9:
                continue
            if raw_avg_px > fallback_px + 1e-6:
                if not warned_bad_px:
                    LOGGER.warning(
                        self._paladin_live_log_prefix + " ignoring suspicious order avg %.4f above limit %.4f | oid=%s",
                        raw_avg_px,
                        fallback_px,
                        order_id[:24] + "…",
                    )
                    warned_bad_px = True
                continue
            if filled > 1e-9 and confirmed_filled + share_tol < filled:
                if not warned_stale_size:
                    LOGGER.debug(
                        self._paladin_live_log_prefix + " avg confirm waiting for fuller size %.4f/%.4f | oid=%s",
                        confirmed_filled,
                        filled,
                        order_id[:24] + "…",
                    )
                    warned_stale_size = True
                continue
            if last_good_px > 1e-9 and abs(raw_avg_px - last_good_px) <= 1e-4:
                streak += 1
            else:
                last_good_px = raw_avg_px
                streak = 1
            if streak >= need:
                return raw_avg_px
        return fallback_px

    @staticmethod
    def _order_status_is_open(status: str) -> bool:
        return status.lower() in {
            "open",
            "live",
            "active",
            "pending",
            "partially_filled",
            "unmatched",
            "delayed",
        }

    @staticmethod
    def _order_status_is_closed(status: str) -> bool:
        return status.lower() in {
            "filled",
            "matched",
            "cancelled",
            "canceled",
            "expired",
            "closed",
        }

    def _set_active_limit_order(self, order_id: str, side: str, reason: str, req_shares: float) -> None:
        self._active_limit_order_id = str(order_id or "")
        self._active_limit_order_side = str(side)
        self._active_limit_order_reason = str(reason)
        self._active_limit_order_req_shares = float(req_shares)
        self._active_limit_order_last_check_ts = 0.0
        self._active_limit_order_cancel_requested = False
        self._active_limit_order_absent_checks = 0
        self._active_limit_order_limit_px = 0.0
        self._active_limit_order_api_before = 0.0
        self._active_limit_order_force_cancel_ts = 0.0
        self._active_limit_order_persistent = False
        self._active_limit_order_cancel_at_elapsed = None

    def _clear_active_limit_order(self) -> None:
        self._active_limit_order_id = ""
        self._active_limit_order_side = ""
        self._active_limit_order_reason = ""
        self._active_limit_order_req_shares = 0.0
        self._active_limit_order_last_check_ts = 0.0
        self._active_limit_order_cancel_requested = False
        self._active_limit_order_absent_checks = 0
        self._active_limit_order_limit_px = 0.0
        self._active_limit_order_api_before = 0.0
        self._active_limit_order_force_cancel_ts = 0.0
        self._active_limit_order_persistent = False
        self._active_limit_order_cancel_at_elapsed = None
        self._limit_order_busy_until_ts = 0.0
        self._limit_order_busy_reason = ""

    def _reset_api_reality_probe(self) -> None:
        self._api_reality_mismatch_count = 0
        self._api_reality_last_u = -1.0
        self._api_reality_last_d = -1.0
        self._api_reality_next_check_ts = 0.0

    def _v7_max_single_buy_shares(self) -> int:
        """Configured base clip, capped at V7_LIVE_MAX_SHARES_PER_SINGLE_ORDER (never trust env alone)."""
        raw = int(round(float(self.config.paladin_v7_base_order_shares)))
        raw = max(1, raw)
        return int(min(V7_LIVE_MAX_SHARES_PER_SINGLE_ORDER, raw))

    def _clip_cap_for_reason(self, reason: str) -> int:
        fixed_clip_reasons = {
            "v7_first_window_lead",
            "v7_first_binance_spike",
            "v7_balanced_btc_spike",
            "v7_layer2_dip_lead",
            "v7_layer2_lowvwap_dip",
            "v7_hedge_cheap",
            "v7_hedge_forced",
            "v7_api_imbalance_flatten",
        }
        return self._v7_max_single_buy_shares() if str(reason) in fixed_clip_reasons else 10**9

    def _cap_requested_live_size(self, shares: float, reason: str) -> int:
        raw_size = max(0, int(round(float(shares))))
        return min(raw_size, self._clip_cap_for_reason(reason))

    def _cap_confirmed_fill(self, filled: float, req_shares: float, reason: str, order_id: str) -> float:
        req = max(0.0, float(req_shares))
        got = max(0.0, float(filled))
        if got <= req + 1e-6:
            return got
        LOGGER.warning(
            self._paladin_live_log_prefix + " capping suspicious fill %.4f -> %.4f | %s | oid=%s",
            got,
            req,
            reason,
            order_id[:24] + "…" if order_id else "?",
        )
        return req

    @staticmethod
    def _order_token_id(order: dict[str, Any]) -> str:
        for key in ("asset_id", "assetId", "token_id", "tokenId", "market_id", "marketId"):
            raw = order.get(key)
            if raw not in (None, ""):
                return str(raw)
        return ""

    @staticmethod
    def _order_side_value(order: dict[str, Any]) -> str:
        raw = order.get("side")
        if raw in (None, ""):
            return ""
        return str(raw).strip().lower()

    def _has_untracked_open_buy_order(self, contract: ActiveContract, now: float) -> bool:
        known_id = str(self._active_limit_order_id or "")
        active_tokens = {str(contract.up.token_id), str(contract.down.token_id)}
        try:
            open_orders = self.trader.get_open_orders()
        except Exception as exc:
            LOGGER.debug(self._paladin_live_log_prefix + " stray open-order check skipped: %s", exc)
            return False
        for od in open_orders:
            oid = str(od.get("id") or od.get("orderID") or od.get("order_id") or "")
            if known_id and oid == known_id:
                continue
            tok = self._order_token_id(od)
            if not tok or tok not in active_tokens:
                continue
            side = self._order_side_value(od)
            if side and side not in {"buy", "bid"}:
                continue
            self._limit_order_busy_until_ts = max(self._limit_order_busy_until_ts, now + 1.0)
            self._limit_order_busy_reason = "untracked_open_order"
            if now - self._last_untracked_open_order_log_ts >= 5.0:
                self._last_untracked_open_order_log_ts = now
                LOGGER.warning(
                    self._paladin_live_log_prefix + " blocking new buy: exchange still shows open order oid=%s token=%s side=%s",
                    oid[:24] + "…" if oid else "?",
                    tok[:16] + "…" if tok else "?",
                    side or "?",
                )
            return True
        return False

    def _has_unresolved_active_limit_order(self, now: float, limit_px: float | None = None) -> bool:
        order_id = str(self._active_limit_order_id or "")
        if not order_id:
            return False
        if now - self._active_limit_order_last_check_ts < 0.4:
            return True
        self._active_limit_order_last_check_ts = now
        order_state: dict[str, Any] | None = None
        try:
            order_state = self.trader.get_order(order_id)
        except Exception as exc:
            LOGGER.debug(self._paladin_live_log_prefix + " active get_order %s: %s", order_id[:18], exc)
        status = ""
        filled = 0.0
        if order_state is not None:
            px_hint = float(limit_px) if limit_px is not None else 0.5
            filled, _spent, _avg_px, status = self._buy_fill_from_order(order_state, px_hint)
            if filled + 1e-9 >= float(self._active_limit_order_req_shares):
                self._clear_active_limit_order()
                return False
            if self._order_status_is_closed(status):
                self._clear_active_limit_order()
                return False
            if self._order_status_is_open(status):
                self._limit_order_busy_until_ts = max(self._limit_order_busy_until_ts, now + 1.0)
                self._active_limit_order_absent_checks = 0
                return True
        try:
            open_orders = self.trader.get_open_orders()
        except Exception as exc:
            LOGGER.debug(self._paladin_live_log_prefix + " active get_open_orders %s: %s", order_id[:18], exc)
            self._limit_order_busy_until_ts = max(self._limit_order_busy_until_ts, now + 1.0)
            return True
        for od in open_orders:
            oid = str(od.get("id") or od.get("orderID") or od.get("order_id") or "")
            if oid == order_id:
                self._limit_order_busy_until_ts = max(self._limit_order_busy_until_ts, now + 1.0)
                self._active_limit_order_absent_checks = 0
                return True
        self._active_limit_order_absent_checks += 1
        if self._active_limit_order_absent_checks < 2:
            self._limit_order_busy_until_ts = max(self._limit_order_busy_until_ts, now + 0.8)
            return True
        if not self._active_limit_order_cancel_requested:
            LOGGER.warning(
                self._paladin_live_log_prefix + " order %s missing from checks before cancel confirmation; holding new orders | %s",
                order_id[:24] + "…",
                self._active_limit_order_reason,
            )
            self._limit_order_busy_until_ts = max(self._limit_order_busy_until_ts, now + 1.0)
            return True
        if status and not self._order_status_is_closed(status):
            LOGGER.warning(
                self._paladin_live_log_prefix + " order %s unresolved after cancel check; holding new orders | %s",
                order_id[:24] + "…",
                self._active_limit_order_reason,
            )
        self._clear_active_limit_order()
        return False

    def _finalize_active_limit_fill(
        self,
        contract: ActiveContract,
        st: SimState,
        *,
        t: int,
        order_state: dict[str, Any] | None,
    ) -> None:
        order_id = str(self._active_limit_order_id or "")
        side = str(self._active_limit_order_side or "")
        reason = str(self._active_limit_order_reason or "")
        req_shares = float(self._active_limit_order_req_shares or 0.0)
        limit_px = float(self._active_limit_order_limit_px or 0.0)
        api_before = float(self._active_limit_order_api_before or 0.0)
        tok = contract.up if side == "up" else contract.down
        filled, _spent, _avg_px, status = self._buy_fill_from_order(order_state, limit_px)
        if filled <= 1e-9:
            try:
                api_after = float(self.trader.token_balance_allowance_refreshed(tok.token_id))
            except Exception as exc:
                LOGGER.debug(self._paladin_live_log_prefix + " active-order post balance read skipped: %s", exc)
                api_after = api_before
            delta_api = max(0.0, api_after - api_before)
            if delta_api > max(1e-9, float(self.config.paladin_v7_reconcile_share_tolerance)):
                filled = min(delta_api, float(req_shares))
        filled = self._cap_confirmed_fill(filled, req_shares, reason, order_id)
        if filled > 1e-9:
            avg_px = self._confirm_live_buy_avg_price(
                order_id,
                limit_px=limit_px if limit_px > 1e-9 else 0.5,
                filled=filled,
                initial_order=order_state,
            )
            spent = filled * avg_px
            self._apply_live_buy_fill(
                st,
                t=t,
                side=side,
                filled=filled,
                avg_px=avg_px,
                spent=spent,
                reason=reason,
                order_id=order_id,
            )
            self._align_leg_to_api_after_live_buy(
                contract, st, t=t, side=side, px_hint=avg_px, max_positive_delta=float(req_shares)
            )
        elif self._order_status_is_closed(status):
            LOGGER.info(
                self._paladin_live_log_prefix + " active limit closed with no fill | %s | oid=%s",
                reason,
                order_id[:24] + "…" if order_id else "?",
            )
        self._clear_active_limit_order()

    def _process_persistent_limit_order(
        self,
        contract: ActiveContract,
        st: SimState,
        *,
        t: int,
        now: float,
    ) -> bool:
        """Poll resting GTC state. Returns True only when a fill was applied this poll (caller may skip rest).

        **Important:** returns False while the order is merely open/waiting so ``paladin_v7_step`` still runs
        each window second (forced hedge uses ``elapsed`` age, not wall time).
        """
        order_id = str(self._active_limit_order_id or "")
        if not order_id:
            return False
        if not self._active_limit_order_persistent:
            return self._has_unresolved_active_limit_order(now, self._active_limit_order_limit_px)
        if now - self._active_limit_order_last_check_ts < 0.4:
            return False
        self._active_limit_order_last_check_ts = now
        limit_px = float(self._active_limit_order_limit_px or 0.5)
        order_state: dict[str, Any] | None = None
        try:
            order_state = self.trader.get_order(order_id)
        except Exception as exc:
            LOGGER.debug(self._paladin_live_log_prefix + " persistent get_order %s: %s", order_id[:18], exc)
        status = ""
        filled = 0.0
        if order_state is not None:
            filled, _spent, _avg_px, status = self._buy_fill_from_order(order_state, limit_px)
            if filled + 1e-9 >= float(self._active_limit_order_req_shares):
                self._finalize_active_limit_fill(contract, st, t=t, order_state=order_state)
                return True
            if self._order_status_is_closed(status):
                self._finalize_active_limit_fill(contract, st, t=t, order_state=order_state)
                return True
            if self._order_status_is_open(status):
                cancel_at_el = self._active_limit_order_cancel_at_elapsed
                is_cheap = str(self._active_limit_order_reason) == "v7_hedge_cheap"
                hedge_deadline_hit = bool(is_cheap and cancel_at_el is not None and t >= int(cancel_at_el))
                wall_deadline_hit = bool(
                    self._active_limit_order_force_cancel_ts > 0.0
                    and now >= self._active_limit_order_force_cancel_ts - 1e-9
                    and (not is_cheap or cancel_at_el is None)
                )
                if (hedge_deadline_hit or wall_deadline_hit) and not self._active_limit_order_cancel_requested:
                    self._active_limit_order_cancel_requested = True
                    cancelled = self.trader.cancel_order(order_id)
                    LOGGER.info(
                        self._paladin_live_log_prefix + " persistent hedge timeout cancel oid=%s cancelled=%s | %s | "
                        "elapsed=%d cancel_at_elapsed=%s wall_fallback=%s",
                        order_id[:24] + "…",
                        cancelled,
                        self._active_limit_order_reason,
                        t,
                        cancel_at_el,
                        wall_deadline_hit,
                    )
                    confirm_deadline = now + max(4.0, 2.0)
                    while time.time() < confirm_deadline:
                        try:
                            order_state = self.trader.get_order(order_id)
                        except Exception:
                            order_state = None
                        if order_state is None:
                            break
                        stt = str(order_state.get("status") or order_state.get("order_status") or "")
                        if self._order_status_is_closed(stt):
                            break
                        time.sleep(0.2)
                    self._finalize_active_limit_fill(contract, st, t=t, order_state=order_state)
                    self._limit_order_busy_until_ts = max(self._limit_order_busy_until_ts, time.time() + 0.5)
                    return True
                self._limit_order_busy_until_ts = max(self._limit_order_busy_until_ts, now + 0.25)
                self._active_limit_order_absent_checks = 0
                return False
        try:
            open_orders = self.trader.get_open_orders()
        except Exception as exc:
            LOGGER.debug(self._paladin_live_log_prefix + " persistent get_open_orders %s: %s", order_id[:18], exc)
            self._limit_order_busy_until_ts = max(self._limit_order_busy_until_ts, now + 1.0)
            return False
        for od in open_orders:
            oid = str(od.get("id") or od.get("orderID") or od.get("order_id") or "")
            if oid == order_id:
                self._limit_order_busy_until_ts = max(self._limit_order_busy_until_ts, now + 0.25)
                self._active_limit_order_absent_checks = 0
                return False
        self._active_limit_order_absent_checks += 1
        if self._active_limit_order_absent_checks < 2:
            self._limit_order_busy_until_ts = max(self._limit_order_busy_until_ts, now + 0.8)
            return False
        if not self._active_limit_order_cancel_requested:
            LOGGER.warning(
                self._paladin_live_log_prefix + " persistent order %s missing before timeout cancel; still holding | %s",
                order_id[:24] + "…",
                self._active_limit_order_reason,
            )
            self._limit_order_busy_until_ts = max(self._limit_order_busy_until_ts, now + 1.0)
            return False
        self._finalize_active_limit_fill(contract, st, t=t, order_state=order_state)
        return True

    def _apply_live_buy_fill(
        self,
        st: SimState,
        *,
        t: int,
        side: str,
        filled: float,
        avg_px: float,
        spent: float,
        reason: str,
        order_id: str,
    ) -> None:
        su, au, sd, ad = apply_buy_fill(
            st.size_up,
            st.avg_up,
            st.size_down,
            st.avg_down,
            side=side,  # type: ignore[arg-type]
            add_shares=filled,
            fill_price=avg_px,
        )
        st.size_up, st.avg_up, st.size_down, st.avg_down = su, au, sd, ad
        st.spent_usdc += spent
        st.trades.append(
            Trade(
                t,
                side,  # type: ignore[arg-type]
                filled,
                avg_px,
                spent,
                f"{reason}|live",
            )
        )
        LOGGER.info(
            self._paladin_live_log_prefix + " LIMIT filled %s %.4f sh @ %.4f ($%.2f) | %s | oid=%s",
            side.upper(),
            filled,
            avg_px,
            spent,
            reason,
            (order_id[:24] + "…") if order_id else "?",
        )

    def _live_buy(
        self,
        contract: ActiveContract,
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
        persistent_limit_until_ts: float | None = None,
        persistent_cancel_at_elapsed: int | None = None,
        tape_pm_u: float | None = None,
        tape_pm_d: float | None = None,
    ) -> float:
        px = float(px)
        px = round(px, 4)
        now = time.time()
        if self._has_unresolved_active_limit_order(now):
            LOGGER.warning(self._paladin_live_log_prefix + " refusing new %s order while tracked order is still unresolved", reason)
            return 0.0
        if self._has_untracked_open_buy_order(contract, now):
            LOGGER.warning(self._paladin_live_log_prefix + " refusing new %s order while exchange still shows an open buy", reason)
            return 0.0
        self._reset_api_reality_probe()
        exchange_min_shares = int(math.ceil(float(self.config.paladin_v7_min_shares)))
        size = self._cap_requested_live_size(shares, reason)
        if size <= 0 or size < max(exchange_min_shares, int(math.ceil(min_shares))):
            return 0.0
        req_shares = float(size)
        notion = req_shares * px
        if req_shares < min_shares - 1e-9 or notion < min_notional - 1e-9:
            LOGGER.info(
                self._paladin_live_log_prefix + " skip BUY %s shares=%.4f px=%.4f notion=$%.2f reason=%s "
                "(min_shares=%.4f min_notional=%.2f)",
                side.upper(),
                req_shares,
                px,
                notion,
                reason,
                min_shares,
                min_notional,
            )
            return 0.0
        tok = contract.up if side == "up" else contract.down
        if self.config.dry_run:
            LOGGER.info(
                "[%s dry_run] BUY %s size=%d @ %.4f (%s) ~$%.2f",
                self._paladin_live_log_prefix,
                side.upper(),
                size,
                px,
                reason,
                notion,
            )
            return sim_try_buy(
                st,
                t=t,
                side=side,  # type: ignore[arg-type]
                shares=float(size),
                px=px,
                reason=reason,
                budget=budget,
                min_notional=min_notional,
                min_shares=min_shares,
                pm_u=tape_pm_u,
                pm_d=tape_pm_d,
            )
        api_before = 0.0
        try:
            api_before = float(self.trader.token_balance_allowance_refreshed(tok.token_id))
        except Exception as exc:
            LOGGER.debug(self._paladin_live_log_prefix + " pre-buy balance read skipped: %s", exc)
        market_reasons = {"v7_first_binance_spike", "v7_balanced_btc_spike"}
        if str(reason) in market_reasons:
            try:
                res = self.trader.place_marketable_buy_with_result(
                    tok,
                    px,
                    size,
                    confirm_get_order=True,
                )
            except PolyApiException as exc:
                LOGGER.warning(self._paladin_live_log_prefix + " MARKET BUY rejected %s %s @ %.4f: %s", side, size, px, exc)
                return 0.0
            except Exception as exc:
                LOGGER.warning(self._paladin_live_log_prefix + " live market BUY failed %s %s @ %.4f: %s", side, size, px, exc)
                return 0.0
            filled = max(0.0, float(getattr(res, "filled_shares", 0.0) or 0.0))
            order_id = str(getattr(res, "order_id", "") or "")
            # Always refresh balance for delta vs exchange (FAK can report filled_shares > 0 while we still
            # need api_after for reconcile); previously api_after was only set when filled==0 → UnboundLocalError.
            api_after = api_before
            try:
                api_after = float(self.trader.token_balance_allowance_refreshed(tok.token_id))
            except Exception as exc:
                LOGGER.debug(self._paladin_live_log_prefix + " market post-buy balance read skipped: %s", exc)
            delta_api = max(0.0, api_after - api_before)
            if delta_api > max(1e-9, float(self.config.paladin_v7_reconcile_share_tolerance)):
                filled = min(delta_api, float(req_shares))
            filled = self._cap_confirmed_fill(filled, req_shares, reason, order_id)
            if filled <= 1e-9:
                return 0.0
            avg_px = px
            if order_id:
                avg_px = self._confirm_live_buy_avg_price(
                    order_id,
                    limit_px=px,
                    filled=filled,
                    initial_order=None,
                )
            spent = filled * avg_px
            if not _can_afford_live(st.spent_usdc, spent, budget):
                LOGGER.warning(self._paladin_live_log_prefix + ": market fill would exceed budget; skipping state update (filled=%.4f)", filled)
                return 0.0
            self._apply_live_buy_fill(
                st,
                t=t,
                side=side,
                filled=filled,
                avg_px=avg_px,
                spent=spent,
                reason=reason,
                order_id=order_id,
            )
            self._align_leg_to_api_after_live_buy(
                contract, st, t=t, side=side, px_hint=avg_px, max_positive_delta=float(req_shares)
            )
            return filled
        self._live_order_serial += 1
        order_id = ""
        try:
            res = self.trader.place_limit_buy(
                tok,
                px,
                size,
            )
        except PolyApiException as exc:
            LOGGER.warning(self._paladin_live_log_prefix + " LIMIT POST rejected %s %s @ %.4f: %s", side, size, px, exc)
            return 0.0
        except Exception as exc:
            LOGGER.warning(self._paladin_live_log_prefix + " live limit BUY failed %s %s @ %.4f: %s", side, size, px, exc)
            return 0.0
        if isinstance(res, dict):
            order_id = str(res.get("orderID") or res.get("order_id") or res.get("id") or "")
        if not order_id:
            LOGGER.warning(self._paladin_live_log_prefix + " LIMIT post missing order id | %s %s @ %.4f | %s", side, size, px, reason)
            return 0.0

        if persistent_limit_until_ts is not None:
            self._set_active_limit_order(order_id, side, reason, req_shares)
            self._active_limit_order_limit_px = px
            self._active_limit_order_api_before = api_before
            self._active_limit_order_force_cancel_ts = float(persistent_limit_until_ts)
            self._active_limit_order_persistent = True
            if persistent_cancel_at_elapsed is not None:
                self._active_limit_order_cancel_at_elapsed = int(persistent_cancel_at_elapsed)
            self._limit_order_busy_until_ts = max(self._limit_order_busy_until_ts, time.time() + 0.8)
            self._limit_order_busy_reason = str(reason)
            LOGGER.info(
                self._paladin_live_log_prefix + " LIMIT posted persistent %s %d @ %.4f until hedge timeout | %s | oid=%s",
                side.upper(),
                size,
                px,
                reason,
                order_id[:24] + "…",
            )
            return 0.0

        cancel_after = float(self.config.paladin_v7_limit_order_cancel_seconds)
        deadline = time.time() + cancel_after
        self._set_active_limit_order(order_id, side, reason, req_shares)
        self._active_limit_order_limit_px = px
        self._active_limit_order_api_before = api_before
        self._limit_order_busy_until_ts = max(self._limit_order_busy_until_ts, deadline)
        self._limit_order_busy_reason = str(reason)
        order_state: dict[str, Any] | None = None
        filled = 0.0
        spent = 0.0
        avg_px = 0.0
        status = ""
        while time.time() < deadline:
            try:
                order_state = self.trader.get_order(order_id)
            except Exception as exc:
                LOGGER.debug(self._paladin_live_log_prefix + " get_order %s before cancel: %s", order_id[:18], exc)
                time.sleep(0.25)
                continue
            filled, spent, avg_px, status = self._buy_fill_from_order(order_state, px)
            # Keep the order lifecycle closed for the full cancel window unless the requested clip
            # is completely filled. Partial fills must not unlock another order 1-2 seconds later.
            if filled + 1e-9 >= req_shares:
                break
            time.sleep(0.25)

        if filled + 1e-9 < req_shares:
            self._active_limit_order_cancel_requested = True
            cancelled = self.trader.cancel_order(order_id)
            LOGGER.info(
                self._paladin_live_log_prefix + " LIMIT cancel %s oid=%s age=%.1fs cancelled=%s | %s",
                side.upper(),
                order_id[:24] + "…",
                cancel_after,
                cancelled,
                reason,
            )
            cancel_confirm_deadline = time.time() + max(2.0, cancel_after)
            while time.time() < cancel_confirm_deadline:
                if not self._has_unresolved_active_limit_order(time.time(), px):
                    break
                time.sleep(0.25)
        try:
            order_state = self.trader.get_order(order_id)
        except Exception as exc:
            LOGGER.debug(self._paladin_live_log_prefix + " get_order %s after cancel: %s", order_id[:18], exc)
        filled, spent, avg_px, status = self._buy_fill_from_order(order_state, px)
        if filled <= 1e-9:
            try:
                api_after = float(self.trader.token_balance_allowance_refreshed(tok.token_id))
            except Exception as exc:
                LOGGER.debug(self._paladin_live_log_prefix + " post-buy balance read skipped: %s", exc)
                api_after = api_before
            delta_api = max(0.0, api_after - api_before)
            if delta_api > max(1e-9, float(self.config.paladin_v7_reconcile_share_tolerance)):
                filled = min(delta_api, float(req_shares))
                avg_px = px
                spent = filled * avg_px
        filled = self._cap_confirmed_fill(filled, req_shares, reason, order_id)
        if filled <= 1e-9:
            return 0.0
        avg_px = self._confirm_live_buy_avg_price(
            order_id,
            limit_px=px,
            filled=filled,
            initial_order=order_state,
        )
        spent = filled * avg_px
        if filled + 1e-9 >= req_shares or self._order_status_is_closed(status):
            self._clear_active_limit_order()
        if not _can_afford_live(st.spent_usdc, spent, budget):
            LOGGER.warning(self._paladin_live_log_prefix + ": fill would exceed budget; skipping state update (filled=%.4f)", filled)
            return 0.0
        self._apply_live_buy_fill(
            st,
            t=t,
            side=side,
            filled=filled,
            avg_px=avg_px,
            spent=spent,
            reason=reason,
            order_id=order_id,
        )
        self._align_leg_to_api_after_live_buy(
            contract, st, t=t, side=side, px_hint=avg_px, max_positive_delta=float(req_shares)
        )
        return filled

    @staticmethod
    def _shrink_leg(st: SimState, side: str, remove: float) -> None:
        remove = max(0.0, float(remove))
        if remove <= 1e-12:
            return
        if side == "up":
            st.size_up = max(0.0, float(st.size_up) - remove)
            if st.size_up < 1e-9:
                st.size_up, st.avg_up = 0.0, 0.0
        else:
            st.size_down = max(0.0, float(st.size_down) - remove)
            if st.size_down < 1e-9:
                st.size_down, st.avg_down = 0.0, 0.0

    @staticmethod
    def _latest_exec_fill_price(st: SimState, side: str) -> float | None:
        """Last non-reconcile trade price for ``side`` (actual live buy VWAP), for reconcile economics."""
        for tr in reversed(st.trades):
            if str(tr.side) != side:
                continue
            r = str(tr.reason)
            if "v7_api_reconcile_sync" in r or "v7_post_buy_api_sync" in r:
                continue
            if float(tr.price) > 1e-9:
                return float(tr.price)
        return None

    def _align_leg_to_api_after_live_buy(
        self,
        contract: ActiveContract,
        st: SimState,
        *,
        t: int,
        side: str,
        px_hint: float,
        max_positive_delta: float | None = None,
    ) -> None:
        """One refresh vs CLOB balance for the bought token; only add missing shares immediately after a buy.

        ``max_positive_delta`` caps how many shares we will *add* in this align (the size we just
        ordered, or reconcile tolerance when None). Prevents a bogus API jump from inflating inventory.
        """
        tok = contract.up if side == "up" else contract.down
        tol = float(self.config.paladin_v7_reconcile_share_tolerance)
        pos_cap = float(max_positive_delta) if max_positive_delta is not None else tol
        pos_cap = max(0.0, min(float(self._v7_max_single_buy_shares()), pos_cap))
        cur = float(st.size_up) if side == "up" else float(st.size_down)
        if cur < 1e-6:
            return

        api = 0.0
        for attempt, delay in enumerate((0.0, 0.1, 0.22, 0.38)):
            if delay > 0:
                time.sleep(delay)
            try:
                api = float(self.trader.token_balance_allowance_refreshed(tok.token_id))
            except Exception as exc:
                LOGGER.debug("post-buy balance read skipped: %s", exc)
                return
            if api > 0.25 or abs(api - cur) <= tol:
                break
            if attempt == 3:
                break

        ms = float(self.config.paladin_v7_min_shares)
        # CLOB balances often lag right after a buy; API=0 with model>0 would incorrectly zero the leg.
        if cur + 1e-9 >= ms and api < 0.25:
            LOGGER.warning(
                self._paladin_live_log_prefix + " post-buy: skip API align %s (API=%.4f vs model=%.4f; likely stale balance read)",
                side.upper(),
                api,
                cur,
            )
            return

        delta = api - cur
        if abs(delta) <= tol:
            return
        if delta < -tol:
            LOGGER.warning(
                self._paladin_live_log_prefix + " post-buy: skip trim %s (API %.4f vs model %.4f; wait for reconcile confirm)",
                side.upper(),
                api,
                cur,
            )
            return
        if delta > pos_cap + 1e-6:
            LOGGER.error(
                self._paladin_live_log_prefix + " post-buy: clamping API add %s delta=%.4f -> cap=%.4f (API=%.4f model=%.4f)",
                side.upper(),
                delta,
                pos_cap,
                api,
                cur,
            )
        delta = min(delta, pos_cap)
        su, au, sd, ad = apply_buy_fill(
            st.size_up,
            st.avg_up,
            st.size_down,
            st.avg_down,
            side=side,  # type: ignore[arg-type]
            add_shares=delta,
            fill_price=float(px_hint),
        )
        st.size_up, st.avg_up, st.size_down, st.avg_down = su, au, sd, ad
        notion = float(delta) * float(px_hint)
        st.spent_usdc += notion
        st.trades.append(
            Trade(
                t,
                side,  # type: ignore[arg-type]
                float(delta),
                float(px_hint),
                notion,
                "v7_post_buy_api_sync|live",
            )
        )
        LOGGER.warning(
            self._paladin_live_log_prefix + " post-buy API add %s +%.4f sh @ %.4f (API %.4f vs model %.4f)",
            side.upper(),
            delta,
            float(px_hint),
            api,
            cur,
        )

    def _sync_state_to_api_balances(
        self,
        runner: PaladinV7Runner,
        api_u: float,
        api_d: float,
        pm_u: float,
        pm_d: float,
        elapsed: int,
    ) -> None:
        """Align SimState sizes (and spend on positive deltas) with CLOB conditional balances."""
        st = runner.st
        for side, api_sz, pm in (("up", api_u, pm_u), ("down", api_d, pm_d)):
            cur = float(st.size_up) if side == "up" else float(st.size_down)
            delta = float(api_sz) - cur
            if abs(delta) <= 1e-9:
                continue
            if delta > 0:
                mx = float(self._v7_max_single_buy_shares())
                if delta > mx + 1e-6:
                    LOGGER.warning(
                        self._paladin_live_log_prefix + " reconcile: capping positive %s add %.4f -> %.4f (API vs model)",
                        side,
                        delta,
                        mx,
                    )
                    delta = mx
                fill_px = float(self._latest_exec_fill_price(st, side) or pm)
                su, au, sd, ad = apply_buy_fill(
                    st.size_up,
                    st.avg_up,
                    st.size_down,
                    st.avg_down,
                    side=side,  # type: ignore[arg-type]
                    add_shares=delta,
                    fill_price=fill_px,
                )
                st.size_up, st.avg_up, st.size_down, st.avg_down = su, au, sd, ad
                notion = float(delta) * float(fill_px)
                st.spent_usdc += notion
                st.trades.append(
                    Trade(
                        elapsed,
                        side,  # type: ignore[arg-type]
                        float(delta),
                        float(fill_px),
                        notion,
                        "v7_api_reconcile_sync",
                    )
                )
                LOGGER.warning(
                    self._paladin_live_log_prefix + " reconcile SYNC +%.4f %s sh @ %.4f (~$%.2f) to match API (mid=%.4f if no exec ref)",
                    delta,
                    side,
                    float(fill_px),
                    notion,
                    float(pm),
                )
            else:
                self._shrink_leg(st, side, -delta)
                LOGGER.warning(
                    self._paladin_live_log_prefix + " reconcile TRIM %.4f %s sh (model ahead of API)",
                    -delta,
                    side,
                )

    @staticmethod
    def _hedge_t0_preserve_on_resync(
        prev: tuple[str, float, float, int] | None,
        hedge_side: str,
        elapsed: int,
    ) -> int:
        """Keep original first-leg time for forced-hedge age; reconciles must not set t0=elapsed each time."""
        if prev is not None and str(prev[0]) == hedge_side:
            return int(prev[3])
        return int(elapsed)

    def _resync_pending_second_after_reconcile(self, runner: PaladinV7Runner, elapsed: int) -> None:
        """Rebuild open-hedge intent from inventory after API sync (do not drop pending on stale reads)."""
        st = runner.st
        du = float(st.size_up) - float(st.size_down)
        # Treat only small drift as "flat hedge need" — not min_shares*0.51 (~2.55 sh), which cleared
        # pending while still multi-share imbalanced and blocked hedges after reconcile.
        eps = max(0.05, float(self.config.paladin_v7_reconcile_share_tolerance))
        prev = runner.pending_second
        if abs(du) <= eps:
            runner.pending_second = None
            return
        if float(st.size_up) < 1e-9 and float(st.size_down) < 1e-9:
            runner.pending_second = None
            return
        if du > eps:
            if float(st.avg_up) <= 1e-9:
                runner.pending_second = None
                return
            t0 = self._hedge_t0_preserve_on_resync(prev, "down", elapsed)
            runner.pending_second = ("down", float(du), float(st.avg_up), t0)
        elif du < -eps:
            if float(st.avg_down) <= 1e-9:
                runner.pending_second = None
                return
            t0 = self._hedge_t0_preserve_on_resync(prev, "up", elapsed)
            runner.pending_second = ("up", float(-du), float(st.avg_down), t0)

    def _maybe_accept_api_balance_reality(
        self,
        contract: ActiveContract,
        runner: PaladinV7Runner,
        pm_u: float,
        pm_d: float,
        now: float,
        elapsed: int,
    ) -> bool:
        st = runner.st
        bal_tol = float(self.config.paladin_v7_balance_share_tolerance)
        model_gap = abs(float(st.size_up) - float(st.size_down))
        if model_gap > bal_tol + 1e-9:
            self._reset_api_reality_probe()
            return False
        if now < self._api_reality_next_check_ts - 1e-9:
            return self._api_reality_mismatch_count > 0
        self._api_reality_next_check_ts = now + float(self.config.paladin_v7_api_reality_confirm_interval_seconds)
        try:
            api_u = float(self.trader.token_balance_allowance_refreshed(contract.up.token_id))
            api_d = float(self.trader.token_balance_allowance_refreshed(contract.down.token_id))
        except Exception as exc:
            LOGGER.debug(self._paladin_live_log_prefix + " balanced-state API reality check skipped: %s", exc)
            return self._api_reality_mismatch_count > 0
        tol = float(self.config.paladin_v7_reconcile_share_tolerance)
        api_gap = abs(api_u - api_d)
        if api_gap <= max(tol, bal_tol):
            self._reset_api_reality_probe()
            return False
        if abs(api_u - float(st.size_up)) <= tol and abs(api_d - float(st.size_down)) <= tol:
            self._reset_api_reality_probe()
            return False
        stable = (
            self._api_reality_last_u >= 0.0
            and abs(api_u - self._api_reality_last_u) <= tol
            and abs(api_d - self._api_reality_last_d) <= tol
        )
        if stable:
            self._api_reality_mismatch_count += 1
        else:
            self._api_reality_mismatch_count = 1
            self._api_reality_last_u = api_u
            self._api_reality_last_d = api_d
        need = max(1, int(self.config.paladin_v7_api_reality_confirm_reads))
        LOGGER.info(
            self._paladin_live_log_prefix + " balanced-state API check | model U=%.4f D=%.4f | API U=%.4f D=%.4f | gap=%.3f | streak=%d/%d",
            st.size_up,
            st.size_down,
            api_u,
            api_d,
            api_gap,
            self._api_reality_mismatch_count,
            need,
        )
        if self._api_reality_mismatch_count < need:
            return True
        LOGGER.warning(
            self._paladin_live_log_prefix + " accepting API reality after %d balanced-state confirmations: model U=%.4f D=%.4f -> API U=%.4f D=%.4f",
            need,
            st.size_up,
            st.size_down,
            api_u,
            api_d,
        )
        self._reset_api_reality_probe()
        self._v7_window_reconcile_applies += 1
        self._sync_state_to_api_balances(runner, api_u, api_d, pm_u, pm_d, elapsed)
        self._resync_pending_second_after_reconcile(runner, elapsed)
        return True

    def _maybe_flatten_inventory(
        self,
        contract: ActiveContract,
        runner: PaladinV7Runner,
        pm_u: float,
        pm_d: float,
        now: float,
        elapsed: int,
    ) -> None:
        if not self.config.paladin_v7_reconcile_flatten:
            return
        if now - self._last_flatten_ts < float(self.config.paladin_v7_reconcile_flatten_cooldown_seconds):
            return
        st = runner.st
        imb = float(st.size_up) - float(st.size_down)
        tol = float(self.config.paladin_v7_reconcile_flatten_min_imbalance)
        if abs(imb) <= tol:
            return
        lighter = "down" if imb > 0 else "up"
        px = float(pm_d) if imb > 0 else float(pm_u)
        cap = max(float(self.config.paladin_v7_max_shares_per_side), float(st.size_up), float(st.size_down))
        cur_light = float(st.size_down) if imb > 0 else float(st.size_up)
        room = max(0.0, cap - cur_light)
        need = abs(imb)
        clip = float(self._v7_max_single_buy_shares())
        sh = float(min(need, clip, room))
        if sh < float(self.config.paladin_v7_min_shares) - 1e-9:
            LOGGER.info(
                self._paladin_live_log_prefix + " flatten skip: need=%.3f room=%.3f below min_shares",
                need,
                room,
            )
            return
        budget = float(self.config.strategy_budget_cap_usdc)
        filled = self._live_buy(
            contract,
            st,
            t=elapsed,
            side=lighter,
            shares=sh,
            px=px,
            reason="v7_api_imbalance_flatten",
            budget=budget,
            min_notional=float(self.config.paladin_v7_min_notional),
            min_shares=float(self.config.paladin_v7_min_shares),
            tape_pm_u=pm_u,
            tape_pm_d=pm_d,
        )
        if filled > 1e-9:
            self._last_flatten_ts = now
            self._v7_window_flatten_fills += 1
            LOGGER.warning(
                self._paladin_live_log_prefix + " flatten FAK | bought %s %.4f @ %.4f (imb was %.3f)",
                lighter.upper(),
                filled,
                px,
                imb,
            )

    def _maybe_reconcile_and_flatten(
        self,
        contract: ActiveContract,
        runner: PaladinV7Runner,
        pm_u: float,
        pm_d: float,
        now: float,
        elapsed: int,
    ) -> None:
        if not self.config.paladin_v7_reconcile_enabled:
            return
        if now - self._last_reconcile_ts < float(self.config.paladin_v7_reconcile_interval_seconds):
            return
        self._last_reconcile_ts = now

        api_u = float(self.trader.token_balance_allowance_refreshed(contract.up.token_id))
        api_d = float(self.trader.token_balance_allowance_refreshed(contract.down.token_id))
        st = runner.st
        tol = float(self.config.paladin_v7_reconcile_share_tolerance)
        du = abs(api_u - float(st.size_up))
        dd = abs(api_d - float(st.size_down))
        mismatch = du > tol or dd > tol
        if mismatch:
            self._reconcile_mismatch_count += 1
            LOGGER.info(
                self._paladin_live_log_prefix + " reconcile | model U=%.4f D=%.4f | API U=%.4f D=%.4f | "
                "|dU|=%.3f |dD|=%.3f | streak=%d/%d",
                st.size_up,
                st.size_down,
                api_u,
                api_d,
                du,
                dd,
                self._reconcile_mismatch_count,
                int(self.config.paladin_v7_reconcile_confirm_reads),
            )
        else:
            self._reconcile_mismatch_count = 0
            return

        if self._reconcile_mismatch_count < int(self.config.paladin_v7_reconcile_confirm_reads):
            return

        self._reconcile_mismatch_count = 0
        LOGGER.warning(
            self._paladin_live_log_prefix + " reconcile: applying API balances (confirmed %d reads)",
            int(self.config.paladin_v7_reconcile_confirm_reads),
        )
        self._v7_window_reconcile_applies += 1
        self._sync_state_to_api_balances(runner, api_u, api_d, pm_u, pm_d, elapsed)
        self._resync_pending_second_after_reconcile(runner, elapsed)
        # Avoid flatten FAK competing with the same-cycle v7 hedge when pending was rebuilt from API skew.
        if runner.pending_second is None:
            self._maybe_flatten_inventory(contract, runner, pm_u, pm_d, now, elapsed)

    def _loop_once(self) -> None:
        contract = self.locator.get_active_contract()
        if contract is None:
            return

        now = time.time()
        end_ts = int(contract.end_time.timestamp())
        start_ts = end_ts - self.config.window_size_seconds
        slug = contract.slug
        wsec = self.config.window_size_seconds

        if slug != self._slug:
            self._slug = slug
            self._runner = PaladinV7Runner()
            self._sec_pm_u = [0.5] * wsec
            self._sec_pm_d = [0.5] * wsec
            self._sec_btc_px = [0.0] * wsec
            self._sec_btc_vol = [0.0] * wsec
            self._pre_window_warned_slug = None
            self._force_exit_warned_slug = None
            self._entry_delay_warned_slug = None
            self._new_cutoff_warned_slug = None
            self._last_reconcile_ts = 0.0
            self._reconcile_mismatch_count = 0
            self._last_flatten_ts = 0.0
            self._v7_window_reconcile_applies = 0
            self._v7_window_flatten_fills = 0
            self._live_order_serial = 0
            self._last_untracked_open_order_log_ts = 0.0
            self._reset_api_reality_probe()
            if not self._active_limit_order_id:
                self._limit_order_busy_until_ts = 0.0
                self._limit_order_busy_reason = ""
            else:
                LOGGER.warning(
                    self._paladin_live_log_prefix + " live: carrying unresolved order %s into new window; no new orders until resolved",
                    self._active_limit_order_id[:24] + "…",
                )
            self._v7_steps_fired = set()
            LOGGER.info(self._paladin_live_log_prefix + " live: new window %s", slug)
            if self._ws is not None:
                self._ws.set_assets([contract.up.token_id, contract.down.token_id])

        assert self._runner is not None
        runner = self._runner

        if now < start_ts:
            if self._pre_window_warned_slug != slug:
                self._pre_window_warned_slug = slug
                LOGGER.info(
                    self._paladin_live_log_prefix + " live: pre-window for %s (opens in %.0fs); no entries until then",
                    slug,
                    start_ts - now,
                )
            return

        elapsed = int(now - start_ts)
        elapsed = max(0, min(elapsed, wsec - 1))

        secs_left = end_ts - now
        pend_for_force = runner.pending_second
        if secs_left <= float(self.config.force_exit_before_end_seconds):
            if pend_for_force is None:
                if self._force_exit_warned_slug != slug:
                    self._force_exit_warned_slug = slug
                    LOGGER.info(
                        self._paladin_live_log_prefix + " live: force-exit zone (%.0fs left); no new entries (no open hedge)",
                        secs_left,
                    )
                return
            if self._force_exit_warned_slug != slug:
                self._force_exit_warned_slug = slug
                LOGGER.info(
                    self._paladin_live_log_prefix + " live: force-exit zone (%.0fs left) but pending hedge — still trying",
                    secs_left,
                )

        pm_u = self._token_price(contract.up)
        pm_d = self._token_price(contract.down)
        if pm_u is None or pm_d is None:
            hb = float(self.config.paladin_heartbeat_seconds)
            if now - self._last_missing_price_log_ts >= hb:
                self._last_missing_price_log_ts = now
                LOGGER.info(
                    self._paladin_live_log_prefix + " live: waiting for up/down mids (WS/REST); slug=%s",
                    slug,
                )
            return

        if self._btc is None:
            if now - self._last_missing_price_log_ts >= 30.0:
                self._last_missing_price_log_ts = now
                LOGGER.warning(self._paladin_live_log_prefix + ": BTC feed disabled — cannot evaluate spikes; enable BOT_BTC_FEED_ENABLED")
            return

        btc_point = self._btc.poll()
        bv = float(btc_point.base_volume or 0.0)
        self._sec_pm_u[elapsed] = float(pm_u)
        self._sec_pm_d[elapsed] = float(pm_d)
        self._sec_btc_px[elapsed] = float(btc_point.price)
        self._sec_btc_vol[elapsed] += bv

        if self._process_persistent_limit_order(contract, runner.st, t=elapsed, now=now):
            return
        if self._has_unresolved_active_limit_order(now):
            # Resting cheap hedge must not block ``paladin_v7_step`` (forced hedge uses window ``elapsed``).
            if not (
                self._active_limit_order_persistent
                and str(self._active_limit_order_reason) == "v7_hedge_cheap"
            ):
                return
        if self._has_untracked_open_buy_order(contract, now):
            return
        if now < self._limit_order_busy_until_ts - 1e-9:
            return

        order_serial_0 = self._live_order_serial
        self._maybe_reconcile_and_flatten(contract, runner, float(pm_u), float(pm_d), now, elapsed)
        if self._live_order_serial != order_serial_0:
            return
        if self._maybe_accept_api_balance_reality(contract, runner, float(pm_u), float(pm_d), now, elapsed):
            return
        pend = runner.pending_second

        cutoff = float(self.config.strategy_new_order_cutoff_seconds)
        if secs_left <= cutoff and pend is None:
            if self._new_cutoff_warned_slug != slug:
                self._new_cutoff_warned_slug = slug
                LOGGER.info(
                    self._paladin_live_log_prefix + " live: new-order cutoff (%.0fs left <= %.0fs); no new clips (hedges still run)",
                    secs_left,
                    cutoff,
                )
            return

        hb_sec = float(self.config.paladin_heartbeat_seconds)
        if now - self._last_hb_ts >= hb_sec:
            self._last_hb_ts = now
            snap = runner.st.snapshot_metrics()
            pend_s = f"{pend[0]}×{pend[1]:.0f}" if pend is not None else "—"
            LOGGER.info(
                self._paladin_live_log_prefix + " hb | %s | el=%ds left=%.0fs | mkt_mid up=%.4f dn=%.4f | "
                "btc=%.2f vol+%.4f/sum_el | spent=$%.2f | inv UP=%.2fsh vwap=%.3f | inv DN=%.2fsh vwap=%.3f | "
                "pnl_up=$%.2f pnl_dn=$%.2f | pending=%s trades=%d | "
                "api_rec=%d api_flat=%d",
                slug,
                elapsed,
                secs_left,
                pm_u,
                pm_d,
                float(btc_point.price),
                bv,
                runner.st.spent_usdc,
                runner.st.size_up,
                runner.st.avg_up,
                runner.st.size_down,
                runner.st.avg_down,
                snap["pnl_if_up_usdc"],
                snap["pnl_if_down_usdc"],
                pend_s,
                len(runner.st.trades),
                self._v7_window_reconcile_applies,
                self._v7_window_flatten_fills,
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
            px_eff = float(px)
            persistent_limit_until_ts: float | None = None
            persistent_cancel_at_elapsed: int | None = None
            mh = float(self.config.paladin_v7_cheap_pair_avg_sum_nonforced_max)
            slip = float(self.config.paladin_v7_cheap_hedge_slip_buffer)
            # First hedges from a one-sided book still use the held+opp pair-cost cap.
            # Once both sides exist, the strategy itself already gated on a better smaller-side price.
            if reason == "v7_hedge_cheap" and runner.pending_second is not None:
                t0 = int(runner.pending_second[3])
                ht = float(self.config.paladin_v7_hedge_timeout_seconds)
                # Cancel resting cheap hedge at the same window-second deadline as sim ``ok_forced`` (t0 + timeout).
                persistent_cancel_at_elapsed = t0 + int(math.ceil(ht))
                # Wall-clock fallback only (elapsed-based cancel is authoritative for v7_hedge_cheap).
                persistent_limit_until_ts = time.time() + max(600.0, ht + 600.0)
                if min(float(st.size_up), float(st.size_down)) < float(self.config.paladin_v7_min_shares) - 1e-9:
                    avg_first = float(runner.pending_second[2])
                    px_eff = min(px_eff, max(0.01, mh - avg_first - slip - 1e-4))
            elif reason == "v7_first_window_lead":
                tok = contract.up if side == "up" else contract.down
                ask = self._best_ask_price(tok)
                if ask is not None:
                    # First-window lead should actually open the book, not rest near the midpoint.
                    px_eff = max(px_eff, ask)
            elif reason == "v7_hedge_forced":
                tok = contract.up if side == "up" else contract.down
                ask = self._best_ask_price(tok)
                if ask is not None:
                    # Forced hedge should be willing to pay the current ask; otherwise "forced"
                    # can keep posting near the mid and miss indefinitely.
                    px_eff = max(px_eff, ask)
            elif reason in {"v7_first_binance_spike", "v7_balanced_btc_spike"}:
                tok = contract.up if side == "up" else contract.down
                ask = self._best_ask_price(tok)
                spike_buf = float(self.config.paladin_v7_spike_market_price_buffer)
                ask_cross = (float(ask) + min(0.01, spike_buf)) if ask is not None else 0.0
                px_eff = min(0.99, max(px_eff + spike_buf, ask_cross))
            return self._live_buy(
                contract,
                st,
                t=t,
                side=side,
                shares=shares,
                px=px_eff,
                reason=reason,
                budget=budget,
                min_notional=min_notional,
                min_shares=min_shares,
                persistent_limit_until_ts=persistent_limit_until_ts,
                persistent_cancel_at_elapsed=persistent_cancel_at_elapsed,
                tape_pm_u=float(pm_u),
                tape_pm_d=float(pm_d),
            )

        # Exactly one strategy step per market second (see module docstring).
        if elapsed in self._v7_steps_fired:
            return
        self._v7_steps_fired.add(elapsed)
        ticks = _build_ticks(self._sec_pm_u, self._sec_pm_d, self._sec_btc_px, self._sec_btc_vol, elapsed, wsec)
        paladin_v7_step(runner, elapsed, ticks, params=self._v7_params, try_buy_fn=try_buy_fn)
