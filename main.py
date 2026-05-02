#!/usr/bin/env python3
"""KNG3 Docker: SHAMAN v1 (5m/15m rules) or PRST1 UP tape scalp (Binance fair vs PM UP)."""

from __future__ import annotations

import sys

from config import BotConfig, BotConfigError
from market_locator import GammaMarketLocator
from trader import PolymarketTrader


def main() -> int:
    try:
        config = BotConfig.from_env()
    except BotConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    locator = GammaMarketLocator(config)
    trader = PolymarketTrader(config)

    if config.strategy_mode == "shaman_v1":
        from shaman_v1_engine import ShamanV1Engine, configure_shaman_runtime_logging  # noqa: PLC0415

        configure_shaman_runtime_logging()
        ShamanV1Engine(config, locator, trader).run()
        return 0

    if config.strategy_mode == "prst1_up":
        from prst1_up_engine import Prst1UpEngine, configure_prst1_runtime_logging  # noqa: PLC0415

        configure_prst1_runtime_logging()
        Prst1UpEngine(config, locator, trader).run()
        return 0

    print(
        "KNG3 image supports BOT_STRATEGY_MODE=shaman_v1 or prst1_up "
        f"(got {config.strategy_mode!r}).",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
