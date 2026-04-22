# Hostinger VPS Docker Setup

This folder is isolated from the current project runtime. It adds only the files needed to deploy the live bot through Hostinger Docker Manager from a GitHub URL.

## Files

- `Dockerfile`: minimal production image for the current bot entrypoint
- `.env.example`: environment variables to define in Hostinger

## What This Image Runs

- Entrypoint: `python main.py`
- Working directory inside container: `/app`
- Writable runtime folders:
  - `/app/logs`
  - `/app/exports`

The image copies only the files required for the live bot:

- `requirements.txt`
- `main.py`
- `config.py`
- `btc15_redeem_engine.py`
- `btc_price_feed.py`
- `market_locator.py`
- `trader.py`
- `signal_analyzer.py`
- `http_session.py`
- `paladin_live_engine.py`, `polymarket_ws.py`
- `PALADIN/paladin_engine.py`, `PALADIN/simulate_paladin_window.py`, `PALADIN/paladin_sim_config.json`

It does not copy local logs, exports, virtualenv files, backups, or analysis scripts into the image.

## Hostinger Docker Manager

Use these values when creating the app from GitHub:

- Repository: this repo
- Dockerfile path: `deploy/hostinger-docker/Dockerfile`
- Start command: leave empty, use Dockerfile default
- Port mapping: none needed

Before switching out of dry-run:

- verify all secrets are set in Hostinger
- keep `POLY_DRY_RUN=true` for the first deployment
- check logs for market discovery, WS/REST mids, and `paladin` (PALADIN v3) startup
- only then flip `POLY_DRY_RUN=false`

## Environment Variables

Set these in Hostinger Docker Manager, not in Git:

- Required:
  - `POLY_PRIVATE_KEY`
  - `POLY_FUNDER`
- Usually needed:
  - `POLY_SIGNATURE_TYPE`
  - `POLY_DRY_RUN`
  - `BOT_STRATEGY_MODE`
- Optional relayer values:
  - `RELAYER_API_KEY`
  - `RELAYER_SECRET`
  - `RELAYER_PASSPHRASE`

Use `.env.example` in this folder as the reference set.

## Persistent Storage

If Hostinger supports host path or named volume mounts, mount these paths so data survives redeploys:

- `/app/logs`
- `/app/exports`

Recommended:

- keep `logs` persistent
- keep `exports` persistent if you want snapshots, reports, or strategy artifacts to survive redeploys

## Strategy Note

Docker Compose defaults to **`BOT_STRATEGY_MODE=paladin`** (PALADIN v3 pair ladder): **10 shares per side** per window, causal ladder pacing, optional relayer envs unchanged. Override with `BOT_PALADIN_*` vars (see `.env.example`).

For **`champ4_6s`** (dual-side hedge, 6-share clips), set `BOT_STRATEGY_MODE=champ4_6s` and `BOT_SHARES_PER_LEVEL=6`.

If you want the perpetual-style runner instead, set `BOT_STRATEGY_MODE=btc_perp15` manually.

For **`volume_scalp_up`**, TP is **not** fixed at 0.99: it is entry anchor + `BOT_VOLUME_SCALP_TP_OFFSET` (capped at 0.99). If you set `BOT_VOLUME_SCALP_TP_OFFSET=10`, that means **+10¢** (normalized from cents).

The live runtime still uses the repo's existing poll-based BTC and order-book access, not a streaming L2 WebSocket plant.
In `volume_t10_hybrid` mode, `main.py` does not attach the signal analyzer.

If you switch to `mimic_lot`, the bot may look for:

- `/app/exports/wallet10_mimic_search.json`

In that case, provide the file through the mounted `/app/exports` volume before starting the container.

## Security

- Do not commit private keys or relayer secrets into GitHub.
- Put secrets only in Hostinger environment variables.
- If any private key has already been committed anywhere in this repo, rotate it before deployment.

## Notes

- This container is for a background worker, not a web service.
- No reverse proxy, domain, or HTTP port is required.
- If Hostinger builds too slowly because the repo is large, the next step would be a root-level `.dockerignore`. That is intentionally not added here to avoid changing the current project layout.
