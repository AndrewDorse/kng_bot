#!/usr/bin/env python3
"""BTC 15-minute continuous redeem-hold bot entry point."""

from __future__ import annotations

import sys

from btc15_redeem_engine import Btc15RedeemEngine
from config import BotConfig, BotConfigError, LOGGER, configure_logging
from market_locator import GammaMarketLocator
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
        LOGGER.info("strategy_id  = %s", "BTC_VOLUME_SCALP_UP_v1")
    elif config.strategy_mode == "btc_perp15":
        LOGGER.info("strategy_id  = %s", "BTC_PERP15_v1")
    elif config.strategy_mode == "signal_only":
        LOGGER.info("signal_preset= %s", config.signal_preset)
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
