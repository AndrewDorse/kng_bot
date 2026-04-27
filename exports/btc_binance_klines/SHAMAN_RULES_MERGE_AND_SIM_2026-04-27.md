# SHAMAN rules update + aggregate simulation

## Merge (discovery top 20 → `PALADIN/shaman_v1_rules.json`)

- **Source:** `exports/btc_binance_klines/SHAMAN_NEW_SIGNALS_MULTI_SLICE.csv`, top **20** rows by `sum_pnl_slices` (60%+ WR on each 1/3/7/14/28d slice; keys not in live at discovery time).
- **Dedup key:** `(timeframe, family, pattern_key, pred)` — same as discovery.
- **Result:** **0** duplicates against the pre-merge file; **20** rules appended.
- **Counts:** was **163** rules → now **183** (**123** × 5m, **60** × 15m).
- **Metadata on new rows:** `wr` = `wr_28d`, `n` = `hits_28d` from the discovery CSV.

## Simulation (`PALADIN/simulate_shaman_fixed_entry.py`)

Model: fixed entry **0.50** payoff; stake **$1.25** if one rule on winning side, else **$1 × n**; **uncapped** notional (`--notional-max 0`). Binance fetch (no static CSV). Combined = 5m + 15m separate bar clocks.

| Window | 5m trades | 5m WR | 5m staked | 5m PnL | 5m ROI on staked | 15m trades | 15m WR | 15m staked | 15m PnL | 15m ROI | **Combined trades** | **Combined WR** | **Combined staked** | **Combined PnL** | **Combined ROI** |
|--------|----------:|------:|----------:|-------:|-----------------:|-----------:|-------:|-----------:|--------:|--------:|--------------------:|----------------:|----------------------:|-----------------:|-----------------:|
| **3d** | 237 | 63.71% | $411 | +$149 | +36.25% | 138 | 68.84% | $253 | +$109 | +43.08% | 375 | 65.60% | $664 | **+$258** | +38.86% |
| **7d** | 616 | 68.67% | $1,143 | +$495 | +43.31% | 291 | 68.04% | $560 | +$226 | +40.36% | 907 | 68.47% | $1,703 | **+$721** | +42.34% |
| **28d** | 2,408 | 64.66% | $4,366 | +$1,540 | +35.27% | 1,163 | 62.34% | $2,229 | +$653 | +29.30% | 3,571 | 63.90% | $5,595 | **+$2,193** | +33.25% |

Commands used:

```text
python PALADIN/simulate_shaman_fixed_entry.py --last-days 3 --notional-max 0
python PALADIN/simulate_shaman_fixed_entry.py --last-days 7 --notional-max 0
python PALADIN/simulate_shaman_fixed_entry.py --last-days 28 --notional-max 0
```
