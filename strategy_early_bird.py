from __future__ import annotations

import logging

LOGGER = logging.getLogger("polymarket_btc_scalper")


class EarlyBirdStrategy:
    def __init__(self) -> None:
        self.window_slug: str | None = None
        self._last_balanced_side: dict[str, str] = {}

    def reset_for_window(self, contract) -> None:
        self.window_slug = contract.slug
        self._last_balanced_side.pop(contract.slug, None)

    def evaluate(
        self,
        engine,
        contract,
        btc_price,
        positions,
        open_orders,
        now_ts,
        elapsed_second,
        seconds_remaining,
        seconds_to_start,
    ) -> None:
        if self.window_slug != contract.slug:
            self.reset_for_window(contract)

        snapshot = engine._get_fullset_snapshot(contract, positions, open_orders)
        if snapshot is None:
            LOGGER.info("[FULLSET WAIT] %s | no valid live prices yet", contract.slug)
            return

        if snapshot.get("window_stopped"):
            LOGGER.info("[FULLSET WAIT] %s | window stopped", contract.slug)
            return

        pending_up_orders = int(snapshot.get("pending_up_orders", 0))
        pending_down_orders = int(snapshot.get("pending_down_orders", 0))
        pending_total = pending_up_orders + pending_down_orders

        live_up = int(snapshot.get("live_up_int", 0))
        live_down = int(snapshot.get("live_down_int", 0))
        step_up = int(snapshot.get("live_step_up", 0))
        step_down = int(snapshot.get("live_step_down", 0))

        avg_sum = snapshot.get("avg_sum")
        if step_up == step_down and live_up > 0 and live_down > 0 and avg_sum is not None and float(avg_sum) < 0.96:
            LOGGER.info("[FULLSET WAIT] %s | balanced lock avg_sum=%.4f", contract.slug, float(avg_sum))
            return

        if step_up != step_down:
            LOGGER.info("[FULLSET WAIT] %s | live inventory not balanced | step_up=%d | step_down=%d", contract.slug, step_up, step_down)
            return

        if pending_total >= 2:
            LOGGER.info("[FULLSET WAIT API] %s | balanced pending cap | pending_up=%d | pending_down=%d", contract.slug, pending_up_orders, pending_down_orders)
            return

        if pending_up_orders > 0 and pending_down_orders == 0:
            order_plan = [("DOWN", float(snapshot["down_buy_price"]), "balanced_down_fill")]
        elif pending_down_orders > 0 and pending_up_orders == 0:
            order_plan = [("UP", float(snapshot["up_buy_price"]), "balanced_up_fill")]
        else:
            last_side = self._last_balanced_side.get(contract.slug)
            first_side = "DOWN" if last_side == "UP" else "UP"
            second_side = "DOWN" if first_side == "UP" else "UP"
            order_plan = [
                (first_side, float(snapshot["up_buy_price"] if first_side == "UP" else snapshot["down_buy_price"]), f"balanced_{first_side.lower()}"),
                (second_side, float(snapshot["up_buy_price"] if second_side == "UP" else snapshot["down_buy_price"]), f"balanced_{second_side.lower()}"),
            ]

        opened_sides: list[str] = []
        for chosen_side, chosen_price, reason in order_plan:
            allowed, why = engine._can_place_fullset_pending_order(contract, chosen_side, open_orders, positions)
            if not allowed:
                LOGGER.info("[FULLSET WAIT API] %s | blocked by guard | side=%s | %s", contract.slug, chosen_side, why)
                continue

            ok = engine._place_fullset_limit_buy(
                contract=contract,
                side_label=chosen_side,
                price=chosen_price,
                share_count=engine._fullset_order_shares(),
                open_orders=open_orders,
                positions=positions,
                reason=reason,
            )
            if ok:
                opened_sides.append(chosen_side)
                LOGGER.info("[FULLSET OPEN] %s | side=%s | price=%.2f | reason=%s | live_up=%d | live_down=%d", contract.slug, chosen_side, chosen_price, reason, live_up, live_down)
            else:
                LOGGER.info("[FULLSET WAIT API] %s | place guard blocked | side=%s", contract.slug, chosen_side)

        if step_up == step_down and opened_sides:
            self._last_balanced_side[contract.slug] = opened_sides[-1]
