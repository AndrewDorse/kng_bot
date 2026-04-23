#!/usr/bin/env python3
"""
Live PALADIN v7: Binance agg-trade volume + spot move (via RealtimeBtcPriceFeed) + Polymarket mids.

Each poll updates the per-second PM/BTC arrays. ``paladin_v7_step`` is designed for replay: **one call
per integer market second** ``elapsed``. When ``poll_interval_seconds`` is below 1, many polls can share the
same ``elapsed``; we therefore run ``paladin_v7_step`` at most once per ``(slug, elapsed)`` so signals
are not fired twice in the same second (duplicate FAKs). A **set** of fired ``elapsed`` values (not only
``last == elapsed``) avoids re-running an older second if ``elapsed`` ever moves backward (clock skew).
``pending_second`` is re-read **after** reconcile so cutoff/entry-delay gates match post-sync state.
FAK POST+confirm stays serialized in
``PolymarketTrader`` via a lock.
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
        cheap_other_margin=float(cfg.paladin_v7_cheap_other_margin),
        cheap_pair_sum_max=float(cfg.paladin_v7_cheap_pair_sum_max),
        cheap_pair_avg_sum_nonforced_max=float(cfg.paladin_v7_cheap_pair_avg_sum_nonforced_max),
        cheap_hedge_slip_buffer=float(cfg.paladin_v7_cheap_hedge_slip_buffer),
        cheap_hedge_min_delay_sec=float(cfg.paladin_v7_cheap_hedge_min_delay_sec),
        hedge_timeout_seconds=float(cfg.paladin_v7_hedge_timeout_seconds),
        forced_hedge_max_book_sum=float(cfg.paladin_v7_forced_hedge_max_book_sum),
        layer2_dip_below_avg=float(cfg.paladin_v7_layer2_dip_below_avg),
        layer2_low_vwap_dip_below_avg=float(cfg.paladin_v7_layer2_low_vwap_dip_below_avg),
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
    """Continuous BTC 15m PALADIN v7: Binance volume spike + BTC impulse → FAK legs."""

    def __init__(self, config: BotConfig, locator: GammaMarketLocator, trader: PolymarketTrader) -> None:
        self.config = config
        self.locator = locator
        self.trader = trader
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
            "PALADIN v7 live started | dry_run=%s poll=%.1fs budget=$%.2f base_order=%.1f max/side=%.0f "
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
            "PALADIN v7 reconcile | enabled=%s every=%.1fs tol=%.2f sh confirm_reads=%d flatten=%s",
            self.config.paladin_v7_reconcile_enabled,
            float(self.config.paladin_v7_reconcile_interval_seconds),
            float(self.config.paladin_v7_reconcile_share_tolerance),
            int(self.config.paladin_v7_reconcile_confirm_reads),
            self.config.paladin_v7_reconcile_flatten,
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
        LOGGER.info("PALADIN v7 live: shutdown requested")
        self._stop = True

    def _token_price(self, tm: TokenMarket) -> float | None:
        if self._ws is not None:
            mid = self._ws.mid_for(tm.token_id, max_age_sec=5.0)
            if mid is not None and mid > 0:
                return float(mid)
        p = self.trader.get_market_price(tm.token_id)
        if p is not None and p > 0:
            return float(p)
        mid = self.trader.get_midpoint(tm.token_id)
        return float(mid) if mid is not None and mid > 0 else None

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
    ) -> float:
        px = float(px)
        # Raise FAK cap vs signal mid so CLOB can match (cheap gate uses the same buffer in pair_held_quote_sum).
        if str(reason).startswith("v7_"):
            px = min(0.99, px + float(self.config.paladin_v7_cheap_hedge_slip_buffer))
        px = round(px, 4)
        notion = shares * px
        if shares < min_shares - 1e-9 or notion < min_notional - 1e-9:
            return 0.0
        size = int(round(shares))
        if size < int(math.ceil(min_shares)):
            return 0.0
        tok = contract.up if side == "up" else contract.down
        if self.config.dry_run:
            LOGGER.info(
                "[PALADIN v7 dry_run] BUY %s size=%d @ %.4f (%s) ~$%.2f",
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
            )
        try:
            res = self.trader.place_marketable_buy_with_result(
                tok,
                px,
                size,
                confirm_get_order=self.config.polymarket_fak_confirm_get_order,
            )
        except PolyApiException as exc:
            LOGGER.warning("PALADIN v7 FAK POST rejected %s %s @ %.4f: %s", side, size, px, exc)
            return 0.0
        except Exception as exc:
            LOGGER.warning("PALADIN v7 live BUY failed %s %s @ %.4f: %s", side, size, px, exc)
            return 0.0

        if not res.matched_any:
            LOGGER.warning(
                "PALADIN v7 FAK no fill | status=%s err=%s oid=%s",
                res.status,
                res.error,
                (res.order_id[:20] + "…") if res.order_id else "",
            )
            return 0.0

        filled = float(res.filled_shares)
        avg_px = float(res.avg_price) if res.avg_price > 0 else px
        spent = float(res.filled_usdc) if res.filled_usdc > 1e-9 else filled * avg_px
        if filled <= 1e-9:
            return 0.0
        if not _can_afford_live(st.spent_usdc, spent, budget):
            LOGGER.warning("PALADIN v7: fill would exceed budget; skipping state update (filled=%.4f)", filled)
            return 0.0

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
            "PALADIN v7 FAK filled %s %.4f sh @ %.4f ($%.2f) | %s | oid=%s",
            side.upper(),
            filled,
            avg_px,
            spent,
            reason,
            (res.order_id[:24] + "…") if res.order_id else "?",
        )
        self._align_leg_to_api_after_fak(contract, st, t=t, side=side, px_hint=avg_px)
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
        """Last non-reconcile trade price for ``side`` (actual FAK VWAP), for reconcile economics."""
        for tr in reversed(st.trades):
            if str(tr.side) != side:
                continue
            r = str(tr.reason)
            if "v7_api_reconcile_sync" in r or "v7_post_fak_api_sync" in r:
                continue
            if float(tr.price) > 1e-9:
                return float(tr.price)
        return None

    def _align_leg_to_api_after_fak(
        self,
        contract: ActiveContract,
        st: SimState,
        *,
        t: int,
        side: str,
        px_hint: float,
    ) -> None:
        """One refresh vs CLOB balance for the bought token; trim or add model shares if drift exceeds tolerance."""
        tok = contract.up if side == "up" else contract.down
        tol = float(self.config.paladin_v7_reconcile_share_tolerance)
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
                LOGGER.debug("post-FAK balance read skipped: %s", exc)
                return
            if api > 0.25 or abs(api - cur) <= tol:
                break
            if attempt == 3:
                break

        ms = float(self.config.paladin_v7_min_shares)
        # CLOB balances often lag right after a FAK; API=0 with model>0 would incorrectly zero the leg.
        if cur + 1e-9 >= ms and api < 0.25:
            LOGGER.warning(
                "PALADIN v7 post-FAK: skip API align %s (API=%.4f vs model=%.4f; likely stale balance read)",
                side.upper(),
                api,
                cur,
            )
            return

        delta = api - cur
        if abs(delta) <= tol:
            return
        if delta < -tol:
            if cur + 1e-9 >= ms and api < 1.0:
                LOGGER.warning(
                    "PALADIN v7 post-FAK: refuse trim %s (API=%.4f vs model=%.4f; likely stale)",
                    side.upper(),
                    api,
                    cur,
                )
                return
            remove = -delta
            prev_avg = float(st.avg_up) if side == "up" else float(st.avg_down)
            self._shrink_leg(st, side, remove)
            st.spent_usdc = max(0.0, float(st.spent_usdc) - remove * prev_avg)
            LOGGER.warning(
                "PALADIN v7 post-FAK API trim %s by %.4f sh (API %.4f vs model %.4f)",
                side.upper(),
                remove,
                api,
                cur,
            )
            return
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
                "v7_post_fak_api_sync|live",
            )
        )
        LOGGER.warning(
            "PALADIN v7 post-FAK API add %s +%.4f sh @ %.4f (API %.4f vs model %.4f)",
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
                    "PALADIN v7 reconcile SYNC +%.4f %s sh @ %.4f (~$%.2f) to match API (mid=%.4f if no exec ref)",
                    delta,
                    side,
                    float(fill_px),
                    notion,
                    float(pm),
                )
            else:
                self._shrink_leg(st, side, -delta)
                LOGGER.warning(
                    "PALADIN v7 reconcile TRIM %.4f %s sh (model ahead of API)",
                    -delta,
                    side,
                )

    def _resync_pending_second_after_reconcile(self, runner: PaladinV7Runner, elapsed: int) -> None:
        """Rebuild open-hedge intent from inventory after API sync (do not drop pending on stale reads)."""
        st = runner.st
        du = float(st.size_up) - float(st.size_down)
        # Treat only small drift as "flat hedge need" — not min_shares*0.51 (~2.55 sh), which cleared
        # pending while still multi-share imbalanced and blocked hedges after reconcile.
        eps = max(0.05, float(self.config.paladin_v7_reconcile_share_tolerance))
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
            runner.pending_second = ("down", float(du), float(st.avg_up), int(elapsed))
        elif du < -eps:
            if float(st.avg_down) <= 1e-9:
                runner.pending_second = None
                return
            runner.pending_second = ("up", float(-du), float(st.avg_down), int(elapsed))

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
        cap = float(self.config.paladin_v7_max_shares_per_side)
        cur_light = float(st.size_down) if imb > 0 else float(st.size_up)
        room = max(0.0, cap - cur_light)
        need = abs(imb)
        clip = float(self.config.paladin_v7_base_order_shares)
        sh = float(min(need, clip, room))
        if sh < float(self.config.paladin_v7_min_shares) - 1e-9:
            LOGGER.info(
                "PALADIN v7 flatten skip: need=%.3f room=%.3f below min_shares",
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
        )
        if filled > 1e-9:
            self._last_flatten_ts = now
            self._v7_window_flatten_fills += 1
            LOGGER.warning(
                "PALADIN v7 flatten FAK | bought %s %.4f @ %.4f (imb was %.3f)",
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
                "PALADIN v7 reconcile | model U=%.4f D=%.4f | API U=%.4f D=%.4f | "
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
            "PALADIN v7 reconcile: applying API balances (confirmed %d reads)",
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
            self._v7_steps_fired = set()
            LOGGER.info("PALADIN v7 live: new window %s", slug)
            if self._ws is not None:
                self._ws.set_assets([contract.up.token_id, contract.down.token_id])

        assert self._runner is not None
        runner = self._runner

        if now < start_ts:
            if self._pre_window_warned_slug != slug:
                self._pre_window_warned_slug = slug
                LOGGER.info(
                    "PALADIN v7 live: pre-window for %s (opens in %.0fs); no entries until then",
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
                        "PALADIN v7 live: force-exit zone (%.0fs left); no new entries (no open hedge)",
                        secs_left,
                    )
                return
            if self._force_exit_warned_slug != slug:
                self._force_exit_warned_slug = slug
                LOGGER.info(
                    "PALADIN v7 live: force-exit zone (%.0fs left) but pending hedge — still trying",
                    secs_left,
                )

        pm_u = self._token_price(contract.up)
        pm_d = self._token_price(contract.down)
        if pm_u is None or pm_d is None:
            hb = float(self.config.paladin_heartbeat_seconds)
            if now - self._last_missing_price_log_ts >= hb:
                self._last_missing_price_log_ts = now
                LOGGER.info(
                    "PALADIN v7 live: waiting for up/down mids (WS/REST); slug=%s",
                    slug,
                )
            return

        if self._btc is None:
            if now - self._last_missing_price_log_ts >= 30.0:
                self._last_missing_price_log_ts = now
                LOGGER.warning("PALADIN v7: BTC feed disabled — cannot evaluate spikes; enable BOT_BTC_FEED_ENABLED")
            return

        btc_point = self._btc.poll()
        bv = float(btc_point.base_volume or 0.0)
        self._sec_pm_u[elapsed] = float(pm_u)
        self._sec_pm_d[elapsed] = float(pm_d)
        self._sec_btc_px[elapsed] = float(btc_point.price)
        self._sec_btc_vol[elapsed] += bv

        self._maybe_reconcile_and_flatten(contract, runner, float(pm_u), float(pm_d), now, elapsed)
        pend = runner.pending_second

        entry_delay = int(self.config.strategy_entry_delay_seconds)
        if elapsed < entry_delay and pend is None:
            if self._entry_delay_warned_slug != slug:
                self._entry_delay_warned_slug = slug
                LOGGER.info(
                    "PALADIN v7 live: entry delay (%ds) for %s; elapsed=%ds",
                    entry_delay,
                    slug,
                    elapsed,
                )
            return

        cutoff = float(self.config.strategy_new_order_cutoff_seconds)
        if secs_left <= cutoff and pend is None:
            if self._new_cutoff_warned_slug != slug:
                self._new_cutoff_warned_slug = slug
                LOGGER.info(
                    "PALADIN v7 live: new-order cutoff (%.0fs left <= %.0fs); no new clips (hedges still run)",
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
                "PALADIN v7 hb | %s | el=%ds left=%.0fs | mkt_mid up=%.4f dn=%.4f | "
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
        ) -> float:
            px_eff = float(px)
            mh = float(self.config.paladin_v7_cheap_pair_avg_sum_nonforced_max)
            slip = float(self.config.paladin_v7_cheap_hedge_slip_buffer)
            # Non-forced cap on *our* hedge: clamp FAK vs held first-leg VWAP (same economics as sim).
            if reason == "v7_hedge_cheap" and runner.pending_second is not None:
                avg_first = float(runner.pending_second[2])
                px_eff = min(px_eff, max(0.01, mh - avg_first - slip - 1e-4))
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
            )

        # Exactly one strategy step per market second (see module docstring).
        if elapsed in self._v7_steps_fired:
            return
        self._v7_steps_fired.add(elapsed)
        ticks = _build_ticks(self._sec_pm_u, self._sec_pm_d, self._sec_btc_px, self._sec_btc_vol, elapsed, wsec)
        paladin_v7_step(runner, elapsed, ticks, params=self._v7_params, try_buy_fn=try_buy_fn)
