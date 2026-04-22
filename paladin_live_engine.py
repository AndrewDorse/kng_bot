#!/usr/bin/env python3
"""
Live PALADIN pair-only loop: WebSocket mids + FAK buys (POST + optional GET fill confirm).

Core loop (each poll, default ~1s via BOT_POLL_INTERVAL_SECONDS):
  1) Resolve active 15m contract; refresh WS asset ids on window change.
  2) Read UP/DOWN mids (WS first, REST fallback).
  3) Build window elapsed time; apply pre-window / entry-delay / new-order-cutoff / end-game guards
     (pending hedge legs are still completed when we would otherwise block new risk).
  4) Run paladin_step on shared PALADIN rules: profit-lock (PnL+ROI vs paladin_sim_config.json),
     staggered or symmetric pair adds, marginal ROI + pair-sum gates (with per-fill tighten + floor),
     hedge force timer, imbalance bypass and post-force relax (BOT_PALADIN_*), max shares/side cap.
  5) Execute marketable buys via PolymarketTrader (FAK); update sim state for next tick.

Cooldown: BOT_PALADIN_COOLDOWN_SEC=0 on live (no sim-only delay between legs). Replay uses ~2s.
"""

from __future__ import annotations

import math
import signal
import sys
import time
from pathlib import Path
from typing import Any

from config import LOGGER, ActiveContract, BotConfig, TokenMarket
from market_locator import GammaMarketLocator
from py_clob_client.exceptions import PolyApiException
from trader import PolymarketTrader

_PALADIN = Path(__file__).resolve().parent / "PALADIN"
if str(_PALADIN) not in sys.path:
    sys.path.insert(0, str(_PALADIN))

from paladin_engine import PaladinParams, apply_buy_fill  # noqa: E402
from simulate_paladin_window import (  # noqa: E402
    PaladinPairRunner,
    SimState,
    Trade,
    load_profit_lock_config,
    paladin_step,
    try_buy,
)


def _can_afford_live(spent: float, add: float, budget: float) -> bool:
    return spent + add <= budget + 1e-6


