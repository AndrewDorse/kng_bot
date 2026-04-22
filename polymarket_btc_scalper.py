#!/usr/bin/env python3
"""Automated BTC 5-minute market-making ladder bot for Polymarket.

Strategy:
- Direction-neutral: buys BOTH UP and DOWN tokens at cheap prices
- 5 ladder levels per side, 5 shares each
- When UP+DOWN pair completes at combined cost < $0.96, profit = $0.04+/pair
- Every second: check fills, reload completed levels, manage end-of-window

Ladder (each side):
  Level 1: $0.44  (5 shares = $2.20)
  Level 2: $0.39  (5 shares = $1.95)
  Level 3: $0.34  (5 shares = $1.70)
  Level 4: $0.29  (5 shares = $1.45)
  Level 5: $0.24  (5 shares = $1.20)

Per side total: $8.50 (5 levels × 5 shares)
Both sides: $17.00 max if ALL levels fill simultaneously
Peak capital (worst case all 10 orders fill): $17.00

But in practice, fills happen sequentially with reloads,
so working capital stays much lower (~$8-12 active at once).

Version: 2026-04-02d (ladder rewrite)
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from functools import wraps
import re

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    OpenOrderParams,
    OrderArgs,
    OrderType,
    TradeParams,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BUY = "BUY"
SELL = "SELL"
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
GAMMA_URL = "https://gamma-api.polymarket.com"

LOGGER = logging.getLogger("polymarket_btc_scalper")

# ---------------------------------------------------------------------------
# Ladder configuration
# ---------------------------------------------------------------------------
LADDER_PRICES = [0.44, 0.39, 0.34, 0.29, 0.24]  # 5 levels
SHARES_PER_LEVEL = 5
PAIR_SUM_TARGET = 0.96  # UP_price + DOWN_price target for pairing

# Capital math:
# Per level: SHARES_PER_LEVEL * price
# Level costs: 5*0.44=2.20, 5*0.39=1.95, 5*0.34=1.70, 5*0.29=1.45, 5*0.24=1.20
# Per side total: $8.50
# Both sides: $17.00
# Profit per matched pair at level: (0.96 - 2*price) * 5 if same level
# But actually: each share pays out $1 if correct side wins
# Matched pair: cost = up_price + down_price per share, payout = $1.00
# Profit per share = $1.00 - (up_price + down_price)
# At 0.44+0.44 = $0.88 cost → $0.12 profit per share × 5 = $0.60 per level pair
# At 0.39+0.39 = $0.78 cost → $0.22 profit per share × 5 = $1.10 per level pair
# etc.

# But fills won't always be at same level. We pair ANY up fill with ANY down fill.
# Minimum profit per share = $1.00 - $0.96 = $0.04 (worst case both at 0.48)
# Our ladder max is 0.44, so minimum profit = $1.00 - $0.88 = $0.12/share


# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------
def _retry(
    max_attempts: int = 3,
    backoff_base: float = 0.5,
    retryable_exceptions: tuple = (requests.RequestException, ConnectionError, TimeoutError),
):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exc = exc
                    if attempt < max_attempts:
                        time.sleep(backoff_base * (2 ** (attempt - 1)))
            raise last_exc
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class BotConfigError(RuntimeError):
    pass


@dataclass(slots=True)
class BotConfig:
    private_key: str
    funder: str
    signature_type: int = 0
    dry_run: bool = True
    poll_interval_seconds: float = 1.0
    request_timeout_seconds: float = 10.0
    log_level: str = "INFO"
    relayer_api_key: str = ""
    relayer_secret: str = ""
    relayer_passphrase: str = ""
    # Window timing
    force_exit_before_end_seconds: int = 15
    # Ladder
    ladder_prices: list = field(default_factory=lambda: [0.44, 0.39, 0.34, 0.29, 0.24])
    shares_per_level: int = 5

    @classmethod
    def from_env(cls) -> "BotConfig":
        private_key = os.getenv("POLY_PRIVATE_KEY")
        funder = os.getenv("POLY_FUNDER")
        if not private_key:
            raise BotConfigError("POLY_PRIVATE_KEY is required.")
        if not funder:
            raise BotConfigError("POLY_FUNDER is required.")
        return cls(
            private_key=private_key,
            funder=funder,
            signature_type=_env_int("POLY_SIGNATURE_TYPE", 1),
            relayer_api_key=os.getenv("RELAYER_API_KEY", ""),
            relayer_secret=os.getenv("RELAYER_SECRET", ""),
            relayer_passphrase=os.getenv("RELAYER_PASSPHRASE", ""),
            dry_run=_env_bool("POLY_DRY_RUN", True),
            poll_interval_seconds=_env_float("BOT_POLL_INTERVAL_SECONDS", 1.0),
            request_timeout_seconds=_env_float("BOT_REQUEST_TIMEOUT_SECONDS", 10.0),
            log_level=os.getenv("BOT_LOG_LEVEL", "INFO").upper(),
            force_exit_before_end_seconds=_env_int("BOT_FORCE_EXIT_BEFORE_END_SECONDS", 15),
            shares_per_level=_env_int("BOT_SHARES_PER_LEVEL", 5),
        )


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class TokenMarket:
    market_id: str
    condition_id: str
    slug: str
    question: str
    token_id: str
    outcome: str
    end_time: datetime
    enable_order_book: bool


@dataclass(slots=True)
class ActiveContract:
    market_id: str
    slug: str
    question: str
    condition_id: str
    end_time: datetime
    up: TokenMarket
    down: TokenMarket
    raw_market: dict[str, Any] = field(repr=False)


@dataclass(slots=True)
class LadderLevel:
    """Tracks one price level on one side."""
    side: str           # "UP" or "DOWN"
    price: float        # limit buy price
    shares: int         # shares per order
    order_id: str | None = None    # live order ID if placed
    filled: bool = False           # True once fill detected
    filled_shares: float = 0.0
    paired: bool = False           # True once matched with opposite side


@dataclass(slots=True)
class MatchedPair:
    """A completed UP+DOWN pair."""
    up_price: float
    down_price: float
    shares: int
    profit_per_share: float
    total_profit: float
    matched_at: float  # timestamp


# ---------------------------------------------------------------------------
# Balance parser
# ---------------------------------------------------------------------------
def _parse_balance_response(response: Any, decimals: int = 6) -> float:
    if isinstance(response, dict):
        raw = response.get("balance")
    else:
        raw = response
    if raw is None or raw == "":
        return 0.0
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val > 1_000_000:
            return val / (10 ** decimals)
        return val
    if isinstance(raw, str):
        cleaned = raw.strip()
        if not cleaned:
            return 0.0
        if cleaned.isdigit():
            return int(cleaned) / (10 ** decimals)
        if "." in cleaned:
            try:
                val = float(cleaned)
                if val > 1_000_000:
                    return val / (10 ** decimals)
                return val
            except ValueError:
                return 0.0
    return 0.0


# ---------------------------------------------------------------------------
# Market locator
# ---------------------------------------------------------------------------
class GammaMarketLocator:
    def __init__(self, config: BotConfig):
        self.config = config
        self.session = requests.Session()
        self._cached_contract: ActiveContract | None = None
        self._cache_expires_at = 0.0

    def get_active_contract(self) -> ActiveContract | None:
        now = time.time()
        if (
            self._cached_contract is not None
            and now < self._cache_expires_at
            and self._cached_contract.end_time > datetime.now(timezone.utc)
        ):
            return self._cached_contract

        contract = self._discover()
        if contract:
            self._cached_contract = contract
            self._cache_expires_at = now + 30.0
        return contract

    @_retry(max_attempts=2, backoff_base=1.0)
    def _discover(self) -> ActiveContract | None:
        now = datetime.now(timezone.utc)
        now_ts = int(now.timestamp())
        window_size = 300
        current_start = (now_ts // window_size) * window_size
        seconds_in = now_ts - current_start
        target_start = current_start if seconds_in < 30 else current_start + window_size

        slug = f"btc-updown-5m-{target_start}"
        resp = self.session.get(
            f"{GAMMA_URL}/markets",
            params={"slug": slug},
            timeout=self.config.request_timeout_seconds,
        )
        resp.raise_for_status()
        markets = resp.json()
        if not markets:
            return None
        return self._parse(markets[0], now)

    def _parse(self, market: dict[str, Any], now: datetime) -> ActiveContract | None:
        if not market.get("active") or market.get("closed") or market.get("archived"):
            return None

        question = str(market.get("question") or "")
        slug = str(market.get("slug") or "")
        end_time = _parse_datetime(market.get("endDate") or market.get("endDateIso"))
        if end_time is None or end_time <= now:
            return None

        outcome_names = _parse_jsonish_list(market.get("outcomes"))
        token_ids = _parse_jsonish_list(market.get("clobTokenIds"))
        if len(outcome_names) != len(token_ids) or len(token_ids) < 2:
            return None

        up_token = down_token = None
        for name, tid in zip(outcome_names, token_ids):
            td = TokenMarket(
                market_id=str(market.get("id") or ""),
                condition_id=str(market.get("conditionId") or ""),
                slug=slug, question=question,
                token_id=str(tid), outcome=str(name),
                end_time=end_time,
                enable_order_book=bool(market.get("enableOrderBook", True)),
            )
            if str(name).strip().upper() == "UP":
                up_token = td
            elif str(name).strip().upper() == "DOWN":
                down_token = td

        if not up_token or not down_token:
            return None

        return ActiveContract(
            market_id=str(market.get("id") or ""),
            slug=slug, question=question,
            condition_id=str(market.get("conditionId") or ""),
            end_time=end_time, up=up_token, down=down_token,
            raw_market=market,
        )


# ---------------------------------------------------------------------------
# Trader (Polymarket API wrapper)
# ---------------------------------------------------------------------------
class PolymarketTrader:
    def __init__(self, config: BotConfig):
        self.config = config
        self.client = ClobClient(
            HOST, chain_id=CHAIN_ID, key=config.private_key,
            signature_type=config.signature_type, funder=config.funder,
        )
        if config.relayer_api_key:
            from py_clob_client.clob_types import ApiCreds
            creds = ApiCreds(
                api_key=config.relayer_api_key,
                api_secret=config.relayer_secret or "",
                api_passphrase=config.relayer_passphrase or "",
            )
        else:
            creds = self.client.create_or_derive_api_creds()
        self.client.set_api_creds(creds)
        LOGGER.info("API credentials set for funder: %s", config.funder)

    def wallet_balance_usdc(self) -> float:
        try:
            resp = self.client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=self.config.signature_type,
                )
            )
            return _parse_balance_response(resp)
        except Exception as e:
            LOGGER.debug("Balance fetch error: %s", e)
            return 0.0

    def get_conditional_balance(self, token_id: str) -> float:
        try:
            try:
                self.client.update_balance_allowance(
                    BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=token_id,
                        signature_type=self.config.signature_type,
                    )
                )
            except Exception:
                pass
            resp = self.client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                    signature_type=self.config.signature_type,
                )
            )
            return _parse_balance_response(resp)
        except Exception:
            return 0.0

    @_retry(max_attempts=2, backoff_base=0.5)
    def place_limit_buy(self, token: TokenMarket, price: float, size: int) -> dict[str, Any]:
        order = OrderArgs(
            token_id=token.token_id,
            price=round(price, 2),
            size=float(size),
            side=BUY,
        )
        signed = self.client.create_order(order)
        return self.client.post_order(signed, OrderType.GTC)

    @_retry(max_attempts=2, backoff_base=0.5)
    def place_limit_sell(self, token: TokenMarket, price: float, size: float) -> dict[str, Any]:
        order = OrderArgs(
            token_id=token.token_id,
            price=round(price, 2),
            size=round(size, 2),
            side=SELL,
        )
        signed = self.client.create_order(order)
        return self.client.post_order(signed, OrderType.GTC)

    def get_open_orders(self) -> list[dict[str, Any]]:
        try:
            return self.client.get_orders(OpenOrderParams())
        except Exception:
            return []

    def cancel_order(self, order_id: str) -> Any:
        return self.client.cancel(order_id)

    def cancel_all_orders(self, open_orders: list[dict[str, Any]]) -> int:
        cancelled = 0
        for order in open_orders:
            oid = str(order.get("id") or order.get("orderID") or "")
            if oid:
                try:
                    self.client.cancel(oid)
                    cancelled += 1
                except Exception as exc:
                    LOGGER.debug("Cancel failed %s: %s", oid, exc)
        return cancelled

    def refresh_conditional_allowance(self, token_id: str) -> None:
        try:
            self.client.update_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                    signature_type=self.config.signature_type,
                )
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Ladder Engine — the core strategy
# ---------------------------------------------------------------------------
class LadderEngine:
    """
    Market-making ladder: places limit buys on both UP and DOWN at multiple
    price levels. When both sides fill, we have a matched pair worth $1.00
    at resolution regardless of outcome.

    Every second:
      1. Check which orders have filled
      2. Match filled UP orders with filled DOWN orders
      3. Reload empty ladder levels
      4. Near window end: cancel everything, let matched pairs resolve
    """

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

        # Ladder state: keyed by (side, price)
        self._ladder: dict[tuple[str, float], LadderLevel] = {}

        # Tracking
        self._pending_order_ids: dict[str, tuple[str, float]] = {}  # order_id → (side, price)
        self._filled_up: list[tuple[float, int, float]] = []    # (price, shares, fill_time)
        self._filled_down: list[tuple[float, int, float]] = []
        self._matched_pairs: list[MatchedPair] = []

        # Session stats
        self._session_profit = 0.0
        self._session_pairs = 0
        self._window_profit = 0.0
        self._window_pairs = 0
        self._window_orders_placed = 0
        self._window_fills = 0

        # Cooldown to avoid hammering API
        self._last_order_time: dict[tuple[str, float], float] = {}
        self._order_cooldown = 3.0  # seconds between placing same level

    def shutdown(self, *_: Any) -> None:
        LOGGER.info("Shutdown requested.")
        self._shutdown = True

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

        LOGGER.info(
            "Starting BTC 5-min ladder bot | dry_run=%s | levels=%s | shares=%d",
            self.config.dry_run,
            self.config.ladder_prices,
            self.config.shares_per_level,
        )

        balance = self.trader.wallet_balance_usdc()
        LOGGER.info("Starting balance: $%.2f", balance)
        self._log_capital_requirements()

        while not self._shutdown:
            started = time.time()
            try:
                self._loop_once()
            except Exception:
                LOGGER.exception("Loop error")
            elapsed = time.time() - started
            time.sleep(max(0.0, self.config.poll_interval_seconds - elapsed))

    def _log_capital_requirements(self) -> None:
        per_side = sum(p * self.config.shares_per_level for p in self.config.ladder_prices)
        both_sides = per_side * 2
        min_profit = (1.0 - 2 * max(self.config.ladder_prices)) * self.config.shares_per_level
        max_profit = (1.0 - 2 * min(self.config.ladder_prices)) * self.config.shares_per_level
        LOGGER.info(
            "[CAPITAL] Per side: $%.2f | Both sides: $%.2f | "
            "Profit per pair: $%.2f-$%.2f | Levels: %d",
            per_side, both_sides, min_profit, max_profit,
            len(self.config.ladder_prices),
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def _loop_once(self) -> None:
        contract = self.locator.get_active_contract()
        if contract is None:
            return

        now = time.time()
        window_start_ts = int(contract.end_time.timestamp()) - 300
        window_end_ts = int(contract.end_time.timestamp())
        seconds_remaining = max(0.0, window_end_ts - now)
        elapsed = max(0.0, now - window_start_ts)

        # Not yet in window
        if now < window_start_ts:
            seconds_to_start = window_start_ts - now
            if seconds_to_start < 10:
                LOGGER.info(
                    "[PRE-WINDOW] %s | starts in %ds",
                    contract.slug, int(seconds_to_start),
                )
            return

        # Window change detection
        if contract.slug != self._current_window_slug:
            self._handle_window_change(contract)

        # Phase 5: End of window — cancel all, let pairs resolve
        if seconds_remaining <= self.config.force_exit_before_end_seconds:
            self._handle_window_end(contract, seconds_remaining)
            return

        # Core loop phases:
        # 1. Fetch open orders from exchange
        open_orders = self._get_contract_orders(contract)

        # 2. Detect fills by comparing ladder state vs live orders
        self._detect_fills(contract, open_orders)

        # 3. Match filled UP with filled DOWN
        new_matches = self._match_pairs()

        # 4. Reload empty ladder levels
        self._reload_ladder(contract, open_orders)

        # 5. Log state
        self._log_heartbeat(contract, open_orders, elapsed, seconds_remaining)

    # ------------------------------------------------------------------
    # Window management
    # ------------------------------------------------------------------
    def _handle_window_change(self, contract: ActiveContract) -> None:
        if self._current_window_slug is not None:
            LOGGER.info(
                "[WINDOW CHANGE] %s → %s | pairs=%d profit=$%.4f",
                self._current_window_slug, contract.slug,
                self._window_pairs, self._window_profit,
            )
            # Cancel any leftover orders from previous window
            try:
                old_orders = self.trader.get_open_orders()
                if old_orders:
                    cancelled = self.trader.cancel_all_orders(old_orders)
                    LOGGER.info("[WINDOW CLEANUP] Cancelled %d old orders", cancelled)
            except Exception as exc:
                LOGGER.warning("[WINDOW CLEANUP ERROR] %s", exc)

        self._current_window_slug = contract.slug

        # Reset window state
        self._ladder.clear()
        self._pending_order_ids.clear()
        self._filled_up.clear()
        self._filled_down.clear()
        self._matched_pairs.clear()
        self._window_profit = 0.0
        self._window_pairs = 0
        self._window_orders_placed = 0
        self._window_fills = 0
        self._last_order_time.clear()

        # Initialize ladder levels
        for price in self.config.ladder_prices:
            for side in ("UP", "DOWN"):
                level = LadderLevel(
                    side=side,
                    price=price,
                    shares=self.config.shares_per_level,
                )
                self._ladder[(side, price)] = level

        balance = self.trader.wallet_balance_usdc()
        LOGGER.info(
            "[NEW WINDOW] %s | ends=%s | balance=$%.2f | levels=%d per side",
            contract.slug,
            contract.end_time.strftime("%H:%M:%S"),
            balance,
            len(self.config.ladder_prices),
        )

        setup_file_logger(contract.slug)

    def _handle_window_end(self, contract: ActiveContract, remaining: float) -> None:
        """Cancel all open orders. Matched pairs will resolve automatically."""
        open_orders = self._get_contract_orders(contract)
        if open_orders:
            cancelled = self.trader.cancel_all_orders(open_orders)
            LOGGER.info(
                "[WINDOW END] %s | remaining=%.0fs | cancelled=%d orders",
                contract.slug, remaining, cancelled,
            )
            self._pending_order_ids.clear()

        # Count unmatched fills (these are at-risk positions)
        unmatched_up = len(self._filled_up)
        unmatched_down = len(self._filled_down)

        LOGGER.info(
            "[WINDOW SUMMARY] %s | matched_pairs=%d | profit=$%.4f | "
            "unmatched_up=%d | unmatched_down=%d | total_fills=%d | orders_placed=%d",
            contract.slug, self._window_pairs, self._window_profit,
            unmatched_up, unmatched_down,
            self._window_fills, self._window_orders_placed,
        )

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------
    def _get_contract_orders(self, contract: ActiveContract) -> list[dict[str, Any]]:
        token_ids = {contract.up.token_id, contract.down.token_id}
        all_orders = self.trader.get_open_orders()
        return [
            o for o in all_orders
            if str(o.get("asset_id") or o.get("assetId") or
                   o.get("token_id") or o.get("tokenId") or "") in token_ids
        ]

    def _detect_fills(self, contract: ActiveContract, open_orders: list[dict[str, Any]]) -> None:
        """Compare pending orders against live orders to detect fills."""
        live_order_ids = set()
        for order in open_orders:
            oid = str(order.get("id") or order.get("orderID") or "")
            if oid:
                live_order_ids.add(oid)

        # Check each pending order — if it's gone from exchange, it filled
        filled_ids = []
        for order_id, (side, price) in self._pending_order_ids.items():
            if order_id not in live_order_ids:
                # Order disappeared → filled (or cancelled, but we only cancel at window end)
                level_key = (side, price)
                level = self._ladder.get(level_key)
                if level and not level.filled:
                    level.filled = True
                    level.filled_shares = float(level.shares)
                    level.order_id = None
                    self._window_fills += 1

                    now = time.time()
                    if side == "UP":
                        self._filled_up.append((price, level.shares, now))
                    else:
                        self._filled_down.append((price, level.shares, now))

                    LOGGER.info(
                        "★ [FILL] %s | side=%s | price=$%.2f | shares=%d | "
                        "pending_up=%d pending_down=%d",
                        contract.slug, side, price, level.shares,
                        len(self._filled_up), len(self._filled_down),
                    )
                filled_ids.append(order_id)

        for oid in filled_ids:
            self._pending_order_ids.pop(oid, None)

    def _match_pairs(self) -> int:
        """Match filled UP orders with filled DOWN orders. FIFO on both sides."""
        matches = 0
        while self._filled_up and self._filled_down:
            up_price, up_shares, up_time = self._filled_up[0]
            down_price, down_shares, down_time = self._filled_down[0]

            # Match the smaller quantity
            match_shares = min(up_shares, down_shares)
            cost_per_share = up_price + down_price
            profit_per_share = 1.0 - cost_per_share
            total_profit = profit_per_share * match_shares

            pair = MatchedPair(
                up_price=up_price,
                down_price=down_price,
                shares=match_shares,
                profit_per_share=profit_per_share,
                total_profit=total_profit,
                matched_at=time.time(),
            )
            self._matched_pairs.append(pair)
            self._window_profit += total_profit
            self._session_profit += total_profit
            self._window_pairs += 1
            self._session_pairs += 1
            matches += 1

            LOGGER.info(
                "★★ [PAIR MATCHED] UP@$%.2f + DOWN@$%.2f = $%.2f cost | "
                "shares=%d | profit=$%.4f ($%.4f/share) | "
                "window_total=$%.4f pairs=%d",
                up_price, down_price, cost_per_share,
                match_shares, total_profit, profit_per_share,
                self._window_profit, self._window_pairs,
            )

            # Consume from filled queues
            remaining_up = up_shares - match_shares
            remaining_down = down_shares - match_shares

            self._filled_up.pop(0)
            self._filled_down.pop(0)

            if remaining_up > 0:
                self._filled_up.insert(0, (up_price, remaining_up, up_time))
            if remaining_down > 0:
                self._filled_down.insert(0, (down_price, remaining_down, down_time))

            # Mark ladder levels as available for reload
            for key, level in self._ladder.items():
                if level.filled and level.paired is False:
                    if key[0] == "UP" and key[1] == up_price:
                        level.paired = True
                    elif key[0] == "DOWN" and key[1] == down_price:
                        level.paired = True

        return matches

    def _reload_ladder(self, contract: ActiveContract, open_orders: list[dict[str, Any]]) -> None:
        """Place orders for any empty ladder levels."""
        now = time.time()

        # Build set of currently-live order prices per side
        live_buy_prices: dict[str, set[float]] = {"UP": set(), "DOWN": set()}
        token_to_side = {
            contract.up.token_id: "UP",
            contract.down.token_id: "DOWN",
        }
        for order in open_orders:
            side_str = str(order.get("side") or "").upper()
            if side_str != BUY:
                continue
            tid = str(order.get("asset_id") or order.get("assetId") or
                      order.get("token_id") or order.get("tokenId") or "")
            mapped = token_to_side.get(tid)
            if mapped:
                order_price = _to_float(order.get("price")) or 0.0
                live_buy_prices[mapped].add(round(order_price, 2))

        for (side, price), level in self._ladder.items():
            # Skip if order already live at this level
            if round(price, 2) in live_buy_prices[side]:
                continue

            # Skip if we already have a pending order tracked for this level
            if level.order_id is not None:
                continue

            # If level was filled and paired, reset it for reuse
            if level.filled and level.paired:
                level.filled = False
                level.paired = False
                level.filled_shares = 0.0
                LOGGER.info(
                    "[LADDER RESET] %s@$%.2f | ready for reload",
                    side, price,
                )

            # If level is filled but not yet paired, don't reload
            # (we're waiting for the other side to fill)
            if level.filled and not level.paired:
                continue

            # Cooldown check
            last_placed = self._last_order_time.get((side, price), 0.0)
            if now - last_placed < self._order_cooldown:
                continue

            # Place the order
            token = contract.up if side == "UP" else contract.down
            self._place_ladder_order(contract, token, side, price, level.shares)

    def _place_ladder_order(
        self,
        contract: ActiveContract,
        token: TokenMarket,
        side: str,
        price: float,
        shares: int,
    ) -> bool:
        now = time.time()

        if self.config.dry_run:
            LOGGER.info(
                "DRY RUN [LADDER BUY] %s | side=%s | price=$%.2f | shares=%d",
                contract.slug, side, price, shares,
            )
            self._last_order_time[(side, price)] = now
            self._window_orders_placed += 1
            return True

        try:
            resp = self.trader.place_limit_buy(token, price, shares)
            order_id = str(resp.get("orderID") or resp.get("id") or "")

            if order_id:
                self._pending_order_ids[order_id] = (side, price)
                level = self._ladder.get((side, price))
                if level:
                    level.order_id = order_id

            self._last_order_time[(side, price)] = now
            self._window_orders_placed += 1

            LOGGER.info(
                "[LADDER BUY] %s | side=%s | price=$%.2f | shares=%d | order=%s",
                contract.slug, side, price, shares, order_id[:12] if order_id else "?",
            )
            return True

        except Exception as exc:
            LOGGER.error(
                "[LADDER BUY FAILED] %s | side=%s | price=$%.2f | %s",
                contract.slug, side, price, exc,
            )
            self._last_order_time[(side, price)] = now  # backoff even on failure
            return False

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def _log_heartbeat(
        self,
        contract: ActiveContract,
        open_orders: list[dict[str, Any]],
        elapsed: float,
        remaining: float,
    ) -> None:
        buy_count = sum(
            1 for o in open_orders
            if str(o.get("side") or "").upper() == BUY
        )
        unmatched_up = len(self._filled_up)
        unmatched_down = len(self._filled_down)
        unmatched_up_shares = sum(s for _, s, _ in self._filled_up)
        unmatched_down_shares = sum(s for _, s, _ in self._filled_down)

        LOGGER.info(
            "[HEARTBEAT] %s | elapsed=%ds | remaining=%ds | "
            "live_orders=%d | fills=%d | pairs=%d | profit=$%.4f | "
            "unmatched: UP=%d(%dsh) DOWN=%d(%dsh) | "
            "session: pairs=%d profit=$%.4f",
            contract.slug,
            int(elapsed), int(remaining),
            buy_count, self._window_fills, self._window_pairs,
            self._window_profit,
            unmatched_up, unmatched_up_shares,
            unmatched_down, unmatched_down_shares,
            self._session_pairs, self._session_profit,
        )


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------
def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", ""))
        except ValueError:
            return None
    return None


def _parse_jsonish_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            return [p.strip() for p in text.split(",") if p.strip()]
    return [value]


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _parse_datetime(int(text))
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise BotConfigError(f"{name} must be a float") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise BotConfigError(f"{name} must be an int") from exc


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
def configure_logging(level: str) -> logging.Logger:
    logger = logging.getLogger("polymarket_btc_scalper")
    logger.setLevel(getattr(logging, level, logging.INFO))
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, level, logging.INFO))
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s - %(message)s"))
    logger.addHandler(console)
    return logger


def setup_file_logger(window_slug: str) -> logging.Logger:
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = logs_dir / f"{ts}_{window_slug}.log"

    logger = logging.getLogger("polymarket_btc_scalper")
    for h in logger.handlers[:]:
        if isinstance(h, logging.FileHandler):
            h.close()
            logger.removeHandler(h)

    fh = logging.FileHandler(filepath)
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s - %(message)s"))
    logger.addHandler(fh)
    LOGGER.info("Log file: %s", filepath.absolute())
    return logger


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    global LOGGER
    try:
        config = BotConfig.from_env()
    except BotConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    LOGGER = configure_logging(config.log_level)
    LOGGER.info(
        "Config: dry_run=%s | levels=%s | shares=%d | force_exit=%ds",
        config.dry_run, config.ladder_prices, config.shares_per_level,
        config.force_exit_before_end_seconds,
    )

    locator = GammaMarketLocator(config)
    trader = PolymarketTrader(config)
    engine = LadderEngine(config, locator, trader)
    engine.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
