# Paladin v7 — when we buy (first legs & layers)

Order of work **each market second** (`paladin_v7_step`): **(0) hedge** if a pair is open → **(1) imbalance repair** → **(2) layer dips** → **(3) Binance spike first leg**. At most one “new risk” path that sets `pending_second` runs per second (hedge can fill partially across seconds).

---

## Shared vocabulary

- **Flat**: no UP and no DOWN inventory.
- **Balanced**: `|size_up − size_down| ≤ balance_share_tolerance` (default 1 share).
- **Both-sided enough for layers**: `min(size_up, size_down) ≥ max(0, min_shares − balance_share_tolerance)` (e.g. 4.5 vs 5.5 with min_shares 5 and tol 1).
- **Clip size**: `base_order_shares` (capped by `max_shares_per_side` and `min_shares`).

---

## 0) Pending hedge (second leg) — always first

**When:** `pending_second` is set (after any first leg below).

**Side / size:** Opposite side; `sh_need` = first-leg filled shares (can shrink on partial hedge fills).

**Cheap hedge (`v7_hedge_cheap`):**  
Age since first leg `t0` ≥ `cheap_hedge_min_delay_sec` **and**  
`avg_first + opposite_mid + cheap_hedge_slip_buffer ≤` non‑forced cap (`cheap_pair_*` / `_nonforced_pair_cap`).

**Forced hedge (`v7_hedge_forced`):**  
Age ≥ `hedge_timeout_seconds` (default 90s). **Not** blocked by `pm_up + pm_down`.

**Label:** If both cheap and forced are true, cheap wins for the `reason` string.

**Live note:** After API reconcile, `pending_second` may be rebuilt from inventory; the hedge **age clock `t0` is preserved** when the same hedge side is rebuilt so forced can actually fire.

---

## 1) Imbalance repair (not a paired first leg)

**When:** Not flat, not balanced (`|Δ| > tolerance`), and no `pending_second` block ran this second.

**Buy:** Lighter side, up to the gap, if `pm_light + avg(heavy) < imbalance_repair_max_pair_sum` (default 0.97).

**Reason:** `v7_imbalance_repair`. Does **not** set `pending_second`.

---

## 2) Extra layers (only if balanced + both-sided + cooldown)

**When:** Balanced, both-sided enough, and `elapsed − last_completed_pair_elapsed ≥ layer2_cooldown_sec` (min 1s).

**Try in order (first successful buy returns; no fill → try next):**

1. **Higher‑VWAP dip** (`v7_layer2_dip_lead`)  
   - Leg = side with **higher** held VWAP (tie → higher PM mid).  
   - **Condition:** that side’s mid **<** its own VWAP − `layer2_dip_below_avg` (default 0.05).  
   - **Size:** `base_order_shares` (clamped). Then set `pending_second` for the **opposite** hedge.

2. **Lower‑VWAP deep dip** (`v7_layer2_lowvwap_dip`)  
   - Leg = **lower** held VWAP (tie → opposite of PM lead).  
   - **Condition:** that side’s mid **<** its own VWAP − `layer2_low_vwap_dip_below_avg` (default 0.20).  
   - Same clip / pending pattern as (1).

3. *(Not a “dip”; next block)* Binance spike first leg when still allowed (see §3).

---

## 3) Binance spike first leg (`v7_first_binance_spike`)

**When:** `can_open` = flat **or** (balanced **and** both-sided enough).  
**Cooldown:** Unless flat, require `elapsed − last_completed_pair_elapsed ≥ pair_cooldown_sec` (min 1s).

**Market gates (same second):**

- **Volume spike:** this second’s Binance base volume ≥ `volume_spike_ratio × max(volume_floor, rolling_mean_vol over lookback excluding t)`.
- **Price jump:** `|btc_px[t] − btc_px[t−1]| ≥ btc_abs_move_min_usd` (with small fallback window if 1s move is flat).

**Side:** BTC momentum — up move → buy **UP**, down move → buy **DOWN** (`_btc_momentum_side`).

**PM gate:** chosen side’s mid ≤ `first_leg_max_pm` (default 0.62).

**Size:** `base_order_shares` clamped; then set `pending_second` for opposite hedge.

---

## Hedge after any first leg

Same §0 rules for hedges opened by spike, higher‑VWAP layer, or lower‑VWAP layer.

---

*Source of truth: `PALADIN/paladin_v7.py` → `paladin_v7_step`.*
