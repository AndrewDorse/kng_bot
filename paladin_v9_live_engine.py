#!/usr/bin/env python3
"""
PALADIN v9 live trading — production path.

**Strategy identity:** v9 is the *product* name for the stack that backtests as
``PALADIN/paladin_v9_second_simulator.py``; the **rule kernel** is still ``paladin_v7_step`` (same as
``paladin_v7_live_engine.PaladinV7LiveEngine``). This module subclasses that engine so logs and metrics
say v9 while reusing the audited path: Binance agg volume/price → per-second ticks → Polymarket mids
(REST or ``MarketWsFeed``) → ``paladin_v7_step`` → ``PolymarketTrader`` (CLOB limit buys, reconcile,
flatten, dry-run).

---------------------------------------------------------------------------
Go-live checklist (read before ``POLY_DRY_RUN=false``)
---------------------------------------------------------------------------

1. **Config / secrets**
   - ``PRIVATE_KEY`` (or project env), ``FUNDER`` / proxy per ``py-clob-client`` expectations.
   - ``BOT_STRATEGY_MODE=paladin_v9`` (or alias below).
   - ``BOT_STRATEGY_BUDGET_CAP_USDC`` — per-window spend cap (second sim default was 400; v7 live default was lower).
   - All ``BOT_PALADIN_V7_*`` knobs still apply (shared kernel); tune in staging first.

2. **Dry run**
   - ``POLY_DRY_RUN=true`` — engine runs, **no** CLOB posts; verify logs and window transitions.

3. **Feeds**
   - ``BOT_BTC_FEED_ENABLED=true`` (default): Binance Vision agg trades for volume spike logic.
   - ``BOT_POLYMARKET_WS_ENABLED=true`` (recommended): CLOB market WS for fresher mids; else REST fallback in engine.

4. **Collateral & allowances**
   - USDC on Polygon for funder; run bot once so ``PolymarketTrader`` can ``set_allowances`` / sync collateral.

5. **Operational**
   - Start near window open; respect ``BOT_STRATEGY_NEW_ORDER_CUTOFF_SECONDS`` / force-exit envs in ``BotConfig``.
   - **Deploy:** production Docker edits belong in the KNG3 mirror repo per workspace deploy rule; sync from here.

6. **Preflight**
   - ``python check_paladin_v9_live_ready.py`` — config, balance read, optional Binance poll (no orders).

This file does **not** duplicate order logic; any fix to live behavior belongs in ``paladin_v7_live_engine.py``
unless v9-specific orchestration is added later.
"""

from __future__ import annotations

from config import LOGGER, BotConfig
from market_locator import GammaMarketLocator
from paladin_v7_live_engine import PaladinV7LiveEngine
from trader import PolymarketTrader


class PaladinV9LiveEngine(PaladinV7LiveEngine):
    """Same behavior as ``PaladinV7LiveEngine``; branding and log prefix for PALADIN v9."""

    def __init__(self, config: BotConfig, locator: GammaMarketLocator, trader: PolymarketTrader) -> None:
        super().__init__(config, locator, trader)
        self._paladin_live_log_prefix = "PALADIN v9"

    def run(self) -> None:
        LOGGER.info("=" * 60)
        LOGGER.info(
            "PALADIN v9 LIVE | kernel=paladin_v7_step | stack=paladin_v7_live_engine "
            "| backtest=PALADIN/paladin_v9_second_simulator.py"
        )
        LOGGER.info("=" * 60)
        super().run()
