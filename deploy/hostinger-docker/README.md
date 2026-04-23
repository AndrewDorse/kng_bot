# Hostinger VPS Docker Setup

This folder deploys the **kng_bot3** live bot (`python main.py`) from the **repo root** as build context (`context: ..` in `docker-compose.yml`).

## Files

- `Dockerfile`: production image (full `main.py` + engines, not the minimal KNG3-only image)
- `.env.example`: copy to `.env` for local compose; mirror vars in Hostinger UI
- `docker-compose.yml`: optional local run; defaults **`BOT_STRATEGY_MODE=paladin_v7`**

## What This Image Runs

- Entrypoint: `python main.py`
- Working directory: `/app`
- Writable: `/app/logs`, `/app/exports`

Copied artifacts include **`paladin_v7_live_engine.py`**, **`PALADIN/paladin_v7.py`**, and the rest of the stack listed in the `Dockerfile`.

## Hostinger Docker Manager

- **Repository:** kng_bot3 (this repo)
- **Dockerfile path:** `deploy/hostinger-docker/Dockerfile`
- **Start command:** leave empty (use `CMD` in Dockerfile)

Before live orders: `POLY_DRY_RUN=true`, confirm logs for **PALADIN v7** (`our_pair_cap`, hedge timeout, WS mids, Binance feed).

## Minimal v7-only mirror (optional)

For a **small** image that only runs `paladin_v7`, use the separate **KNG3** repo and its root `Dockerfile`. From kng_bot3, sync runtime files into that checkout:

`powershell -File deploy\sync_kng3_mirror.ps1`

(Path is read from `deploy/KNG3_MIRROR.txt`.)

## Environment

Use `.env.example` here. **`BOT_PALADIN_V7_*`** vars control v7; **`BOT_PALADIN_*`** (without `V7`) apply when `BOT_STRATEGY_MODE=paladin` (v4 ladder).

## Persistent storage

Mount `/app/logs` and optionally `/app/exports` if the host supports volumes.

## Security

Do not commit secrets. Rotate any key that was ever committed.
