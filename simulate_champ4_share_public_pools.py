from __future__ import annotations

import argparse
import csv
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SNAPSHOT_ROOT = Path("exports/window_price_snapshots_public")
OUTDIR = Path("exports/champ4_share_public_pools")
COMPLETE_MIN_ELAPSED = 840
POOL_SIZES = (100, 200, 300, 800)
CLIP_SHARES = 6
# Matches btc15_redeem_engine.py _champ4_evaluate_tick + CHAMP4_*_DELAY / CHAMP4_LATE_SPREAD_MIN
CHAMP4_ENTRY_SEC = 24
CHAMP4_MID1_SEC = 90
CHAMP4_MID2_SEC = 540
CHAMP4_LATE_SEC = 600
CHAMP4_LATE_SPREAD_MIN = 0.12

RULES = {
    "name": "CHAMP4_6S",
    "description": (
        "Live champ4_6s replay: 6-share clips, BTC direction = (last_btc - open) / open using "
        "the snapshot row at each stage; stages at 24s / 90s / 540s / 600s."
    ),
    "base_clip_shares": CLIP_SHARES,
    "rules": [
        "At >=24s: enqueue 4 clips on BTC-dir side and 2 on opposite (interleaved in live; same row prices here).",
        "At >=90s: 1 BTC-dir clip + 2 opposite clips.",
        "At >=540s: if BTC-dir agrees with PM leader, 1 opposite clip; else 1 BTC-dir + 3 opposite clips.",
        "At >=600s: if BTC-dir agrees with leader and |up-down| >= 0.12, 1 clip on leader.",
        "Hold to settlement; no sells.",
    ],
}


@dataclass(slots=True)
class SnapshotRow:
    slug: str
    title: str
    elapsed_sec: int
    up_price: float
    down_price: float
    btc_price: float

    @property
    def leader(self) -> str:
        return "UP" if self.up_price >= self.down_price else "DOWN"


@dataclass(slots=True)
class Fill:
    elapsed_sec: int
    side: str
    price: float
    shares: int
    spend: float
    reason: str


