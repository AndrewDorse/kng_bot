#!/usr/bin/env python3
"""Lightweight real-time BTC price and per-poll volume feed backed by Binance Vision."""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests


BINANCE_VISION_AGG_TRADES_URL = "https://data-api.binance.vision/api/v3/aggTrades"


@dataclass(slots=True)
class BtcPricePoint:
    ts: float
    price: float
    base_volume: float | None = None
    quote_volume: float | None = None
    trade_count: int = 0


class RealtimeBtcPriceFeed:
    """Polls Binance Vision aggregate trades and derives per-poll traded volume."""

    def __init__(self, config) -> None:
        self.config = config
        self.session = requests.Session()
        self._last_poll_ts = 0.0
        self._last_price: float | None = None

    def poll(self) -> BtcPricePoint:
        now = time.time()
        if (
            self._last_price is not None
            and now - self._last_poll_ts < max(0.1, float(self.config.btc_feed_poll_seconds))
        ):
            return BtcPricePoint(ts=now, price=self._last_price, base_volume=0.0, quote_volume=0.0, trade_count=0)

        params = {
            "symbol": self.config.btc_feed_symbol,
            "limit": 1000,
        }
        if self._last_poll_ts > 0:
            params["startTime"] = int(self._last_poll_ts * 1000) + 1
            params["endTime"] = int(now * 1000)

        response = self.session.get(
            BINANCE_VISION_AGG_TRADES_URL,
            params=params,
            timeout=self.config.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()

        if payload:
            last_trade = payload[-1]
            price = float(last_trade["p"])
            base_volume = sum(float(trade["q"]) for trade in payload)
            quote_volume = sum(float(trade["p"]) * float(trade["q"]) for trade in payload)
            trade_count = len(payload)
        else:
            price = self._last_price
            base_volume = 0.0
            quote_volume = 0.0
            trade_count = 0

        if price is None:
            # First poll bootstrap: request the latest aggregate trade to seed price.
            response = self.session.get(
                BINANCE_VISION_AGG_TRADES_URL,
                params={"symbol": self.config.btc_feed_symbol, "limit": 1},
                timeout=self.config.request_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            if not payload:
                raise RuntimeError(f"No Binance aggregate trade returned for {self.config.btc_feed_symbol}")
            price = float(payload[-1]["p"])

        point = BtcPricePoint(
            ts=now,
            price=price,
            base_volume=base_volume,
            quote_volume=quote_volume,
            trade_count=trade_count,
        )
        self._last_poll_ts = now
        self._last_price = point.price
        return point