class PaladinLiveEngine:
    """Continuous BTC 15m PALADIN: priced + inventory-aware pair/hedge FAK execution."""

    def __init__(self, config: BotConfig, locator: GammaMarketLocator, trader: PolymarketTrader) -> None:
        self.config = config
        self.locator = locator
        self.trader = trader
        self._stop = False
        cfg_path = _PALADIN / "paladin_sim_config.json"
        pl = load_profit_lock_config(cfg_path)
        self._params = PaladinParams(
            profit_lock_min_shares_per_side=float(pl["profit_lock_min_shares_per_side"]),
            roi_lock_min_each=float(pl["roi_lock_min_each"]),
            profit_lock_usdc_each_scenario=float(pl["profit_lock_usdc_each_scenario"]),
        )
        self._runner: PaladinPairRunner | None = None
        self._slug: str | None = None
        self._ws: Any = None
        self._last_hb_ts: float = 0.0
        self._last_missing_price_log_ts: float = 0.0
        self._pre_window_warned_slug: str | None = None
        self._force_exit_warned_slug: str | None = None
        self._entry_delay_warned_slug: str | None = None
        self._new_cutoff_warned_slug: str | None = None
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
            "PALADIN live engine started | dry_run=%s poll=%.1fs paladin_cooldown=%.2fs (0=live)",
            self.config.dry_run,
            float(self.config.poll_interval_seconds),
            float(self.config.paladin_cooldown_seconds),
        )
        LOGGER.info(
            "PALADIN discipline | tighten_pf=%.4f sum_floor=%.2f imb_bypass_sh=%s relax_after_force_s=%s",
            float(self.config.paladin_pair_sum_tighten_per_fill),
            float(self.config.paladin_pair_sum_min_floor),
            self.config.paladin_pending_hedge_bypass_imbalance_shares,
            self.config.paladin_discipline_relax_after_forced_sec,
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
        LOGGER.info("PALADIN live: shutdown requested")
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
                "[PALADIN dry_run] BUY %s size=%d @ %.4f (%s) ~$%.2f",
                side.upper(),
                size,
                px,
                reason,
                notion,
            )
            return try_buy(
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
            LOGGER.warning("PALADIN FAK POST rejected %s %s @ %.4f: %s", side, size, px, exc)
            return 0.0
        except Exception as exc:
            LOGGER.warning("PALADIN live BUY failed %s %s @ %.4f: %s", side, size, px, exc)
            return 0.0

        if not res.matched_any:
            LOGGER.warning(
                "PALADIN FAK no fill | status=%s err=%s oid=%s",
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
            LOGGER.warning("PALADIN: fill would exceed budget; skipping state update (filled=%.4f)", filled)
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
            "PALADIN FAK filled %s %.4f sh @ %.4f ($%.2f) | %s | oid=%s",
            side.upper(),
            filled,
            avg_px,
            spent,
            reason,
            (res.order_id[:24] + "…") if res.order_id else "?",
        )
        return filled

    def _loop_once(self) -> None:
        contract = self.locator.get_active_contract()
        if contract is None:
            return

        now = time.time()
        end_ts = int(contract.end_time.timestamp())
        start_ts = end_ts - self.config.window_size_seconds
        slug = contract.slug

        if slug != self._slug:
            self._slug = slug
            self._runner = PaladinPairRunner()
            self._pre_window_warned_slug = None
            self._force_exit_warned_slug = None
            self._entry_delay_warned_slug = None
            self._new_cutoff_warned_slug = None
            LOGGER.info("PALADIN live: new window %s", slug)
            if self._ws is not None:
                self._ws.set_assets([contract.up.token_id, contract.down.token_id])

        assert self._runner is not None
        runner = self._runner

        if runner.st.locked:
            return

        if now < start_ts:
            if self._pre_window_warned_slug != slug:
                self._pre_window_warned_slug = slug
                LOGGER.info(
                    "PALADIN live: pre-window for %s (opens in %.0fs); no entries until then",
                    slug,
                    start_ts - now,
                )
            return

        elapsed = int(now - start_ts)
        elapsed = max(0, min(elapsed, self.config.window_size_seconds - 1))

        secs_left = end_ts - now
        pend0 = runner.pending_second_leg
        if secs_left <= float(self.config.force_exit_before_end_seconds):
            if pend0 is None:
                if self._force_exit_warned_slug != slug:
                    self._force_exit_warned_slug = slug
                    LOGGER.info(
                        "PALADIN live: force-exit zone (%.0fs left in %s); no new entries (no open hedge)",
                        secs_left,
                        slug,
                    )
                return
            if self._force_exit_warned_slug != slug:
                self._force_exit_warned_slug = slug
                LOGGER.info(
                    "PALADIN live: force-exit zone (%.0fs left) but pending hedge %s×%.0f — still trying to balance",
                    secs_left,
                    pend0[0],
                    pend0[1],
                )

        pm_u = self._token_price(contract.up)
        pm_d = self._token_price(contract.down)
        if pm_u is None or pm_d is None:
            hb = float(self.config.paladin_heartbeat_seconds)
            if now - self._last_missing_price_log_ts >= hb:
                self._last_missing_price_log_ts = now
                LOGGER.info(
                    "PALADIN live: waiting for up/down mids (WS/REST); slug=%s up=%s down=%s",
                    slug,
                    "ok" if pm_u is not None else "missing",
                    "ok" if pm_d is not None else "missing",
                )
            return

        entry_delay = int(self.config.strategy_entry_delay_seconds)
        if elapsed < entry_delay and runner.pending_second_leg is None:
            if self._entry_delay_warned_slug != slug:
                self._entry_delay_warned_slug = slug
                LOGGER.info(
                    "PALADIN live: entry delay (%ds) for %s; no new clips yet (elapsed=%ds)",
                    entry_delay,
                    slug,
                    elapsed,
                )
            return

        cutoff = float(self.config.strategy_new_order_cutoff_seconds)
        if secs_left <= cutoff and runner.pending_second_leg is None:
            if self._new_cutoff_warned_slug != slug:
                self._new_cutoff_warned_slug = slug
                LOGGER.info(
                    "PALADIN live: new-order cutoff (%.0fs left <= %.0fs) for %s; no new clips (hedges still run if pending)",
                    secs_left,
                    cutoff,
                    slug,
                )
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
        ) -> float:
            return self._live_buy(
                contract,
                st,
                t=t,
                side=side,
                shares=shares,
                px=px,
                reason=reason,
                budget=budget,
                min_notional=min_notional,
                min_shares=min_shares,
            )

        pair_max = float(self.config.paladin_pair_sum_max)
        roi_tgt = float(self.config.paladin_target_min_roi)
        hb_sec = float(self.config.paladin_heartbeat_seconds)
        s = float(pm_u) + float(pm_d)
        implied_second_leg_cap = 1.0 / (1.0 + roi_tgt) if roi_tgt > -1.0 else 1.0
        if now - self._last_hb_ts >= hb_sec:
            self._last_hb_ts = now
            pend = runner.pending_second_leg
            pend_s = f"{pend[0]}×{pend[1]:.0f}" if pend is not None else "—"
            snap = runner.st.snapshot_metrics()
            LOGGER.info(
                "PALADIN heartbeat | %s | elapsed=%ds left=%.0fs | mid_up=%.4f mid_dn=%.4f sum=%.4f "
                "| stagger=%s 1st_leg_mid<=%.3f pending=%s | 2nd_leg: sum<=%.3f roi>=%.3f (~sum<=%.3f) "
                "| spent=$%.2f | U=%.2f@%.3f D=%.2f@%.3f | pnl_if_up=$%.2f pnl_if_dn=$%.2f roi_u=%.4f roi_d=%.4f",
                slug,
                elapsed,
                secs_left,
                pm_u,
                pm_d,
                s,
                self.config.paladin_stagger_pair,
                float(self.config.paladin_first_leg_max_px),
                pend_s,
                pair_max,
                roi_tgt,
                implied_second_leg_cap,
                runner.st.spent_usdc,
                runner.st.size_up,
                runner.st.avg_up,
                runner.st.size_down,
                runner.st.avg_down,
                snap["pnl_if_up_usdc"],
                snap["pnl_if_down_usdc"],
                snap["roi_up"],
                snap["roi_dn"],
            )

        stopped = paladin_step(
            runner,
            elapsed,
            pm_u,
            pm_d,
            budget_usdc=float(self.config.strategy_budget_cap_usdc),
            params=self._params,
            pair_sum_max=pair_max,
            single_leg_max_px=float(self.config.paladin_first_leg_max_px),
            pair_only=True,
            stagger_pair_entry=bool(self.config.paladin_stagger_pair),
            stagger_hedge_force_after_seconds=self.config.paladin_stagger_hedge_force_after_seconds,
            max_shares_per_side=self.config.paladin_max_shares_per_side,
            target_min_roi=roi_tgt,
            cooldown_seconds=float(self.config.paladin_cooldown_seconds),
            dynamic_clip_cap=float(self.config.paladin_dynamic_clip_cap),
            pair_size_pick="max_feasible",
            min_elapsed_for_flat_open=int(self.config.strategy_entry_delay_seconds),
            try_buy_fn=try_buy_fn,
            pair_sum_tighten_per_fill=float(self.config.paladin_pair_sum_tighten_per_fill),
            pair_sum_min_floor=float(self.config.paladin_pair_sum_min_floor),
            pending_hedge_bypass_imbalance_shares=self.config.paladin_pending_hedge_bypass_imbalance_shares,
            discipline_relax_after_forced_sec=self.config.paladin_discipline_relax_after_forced_sec,
            second_leg_book_improve_eps=float(self.config.paladin_second_leg_book_improve_eps),
            max_blended_pair_avg_sum=self.config.paladin_max_blended_pair_avg_sum,
            stagger_winning_side_first_when_position=bool(
                self.config.paladin_stagger_winning_side_first_when_position
            ),
            stagger_symmetric_fallback_when_balanced=bool(
                self.config.paladin_stagger_symmetric_fallback_when_balanced
            ),
            stagger_symmetric_fallback_roi_discount=float(
                self.config.paladin_stagger_symmetric_fallback_roi_discount
            ),
            stagger_symmetric_fallback_skip_first_leg_blend_cap=bool(
                self.config.paladin_stagger_symmetric_fallback_skip_first_leg_blend_cap
            ),
            stagger_alternate_first_leg_when_balanced=bool(
                self.config.paladin_stagger_alternate_first_leg_when_balanced
            ),
            min_elapsed_between_pair_starts=self.config.paladin_min_elapsed_between_pair_starts,
            entry_trailing_min_low_seconds=self.config.paladin_entry_trailing_min_low_seconds,
            entry_trailing_low_slippage=float(
                self.config.paladin_entry_trailing_low_slippage
            ),
        )
        if stopped:
            m = runner.st.snapshot_metrics()
            LOGGER.info(
                "PALADIN profit-lock: %s | spent=$%.2f pnl_up=$%.2f pnl_dn=$%.2f",
                runner.st.lock_reason,
                m["spent_usdc"],
                m["pnl_if_up_usdc"],
                m["pnl_if_down_usdc"],
            )
