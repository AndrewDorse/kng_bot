#!/usr/bin/env python3
"""PRST1 UP-only: Binance fair vs PM UP mid; $1 market buy; exit on TP (mid−slip) or hold timeout."""

from __future__ import annotations

import logging
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
import requests

from config import ActiveContract, BotConfig, TokenMarket
from market_locator import GammaMarketLocator
from trader import PolymarketTrader

_LOG = logging.getLogger("prst1_up")
_BINANCE_PRICE = "https://api.binance.com/api/v3/ticker/price"
_BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


def configure_prst1_runtime_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.CRITICAL)
    for noisy in (
        "urllib3",
        "requests",
        "charset_normalizer",
        "polymarket_btc_ladder",
        "http_session",
        "trader",
        "market_locator",
        "web3",
        "web3.providers",
    ):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)
        logging.getLogger(noisy).propagate = False

    _LOG.setLevel(logging.INFO)
    _LOG.propagate = False
    if not _LOG.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setLevel(logging.INFO)
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        _LOG.addHandler(h)


def implied_up(btc: float, start: float, sigma: float) -> float:
    x = (btc - start) / max(sigma, 1e-6)
    return max(0.06, min(0.94, 0.5 + 0.44 * math.tanh(x * 1.35)))


def _window_start_ts_from_slug(slug: str) -> int | None:
    m = re.search(r"-(\d+)$", slug.strip())
    return int(m.group(1)) if m else None


def _binance_kline_interval(window_minutes: int) -> str:
    wm = int(window_minutes)
    if wm <= 5:
        return "5m"
    if wm <= 15:
        return "15m"
    return "15m"


def _fetch_btc_spot(session: requests.Session, symbol: str, timeout: float) -> float | None:
    try:
        r = session.get(_BINANCE_PRICE, params={"symbol": symbol.upper()}, timeout=timeout)
        r.raise_for_status()
        px = float(r.json().get("price", 0) or 0.0)
        return px if px > 0 else None
    except Exception as exc:
        _LOG.warning("binance_price_error err=%s", exc)
        return None


