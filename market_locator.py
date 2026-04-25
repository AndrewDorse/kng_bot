#!/usr/bin/env python3
"""Discovers active UP/DOWN markets from Gamma API."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any

import requests

from config import (
    GAMMA_URL, LOGGER, BotConfig, ActiveContract, TokenMarket,
    parse_datetime, parse_jsonish_list,
)
from http_session import create_polymarket_session


def _retry(
    max_attempts=3,
    backoff_base=0.5,
    retryable=(
        requests.RequestException,
        requests.exceptions.SSLError,
    ),
):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except retryable as exc:
                    last_exc = exc
                    if attempt < max_attempts:
                        time.sleep(backoff_base * (2 ** (attempt - 1)))
            raise last_exc
        return wrapper
    return decorator


class GammaMarketLocator:
    def __init__(self, config: BotConfig):
        self.config = config
        self.session = create_polymarket_session()
        self._cached_contract: ActiveContract | None = None
        self._cache_expires_at = 0.0

    def get_active_contract(self) -> ActiveContract | None:
        now = time.time()
        now_dt = datetime.now(timezone.utc)
        if (
            self._cached_contract is not None
            and self._cached_contract.end_time > now_dt
        ):
            cached_start = self._cached_contract.end_time.timestamp() - self.config.window_size_seconds
            if now >= cached_start or now < self._cache_expires_at:
                return self._cached_contract

        contract = self._discover()
        if contract:
            self._cached_contract = contract
            self._cache_expires_at = now + 30.0
        return contract

    @_retry(max_attempts=5, backoff_base=0.75)
    def _discover(self) -> ActiveContract | None:
        now = datetime.now(timezone.utc)
        now_ts = int(now.timestamp())
        window_size = self.config.window_size_seconds
        current_start = (now_ts // window_size) * window_size
        # Always pick the slug for the window epoch that *contains* ``now`` (floor to window_size).
        #
        # Older logic used ``window_pick_current_grace_seconds``: after ``grace`` seconds inside the
        # epoch it requested ``current_start + window_size`` (the *next* Gamma slug). That runs for
        # ~10 minutes of every 15m block while the *current* market is still live — the bot then
        # "moved to the next window" mid-period, reset strategy state, and sat in pre-window while
        # positions stayed on the previous contract.
        target_start = current_start

        slug = f"{self.config.market_slug_prefix}-{target_start}"
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
        end_time = parse_datetime(market.get("endDate") or market.get("endDateIso"))
        if end_time is None or end_time <= now:
            return None

        outcome_names = parse_jsonish_list(market.get("outcomes"))
        token_ids = parse_jsonish_list(market.get("clobTokenIds"))
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
            upper = str(name).strip().upper()
            if upper == "UP":
                up_token = td
            elif upper == "DOWN":
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
