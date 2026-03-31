from __future__ import annotations

import logging
import math

LOGGER = logging.getLogger("polymarket_btc_scalper")


class DominanceStrategy:
    """Late-window dominance strategy on the real current window."""

    def __init__(self) -> None:
        self.window_slug: str | None = None
        self.entered = False
        self.safe_sell_triggered = False

    def reset_for_window(self, contract) -> None:
        self.window_slug = contract.slug
        self.entered = False
        self.safe_sell_triggered = False

    def _handle_safe_sell(self, engine, contract, positions, open_orders) -> bool:
        live_positions = [p for p in positions if p.shares >= 1.0]
        if not live_positions:
            return False

        # Cancel outstanding buy orders for this contract before panic selling.
        for order in open_orders:
            side = str(order.get("side") or "").upper()
            order_id = str(order.get("id") or order.get("orderID") or "")
            if side == "BUY" and order_id:
                try:
                    engine.trader.cancel_order(order_id)
                except Exception as exc:
                    LOGGER.debug("[DOMINANCE SAFE SELL] cancel buy failed %s | %s", order_id, exc)

        triggered = False
        for position in live_positions:
            token = contract.up if position.side_label == "UP" else contract.down
            current_price = 0.0
            try:
                current_price = engine.trader.get_token_price(token.token_id)
            except Exception as exc:
                LOGGER.debug("[DOMINANCE SAFE SELL] price fetch failed %s | %s", token.token_id, exc)
                continue

            if current_price >= 0.77:
                continue

            triggered = True
            LOGGER.warning(
                "[DOMINANCE SAFE SELL] %s | side=%s | current=$%.3f < 0.77 | shares=%.4f",
                contract.slug,
                position.side_label,
                current_price,
                position.shares,
            )
            try:
                engine._close_position(position)
                self.safe_sell_triggered = True
            except Exception as exc:
                LOGGER.error("[DOMINANCE SAFE SELL FAILED] %s | side=%s | %s", contract.slug, position.side_label, exc)

        return triggered

    def evaluate(self, engine, contract, btc_price, positions, open_orders, now_ts, elapsed_second, seconds_remaining, seconds_to_start) -> None:
        if self.window_slug != contract.slug:
            self.reset_for_window(contract)

        # Risk protection stays active every tick: if a live dominance-window position drops below 0.77,
        # market-sell it immediately and let the rest of the bot continue normally.
        self._handle_safe_sell(engine, contract, positions, open_orders)

        if self.entered:
            return
        if elapsed_second < 238:
            return
        if elapsed_second > 299:
            return

        # do not stack another dominance entry if there is already a significant live position in this window
        if any(p.shares >= 1.0 for p in positions):
            LOGGER.info("[DOMINANCE SKIP] %s | live position already exists", contract.slug)
            self.entered = True
            return

        up_price = engine._entry_limit_price(contract.up)
        down_price = engine._entry_limit_price(contract.down)
        if up_price is None or down_price is None:
            LOGGER.warning("[DOMINANCE SKIP] %s | could not resolve both side prices | up=%s down=%s", contract.slug, up_price, down_price)
            return

        direction = "UP" if up_price >= down_price else "DOWN"
        dominant_price = up_price if direction == "UP" else down_price
        if dominant_price < 0.85:
            LOGGER.info("[DOMINANCE SKIP] %s | dominant=%s | price=$%.3f < 0.85", contract.slug, direction, dominant_price)
            self.entered = True
            return

        try:
            _, balance = engine.trader.get_all_balances()
        except Exception as exc:
            LOGGER.warning("[DOMINANCE BALANCE] %s | failed to get balance: %s", contract.slug, exc)
            balance = 0.0

        share_count = max(6, int(round(balance * 0.8)))
        buy_price = 0.92
        tp_price = 0.99

        LOGGER.info(
            "[DOMINANCE ENTRY CHECK] %s | elapsed=%ds | BTC=$%.2f | UP=$%.3f | DOWN=$%.3f | dominant=%s | dominant_price=$%.3f | buy_limit=$%.2f | tp=$%.2f | shares=%d",
            contract.slug,
            elapsed_second,
            btc_price,
            up_price,
            down_price,
            direction,
            dominant_price,
            buy_price,
            tp_price,
            share_count,
        )

        ok = engine._place_dominance_limit_buy(
            contract=contract,
            side_label=direction,
            price=buy_price,
            share_count=share_count,
            tp_price=tp_price,
            open_orders=open_orders,
        )
        self.entered = True
        if not ok:
            LOGGER.info("[DOMINANCE BUY NOT PLACED] %s", contract.slug)
