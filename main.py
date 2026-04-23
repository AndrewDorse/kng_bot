#!/usr/bin/env python3
"""BTC 15-minute bot entry point. Default strategy: PALADIN v7 Binance-spike live (see paladin_v7_live_engine.py)."""

from __future__ import annotations

import sys

from btc15_redeem_engine import Btc15RedeemEngine
from config import BotConfig, BotConfigError, LOGGER, configure_logging
from market_locator import GammaMarketLocator
from paladin_live_engine import PaladinLiveEngine
from paladin_v7_live_engine import PaladinV7LiveEngine
from signal_analyzer import SignalAnalyzer
from trader import PolymarketTrader


def main() -> int:
    try:
        config = BotConfig.from_env()
    except BotConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    configure_logging(config.log_level)

    LOGGER.info("=" * 60)
    LOGGER.info("BTC 15-MIN CONTINUOUS BOT")
    LOGGER.info("=" * 60)
    LOGGER.info("version      = %s", config.bot_version)
    LOGGER.info("dry_run      = %s", config.dry_run)
    LOGGER.info("strategy_mode= %s", config.strategy_mode)
    if config.strategy_mode == "wd":
        LOGGER.info("strategy_id  = %s", "WD_wallet_strict_v1")
    elif config.strategy_mode in {"volume_t10", "volume_t10_hybrid"}:
        LOGGER.info(
            "strategy_id  = %s",
            "BTC_VOLUME_T10_hybrid_v2" if config.strategy_mode == "volume_t10_hybrid" else "BTC_VOLUME_T10_dual_v1",
        )
    elif config.strategy_mode == "volume_scalp_up":
        LOGGER.info("strategy_id  = %s", "BTC_VOLUME_SCALP_UP_v2")
    elif config.strategy_mode == "btc_perp15":
        LOGGER.info("strategy_id  = %s", "BTC_PERP15_UP_LADDER_v3")
    elif config.strategy_mode == "signal_only":
        LOGGER.info("signal_preset= %s", config.signal_preset)
    elif config.strategy_mode == "paladin_v7":
        LOGGER.info("strategy_id  = %s", "PALADIN_v7_binance_spike_live")
        LOGGER.info("poly_ws      = %s (%s)", config.polymarket_ws_enabled, config.polymarket_ws_url)
        LOGGER.info(
            "paladin_v7   = budget=$%.2f clip=%.1f max/side=%.0f max_orders=%d vol_ratio=%.2f lookback=%ds btc_move>=%.2f",
            float(config.strategy_budget_cap_usdc),
            float(config.paladin_v7_clip_shares),
            float(config.paladin_v7_max_shares_per_side),
            int(config.paladin_v7_max_orders),
            float(config.paladin_v7_volume_spike_ratio),
            int(config.paladin_v7_volume_lookback_sec),
            float(config.paladin_v7_btc_abs_move_min_usd),
        )
        LOGGER.info(
            "paladin_v7 our_pair_cap<=%.4f (hedge held+opp / refill VWAP sum; not raw pm_u+pm_d) | cheap_min_delay=%.1fs | hedge_timeout=%.1fs | slip=%.4f",
            float(config.paladin_v7_cheap_pair_avg_sum_nonforced_max),
            float(config.paladin_v7_cheap_hedge_min_delay_sec),
            float(config.paladin_v7_hedge_timeout_seconds),
            float(config.paladin_v7_cheap_hedge_slip_buffer),
        )
    elif config.strategy_mode == "paladin":
        LOGGER.info("strategy_id  = %s", "PALADIN_pair_live_v4")
        LOGGER.info("poly_ws      = %s (%s)", config.polymarket_ws_enabled, config.polymarket_ws_url)
        LOGGER.info("fak_confirm  = %s (GET /order after FAK when needed)", config.polymarket_fak_confirm_get_order)
        pforce = config.paladin_pair_sum_max_on_forced_hedge
        LOGGER.info(
            "paladin_pair = pair_sum_max=%.3f (symmetric / tighten) | hedge_timer_2nd_sum<=%s | roi>=%.3f on 2nd | "
            "1st_leg_side<=%.3f | stagger_2nd_live_mid_gate=%s (false=held post_fill avg cap)",
            config.paladin_pair_sum_max,
            f"{pforce:.3f}" if pforce is not None else "strict",
            config.paladin_target_min_roi,
            config.paladin_first_leg_max_px,
            config.paladin_stagger_second_leg_require_live_mid_pair_sum,
        )
        LOGGER.info(
            "paladin_entry= stagger=%s hedge_force_s=%s max_sh/side=%s clip_cap=%.0f cooldown=%.2fs",
            config.paladin_stagger_pair,
            config.paladin_stagger_hedge_force_after_seconds,
            config.paladin_max_shares_per_side,
            config.paladin_dynamic_clip_cap,
            config.paladin_cooldown_seconds,
        )
        LOGGER.info(
            "paladin_disc  = tighten_pf=%.4f sum_floor=%.2f imb_bypass_sh=%s relax_after_force_s=%s",
            config.paladin_pair_sum_tighten_per_fill,
            config.paladin_pair_sum_min_floor,
            config.paladin_pending_hedge_bypass_imbalance_shares,
            config.paladin_discipline_relax_after_forced_sec,
        )
        LOGGER.info(
            "paladin_v4    = second_leg_book_eps=%.4f max_blended_avg_sum=%s "
            "stagger_win_first=%s sym_fallback_balanced=%s sym_skip_first_blend_cap=%s",
            config.paladin_second_leg_book_improve_eps,
            config.paladin_max_blended_pair_avg_sum,
            config.paladin_stagger_winning_side_first_when_position,
            config.paladin_stagger_symmetric_fallback_when_balanced,
            config.paladin_stagger_symmetric_fallback_skip_first_leg_blend_cap,
        )
        tr = config.paladin_entry_trailing_min_low_seconds
        LOGGER.info(
            "paladin_ladder= min_gap_between_pairs=%s trail_low_sec=%s entry_slip=%.3f "
            "alternate_first_when_balanced=%s",
            config.paladin_min_elapsed_between_pair_starts,
            tr if tr is not None else "off",
            float(config.paladin_entry_trailing_low_slippage),
            config.paladin_stagger_alternate_first_leg_when_balanced,
        )
    LOGGER.info("market       = %s", config.market_slug_prefix)
    LOGGER.info("shares/order = %d", config.shares_per_level)
    LOGGER.info("budget cap   = $%.2f", config.strategy_budget_cap_usdc)
    LOGGER.info("reserve      = $%.2f", config.strategy_wallet_reserve_usdc)
    LOGGER.info("entry delay  = %ds", config.strategy_entry_delay_seconds)
    LOGGER.info("new cutoff   = %ds before end", config.strategy_new_order_cutoff_seconds)
    LOGGER.info("force_exit   = %ds before end", config.force_exit_before_end_seconds)
    LOGGER.info("poll         = %.1fs", config.poll_interval_seconds)
    LOGGER.info("continuous   = %s", not config.trade_one_window)
    LOGGER.info("=" * 60)

    locator = GammaMarketLocator(config)
    trader = PolymarketTrader(config)

    if config.strategy_mode == "paladin_v7":
        if config.dry_run:
            LOGGER.warning("PALADIN v7: POLY_DRY_RUN=true — paper only (no CLOB orders).")
        else:
            LOGGER.warning("PALADIN v7: LIVE — FAK buys will execute on Polymarket. Ctrl+C stops the loop.")
        PaladinV7LiveEngine(config, locator, trader).run()
        return 0

    if config.strategy_mode == "paladin":
        if config.dry_run:
            LOGGER.warning("PALADIN: POLY_DRY_RUN=true — paper only (no CLOB orders).")
        else:
            LOGGER.warning("PALADIN: LIVE — FAK buys will execute on Polymarket. Ctrl+C stops the loop.")
        PaladinLiveEngine(config, locator, trader).run()
        return 0

    engine = Btc15RedeemEngine(config, locator, trader)

    signals: SignalAnalyzer | None = None
    if config.strategy_mode == "signal_only":
        signals = SignalAnalyzer(signal_preset=config.signal_preset)
        signals.attach(engine)
        LOGGER.info("Signal analyzer attached (LIVE placing orders on signals)")
    else:
        LOGGER.info("Signal analyzer disabled for strategy_mode=%s", config.strategy_mode)

    engine.run()
    if signals is not None:
        signals.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
