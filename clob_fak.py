#!/usr/bin/env python3
"""Parse Polymarket CLOB FAK (and related) order POST responses; optional GET /order confirmation."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

LOGGER = logging.getLogger("polymarket_btc_ladder")


@dataclass(slots=True)
class FakBuyResult:
    ok: bool
    order_id: str
    status: str
    requested_shares: int
    filled_shares: float
    filled_usdc: float
    avg_price: float
    error: str
    raw: dict[str, Any]

    @property
    def matched_any(self) -> bool:
        return self.ok and self.filled_shares > 1e-9


def _f(x: Any) -> float:
    try:
        if x is None or x == "":
            return 0.0
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _decode_fixed_size(raw: Any) -> float:
    """OpenOrder sizes are fixed-point with 6 decimals (see CLOB OpenAPI)."""
    v = _f(raw)
    if v <= 0:
        return 0.0
    return v / 1_000_000.0


def parse_fak_buy_post_response(
    resp: Any,
    *,
    requested_shares: int,
    limit_price: float,
) -> FakBuyResult:
    """Interpret POST /order JSON after submitting a marketable (FAK) buy."""
    if not isinstance(resp, dict):
        return FakBuyResult(
            ok=False,
            order_id="",
            status="",
            requested_shares=requested_shares,
            filled_shares=0.0,
            filled_usdc=0.0,
            avg_price=0.0,
            error="non_dict_response",
            raw={},
        )

    if resp.get("success") is False:
        err = str(resp.get("errorMsg") or resp.get("error") or "success_false")
        return FakBuyResult(
            ok=False,
            order_id=str(resp.get("orderID") or resp.get("order_id") or ""),
            status=str(resp.get("status") or ""),
            requested_shares=requested_shares,
            filled_shares=0.0,
            filled_usdc=0.0,
            avg_price=0.0,
            error=err,
            raw=resp,
        )

    order_id = str(resp.get("orderID") or resp.get("order_id") or "")
    status = str(resp.get("status") or "").lower()
    taking = _f(resp.get("takingAmount"))
    making = _f(resp.get("makingAmount"))

    filled_sh = 0.0
    filled_usdc = 0.0

    # BUY: receive outcome shares (taking), spend USDC (making) — when API populates both.
    if taking > 1e-12 and making >= 0:
        filled_sh = taking
        filled_usdc = making
    elif status == "matched" and requested_shares > 0:
        # Fallback when amounts omitted: assume full fill at limit (slippage unknown).
        filled_sh = float(requested_shares)
        filled_usdc = filled_sh * limit_price
        LOGGER.debug("FAK post: matched but no amounts; assuming full @ limit")

    avg_px = (filled_usdc / filled_sh) if filled_sh > 1e-12 else float(limit_price)

    ok = filled_sh > 1e-12
    if status in {"unmatched"} and filled_sh <= 1e-12:
        ok = False

    return FakBuyResult(
        ok=ok,
        order_id=order_id,
        status=status,
        requested_shares=requested_shares,
        filled_shares=filled_sh,
        filled_usdc=filled_usdc,
        avg_price=avg_px,
        error="",
        raw=resp,
    )


def refine_fak_buy_with_get_order(
    get_order_fn: Any,
    order_id: str,
    *,
    limit_price: float,
    attempts: int = 10,
    delay_sec: float = 0.12,
) -> tuple[float, float, float]:
    """
    Poll GET /order until size_matched > 0 or attempts exhausted.
    Returns (filled_shares, filled_usdc, avg_price).
    """
    if not order_id:
        return 0.0, 0.0, 0.0
    last_sh = 0.0
    last_px = float(limit_price)
    for i in range(max(1, attempts)):
        try:
            od = get_order_fn(order_id)
        except Exception as exc:
            LOGGER.debug("get_order %s attempt %s: %s", order_id[:18], i, exc)
            time.sleep(delay_sec)
            continue
        if not isinstance(od, dict):
            time.sleep(delay_sec)
            continue
        matched = _decode_fixed_size(od.get("size_matched"))
        px = _f(od.get("price")) or float(limit_price)
        last_px = px
        if matched > 1e-9:
            usdc = matched * px
            LOGGER.info(
                "FAK confirm GET /order: matched=%.4f sh @ %.4f (~$%.2f)",
                matched,
                px,
                usdc,
            )
            return matched, usdc, px
        last_sh = matched
        time.sleep(delay_sec)
    if last_sh > 1e-9:
        return last_sh, last_sh * last_px, last_px
    return 0.0, 0.0, 0.0


def fak_buy_with_confirm(
    get_order_fn: Any,
    post_resp: Any,
    *,
    requested_shares: int,
    limit_price: float,
    confirm: bool = True,
) -> FakBuyResult:
    """Parse POST response; optionally refine fill via GET /order."""
    base = parse_fak_buy_post_response(
        post_resp,
        requested_shares=requested_shares,
        limit_price=limit_price,
    )
    if base.filled_shares > 1e-9:
        return base
    if not base.order_id:
        return base
    if not confirm:
        return base
    if base.status not in {"matched", "delayed", "unmatched", "live", ""}:
        return base

    sh, usdc, apx = refine_fak_buy_with_get_order(
        get_order_fn,
        base.order_id,
        limit_price=limit_price,
    )
    if sh <= 1e-9:
        return FakBuyResult(
            ok=False,
            order_id=base.order_id,
            status=base.status or "unconfirmed",
            requested_shares=requested_shares,
            filled_shares=0.0,
            filled_usdc=0.0,
            avg_price=0.0,
            error="no_fill_confirmed",
            raw=base.raw,
        )
    return FakBuyResult(
        ok=True,
        order_id=base.order_id,
        status="matched_confirmed",
        requested_shares=requested_shares,
        filled_shares=sh,
        filled_usdc=usdc,
        avg_price=apx,
        error="",
        raw=base.raw,
    )
