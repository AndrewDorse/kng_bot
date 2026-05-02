#!/usr/bin/env python3
"""KNG3 Docker: SHAMAN v1 only (Binance 5m/15m -> optional Polymarket FAK)."""

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

    if config.strategy_mode != "shaman_v1":
        print(
            "KNG3 image runs SHAMAN v1 only. Set BOT_STRATEGY_MODE=shaman_v1 "
            f"(got {config.strategy_mode!r}). PRST1 lives in the KNG4 repo.",
            file=sys.stderr,
        )
        return 2

    from shaman_v1_engine import ShamanV1Engine, configure_shaman_runtime_logging  # noqa: PLC0415

    configure_shaman_runtime_logging()
    locator = GammaMarketLocator(config)
    trader = PolymarketTrader(config)
    ShamanV1Engine(config, locator, trader).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
