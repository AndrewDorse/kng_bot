#!/usr/bin/env python3
"""Automated BTC 5-minute momentum bot for Polymarket.

This bot:
- polls Binance BTC/USDT spot price every second,
- discovers the currently-active Polymarket BTC 5 minute UP/DOWN market,
- opens a market order when rapid BTC movement detected,
- recomputes open positions from the Polymarket trade API every loop,
- closes positions with market sells at the configured profit target,
- force-closes positions on timeout or market rollover.

Live trading is disabled by default. Set POLY_DRY_RUN=false to place orders.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import sys
import time
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Deque, Iterable
import re

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams, MarketOrderArgs, OpenOrderParams, OrderArgs, OrderType, TradeParams

BUY = "BUY"
SELL = "SELL"
TP_SIZE_REDUCTION_SHARES = 0.2

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
GAMMA_URL = "https://gamma-api.polymarket.com"
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

LOGGER = logging.getLogger("polymarket_btc_scalper")

try:
    from strategy_early_bird import EarlyBirdStrategy
except Exception as exc:
    LOGGER_IMPORT = logging.getLogger("polymarket_btc_scalper")
    LOGGER_IMPORT.warning("Failed to import EarlyBirdStrategy: %s", exc)
    class EarlyBirdStrategy:
        def evaluate(self, *args, **kwargs):
            return None

try:
    from strategy_dominance import DominanceStrategy
except Exception as exc:
    LOGGER_IMPORT = logging.getLogger("polymarket_btc_scalper")
    LOGGER_IMPORT.warning("Failed to import DominanceStrategy: %s", exc)
    class DominanceStrategy:
        def evaluate(self, *args, **kwargs):
            return None


class BotConfigError(RuntimeError):
    pass


@dataclass(slots=True)
class BotConfig:
    private_key: str
    funder: str
    signature_type: int = 0
    market_lookup_limit: int = 200
    poll_interval_seconds: float = 1.0
    market_refresh_seconds: float = 5.0
    momentum_window_seconds: int = 10
    momentum_threshold_usd: float = 10.0
    min_seconds_remaining_to_enter: int = 10
    max_hold_seconds: int = 12
    early_bird_close_seconds: int = 25
    close_grace_seconds: int = 8
    buy_same_price_cooldown_seconds: int = 5
    enable_early_bird_strategy: bool = True
    enable_dominance_strategy: bool = False
    profit_target_pct: float = 10.0
    trade_balance_fraction: float = 0.10
    min_trade_usd: float = 1.0
    min_entry_price: float = 0.40
    max_entry_price: float = 0.52
    dry_run: bool = True
    request_timeout_seconds: float = 10.0
    log_level: str = "INFO"
    # Relayer API credentials (optional, for pre-registered API keys)
    relayer_api_key: str = ""
    relayer_secret: str = ""
    relayer_passphrase: str = ""

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
            signature_type=_env_int("POLY_SIGNATURE_TYPE", 1),  # Default to 1 for Safe/Proxy wallets
            relayer_api_key=os.getenv("RELAYER_API_KEY", ""),
            relayer_secret=os.getenv("RELAYER_SECRET", ""),
            relayer_passphrase=os.getenv("RELAYER_PASSPHRASE", ""),
            market_lookup_limit=_env_int("POLY_MARKET_LOOKUP_LIMIT", 200),
            poll_interval_seconds=_env_float("BOT_POLL_INTERVAL_SECONDS", 1.0),
            market_refresh_seconds=_env_float("BOT_MARKET_REFRESH_SECONDS", 60.0),  # Cache market for 60s
            momentum_window_seconds=_env_int("BOT_MOMENTUM_WINDOW_SECONDS", 10),
            momentum_threshold_usd=_env_float("BOT_MOMENTUM_THRESHOLD_USD", 10.0),
            min_seconds_remaining_to_enter=_env_int("BOT_ENTRY_MIN_SECONDS_REMAINING", 10),
            max_hold_seconds=_env_int("BOT_MAX_HOLD_SECONDS", 12),
            early_bird_close_seconds=_env_int("BOT_EARLY_BIRD_CLOSE_SECONDS", 25),
            close_grace_seconds=_env_int("BOT_CLOSE_GRACE_SECONDS", 8),
            buy_same_price_cooldown_seconds=_env_int("BOT_BUY_SAME_PRICE_COOLDOWN_SECONDS", 5),
            enable_early_bird_strategy=_env_bool("BOT_ENABLE_EARLY_BIRD_STRATEGY", True),
            enable_dominance_strategy=_env_bool("BOT_ENABLE_DOMINANCE_STRATEGY", False),
            profit_target_pct=_env_float("BOT_PROFIT_TARGET_PCT", 8.0),
            trade_balance_fraction=_env_float("BOT_TRADE_BALANCE_FRACTION", 0.10),
            min_trade_usd=_env_float("BOT_MIN_TRADE_USD", 1.0),
            min_entry_price=_env_float("BOT_MIN_ENTRY_PRICE", 0.40),
            max_entry_price=_env_float("BOT_MAX_ENTRY_PRICE", 0.52),
            dry_run=_env_bool("POLY_DRY_RUN", True),
            request_timeout_seconds=_env_float("BOT_REQUEST_TIMEOUT_SECONDS", 10.0),
            log_level=os.getenv("BOT_LOG_LEVEL", "INFO").upper(),
        )


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

    def token_for_signal(self, signal_direction: str) -> TokenMarket:
        return self.up if signal_direction == "UP" else self.down


@dataclass(slots=True)
class PricePoint:
    ts: float
    price: float


@dataclass(slots=True)
class PositionSnapshot:
    token_id: str
    shares: float
    average_entry_price: float
    opened_at: datetime
    current_bid: float
    pnl_pct: float
    side_label: str
    market: ActiveContract


class BinancePriceFeed:
    def __init__(self, config: BotConfig):
        self.config = config
        self.history: Deque[PricePoint] = deque()
        self.session = requests.Session()
        self._window_open_cache: dict[int, float] = {}

    def poll(self) -> float:
        response = self.session.get(
            BINANCE_TICKER_URL,
            params={"symbol": "BTCUSDT"},
            timeout=self.config.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        price = float(payload["price"])
        now = time.time()
        self.history.append(PricePoint(ts=now, price=price))
        cutoff = now - max(self.config.momentum_window_seconds * 3, 60)
        while self.history and self.history[0].ts < cutoff:
            self.history.popleft()
        return price

    def get_5m_open_price(self, window_start_ts: int) -> float:
        cached = self._window_open_cache.get(window_start_ts)
        if cached is not None:
            return cached

        response = self.session.get(
            BINANCE_KLINES_URL,
            params={
                "symbol": "BTCUSDT",
                "interval": "5m",
                "startTime": window_start_ts * 1000,
                "endTime": (window_start_ts + 300) * 1000,
                "limit": 1,
            },
            timeout=self.config.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload:
            raise RuntimeError(f"No Binance 5m candle returned for window start {window_start_ts}")

        candle = payload[0]
        open_time_ms = int(candle[0])
        open_price = float(candle[1])
        expected_open_time_ms = window_start_ts * 1000
        if open_time_ms != expected_open_time_ms:
            LOGGER.warning(
                "Binance returned candle open %s instead of expected %s for window start %s",
                open_time_ms,
                expected_open_time_ms,
                window_start_ts,
            )
        self._window_open_cache[window_start_ts] = open_price
        return open_price

    def momentum(self) -> float | None:
        """Legacy momentum: current price - price 10s ago."""
        if len(self.history) < 2:
            return None
        current = self.history[-1]
        target_ts = current.ts - self.config.momentum_window_seconds
        reference: PricePoint | None = None
        for point in self.history:
            if point.ts <= target_ts:
                reference = point
            else:
                break
        if reference is None:
            return None
        return current.price - reference.price

    def rapid_movement(self) -> str | None:
        """Check if any of the last 3 one-second candles moved > $2.
        
        Returns:
            "UP" if any 1s candle was +$2 or more
            "DOWN" if any 1s candle was -$2 or more
            None if no rapid movement detected
        """
        if len(self.history) < 4:  # Need at least 4 points for 3 candles
            return None
        
        # Get last 4 price points (3 one-second candles)
        recent = list(self.history)[-4:]
        
        # Check each 1-second candle
        for i in range(1, 4):
            change = recent[i].price - recent[i-1].price
            if change >= 2.0:
                return "UP"
            if change <= -2.0:
                return "DOWN"
        
        return None


class GammaMarketLocator:
    def __init__(self, config: BotConfig):
        self.config = config
        self.session = requests.Session()
        self._cached_contract: ActiveContract | None = None
        self._cache_expires_at = 0.0

    def _compute_target_window_start(self, now_ts: int) -> int:
        """Select target 5-minute window.

        Startup:
          - if current window age < 30s => current window
          - otherwise => next window

        After a window is selected, keep it active until that window ends.
        """
        window_size = 300
        current_window_start = (now_ts // window_size) * window_size

        if self._cached_contract is not None:
            cached_start = int(self._cached_contract.end_time.timestamp()) - window_size
            cached_end = int(self._cached_contract.end_time.timestamp())
            if cached_start <= now_ts < cached_end:
                return cached_start
            if now_ts < cached_start:
                return cached_start

        seconds_into_current = now_ts - current_window_start
        return current_window_start if seconds_into_current < 30 else current_window_start + window_size

    def _fetch_contract_for_window_start(self, target_window_start: int) -> ActiveContract | None:
        now = datetime.now(timezone.utc)
        slug = f"btc-updown-5m-{target_window_start}"
        try:
            response = self.session.get(
                f"{GAMMA_URL}/markets",
                params={"slug": slug},
                timeout=self.config.request_timeout_seconds,
            )
            response.raise_for_status()
            markets = response.json()
            if markets and len(markets) > 0:
                return self._parse_updown_market(markets[0], now)
        except Exception as exc:
            LOGGER.error("Failed to fetch market %s: %s", slug, exc)
        return None

    def get_current_contract(self) -> ActiveContract | None:
        now_ts = int(time.time())
        window_size = 300
        current_window_start = (now_ts // window_size) * window_size
        return self._fetch_contract_for_window_start(current_window_start)

    def get_active_contract(self, force_refresh: bool = False) -> ActiveContract | None:
        now = time.time()
        now_ts = int(now)
        target_window_start = self._compute_target_window_start(now_ts)

        expected_slug = f"btc-updown-5m-{target_window_start}"

        if (
            not force_refresh
            and self._cached_contract is not None
            and now < self._cache_expires_at
            and self._cached_contract.end_time > datetime.now(timezone.utc)
            and self._cached_contract.slug == expected_slug
        ):
            return self._cached_contract

        contract = self._discover_active_contract()
        if contract is None:
            return None
        self._cached_contract = contract
        self._cache_expires_at = now + self.config.market_refresh_seconds
        return contract

    def _discover_active_contract(self) -> ActiveContract | None:
        """Discover the target BTC 5-minute Up/Down market for early-bird trading."""
        now = datetime.now(timezone.utc)
        now_ts = int(now.timestamp())
        window_size = 300
        target_window_start = self._compute_target_window_start(now_ts)

        seconds_to_start = target_window_start - now_ts
        mode = "pre-window" if seconds_to_start > 0 else "current-target"
        target_window_end = target_window_start + window_size
        seconds_into_target = max(0, now_ts - target_window_start)

        slug = f"btc-updown-5m-{target_window_start}"
        expected_end = datetime.fromtimestamp(target_window_end, tz=timezone.utc)
        if mode == "pre-window":
            LOGGER.info("[WINDOW] Looking for target window: %s (starts %s, ends %s, pre-window %ds before start)",
                        slug,
                        datetime.fromtimestamp(target_window_start, tz=timezone.utc).strftime("%H:%M:%S"),
                        expected_end.strftime("%H:%M:%S"),
                        seconds_to_start)
        else:
            LOGGER.info("[WINDOW] Looking for current window: %s (starts %s, ends %s, we are %ds into window)",
                        slug,
                        datetime.fromtimestamp(target_window_start, tz=timezone.utc).strftime("%H:%M:%S"),
                        expected_end.strftime("%H:%M:%S"),
                        seconds_into_target)
        try:
            response = self.session.get(
                f"{GAMMA_URL}/markets",
                params={"slug": slug},
                timeout=self.config.request_timeout_seconds,
            )
            response.raise_for_status()
            markets = response.json()
            if markets and len(markets) > 0:
                market = markets[0]
                contract = self._parse_updown_market(market, now)
                if contract is not None:
                    time_diff = abs((contract.end_time - expected_end).total_seconds())
                    if time_diff < 60:
                        if mode == "pre-window":
                            LOGGER.info("[WINDOW] Preloading next window: %s | Starts: %s | In: %ds",
                                        slug,
                                        datetime.fromtimestamp(target_window_start, tz=timezone.utc).strftime("%H:%M:%S"),
                                        seconds_to_start)
                        else:
                            LOGGER.info("[WINDOW] Trading in current window: %s | Ends: %s | Into window: %ds",
                                        slug, contract.end_time.strftime("%H:%M:%S"), seconds_into_target)
                        return contract
                    else:
                        LOGGER.error("Contract end time mismatch: expected %s, got %s (diff: %ds)",
                                     expected_end, contract.end_time, time_diff)
        except Exception as exc:
            LOGGER.error("Failed to fetch current window market %s: %s", slug, exc)
        raise RuntimeError(f"Cannot find current active BTC 5-min window: {slug}")

    def _parse_updown_market(self, market: dict[str, Any], now: datetime) -> ActiveContract | None:
        """Parse a BTC Up/Down market from Gamma API response."""
        if not market.get("active") or market.get("closed") or market.get("archived"):
            return None
        
        question = str(market.get("question") or "")
        slug = str(market.get("slug") or "")
        
        # Check if this is a BTC 5-minute up/down market
        text = (question + " " + slug).lower()
        if "btc" not in text and "bitcoin" not in text:
            return None
        if "up" not in text or "down" not in text:
            return None
        if "5" not in text and "five" not in text:
            return None  # Not a 5-minute market
        
        end_time = _parse_datetime(market.get("endDate") or market.get("endDateIso"))
        if end_time is None or end_time <= now:
            return None
        
        # Parse outcomes and token IDs
        outcome_names = _parse_jsonish_list(market.get("outcomes"))
        token_ids = _parse_jsonish_list(market.get("clobTokenIds"))
        
        if len(outcome_names) != len(token_ids) or len(token_ids) < 2:
            LOGGER.warning("Market %s has mismatched outcomes/tokens", slug)
            return None
        
        # Find Up and Down tokens
        up_token = None
        down_token = None
        
        for name, token_id in zip(outcome_names, token_ids):
            name_upper = str(name).strip().upper()
            token_data = TokenMarket(
                market_id=str(market.get("id") or ""),
                condition_id=str(market.get("conditionId") or ""),
                slug=slug,
                question=question,
                token_id=str(token_id),
                outcome=str(name),
                end_time=end_time,
                enable_order_book=bool(market.get("enableOrderBook", True)),
            )
            if name_upper == "UP":
                up_token = token_data
            elif name_upper == "DOWN":
                down_token = token_data
        
        if up_token is None or down_token is None:
            LOGGER.warning("Market %s missing Up/Down tokens. Found: %s", slug, outcome_names)
            return None
        
        return ActiveContract(
            market_id=str(market.get("id") or ""),
            slug=slug,
            question=question,
            condition_id=str(market.get("conditionId") or ""),
            end_time=end_time,
            up=up_token,
            down=down_token,
            raw_market=market,
        )




class PolymarketTrader:
    def __init__(self, config: BotConfig):
        self.config = config
        try:
            self.client = ClobClient(
                HOST,
                chain_id=CHAIN_ID,
                key=config.private_key,
                signature_type=config.signature_type,
                funder=config.funder,
            )
            
            # Use relayer API key if provided, otherwise auto-generate
            if config.relayer_api_key:
                from py_clob_client.clob_types import ApiCreds
                api_creds = ApiCreds(
                    api_key=config.relayer_api_key,
                    api_secret=config.relayer_secret or "",
                    api_passphrase=config.relayer_passphrase or "",
                )
                LOGGER.info("Using provided relayer API key: %s...", config.relayer_api_key[:20])
            else:
                api_creds = self.client.create_or_derive_api_creds()
                LOGGER.info("Using auto-generated API credentials")
            
            self.client.set_api_creds(api_creds)
            LOGGER.info("API credentials set successfully for funder: %s", config.funder)
        except Exception as e:
            LOGGER.error("Failed to initialize Polymarket client: %s", e)
            raise

    def wallet_balance_usdc(self) -> float:
        """Get balance - Polymarket auto-converts USDT/USDC."""
        return self.get_total_balance()

    def get_total_balance(self) -> float:
        """Get total available balance (USDC + USDT - Polymarket auto-converts)."""
        total = 0.0
        
        # Check primary collateral endpoint (usually USDC)
        try:
            response = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=self.config.signature_type)
            )
            LOGGER.debug("Collateral balance response: %s", response)
            # Balance is returned as string in wei (6 decimals for USDC)
            if isinstance(response, dict) and "balance" in response:
                balance_wei = response["balance"]
                if isinstance(balance_wei, str) and balance_wei.isdigit():
                    bal = int(balance_wei) / 1e6
                else:
                    bal = _to_float(balance_wei)
                    if bal and bal > 1000000:  # If it looks like wei, convert it
                        bal = bal / 1e6
                if bal:
                    total += bal
                    LOGGER.debug("Collateral balance: $%.4f", bal)
        except Exception as e:
            LOGGER.debug("Error fetching collateral: %s", e)
        
        return total if total > 0 else 0.0

    def get_all_balances(self) -> dict[str, float]:
        """Get all available balances for display."""
        balances = {}
        total = 0.0
        
        # Check collateral endpoint
        try:
            response = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=self.config.signature_type)
            )
            # Balance is returned as string in wei (6 decimals for USDC)
            if isinstance(response, dict) and "balance" in response:
                balance_wei = response["balance"]
                if isinstance(balance_wei, str) and balance_wei.isdigit():
                    bal = int(balance_wei) / 1e6
                else:
                    bal = _to_float(balance_wei)
                    if bal and bal > 1000000:  # If it looks like wei, convert it
                        bal = bal / 1e6
                if bal and bal > 0:
                    balances["Balance"] = bal
                    total = bal
        except:
            pass
        
        return balances, total

    def best_buy_price(self, token_id: str) -> float:
        payload = self.client.get_price(token_id, BUY)
        price = _extract_first_float(payload, ["price"])
        if price is None:
            raise RuntimeError(f"Unable to parse buy price for token {token_id}: {payload}")
        return price

    def get_token_price(self, token_id: str) -> float:
        """Get current market price for a token from Polymarket API."""
        try:
            # Try to get mid price from order book (best bid + best ask)/2
            book = self.client.get_order_book(token_id)
            if book:
                best_bid = max((float(b.price) for b in book.bids if b.price), default=0.0)
                best_ask = min((float(a.price) for a in book.asks if a.price), default=0.0)
                if best_bid > 0 and best_ask > 0:
                    return (best_bid + best_ask) / 2
                elif best_bid > 0:
                    return best_bid
                elif best_ask > 0:
                    return best_ask
            
            # Fallback to last trade price
            payload = self.client.get_price(token_id, SELL)
            price = _extract_first_float(payload, ["price"])
            if price is not None:
                return price
        except Exception as e:
            LOGGER.debug("Could not get price for %s: %s", token_id, e)
        return 0.0

    def get_order_book_top(self, token_id: str) -> tuple[float, float]:
        book = self.client.get_order_book(token_id)
        best_bid = max((float(b.price) for b in getattr(book, "bids", []) if getattr(b, "price", None)), default=0.0)
        best_ask = min((float(a.price) for a in getattr(book, "asks", []) if getattr(a, "price", None)), default=0.0)
        return best_bid, best_ask

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
            response = self.client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                    signature_type=self.config.signature_type,
                )
            )
            if isinstance(response, dict) and "balance" in response:
                bal = response["balance"]
                if isinstance(bal, str) and bal.isdigit():
                    return int(bal) / 1e6
                parsed = _to_float(bal)
                return parsed or 0.0
        except Exception as exc:
            LOGGER.debug("Could not get conditional balance for %s: %s", token_id, exc)
        return 0.0

    def refresh_conditional_allowance(self, token_id: str) -> None:
        try:
            self.client.update_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                    signature_type=self.config.signature_type,
                )
            )
        except Exception as exc:
            LOGGER.debug("Conditional allowance refresh failed for %s: %s", token_id, exc)

    def place_limit_buy(self, token: TokenMarket, price: float, size: float) -> dict[str, Any]:
        order = OrderArgs(token_id=token.token_id, price=round(price, 3), size=round(size, 4), side=BUY)
        signed = self.client.create_order(order)
        return self.client.post_order(signed, OrderType.GTC)

    def place_limit_sell(self, token: TokenMarket, price: float, size: float) -> dict[str, Any]:
        order = OrderArgs(token_id=token.token_id, price=round(price, 3), size=round(size, 4), side=SELL)
        signed = self.client.create_order(order)
        return self.client.post_order(signed, OrderType.GTC)

    def open_orders(self) -> list[dict[str, Any]]:
        try:
            return self.client.get_orders(OpenOrderParams())
        except Exception as exc:
            LOGGER.debug("Could not fetch open orders: %s", exc)
            return []

    def cancel_order(self, order_id: str) -> Any:
        return self.client.cancel(order_id)

    def open_market_buy(self, token: TokenMarket, usd_amount: float) -> dict[str, Any]:
        try:
            order_args = MarketOrderArgs(
                token_id=token.token_id,
                amount=usd_amount,
                side=BUY,
                order_type=OrderType.FOK,
            )
            LOGGER.debug("Creating market order: token=%s, amount=%s, side=%s", token.token_id, usd_amount, BUY)
            signed = self.client.create_market_order(order_args)
            LOGGER.debug("Order signed successfully, posting to API...")
            response = self.client.post_order(signed, OrderType.FOK)
            LOGGER.info("Order placed successfully: %s", response)
            return response
        except Exception as e:
            LOGGER.error("Failed to place buy order: %s", e)
            raise

    def close_market_sell(self, token: TokenMarket, shares: float) -> dict[str, Any]:
        order_args = MarketOrderArgs(
            token_id=token.token_id,
            amount=shares,
            side=SELL,
            order_type=OrderType.FOK,
        )
        signed = self.client.create_market_order(order_args)
        return self.client.post_order(signed, OrderType.FOK)

    def trades_for_market(self, contract: ActiveContract) -> list[dict[str, Any]]:
        trades = self.client.get_trades(TradeParams(market=contract.condition_id))
        return [trade for trade in trades if str(trade.get("asset_id") or trade.get("assetId") or trade.get("token_id") or "") in {contract.up.token_id, contract.down.token_id}]


class StrategyEngine:
    def __init__(self, config: BotConfig, locator: GammaMarketLocator, trader: PolymarketTrader, feed: BinancePriceFeed):
        self.config = config
        self.locator = locator
        self.trader = trader
        self.feed = feed
        self._shutdown = False
        self._signal_count = 0   # Track signals logged
        self._max_signals = 1000 # No limit on signals for live trading
        self._current_window_slug: str | None = None
        self._last_position_log_time: float = 0.0  # For periodic position logging
        self._local_position_cache: dict[str, dict[str, Any]] = {}  # Cache positions we open locally
        self._filled_positions: set[str] = set()  # token_ids confirmed as filled from API
        self._deals_this_window: int = 0  # Count deals in current window
        self._max_deals_per_window: int = 1
        self._max_parallel_deals: int = 1
        self._min_shares_per_order: int = 5
        self._order_roles: dict[str, dict[str, Any]] = {}
        self._tp_retry_after: dict[str, float] = {}
        self._window_beat_prices: dict[str, float] = {}
        self._last_entry_order_ts: float = 0.0
        self._panic_window_slug: str | None = None
        self._last_beat_fetch_attempt: dict[str, float] = {}
        self._page_session = requests.Session()
        self._window_buy_shares: int = 6
        self._window_tp_shares: float = 5.98
        self._window_entry_price_floor: dict[str, float] = {}
        self._recent_buy_requests: dict[tuple[str, float], float] = {}
        self._token_tp_price_override: dict[str, float] = {}
        self._entry_request_pending: bool = False
        self._entry_signal_started_at: float | None = None
        self._prev_signal_price: float | None = None
        self._local_pending_entry_count: int = 0
        self._awaiting_entry_fill_token_id: str | None = None
        self._awaiting_entry_order_id: str | None = None
        self._fullset_sent_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._fullset_window_stopped: set[str] = set()
        self._fullset_last_reset_check: dict[str, float] = {}
        self._fullset_side_cooldown_until: dict[tuple[str, str], float] = {}
        self._fullset_order_first_seen_at: dict[tuple[str, str], float] = {}
        self._fullset_imbalance_lock: dict[str, tuple[int, int, str]] = {}
        self._fullset_low_avg_sum_first_seen: dict[str, float] = {}
        self._tick_second: int = 0
        self._tick_buy_count: int = 0
        self._last_network_error_log_ts: float = 0.0
        self.early_bird_strategy = EarlyBirdStrategy()
        self.dominance_strategy = DominanceStrategy()
        self.enable_early_bird_strategy = True
        self.enable_dominance_strategy = bool(config.enable_dominance_strategy)

    def _recompute_window_sizes(self) -> None:
        self._window_buy_shares = 5
        self._window_tp_shares = round(self._window_buy_shares * 0.98, 2)
        LOGGER.info("[WINDOW SIZE FIXED] Buy Shares: %d | TP Shares: %.2f", self._window_buy_shares, self._window_tp_shares)

    def _cache_opened_position(self, token: TokenMarket, shares: float, entry_price: float, side_label: str) -> None:
        """Cache a position we just opened so we can track it immediately without waiting for trade history."""
        self._local_position_cache[token.token_id] = {
            "token": token,
            "shares": shares,
            "original_shares": shares,  # Track original for reference
            "entry_price": entry_price,
            "original_entry_price": entry_price,  # Never changes
            "side_label": side_label,
            "opened_at": datetime.now(timezone.utc),
            "market_slug": token.slug,
        }
        LOGGER.debug("Cached local position: %s %s shares @ $%.4f", side_label, shares, entry_price)

    def _update_cached_position_after_sell(self, token_id: str) -> None:
        """After a sell, check API to see if position is fully closed."""
        # Position data now comes from API - cache is only for original entry price
        # If API shows no position, we can clear the cache entry price too
        pass  # API is source of truth, _build_position_snapshots handles clearing

    def _clear_cached_position(self, token_id: str) -> None:
        """Remove a position from cache when closed."""
        if token_id in self._local_position_cache:
            del self._local_position_cache[token_id]

    def _get_window_beat_price(self, contract: ActiveContract) -> float | None:
        cached = self._window_beat_prices.get(contract.slug)
        if cached is not None:
            return cached

        window_start_ts = int(contract.end_time.timestamp()) - 300
        try:
            strike_price = self.feed.get_5m_open_price(window_start_ts)
        except Exception as exc:
            LOGGER.warning("Failed to get Binance 5m open price for %s: %s", contract.slug, exc)
            return None

        self._window_beat_prices[contract.slug] = strike_price
        LOGGER.info("Resolved beat price from Binance 5m candle open for %s: $%.2f", contract.slug, strike_price)
        return strike_price

    def _open_position(self, contract: ActiveContract, direction: str) -> None:
        """Open a position in the given direction (used for startup UP+DOWN)."""
        LOGGER.debug("[STARTUP] _open_position called for %s", direction)
        token = contract.token_for_signal(direction)
        if token is None:
            LOGGER.error("[STARTUP %s] Token is None!", direction)
            return
        LOGGER.debug("[STARTUP %s] Got token: %s", direction, token.outcome)
        entry_price = self.trader.best_buy_price(token.token_id)
        LOGGER.debug("[STARTUP %s] Price: $%.4f", direction, entry_price)
        if not (self.config.min_entry_price <= entry_price <= self.config.max_entry_price):
            LOGGER.warning("[STARTUP %s] Price $%.4f outside range [%.2f-%.2f], skipping", direction, entry_price, self.config.min_entry_price, self.config.max_entry_price)
            return
        
        all_balances, balance = self.trader.get_all_balances()
        if balance < self.config.min_trade_usd:
            LOGGER.warning("[STARTUP %s] Insufficient balance: $%.4f", direction, balance)
            return
        
        usd_budget = max(self.config.min_trade_usd, balance * self.config.trade_balance_fraction)
        share_count = max(1, int(round(usd_budget / entry_price)))
        notional = max(self.config.min_trade_usd, round(share_count * entry_price, 2))
        
        self._signal_count += 1
        LOGGER.info("[STARTUP BUY] %s | Price: $%.4f | Shares: %d | Amount: $%.2f", direction, entry_price, share_count, notional)
        
        if self.config.dry_run:
            LOGGER.warning("DRY RUN: would buy %s shares of %s", share_count, token.outcome)
            return
        
        self._cache_opened_position(token, share_count, entry_price, direction)
        try:
            response = self.trader.open_market_buy(token, notional)
            fill_price = _infer_fill_price(response, BUY)
            actual_shares = float(response.get('takingAmount', share_count)) if response else share_count
            LOGGER.info("[STARTUP OPENED] %s | Price: $%.4f | Shares: %.2f | Amount: $%.2f | Deal %d/%d", 
                       direction, fill_price or entry_price, actual_shares, notional,
                       self._deals_this_window + 1, self._max_deals_per_window)
            self._deals_this_window += 1
            self._cache_opened_position(token, actual_shares, fill_price or entry_price, direction)
        except Exception as e:
            LOGGER.error("[STARTUP %s] Buy failed: %s", direction, e)
            self._clear_cached_position(token.token_id)

    def _place_dual_early_bird_orders(self, contract: ActiveContract) -> bool:
        share_count = self._window_buy_shares
        buy_price = 0.48
        if self.config.dry_run:
            LOGGER.info("DRY RUN [EARLY BIRD BOTH] %s | UP/DOWN limit buy at $%.2f | shares=%d", contract.slug, buy_price, share_count)
            return True

        placed_any = False
        for token, direction in ((contract.up, "UP"), (contract.down, "DOWN")):
            try:
                response = self.trader.place_limit_buy(token, buy_price, share_count)
                order_id = str(response.get("orderID") or response.get("id") or "")
                if order_id:
                    self._order_roles[order_id] = {"role": "entry", "token_id": token.token_id, "side_label": direction, "price": buy_price, "shares": float(share_count), "strategy": "early_bird"}
                self._local_position_cache[token.token_id] = {
                    "token": token,
                    "original_shares": float(share_count),
                    "entry_price": float(buy_price),
                    "original_entry_price": float(buy_price),
                    "side_label": direction,
                    "opened_at": datetime.now(timezone.utc),
                    "strategy": "early_bird",
                }
                placed_any = True
                LOGGER.info("[EARLY BIRD BUY] %s | side=%s | limit=$%.2f | shares=%d", contract.slug, direction, buy_price, share_count)
            except Exception as exc:
                LOGGER.error("[EARLY BIRD BUY FAILED] %s | side=%s | %s", contract.slug, direction, exc)
        return placed_any

    def _buy_order_exists_at_price(self, token_id: str, open_orders: list[dict[str, Any]], price: float, tolerance: float = 0.0005) -> bool:
        for order in open_orders:
            side = str(order.get("side") or "").upper()
            oid_token = str(order.get("asset_id") or order.get("assetId") or order.get("token_id") or order.get("tokenId") or "")
            if side != BUY or oid_token != token_id:
                continue
            order_price = _to_float(order.get("price")) or 0.0
            if abs(order_price - price) <= tolerance:
                return True
        return False

    def _buy_request_on_cooldown(self, token_id: str, price: float, now_ts: float) -> bool:
        cooldown = float(getattr(self.config, "buy_same_price_cooldown_seconds", 5) or 0)
        if cooldown <= 0:
            return False
        last_ts = self._recent_buy_requests.get((token_id, price))
        return last_ts is not None and (now_ts - last_ts) < cooldown

    def _place_single_early_bird_buy(self, contract: ActiveContract, side_label: str, price: float, open_orders: list[dict[str, Any]] | None = None) -> bool:
        token = contract.up if side_label == "UP" else contract.down
        rounded = round(max(0.01, min(price, 0.99)), 2)
        share_count = self._window_buy_shares
        live_orders = open_orders or []
        now_ts = time.time()
        if self._buy_order_exists_at_price(token.token_id, live_orders, rounded):
            return False
        if self._buy_request_on_cooldown(token.token_id, rounded, now_ts):
            LOGGER.info("[EARLY BIRD BUY COOLDOWN] %s | side=%s | limit=$%.2f", contract.slug, side_label, rounded)
            return False
        if self.config.dry_run:
            LOGGER.info("DRY RUN [EARLY BIRD BUY] %s | side=%s | limit=$%.2f | shares=%d", contract.slug, side_label, rounded, share_count)
            return True
        try:
            self._recent_buy_requests[(token.token_id, rounded)] = now_ts
            response = self.trader.place_limit_buy(token, rounded, share_count)
            order_id = str(response.get("orderID") or response.get("id") or "")
            if order_id:
                self._order_roles[order_id] = {"role": "entry", "token_id": token.token_id, "side_label": side_label, "price": rounded, "shares": float(share_count), "strategy": "early_bird"}
            self._window_entry_price_floor[token.token_id] = max(self._window_entry_price_floor.get(token.token_id, 0.0), rounded)
            self._local_position_cache[token.token_id] = {
                "token": token,
                "original_shares": float(share_count),
                "entry_price": float(rounded),
                "original_entry_price": float(rounded),
                "side_label": side_label,
                "opened_at": datetime.now(timezone.utc),
                "strategy": "early_bird",
            }
            LOGGER.info("[EARLY BIRD BUY] %s | side=%s | limit=$%.2f | shares=%d", contract.slug, side_label, rounded, share_count)
            return True
        except Exception as exc:
            LOGGER.error("[EARLY BIRD BUY FAILED] %s | side=%s | limit=$%.2f | %s", contract.slug, side_label, rounded, exc)
            return False

    def _fullset_order_shares(self) -> int:
        return 5

    def _arb_entry_threshold(self) -> float:
        return 0.97

    def _position_shares_by_side(self, positions: list[PositionSnapshot]) -> dict[str, float]:
        result = {"UP": 0.0, "DOWN": 0.0}
        for p in positions:
            if p.shares <= 0:
                continue
            if p.side_label in result:
                result[p.side_label] += float(p.shares)
        return result

    def _rounded_position_shares_by_side(self, positions: list[PositionSnapshot]) -> dict[str, int]:
        live = self._position_shares_by_side(positions)
        return {"UP": int(round(live["UP"])), "DOWN": int(round(live["DOWN"]))}

    def _fullset_contract_waiting_for_confirmation(self, contract: ActiveContract) -> bool:
        return self._fullset_waiting_cache(contract)

    def _fullset_clear_imbalance_lock_if_progressed(self, contract: ActiveContract, positions: list[PositionSnapshot]) -> None:
        lock = self._fullset_imbalance_lock.get(contract.slug)
        if not lock:
            return
        live = self._rounded_position_shares_by_side(positions)
        step_up = self._fullset_step_count(live["UP"])
        step_down = self._fullset_step_count(live["DOWN"])
        if (step_up, step_down) != (lock[0], lock[1]) or step_up == step_down:
            LOGGER.info(
                "[REBALANCE LOCK CLEAR] %s | prev=(%d,%d,%s) | now=(%d,%d)",
                contract.slug,
                lock[0],
                lock[1],
                lock[2],
                step_up,
                step_down,
            )
            self._fullset_imbalance_lock.pop(contract.slug, None)

    def _pending_buy_shares_by_side(self, contract: ActiveContract, open_orders: list[dict[str, Any]]) -> dict[str, float]:
        result = {"UP": 0.0, "DOWN": 0.0}
        token_to_side = {contract.up.token_id: "UP", contract.down.token_id: "DOWN"}
        for order in open_orders:
            side = str(order.get("side") or "").upper()
            if side != BUY:
                continue
            token_id = str(order.get("asset_id") or order.get("assetId") or order.get("token_id") or order.get("tokenId") or "")
            mapped = token_to_side.get(token_id)
            if not mapped:
                continue
            shares = _to_float(order.get("size") or order.get("original_size") or order.get("remaining_size")) or 0.0
            result[mapped] += shares
        return result

    def _pending_buy_order_counts_by_side(self, contract: ActiveContract, open_orders: list[dict[str, Any]]) -> dict[str, int]:
        counts = {"UP": 0, "DOWN": 0}
        token_to_side = {contract.up.token_id: "UP", contract.down.token_id: "DOWN"}
        for order in open_orders:
            side = str(order.get("side") or "").upper()
            if side != BUY:
                continue
            token_id = str(order.get("asset_id") or order.get("assetId") or order.get("token_id") or order.get("tokenId") or "")
            mapped = token_to_side.get(token_id)
            if mapped:
                counts[mapped] += 1
        return counts

    def _avg_entry_by_side(self, positions: list[PositionSnapshot]) -> dict[str, float | None]:
        totals = {"UP": {"shares": 0.0, "cost": 0.0}, "DOWN": {"shares": 0.0, "cost": 0.0}}
        for p in positions:
            if p.shares <= 0:
                continue
            if p.side_label not in totals:
                continue
            totals[p.side_label]["shares"] += float(p.shares)
            totals[p.side_label]["cost"] += float(p.shares) * float(p.average_entry_price)

        out: dict[str, float | None] = {"UP": None, "DOWN": None}
        for side_label, data in totals.items():
            if data["shares"] > 0:
                out[side_label] = data["cost"] / data["shares"]
        return out

    def _fullset_step_count(self, shares: float) -> int:
        return int(max(0, int(round(float(shares))) // self._fullset_order_shares()))

    def _fullset_cache_key(self, contract: ActiveContract, side_label: str) -> tuple[str, str]:
        return (contract.slug, side_label)

    def _cleanup_fullset_tracking(self, contract: ActiveContract, positions: list[PositionSnapshot], open_orders: list[dict[str, Any]]) -> None:
        now_ts = time.time()
        live = self._rounded_position_shares_by_side(positions)
        counts = self._pending_buy_order_counts_by_side(contract, open_orders)
        self._fullset_clear_imbalance_lock_if_progressed(contract, positions)
        for side_label in ("UP", "DOWN"):
            key = self._fullset_cache_key(contract, side_label)
            sent_meta = self._fullset_sent_cache.get(key)
            if sent_meta:
                baseline_live = float(sent_meta.get("baseline_live_shares", 0.0) or 0.0)
                live_now = float(live[side_label])
                if counts[side_label] > 0 or live_now > baseline_live:
                    LOGGER.info(
                        "[FULLSET SENT CONFIRMED] %s | side=%s | pending=%d | live=%.2f | baseline=%.2f",
                        contract.slug,
                        side_label,
                        counts[side_label],
                        live_now,
                        baseline_live,
                    )
                    self._fullset_sent_cache.pop(key, None)
            if counts[side_label] > 0:
                self._fullset_order_first_seen_at.setdefault(key, now_ts)
            else:
                self._fullset_order_first_seen_at.pop(key, None)
            until_ts = float(self._fullset_side_cooldown_until.get(key, 0.0) or 0.0)
            if until_ts and until_ts <= now_ts:
                self._fullset_side_cooldown_until.pop(key, None)
        live_slugs = {contract.slug}
        self._fullset_last_reset_check = {k: v for k, v in self._fullset_last_reset_check.items() if k in live_slugs or k == self._current_window_slug}

    def _cancel_order_id(self, order_id: str) -> None:
        try:
            self.trader.cancel_order(order_id)
            self._order_roles.pop(order_id, None)
        except Exception as exc:
            LOGGER.debug("Cancel failed for %s: %s", order_id, exc)

    def _cancel_contract_buy_orders(self, contract: ActiveContract, open_orders: list[dict[str, Any]], only_side: str | None = None) -> int:
        token_id = None
        if only_side == "UP":
            token_id = contract.up.token_id
        elif only_side == "DOWN":
            token_id = contract.down.token_id
        cancelled = 0
        for order in open_orders:
            side = str(order.get("side") or "").upper()
            if side != BUY:
                continue
            oid = str(order.get("id") or order.get("orderID") or "")
            otoken = str(order.get("asset_id") or order.get("assetId") or order.get("token_id") or order.get("tokenId") or "")
            if not oid:
                continue
            if token_id is not None and otoken != token_id:
                continue
            self._cancel_order_id(oid)
            cancelled += 1
        return cancelled

    def _fullset_waiting_cache(self, contract: ActiveContract) -> bool:
        return any(k[0] == contract.slug for k in self._fullset_sent_cache)

    def _mark_window_stopped_if_balanced_lock(self, contract: ActiveContract, positions: list[PositionSnapshot], open_orders: list[dict[str, Any]]) -> None:
        now_ts = time.time()
        live = self._position_shares_by_side(positions)
        live_up = int(round(live["UP"]))
        live_down = int(round(live["DOWN"]))
        if live_up <= 0 or live_down <= 0 or live_up != live_down:
            self._fullset_low_avg_sum_first_seen.pop(contract.slug, None)
            return
        avg = self._avg_entry_by_side(positions)
        if avg["UP"] is None or avg["DOWN"] is None:
            self._fullset_low_avg_sum_first_seen.pop(contract.slug, None)
            return
        avg_sum = float(avg["UP"]) + float(avg["DOWN"])
        if avg_sum >= 0.96:
            self._fullset_low_avg_sum_first_seen.pop(contract.slug, None)
            return

        first_seen = self._fullset_low_avg_sum_first_seen.get(contract.slug)
        if first_seen is None:
            self._fullset_low_avg_sum_first_seen[contract.slug] = now_ts
            LOGGER.info(
                "[WINDOW STOP VERIFY] %s | avg_up=%.4f | avg_down=%.4f | avg_sum=%.4f | waiting_for_recheck",
                contract.slug,
                float(avg["UP"]),
                float(avg["DOWN"]),
                avg_sum,
            )
            return
        if now_ts - first_seen < 6.0:
            LOGGER.info(
                "[WINDOW STOP VERIFY] %s | avg_up=%.4f | avg_down=%.4f | avg_sum=%.4f | recheck_in=%.1fs",
                contract.slug,
                float(avg["UP"]),
                float(avg["DOWN"]),
                avg_sum,
                6.0 - (now_ts - first_seen),
            )
            return

        if contract.slug not in self._fullset_window_stopped:
            self._fullset_window_stopped.add(contract.slug)
            cancelled = self._cancel_contract_buy_orders(contract, open_orders)
            LOGGER.info(
                "[WINDOW STOP] %s | avg_up=%.4f | avg_down=%.4f | avg_sum=%.4f | cancelled=%d",
                contract.slug,
                float(avg["UP"]),
                float(avg["DOWN"]),
                avg_sum,
                cancelled,
            )

    def _cancel_smaller_side_every_15s(self, contract: ActiveContract, positions: list[PositionSnapshot], open_orders: list[dict[str, Any]], now_ts: float) -> None:
        last = self._fullset_last_reset_check.get(contract.slug, 0.0)
        if now_ts - last < 15.0:
            return
        self._fullset_last_reset_check[contract.slug] = now_ts
        live = self._position_shares_by_side(positions)
        live_up = int(round(live["UP"]))
        live_down = int(round(live["DOWN"]))
        if live_up == live_down or live_up == 0 or live_down == 0:
            return
        smaller = "UP" if live_up < live_down else "DOWN"
        heavier = "DOWN" if smaller == "UP" else "UP"
        cancelled_heavier = self._cancel_contract_buy_orders(contract, open_orders, only_side=heavier)
        cancelled_smaller = self._cancel_contract_buy_orders(contract, open_orders, only_side=smaller)

        # Allow one fresh rebalance retry after each 15s cleanup tick.
        self._fullset_imbalance_lock.pop(contract.slug, None)
        self._fullset_side_cooldown_until.pop(self._fullset_cache_key(contract, smaller), None)

        if cancelled_heavier or cancelled_smaller:
            LOGGER.info(
                "[FULLSET 15S RESET] %s | smaller=%s | cancelled_smaller=%d | cancelled_heavier=%d | lock_reset=true",
                contract.slug,
                smaller,
                cancelled_smaller,
                cancelled_heavier,
            )

    def _cancel_invalid_pending_for_imbalance(self, contract: ActiveContract, positions: list[PositionSnapshot], open_orders: list[dict[str, Any]]) -> int:
        live = self._position_shares_by_side(positions)
        live_up = int(round(live["UP"]))
        live_down = int(round(live["DOWN"]))
        if live_up == live_down:
            return 0
        invalid_side = "UP" if live_up > live_down else "DOWN"
        cancelled = self._cancel_contract_buy_orders(contract, open_orders, only_side=invalid_side)
        if cancelled:
            LOGGER.info("[FULLSET INVALID PENDING CANCEL] %s | invalid_side=%s | cancelled=%d", contract.slug, invalid_side, cancelled)
        return cancelled

    def _fullset_side_on_cooldown(self, contract: ActiveContract, side_label: str) -> tuple[bool, float]:
        key = self._fullset_cache_key(contract, side_label)
        until_ts = float(self._fullset_side_cooldown_until.get(key, 0.0) or 0.0)
        now_ts = time.time()
        remaining = max(0.0, until_ts - now_ts)
        return remaining > 0.0, remaining

    def _can_place_fullset_pending_order(self, contract: ActiveContract, side_label: str, open_orders: list[dict[str, Any]], positions: list[PositionSnapshot]) -> tuple[bool, str]:
        self._cleanup_fullset_tracking(contract, positions, open_orders)
        if contract.slug in self._fullset_window_stopped:
            return False, "stopped window"
        if self._fullset_contract_waiting_for_confirmation(contract):
            return False, "waiting for API confirmation"
        if self._tick_buy_count >= 1:
            return False, "max_one_buy_per_tick"
        counts = self._pending_buy_order_counts_by_side(contract, open_orders)
        total_pending = counts["UP"] + counts["DOWN"]
        on_cd, cd_remaining = self._fullset_side_on_cooldown(contract, side_label)
        if on_cd:
            return False, f"side cooldown {cd_remaining:.1f}s"
        live = self._rounded_position_shares_by_side(positions)
        step_up = self._fullset_step_count(live["UP"])
        step_down = self._fullset_step_count(live["DOWN"])

        # Pending-order layout rules:
        # - max 1 pending LIMIT BUY per side always
        # - balanced only: up to 2 total pending buys (one UP + one DOWN)
        # - imbalanced live inventory: do not place new LIMIT BUY orders
        if counts[side_label] >= 1:
            return False, f"max_one_limit_per_side side={side_label} count={counts[side_label]}"
        if step_up == step_down:
            if total_pending >= 2:
                return False, f"balanced_pending_cap up={counts['UP']} down={counts['DOWN']}"
        else:
            return False, f"live_not_balanced step_up={step_up} step_down={step_down}"

        lock = self._fullset_imbalance_lock.get(contract.slug)
        if lock and (step_up, step_down) == (lock[0], lock[1]) and side_label == lock[2]:
            return False, "imbalance lock active"
        if step_up > step_down and side_label != "DOWN":
            return False, "only_down_allowed"
        if step_down > step_up and side_label != "UP":
            return False, "only_up_allowed"

        # Risk rule: allow at most one fullset order (5 shares) imbalance between sides.
        projected_up = step_up + (1 if side_label == "UP" else 0)
        projected_down = step_down + (1 if side_label == "DOWN" else 0)
        if abs(projected_up - projected_down) > 1:
            return False, f"max_imbalance_exceeded projected_up={projected_up} projected_down={projected_down}"
        return True, "ok"

    def _get_fullset_snapshot(self, contract: ActiveContract, positions: list[PositionSnapshot], open_orders: list[dict[str, Any]]) -> dict[str, Any] | None:
        up_price = self._entry_limit_price(contract.up)
        down_price = self._entry_limit_price(contract.down)
        if up_price is None or down_price is None:
            return None

        live_shares = self._position_shares_by_side(positions)
        rounded_live = self._rounded_position_shares_by_side(positions)
        pending_shares = self._pending_buy_shares_by_side(contract, open_orders)
        pending_counts = self._pending_buy_order_counts_by_side(contract, open_orders)
        avg_entry = self._avg_entry_by_side(positions)
        avg_sum = None
        if avg_entry["UP"] is not None and avg_entry["DOWN"] is not None:
            avg_sum = float(avg_entry["UP"]) + float(avg_entry["DOWN"])
        return {
            "up_buy_price": round(up_price, 2),
            "down_buy_price": round(down_price, 2),
            "live_up": live_shares["UP"],
            "live_down": live_shares["DOWN"],
            "live_up_int": rounded_live["UP"],
            "live_down_int": rounded_live["DOWN"],
            "live_step_up": self._fullset_step_count(rounded_live["UP"]),
            "live_step_down": self._fullset_step_count(rounded_live["DOWN"]),
            "pending_up_orders": pending_counts["UP"],
            "pending_down_orders": pending_counts["DOWN"],
            "pending_up_shares": pending_shares["UP"],
            "pending_down_shares": pending_shares["DOWN"],
            "up_avg": avg_entry["UP"],
            "down_avg": avg_entry["DOWN"],
            "avg_sum": avg_sum,
            "window_stopped": contract.slug in self._fullset_window_stopped,
            "contract_waiting_cache": self._fullset_waiting_cache(contract),
            "contract_waiting_confirmation": self._fullset_contract_waiting_for_confirmation(contract),
            "order_size": float(self._fullset_order_shares()),
        }

    def _place_fullset_limit_buy(self, contract: ActiveContract, side_label: str, price: float, share_count: int, open_orders: list[dict[str, Any]] | None = None, positions: list[PositionSnapshot] | None = None, reason: str = "fullset") -> bool:
        token = contract.up if side_label == "UP" else contract.down
        rounded = round(max(0.01, min(price, 0.99)), 2)
        live_orders = list(open_orders or [])
        live_positions = list(positions or [])
        now_ts = time.time()
        allowed, guard_reason = self._can_place_fullset_pending_order(contract, side_label, live_orders, live_positions)
        if not allowed:
            LOGGER.info("[FULLSET BUY BLOCKED] %s | side=%s | reason=%s | %s", contract.slug, side_label, reason, guard_reason)
            return False
        if self._buy_order_exists_at_price(token.token_id, live_orders, rounded):
            return False
        if self._buy_request_on_cooldown(token.token_id, rounded, now_ts):
            LOGGER.info("[FULLSET BUY COOLDOWN] %s | side=%s | limit=$%.2f | reason=%s", contract.slug, side_label, rounded, reason)
            return False
        cache_key = self._fullset_cache_key(contract, side_label)
        live_by_side = self._position_shares_by_side(live_positions)
        self._fullset_sent_cache[cache_key] = {
            "sent_at": now_ts,
            "baseline_live_shares": float(live_by_side[side_label]),
            "side": side_label,
        }
        self._fullset_side_cooldown_until[cache_key] = now_ts + 10.0
        rounded_live = self._rounded_position_shares_by_side(live_positions)
        step_up = self._fullset_step_count(rounded_live["UP"])
        step_down = self._fullset_step_count(rounded_live["DOWN"])
        if reason.startswith("rebalance_"):
            self._fullset_imbalance_lock[contract.slug] = (step_up, step_down, side_label)
            LOGGER.info("[REBALANCE LOCK SET] %s | step_up=%d | step_down=%d | side=%s", contract.slug, step_up, step_down, side_label)
        if self.config.dry_run:
            self._tick_buy_count += 1
            LOGGER.info("DRY RUN [FULLSET BUY] %s | side=%s | limit=$%.2f | shares=%d | reason=%s", contract.slug, side_label, rounded, share_count, reason)
            return True
        try:
            self._recent_buy_requests[(token.token_id, rounded)] = now_ts
            response = self.trader.place_limit_buy(token, rounded, share_count)
            self._tick_buy_count += 1
            order_id = str(response.get("orderID") or response.get("id") or "")
            if order_id:
                self._order_roles[order_id] = {"role": "entry", "token_id": token.token_id, "side_label": side_label, "price": rounded, "shares": float(share_count), "strategy": "fullset_arb", "reason": reason}
            self._window_entry_price_floor[token.token_id] = max(self._window_entry_price_floor.get(token.token_id, 0.0), rounded)
            self._local_position_cache[token.token_id] = {"token": token, "original_shares": float(share_count), "entry_price": float(rounded), "original_entry_price": float(rounded), "side_label": side_label, "opened_at": datetime.now(timezone.utc), "strategy": "fullset_arb", "reason": reason}
            LOGGER.info("[FULLSET BUY] %s | side=%s | limit=$%.2f | shares=%d | reason=%s", contract.slug, side_label, rounded, share_count, reason)
            return True
        except Exception as exc:
            self._fullset_sent_cache.pop(cache_key, None)
            self._fullset_side_cooldown_until.pop(cache_key, None)
            LOGGER.error("[FULLSET BUY FAILED] %s | side=%s | limit=$%.2f | reason=%s | %s", contract.slug, side_label, rounded, reason, exc)
            return False

    def _place_dominance_limit_buy(self, contract: ActiveContract, side_label: str, price: float, share_count: int, tp_price: float, open_orders: list[dict[str, Any]] | None = None) -> bool:
        token = contract.up if side_label == "UP" else contract.down
        rounded = round(max(0.01, min(price, 0.99)), 2)
        live_orders = open_orders or []
        now_ts = time.time()
        if self._buy_order_exists_at_price(token.token_id, live_orders, rounded):
            return False
        if self._buy_request_on_cooldown(token.token_id, rounded, now_ts):
            LOGGER.info("[DOMINANCE BUY COOLDOWN] %s | side=%s | limit=$%.2f", contract.slug, side_label, rounded)
            return False
        if self.config.dry_run:
            LOGGER.info("DRY RUN [DOMINANCE BUY] %s | side=%s | limit=$%.2f | shares=%d | tp=$%.2f", contract.slug, side_label, rounded, share_count, tp_price)
            self._token_tp_price_override[token.token_id] = round(tp_price, 2)
            return True
        try:
            self._recent_buy_requests[(token.token_id, rounded)] = now_ts
            response = self.trader.place_limit_buy(token, rounded, share_count)
            order_id = str(response.get("orderID") or response.get("id") or "")
            if order_id:
                self._order_roles[order_id] = {"role": "entry", "token_id": token.token_id, "side_label": side_label, "price": rounded, "shares": float(share_count), "strategy": "dominance"}
            self._window_entry_price_floor[token.token_id] = max(self._window_entry_price_floor.get(token.token_id, 0.0), rounded)
            self._token_tp_price_override[token.token_id] = round(tp_price, 2)
            self._local_position_cache[token.token_id] = {
                "token": token,
                "original_shares": float(share_count),
                "entry_price": float(rounded),
                "original_entry_price": float(rounded),
                "side_label": side_label,
                "opened_at": datetime.now(timezone.utc),
                "strategy": "dominance",
            }
            LOGGER.info("[DOMINANCE BUY] %s | side=%s | limit=$%.2f | shares=%d | tp=$%.2f", contract.slug, side_label, rounded, share_count, tp_price)
            return True
        except Exception as exc:
            LOGGER.error("[DOMINANCE BUY FAILED] %s | side=%s | limit=$%.2f | %s", contract.slug, side_label, rounded, exc)
            return False

    def _place_early_bird_ladder_orders(self, contract: ActiveContract, open_orders: list[dict[str, Any]], prices: list[float] | None = None) -> int:
        prices = prices or [0.48, 0.47, 0.46, 0.45]
        placed = 0
        for rounded in prices:
            for side_label in ("UP", "DOWN"):
                if self._place_single_early_bird_buy(contract, side_label, rounded, open_orders):
                    placed += 1
        return placed

    def _cancel_buy_orders_only(self, open_orders: list[dict[str, Any]]) -> int:
        cancelled = 0
        for order in open_orders:
            side = str(order.get("side") or "").upper()
            order_id = str(order.get("id") or order.get("orderID") or "")
            if side != BUY or not order_id:
                continue
            try:
                self.trader.cancel_order(order_id)
                self._order_roles.pop(order_id, None)
                cancelled += 1
            except Exception as exc:
                LOGGER.debug("Cancel buy failed for %s: %s", order_id, exc)
        return cancelled

    def _tp_open_shares_by_token(self, open_orders: list[dict[str, Any]]) -> dict[str, float]:
        result: dict[str, float] = {}
        for order in open_orders:
            side = str(order.get("side") or "").upper()
            if side != SELL:
                continue
            token_id = str(order.get("asset_id") or order.get("assetId") or order.get("token_id") or order.get("tokenId") or "")
            shares = _to_float(order.get("size") or order.get("original_size") or order.get("remaining_size")) or 0.0
            if token_id:
                result[token_id] = result.get(token_id, 0.0) + max(0.0, shares)
        return result

    def _cancel_opposite_buy_orders(self, contract: ActiveContract, open_orders: list[dict[str, Any]], keep_side: str) -> None:
        keep_token_id = contract.up.token_id if keep_side == "UP" else contract.down.token_id
        cancelled = 0
        for order in open_orders:
            side = str(order.get("side") or "").upper()
            token_id = str(order.get("asset_id") or order.get("assetId") or order.get("token_id") or order.get("tokenId") or "")
            order_id = str(order.get("id") or order.get("orderID") or "")
            if side != BUY or not order_id:
                continue
            if token_id and token_id != keep_token_id:
                try:
                    self.trader.cancel_order(order_id)
                    self._order_roles.pop(order_id, None)
                    cancelled += 1
                except Exception as exc:
                    LOGGER.debug("Cancel opposite buy failed for %s: %s", order_id, exc)
        if cancelled:
            LOGGER.info("[EARLY BIRD CANCEL OPPOSITE] %s | keep=%s | cancelled=%d", contract.slug, keep_side, cancelled)

    def shutdown(self, *_: Any) -> None:
        LOGGER.info("Shutdown requested.")
        self._shutdown = True

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

        LOGGER.info("Starting BTC 5-minute Polymarket scalper. dry_run=%s", self.config.dry_run)
        
        # Show wallet balance on startup
        try:
            _, balance = self.trader.get_all_balances()
            LOGGER.info("Balance: $%.4f", balance)
        except Exception as e:
            LOGGER.debug("Could not fetch startup balance: %s", e)
        self._recompute_window_sizes()
        
        while not self._shutdown:
            started = time.time()
            try:
                self._loop_once()
            except Exception:
                LOGGER.exception("Strategy loop failed")
            elapsed = time.time() - started
            time.sleep(max(0.0, self.config.poll_interval_seconds - elapsed))

    def _check_window_change(self, contract: ActiveContract | None, positions: list[PositionSnapshot]) -> bool:
        """Reset ONLY when the live/current 5-minute window actually changes."""
        if contract is None:
            return False

        now_ts = time.time()
        window_start_ts = int(contract.end_time.timestamp()) - 300
        window_end_ts = int(contract.end_time.timestamp())

        # Ignore any future/preloaded contract here. This reset must follow ONLY the live window.
        if not (window_start_ts <= now_ts < window_end_ts):
            return False

        if contract.slug != self._current_window_slug:
            if self._current_window_slug is not None:
                LOGGER.info("[WINDOW CHANGE] %s -> %s", self._current_window_slug, contract.slug)
            else:
                LOGGER.info("[WINDOW] %s | Ends: %s", contract.slug, contract.end_time.strftime("%H:%M:%S"))
            self._current_window_slug = contract.slug
            self._local_position_cache.clear()
            self._window_entry_price_floor.clear()
            self._recent_buy_requests.clear()
            self._token_tp_price_override.clear()
            self._fullset_sent_cache.clear()
            self._fullset_side_cooldown_until.clear()
            self._fullset_order_first_seen_at.clear()
            self._fullset_imbalance_lock.clear()
            self._fullset_low_avg_sum_first_seen.clear()
            self._fullset_window_stopped.clear()
            self._fullset_last_reset_check.clear()
            self._deals_this_window = 0
            self._panic_window_slug = None
            self._last_entry_order_ts = 0.0
            self._entry_request_pending = False
            self._entry_signal_started_at = None
            self._prev_signal_price = None
            self._local_pending_entry_count = 0
            self._awaiting_entry_fill_token_id = None
            self._awaiting_entry_order_id = None
            LOGGER.info("[WINDOW RESET] Cache cleared, deals reset to 0 for new live window")
            try:
                _, balance = self.trader.get_all_balances()
                LOGGER.info("[WINDOW BALANCE] $%.4f", balance)
            except Exception as exc:
                LOGGER.warning("[WINDOW BALANCE] Failed to refresh balance: %s", exc)
            self._recompute_window_sizes()
            setup_file_logger(contract.slug)
        return False

    def _exit_window_mode(self, contract: ActiveContract, positions: list[PositionSnapshot], open_orders: list[dict[str, Any]], reason: str) -> None:
        LOGGER.warning("[EXIT WINDOW DISABLED] %s | %s", contract.slug, reason)
        self._panic_window_slug = contract.slug
        return

    def _log_position_transitions(self, positions: list[PositionSnapshot], open_orders: list[dict[str, Any]]) -> None:
        """Optional transition logger. Safe no-op if detailed TP fill tracking is unavailable."""
        return

    def _loop_once(self) -> None:
        now_ts = int(time.time())
        if now_ts != self._tick_second:
            self._tick_second = now_ts
            self._tick_buy_count = 0
        try:
            btc_price = self.feed.poll()
        except requests.RequestException as exc:
            if (time.time() - self._last_network_error_log_ts) >= 5.0:
                LOGGER.warning("[DATA FEED ERROR] Binance poll failed: %s", exc)
                self._last_network_error_log_ts = time.time()
            return

        contract = self.locator.get_active_contract()
        if contract is None:
            return

        positions = self._build_position_snapshots(contract)
        self._check_window_change(contract, positions)
        positions = self._build_position_snapshots(contract)

        now = time.time()
        window_start_ts = int(contract.end_time.timestamp()) - 300
        seconds_to_start = window_start_ts - int(now)
        elapsed = max(0.0, now - window_start_ts)
        seconds_remaining = max(0.0, contract.end_time.timestamp() - now)

        open_orders = self._open_orders_for_contract(contract)
        self._sync_order_roles(open_orders)
        self._cleanup_fullset_tracking(contract, positions, open_orders)
        self._cancel_invalid_pending_for_imbalance(contract, positions, open_orders)
        open_orders = self._open_orders_for_contract(contract)
        self._sync_order_roles(open_orders)
        self._mark_window_stopped_if_balanced_lock(contract, positions, open_orders)
        self._cancel_smaller_side_every_15s(contract, positions, open_orders, now)
        open_orders = self._open_orders_for_contract(contract)
        self._sync_order_roles(open_orders)
        self._log_position_transitions(positions, open_orders)

        snapshot = self._get_fullset_snapshot(contract, positions, open_orders) or {}
        LOGGER.info(
            "[FULLSET STATE] %s | live_up=%s | live_down=%s | pending_up=%s | pending_down=%s | total_up=%s | total_down=%s | step_up=%s | step_down=%s | avg_up=%s | avg_down=%s | avg_sum=%s",
            contract.slug,
            snapshot.get("live_up_int", 0),
            snapshot.get("live_down_int", 0),
            snapshot.get("pending_up_orders", 0),
            snapshot.get("pending_down_orders", 0),
            snapshot.get("live_up", 0.0),
            snapshot.get("live_down", 0.0),
            snapshot.get("live_step_up", 0),
            snapshot.get("live_step_down", 0),
            snapshot.get("up_avg"),
            snapshot.get("down_avg"),
            snapshot.get("avg_sum"),
        )
        has_position = any(p.shares >= 1.0 for p in positions)
        pending_buys = self._count_pending_buy_orders(open_orders)
        LOGGER.info(
            "[CURRENT WINDOW HEARTBEAT] %s | t_to_start=%ds | elapsed=%ds | remaining=%ds | has_position=%s | pending_buys=%d",
            contract.slug,
            int(seconds_to_start),
            int(elapsed),
            int(seconds_remaining),
            has_position,
            pending_buys,
        )

        self._ensure_take_profit_orders(positions, open_orders)
        if self.enable_early_bird_strategy:
            self.early_bird_strategy.evaluate(
                self,
                contract,
                btc_price,
                positions,
                open_orders,
                now,
                int(elapsed),
                int(seconds_remaining),
                int(seconds_to_start),
            )

    def _open_orders_for_contract(self, contract: ActiveContract) -> list[dict[str, Any]]:
        token_ids = {contract.up.token_id, contract.down.token_id}
        orders = []
        for order in self.trader.open_orders():
            token_id = str(order.get("asset_id") or order.get("assetId") or order.get("token_id") or order.get("tokenId") or "")
            if token_id in token_ids:
                orders.append(order)
        return orders

    def _sync_order_roles(self, open_orders: list[dict[str, Any]]) -> None:
        live_ids = {str(order.get("id") or order.get("orderID") or "") for order in open_orders}
        self._order_roles = {oid: meta for oid, meta in self._order_roles.items() if oid in live_ids}
        self._local_pending_entry_count = sum(1 for meta in self._order_roles.values() if meta.get("role") == "entry")

        if self._awaiting_entry_order_id and self._awaiting_entry_order_id not in live_ids:
            if self._awaiting_entry_fill_token_id not in self._filled_positions:
                self._awaiting_entry_fill_token_id = None
            self._awaiting_entry_order_id = None

        if self._awaiting_entry_fill_token_id and self._awaiting_entry_fill_token_id in self._filled_positions:
            self._awaiting_entry_fill_token_id = None
            self._awaiting_entry_order_id = None

    def _cancel_contract_orders(self, open_orders: list[dict[str, Any]]) -> None:
        for order in open_orders:
            order_id = str(order.get("id") or order.get("orderID") or "")
            if not order_id:
                continue
            try:
                self.trader.cancel_order(order_id)
            except Exception as exc:
                LOGGER.debug("Cancel failed for %s: %s", order_id, exc)

    def _count_pending_buy_orders(self, open_orders: list[dict[str, Any]]) -> int:
        return sum(1 for order in open_orders if str(order.get("side") or "").upper() == BUY)

    def _entry_limit_price(self, token: TokenMarket) -> float | None:
        try:
            best_bid, best_ask = self.trader.get_order_book_top(token.token_id)
        except Exception as exc:
            LOGGER.debug("Order book read failed for %s: %s", token.token_id, exc)
            return None
        market_price = best_ask or self.trader.best_buy_price(token.token_id)
        if market_price <= 0:
            return None
        candidate = max(0.01, min(0.99, market_price - 0.05))
        return round(candidate, 3)


    def _panic_close_window(self, contract: ActiveContract, positions: list[PositionSnapshot], open_orders: list[dict[str, Any]], btc_price: float, strike_price: float) -> None:
        LOGGER.warning("[PANIC DISABLED] %s | BTC: $%.2f | Strike: $%.2f | Diff: $%.2f", contract.slug, btc_price, strike_price, abs(btc_price - strike_price))
        self._panic_window_slug = contract.slug
        return

    def _has_active_exposure(self, positions: list[PositionSnapshot], open_orders: list[dict[str, Any]]) -> bool:
        if any(position.shares >= 1.0 for position in positions):
            return True
        for order in open_orders:
            side = str(order.get("side") or "").upper()
            if side in {BUY, SELL}:
                return True
        return False

    def _adjust_entry_price_for_existing_position(self, buy_price: float, positions: list[PositionSnapshot], dominant: str) -> float:
        if not positions:
            return buy_price
        same_side_positions = [p for p in positions if p.side_label == dominant and p.shares >= 1.0]
        if not same_side_positions:
            return buy_price
        adjusted = round(buy_price - 0.05, 3)
        if adjusted < self.config.min_entry_price:
            adjusted = round(self.config.min_entry_price, 3)
        if adjusted > self.config.max_entry_price:
            adjusted = round(self.config.max_entry_price, 3)
        return adjusted

    def _dominant_signal(self, contract: ActiveContract, btc_price: float, strike_price: float) -> str | None:
        up_price = self._entry_limit_price(contract.up)
        down_price = self._entry_limit_price(contract.down)
        if up_price is not None and btc_price >= strike_price + 50 and self.config.min_entry_price <= up_price <= self.config.max_entry_price:
            return "UP"
        if down_price is not None and btc_price <= strike_price - 50 and self.config.min_entry_price <= down_price <= self.config.max_entry_price:
            return "DOWN"
        return None


    def _ensure_take_profit_orders(self, positions: list[PositionSnapshot], open_orders: list[dict[str, Any]]) -> None:
        LOGGER.debug("[TP DISABLED] no sell orders will be placed automatically")
        return

    def _build_position_snapshots(self, contract: ActiveContract) -> list[PositionSnapshot]:
        """Build live wallet positions from balances plus trade-history entry price."""
        token_markets = {
            contract.up.token_id: (contract.up, "UP"),
            contract.down.token_id: (contract.down, "DOWN"),
        }
        positions: list[PositionSnapshot] = []

        market_trades: list[dict[str, Any]] = []
        try:
            market_trades = self.trader.trades_for_market(contract)
        except Exception as exc:
            LOGGER.debug("Could not fetch trades for market %s: %s", contract.slug, exc)

        for token_id, (token_market, label) in token_markets.items():
            shares = float(self.trader.get_conditional_balance(token_id))
            if shares < 1.0:
                continue

            entry_price = 0.0
            opened_at = datetime.now(timezone.utc)
            lots = _open_lots_from_trades(market_trades, token_id)
            if lots:
                total_shares = sum(size for size, _, _ in lots)
                if total_shares > 0:
                    entry_price = sum(size * price for size, price, _ in lots) / total_shares
                    opened_at = lots[0][2]

            cache = self._local_position_cache.get(token_id, {})
            if entry_price <= 0:
                entry_price = float(cache.get("original_entry_price") or cache.get("entry_price") or 0.0)
                if entry_price <= 0:
                    current_px = self.trader.get_token_price(token_id)
                    entry_price = max(0.01, current_px - 0.05)
            if isinstance(cache.get("opened_at"), datetime):
                opened_at = cache.get("opened_at")

            current_price = self.trader.get_token_price(token_id)
            pnl_pct = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0.0
            positions.append(
                PositionSnapshot(
                    token_id=token_id,
                    shares=shares,
                    average_entry_price=entry_price,
                    opened_at=opened_at,
                    current_bid=current_price,
                    pnl_pct=pnl_pct,
                    side_label=label,
                    market=contract,
                )
            )

        return positions

    def _close_position(self, position: PositionSnapshot) -> None:
        LOGGER.warning("[SELL DISABLED] %s | side=%s | shares=%.4f", position.market.slug, position.side_label, position.shares)
        return


def _open_lots_from_trades(trades: Iterable[dict[str, Any]], token_id: str) -> list[tuple[float, float, datetime]]:
    relevant = []
    for trade in trades:
        asset_id = str(trade.get("asset_id") or trade.get("assetId") or trade.get("token_id") or "")
        if asset_id != token_id:
            continue
        side = str(trade.get("side") or "").upper()
        price = _extract_first_float(trade, ["price", "last_trade_price", "lastTradePrice"])
        size = _extract_first_float(trade, ["size", "amount", "matched_amount", "matchedAmount"])
        timestamp = _parse_datetime(
            trade.get("match_time")
            or trade.get("matchTime")
            or trade.get("created_at")
            or trade.get("createdAt")
            or trade.get("timestamp")
        )
        if side not in {BUY, SELL} or price is None or size is None or size <= 0 or timestamp is None:
            continue
        relevant.append((timestamp, side, size, price))

    relevant.sort(key=lambda row: row[0])

    lots: list[list[Any]] = []
    for timestamp, side, size, price in relevant:
        if side == BUY:
            lots.append([size, price, timestamp])
            continue

        remaining = size
        while remaining > 1e-9 and lots:
            lot = lots[0]
            matched = min(lot[0], remaining)
            lot[0] -= matched
            remaining -= matched
            if lot[0] <= 1e-9:
                lots.pop(0)

    return [(float(size), float(price), opened_at) for size, price, opened_at in lots if size > 1e-9]


def _infer_fill_price(response: dict[str, Any], side: str) -> float | None:
    direct = _extract_first_float(response, ["price", "avgPrice", "average_price", "averagePrice"])
    if direct is not None:
        return direct

    making = _extract_first_float(response, ["makingAmount", "making_amount", "makerAmount"])
    taking = _extract_first_float(response, ["takingAmount", "taking_amount", "takerAmount"])
    if making is None or taking is None or making == 0 or taking == 0:
        return None
    return making / taking if side == BUY else taking / making


def _extract_first_float(payload: Any, keys: list[str]) -> float | None:
    if isinstance(payload, dict):
        for key in keys:
            if key in payload:
                converted = _to_float(payload[key])
                if converted is not None:
                    return converted
        for value in payload.values():
            converted = _extract_first_float(value, keys)
            if converted is not None:
                return converted
    elif isinstance(payload, list):
        for item in payload:
            converted = _extract_first_float(item, keys)
            if converted is not None:
                return converted
    return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        try:
            return float(cleaned)
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
            return [part.strip() for part in text.split(",") if part.strip()]
    return [value]


def _normalize_outcome_name(value: str) -> str:
    normalized = value.strip().upper()
    mapping = {
        "HIGHER": "UP",
        "LOWER": "DOWN",
        "ABOVE": "UP",
        "BELOW": "DOWN",
        "YES": "YES",
        "NO": "NO",
        "UP": "UP",
        "DOWN": "DOWN",
    }
    return mapping.get(normalized, normalized)



def _extract_strike_price(contract: ActiveContract) -> float | None:
    candidates = [
        contract.raw_market.get("description"),
        contract.raw_market.get("question"),
        contract.raw_market.get("subtitle"),
        contract.raw_market.get("rules"),
    ]
    patterns = [
        r"price to beat[^\d]{0,40}([\d,]+(?:\.\d+)?)",
        r"opening reference price[^\d]{0,40}([\d,]+(?:\.\d+)?)",
        r"above[^\d]{0,20}([\d,]+(?:\.\d+)?)",
        r"below[^\d]{0,20}([\d,]+(?:\.\d+)?)",
    ]
    for text in candidates:
        if not text:
            continue
        s = str(text)
        for pattern in patterns:
            match = re.search(pattern, s, flags=re.IGNORECASE)
            if match:
                try:
                    value = float(match.group(1).replace(',', ''))
                except ValueError:
                    continue
                if value >= 1000.0:
                    return value
    return None

def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1e12:
            timestamp /= 1000.0
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
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
        raise BotConfigError(f"{name} must be a float, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise BotConfigError(f"{name} must be an int, got {raw!r}") from exc


def configure_logging(level: str) -> logging.Logger:
    """Configure console logging (terminal output)."""
    logger = logging.getLogger("polymarket_btc_scalper")
    logger.setLevel(getattr(logging, level, logging.INFO))
    
    # Console handler (terminal)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, level, logging.INFO))
    console_formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    return logger


def setup_file_logger(window_slug: str) -> logging.Logger:
    """Set up file logger for a specific window."""
    # Create logs directory
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    
    # File handler (one file per window)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{window_slug}.log"
    filepath = logs_dir / filename
    
    logger = logging.getLogger("polymarket_btc_scalper")
    
    # Remove any existing file handlers
    for handler in logger.handlers[:]:
        if isinstance(handler, logging.FileHandler):
            handler.close()
            logger.removeHandler(handler)
    
    # Add new file handler
    file_handler = logging.FileHandler(filepath)
    file_handler.setLevel(logging.INFO)
    file_formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)
    
    logger.info("Logging to file: %s", filepath.absolute())
    return logger


def main() -> int:
    global LOGGER
    try:
        config = BotConfig.from_env()
    except BotConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    LOGGER = configure_logging(config.log_level)

    LOGGER.info(
        "Config loaded poll=%.2fs hold=%ss target=%s%% entry_range=[%.2f, %.2f] dry_run=%s",
        config.poll_interval_seconds,
        config.early_bird_close_seconds,
        config.profit_target_pct,
        config.min_entry_price,
        config.max_entry_price,
        config.dry_run,
    )

    feed = BinancePriceFeed(config)
    locator = GammaMarketLocator(config)
    trader = PolymarketTrader(config)
    engine = StrategyEngine(config, locator, trader, feed)
    engine.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