def round4(value: float) -> float:
    return round(value, 4)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def load_windows() -> list[tuple[str, str, list[SnapshotRow]]]:
    chosen: dict[str, Path] = {}
    for path in sorted(SNAPSHOT_ROOT.glob("*_prices.csv"), key=lambda item: item.stat().st_mtime):
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                first = next(reader, None)
                headers = reader.fieldnames or []
        except OSError:
            continue
        if not first or "btc_price" not in headers:
            continue
        slug = str(first.get("slug") or "")
        if slug.startswith("btc-updown-15m-"):
            chosen[slug] = path

    windows: list[tuple[str, str, list[SnapshotRow]]] = []
    for slug, path in sorted(chosen.items()):
        rows: list[SnapshotRow] = []
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    rows.append(
                        SnapshotRow(
                            slug=slug,
                            title=str(row.get("question") or row.get("title") or slug),
                            elapsed_sec=int(float(row["elapsed_sec"])),
                            up_price=float(row["up_price"]),
                            down_price=float(row["down_price"]),
                            btc_price=float(row["btc_price"]),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
        rows.sort(key=lambda item: item.elapsed_sec)
        if rows and rows[-1].elapsed_sec >= COMPLETE_MIN_ELAPSED:
            windows.append((slug, rows[0].title, rows))
    return windows


def nearest_row(rows: list[SnapshotRow], elapsed_sec: int) -> SnapshotRow:
    return min(rows, key=lambda item: abs(item.elapsed_sec - elapsed_sec))


def btc_dir(open_row: SnapshotRow, row: SnapshotRow) -> str:
    """Aligned with _volume_t10_btc_return: sign of (btc - open) maps to UP/DOWN."""
    o = open_row.btc_price
    if o <= 0:
        return "UP"
    ret = (row.btc_price - o) / o
    return "UP" if ret >= 0 else "DOWN"


def buy(fills: list[Fill], row: SnapshotRow, side: str, clips: int, reason: str) -> None:
    if clips <= 0:
        return
    price = row.up_price if side == "UP" else row.down_price
    if price <= 0:
        return
    for idx in range(clips):
        fills.append(
            Fill(
                elapsed_sec=row.elapsed_sec,
                side=side,
                price=price,
                shares=CLIP_SHARES,
                spend=CLIP_SHARES * price,
                reason=f"{reason}_{idx + 1}",
            )
        )


def simulate_window(rows: list[SnapshotRow]) -> list[Fill]:
    open_row = nearest_row(rows, 0)
    row24 = nearest_row(rows, CHAMP4_ENTRY_SEC)
    row90 = nearest_row(rows, CHAMP4_MID1_SEC)
    row540 = nearest_row(rows, CHAMP4_MID2_SEC)
    row600 = nearest_row(rows, CHAMP4_LATE_SEC)

    fills: list[Fill] = []

    # entry: 4 main + 2 hedge on BTC direction at first entry tick (live: >=24s)
    btc_side = btc_dir(open_row, row24)
    hedge_side = "DOWN" if btc_side == "UP" else "UP"
    buy(fills, row24, btc_side, 4, "champ4|entry|main")
    buy(fills, row24, hedge_side, 2, "champ4|entry|hedge")

    # mid1: 1 + 2
    btc_side = btc_dir(open_row, row90)
    hedge_side = "DOWN" if btc_side == "UP" else "UP"
    buy(fills, row90, btc_side, 1, "champ4|mid1|main")
    buy(fills, row90, hedge_side, 2, "champ4|mid1|hedge")

    # mid2
    btc_side = btc_dir(open_row, row540)
    leader = row540.leader
    hedge_side = "DOWN" if btc_side == "UP" else "UP"
    if btc_side == leader:
        buy(fills, row540, hedge_side, 1, "champ4|mid2|agree_hedge")
    else:
        buy(fills, row540, btc_side, 1, "champ4|mid2|disagree|main")
        buy(fills, row540, hedge_side, 3, "champ4|mid2|disagree|hedge")

    # late: leader clip if agree + spread
    btc_side = btc_dir(open_row, row600)
    leader = row600.leader
    spread = abs(row600.up_price - row600.down_price)
    if btc_side == leader and spread >= CHAMP4_LATE_SPREAD_MIN:
        buy(fills, row600, leader, 1, "champ4|late_confirm")

    fills.sort(key=lambda item: (item.elapsed_sec, item.reason))
    return fills


def summarize_window(pool: str, slug: str, title: str, rows: list[SnapshotRow], fills: list[Fill]) -> dict[str, Any]:
    winner = "UP" if rows[-1].up_price >= rows[-1].down_price else "DOWN"
    spend = sum(fill.spend for fill in fills)
    up_shares = sum(fill.shares for fill in fills if fill.side == "UP")
    down_shares = sum(fill.shares for fill in fills if fill.side == "DOWN")
    pnl = (up_shares - spend) if winner == "UP" else (down_shares - spend)
    roi = pnl / spend if spend else 0.0
    return {
        "pool": pool,
        "slug": slug,
        "title": title,
        "winner": winner,
        "bars": len(rows),
        "last_elapsed_sec": rows[-1].elapsed_sec,
        "deal_count": len(fills),
        "spend_usdc": round4(spend),
        "up_shares": up_shares,
        "down_shares": down_shares,
        "realized_pnl_usdc": round4(pnl),
        "realized_roi": round4(roi),
    }


def summarize_pool(pool_name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    rois = [float(row["realized_roi"]) for row in rows]
    pnls = [float(row["realized_pnl_usdc"]) for row in rows]
    spends = [float(row["spend_usdc"]) for row in rows]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    return {
        "strategy": RULES["name"],
        "pool": pool_name,
        "window_count": len(rows),
        "total_spend_usdc": round4(sum(spends)),
        "total_pnl_usdc": round4(sum(pnls)),
        "gross_win_pnl": round4(sum(wins)),
        "gross_loss_pnl": round4(sum(losses)),
        "avg_roi": round4(statistics.mean(rois)),
        "median_roi": round4(statistics.median(rois)),
        "positive_windows": sum(1 for value in pnls if value > 0),
        "win_rate": round4(sum(1 for value in pnls if value > 0) / len(rows)),
        "avg_spend_per_window": round4(statistics.mean(spends)),
        "avg_win_pnl": round4(sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss_pnl": round4(sum(losses) / len(losses)) if losses else 0.0,
        "profit_factor": round4(sum(wins) / abs(sum(losses))) if losses else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay champ4_6s (6-share clips) on public window snapshots.")
    parser.add_argument(
        "--pool-sizes",
        type=int,
        nargs="+",
        default=None,
        metavar="N",
        help="Pool sizes to run (tail of loaded windows). Default: 100,200,300,800 plus full set.",
    )
    args = parser.parse_args()

    OUTDIR.mkdir(parents=True, exist_ok=True)
    windows = load_windows()
    nwin = len(windows)
    if args.pool_sizes:
        pool_sizes = sorted({min(s, nwin) for s in args.pool_sizes if s > 0})
    else:
        pool_sizes = sorted({min(s, nwin) for s in [*POOL_SIZES, nwin]}) if windows else []

    all_fill_rows: list[dict[str, Any]] = []
    pool_summaries: list[dict[str, Any]] = []

    for pool_size in pool_sizes:
        subset = windows[-pool_size:]
        window_rows: list[dict[str, Any]] = []
        fill_rows: list[dict[str, Any]] = []

        for slug, title, rows in subset:
            fills = simulate_window(rows)
            window_rows.append(summarize_window(f"last_{pool_size}", slug, title, rows, fills))
            for fill in fills:
                fill_rows.append(
                    {
                        "pool": f"last_{pool_size}",
                        "slug": slug,
                        "title": title,
                        "elapsed_sec": fill.elapsed_sec,
                        "side": fill.side,
                        "price": round4(fill.price),
                        "shares": fill.shares,
                        "spend": round4(fill.spend),
                        "reason": fill.reason,
                    }
                )

        summary = summarize_pool(f"last_{pool_size}", window_rows)
        pool_summaries.append(summary)
        all_fill_rows.extend(fill_rows)
        write_csv(OUTDIR / f"champ4_6s_last_{pool_size}_per_window.csv", window_rows)

    write_csv(OUTDIR / "champ4_6s_fills.csv", all_fill_rows)
    write_csv(OUTDIR / "champ4_6s_pool_summaries.csv", pool_summaries)
    (OUTDIR / "champ4_6s_rules.json").write_text(json.dumps(RULES, indent=2), encoding="utf-8")
    (OUTDIR / "champ4_6s_pool_summaries.json").write_text(json.dumps(pool_summaries, indent=2), encoding="utf-8")
    print(json.dumps(pool_summaries, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
