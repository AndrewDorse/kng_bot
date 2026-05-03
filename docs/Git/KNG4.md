# KNG4 (PRST1) — Docker and repo role

**Checkout:** `C:\Users\Lenovo\Documents\Git\KNG4` (or your own clone).

**What it runs:** **PRST1** — implied-fair vs CLOB **UP** scalp (`prst1/`), **not** the streak→cheap rule. Default **15m-only** lane (`PRST1_WINDOW_MINUTES=15`); set `5,15` for dual lanes. Env vars `PRST1_*` (see `.env.example`).

## Docker (go live)

```powershell
cd C:\Users\Lenovo\Documents\Git\KNG4
copy .env.example .env
# fill POLY_PRIVATE_KEY, POLY_FUNDER, relayer fields; keep POLY_DRY_RUN=true until validated
docker compose --env-file .env build
docker compose --env-file .env up -d
docker compose logs -f prst1
```

Image name in compose: `kng4-prst1:local`.

## Strategy numbering (do not confuse repos)

| Label | Repo | Rule (short) |
|--------|------|----------------|
| **Strategy-1** (streak→cheap, **current**) | **KNG6** | **12** s `max(up,down) ≥ 0.76`, then first leg **≤ 0.19**, **$1** FAK once per slug (`KNG6_*` env). |
| PRST1 scalp | **KNG4** (this doc) | `PRST1_OPEN_EDGE`, band, TP / time-stop (`PRST1_*`). |

Research scripts in **`kng_bot3`** (`PALADIN/sim_streak076_last_n_sheet.py`, `sim_streak076_sweep_last_n.py`) align **strategy-1** with **KNG6** defaults.
