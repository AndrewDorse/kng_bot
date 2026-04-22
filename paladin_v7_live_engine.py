#!/usr/bin/env python3
"""
Live PALADIN v7: Binance agg-trade volume + spot move (via RealtimeBtcPriceFeed) + Polymarket mids.

Each poll builds a 900s causal tick vector (per-second PM + accumulated Binance volume in that second),
then runs one ``paladin_v7_step`` at the current window ``elapsed`` with FAK execution wired like
``paladin_live_engine.PaladinLiveEngine``.
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
        clip_shares=float(cfg.paladin_v7_clip_shares),
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
        hedge_timeout_seconds=float(cfg.paladin_v7_hedge_timeout_seconds),
        forced_hedge_max_book_sum=float(cfg.paladin_v7_forced_hedge_max_book_sum),
        refill_clip_fraction=float(cfg.paladin_v7_refill_clip_fraction),
        refill_max_pair_sum=float(cfg.paladin_v7_refill_max_pair_sum),
        pair_cooldown_sec=float(cfg.paladin_v7_pair_cooldown_sec),
        max_orders=int(cfg.paladin_v7_max_orders),
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
        self._pre_window_warned_slug: str | None = None
        self._force_exit_warned_slug: str | None = None
        self._entry_delay_warned_slug: str | None = None
        self._new_cutoff_warned_slug: str | None = None
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
            "PALADIN v7 live started | dry_run=%s poll=%.1fs budget=$%.2f clip=%.1f max/side=%.0f max_orders=%d",
            self.config.dry_run,
            float(self.config.poll_interval_seconds),
            float(self.config.strategy_budget_cap_usdc),
            float(self.config.paladin_v7_clip_shares),
            float(self.config.paladin_v7_max_shares_per_side),
            int(self.config.paladin_v7_max_orders),
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
        return filled

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
        pend = runner.pending_second
        if secs_left <= float(self.config.force_exit_before_end_seconds):
            if pend is None:
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

        ticks = _build_ticks(self._sec_pm_u, self._sec_pm_d, self._sec_btc_px, self._sec_btc_vol, elapsed, wsec)

        hb_sec = float(self.config.paladin_heartbeat_seconds)
        if now - self._last_hb_ts >= hb_sec:
            self._last_hb_ts = now
            snap = runner.st.snapshot_metrics()
            pend_s = f"{pend[0]}×{pend[1]:.0f}" if pend is not None else "—"
            LOGGER.info(
                "PALADIN v7 hb | %s | el=%ds left=%.0fs | mid_up=%.4f mid_dn=%.4f | "
                "btc=%.2f vol+%.4f/sum_el | spent=$%.2f | U=%.2f@%.3f D=%.2f@%.3f | "
                "pnl_up=$%.2f pnl_dn=$%.2f | pending=%s trades=%d",
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

        paladin_v7_step(runner, elapsed, ticks, params=self._v7_params, try_buy_fn=try_buy_fn)