def _fetch_window_open_btc(
    session: requests.Session,
    symbol: str,
    window_start_sec: int,
    window_minutes: int,
    timeout: float,
) -> float | None:
    try:
        start_ms = int(window_start_sec) * 1000
        interval = _binance_kline_interval(window_minutes)
        r = session.get(
            _BINANCE_KLINES,
            params={
                "symbol": symbol.upper(),
                "interval": interval,
                "startTime": start_ms,
                "limit": 1,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            return None
        o = float(rows[0][1])
        return o if o > 0 else None
    except Exception as exc:
        _LOG.warning("binance_kline_open_error err=%s", exc)
        return None


def _pm_seconds_remaining(contract: ActiveContract) -> float:
    now = datetime.now(timezone.utc)
    return (contract.end_time - now).total_seconds()


def _up_mid(trader: PolymarketTrader, token_id: str) -> float | None:
    bid = trader.get_best_bid(token_id)
    ask = trader.get_best_ask(token_id)
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if ask is not None and ask > 0:
        return float(ask)
    if bid is not None and bid > 0:
        return float(bid)
    return None


@dataclass(slots=True)
class _OpenLeg:
    up: TokenMarket
    slug: str
    shares: float
    entry_avg: float
    entry_mono: float
    entry_wall: float


class Prst1UpEngine:
    """One-window state: tight-band cheap UP entries, $1 FAK buys, TP or timeout FAK sells."""

    def __init__(
        self,
        config: BotConfig,
        locator: GammaMarketLocator,
        trader: PolymarketTrader,
    ) -> None:
        self.config = config
        self.locator = locator
        self.trader = trader
        self._http = requests.Session()
        self._slug: str | None = None
        self._start_btc: float | None = None
        self._window_trades = 0
        self._next_entry_mono: float = 0.0
        self._leg: _OpenLeg | None = None

    def _log_init(self) -> None:
        bal_s = "n/a"
        try:
            b = self.trader.wallet_balance_usdc()
            bal_s = f"{b:.2f}"
        except Exception as exc:
            bal_s = f"error:{exc!r}"
        c = self.config
        _LOG.info(
            "INIT PRST1_UP version=%s dry_run=%s wallet_usdc=%s window_min=%d "
            "notional=$%.2f oe=%.3f mn=%.3f band=[%.2f,%.2f] sigma=%.1f slip=%.3f "
            "hold_s=%d cd_s=%d max_trades=%d cutoff_rem_s=%.0f poll_s=%.2f btc=%s",
            c.bot_version,
            c.dry_run,
            bal_s,
            int(c.window_minutes),
            float(c.prst1_notional_usdc),
            float(c.prst1_open_edge),
            float(c.prst1_min_net),
            float(c.prst1_band_lo),
            float(c.prst1_band_hi),
            float(c.prst1_sigma),
            float(c.prst1_slip),
            int(c.prst1_max_hold_sec),
            int(c.prst1_cooldown_sec),
            int(c.prst1_max_trades_per_window),
            float(c.strategy_new_order_cutoff_seconds),
            float(c.poll_interval_seconds),
            c.btc_feed_symbol,
        )

    def _reset_window(self, contract: ActiveContract) -> None:
        slug = contract.slug
        ts = _window_start_ts_from_slug(slug)
        sym = self.config.btc_feed_symbol.upper()
        start_btc: float | None = None
        if ts is not None:
            start_btc = _fetch_window_open_btc(
                self._http,
                sym,
                ts,
                int(self.config.window_minutes),
                float(self.config.request_timeout_seconds),
            )
        if start_btc is None or start_btc <= 0:
            start_btc = _fetch_btc_spot(
                self._http, sym, float(self.config.request_timeout_seconds)
            )
        self._slug = slug
        self._start_btc = start_btc
        self._window_trades = 0
        self._next_entry_mono = 0.0
        _LOG.info(
            "WINDOW slug=%s start_btc=%s end=%s",
            slug,
            f"{start_btc:.2f}" if start_btc else "None",
            contract.end_time.isoformat(),
        )

    def _flatten_up(self, tok: TokenMarket, *, reason: str) -> None:
        """Sell all UP shares for this token (best-effort)."""
        tid = tok.token_id
        bal = self.trader.token_balance_allowance_refreshed(tid)
        if bal <= 1e-5:
            _LOG.info("EXIT %s skip no_balance sh=%.6f", reason, bal)
            self._leg = None
            return
        sh = float(bal)
        if self.config.dry_run:
            _LOG.info(
                "EXIT %s DRY_RUN would FAK sell sh=%.4f tid=%s…",
                reason,
                sh,
                tid[:18],
            )
            self._leg = None
            return
        try:
            self.trader.place_marketable_sell(tok, 0.01, sh)
            slug_s = self._leg.slug if self._leg else ""
            _LOG.info("EXIT %s FAK sell sh=%.4f slug=%s", reason, sh, slug_s)
        except Exception as exc:
            _LOG.warning("EXIT %s sell_err=%s", reason, exc)
        self._leg = None

    def _tick_position(self, contract: ActiveContract) -> None:
        assert self._leg is not None
        slip = float(self.config.prst1_slip)
        mn = float(self.config.prst1_min_net)
        max_hold = int(self.config.prst1_max_hold_sec)
        tok = self._leg.up
        tid = tok.token_id
        mid = _up_mid(self.trader, tid)
        if mid is None:
            return
        sell_touch = max(0.005, mid - slip)
        net = sell_touch - self._leg.entry_avg
        elapsed = time.time() - self._leg.entry_wall
        tp_ok = net + 1e-9 >= mn
        to_ok = elapsed >= float(max_hold)
        if tp_ok:
            self._flatten_up(tok, reason="TP")
            self._next_entry_mono = time.monotonic() + float(self.config.prst1_cooldown_sec)
        elif to_ok:
            self._flatten_up(tok, reason="TIMEOUT")
            self._next_entry_mono = time.monotonic() + float(self.config.prst1_cooldown_sec)

    def _try_entry(self, contract: ActiveContract) -> None:
        if self._leg is not None:
            return
        if self._start_btc is None or self._start_btc <= 0:
            return
        rem = _pm_seconds_remaining(contract)
        if rem <= float(self.config.strategy_new_order_cutoff_seconds):
            return
        if self._window_trades >= int(self.config.prst1_max_trades_per_window):
            return
        if time.monotonic() < self._next_entry_mono:
            return
        sym = self.config.btc_feed_symbol.upper()
        spot = _fetch_btc_spot(self._http, sym, float(self.config.request_timeout_seconds))
        if spot is None or spot <= 0:
            return
        imp = implied_up(spot, self._start_btc, float(self.config.prst1_sigma))
        tid = contract.up.token_id
        mid = _up_mid(self.trader, tid)
        if mid is None:
            return
        lo = float(self.config.prst1_band_lo)
        hi = float(self.config.prst1_band_hi)
        oe = float(self.config.prst1_open_edge)
        if not (lo <= mid <= hi):
            return
        if imp - mid < oe:
            return
        notional = float(self.config.prst1_notional_usdc)
        if notional < 0.5:
            return
        if self.config.dry_run:
            _LOG.info(
                "ENTRY DRY_RUN would buy UP usdc=%.2f mid=%.4f imp=%.4f edge=%.4f rem_s=%.0f slug=%s",
                notional,
                mid,
                imp,
                imp - mid,
                rem,
                contract.slug,
            )
            return
        try:
            res = self.trader.place_market_buy_usdc_with_result(
                contract.up,
                notional,
                confirm_get_order=self.config.polymarket_fak_confirm_get_order,
            )
            filled = float(getattr(res, "filled_shares", 0.0) or 0.0)
            avg_px = float(getattr(res, "avg_price", 0.0) or 0.0)
            if filled <= 1e-6 or avg_px <= 0:
                _LOG.warning(
                    "ENTRY buy no_fill slug=%s status=%s",
                    contract.slug,
                    getattr(res, "status", ""),
                )
                return
            self._leg = _OpenLeg(
                up=contract.up,
                slug=contract.slug,
                shares=filled,
                entry_avg=avg_px,
                entry_mono=time.monotonic(),
                entry_wall=time.time(),
            )
            self._window_trades += 1
            _LOG.info(
                "ENTRY BUY UP usdc=%.2f filled_sh=%.4f avg=%.4f imp=%.4f mid=%.4f rem_s=%.0f slug=%s",
                notional,
                filled,
                avg_px,
                imp,
                mid,
                rem,
                contract.slug,
            )
        except Exception as exc:
            _LOG.warning("ENTRY buy_err slug=%s err=%s", contract.slug, exc)

    def run(self) -> None:
        self._log_init()
        while True:
            try:
                contract = self.locator.get_active_contract()
                if contract is None:
                    time.sleep(max(0.5, float(self.config.poll_interval_seconds)))
                    continue
                if self._slug != contract.slug:
                    if self._leg is not None:
                        self._flatten_up(self._leg.up, reason="WINDOW_ROLL")
                    self._reset_window(contract)
                if self._leg is not None:
                    self._tick_position(contract)
                else:
                    self._try_entry(contract)
            except Exception:
                _LOG.exception("PRST1 loop_error (continuing)")
            time.sleep(max(0.2, min(2.0, float(self.config.poll_interval_seconds))))


__all__ = ["Prst1UpEngine", "configure_prst1_runtime_logging"]
