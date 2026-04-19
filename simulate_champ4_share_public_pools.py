from __future__ import annotations

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

RULES = {
    "name": "CHAMP4_6S",
    "description": (
        "Share-normalized CHAMP4. Uses only 6-share clips so results are comparable to other "
        "share-based wallet mimics instead of fixed-dollar sizing."
    ),
    "base_clip_shares": CLIP_SHARES,
    "rules": [
        "At 30s, compute BTC direction from open. Buy 4 clips (24 shares) on BTC direction and 3 clips (18 shares) on the opposite side.",
        "At 240s, recompute BTC direction from open.",
        "If BTC direction agrees with the current PM leader, buy 3 clips (18 shares) on BTC direction and 1 clip (6 shares) on the opposite side.",
        "If BTC direction disagrees with the current PM leader, buy 2 clips (12 shares) on BTC direction and 2 clips (12 shares) on the opposite side.",
        "At 600s, if BTC direction still agrees with the current PM leader and spread >= 0.08, buy 1 clip (6 shares) on the leader.",
        "Every order is exactly 6 shares. Hold to expiry; no sells.",
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
    return "UP" if row.btc_price >= open_row.btc_price else "DOWN"


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
    row30 = nearest_row(rows, 30)
    row240 = nearest_row(rows, 240)
    row600 = nearest_row(rows, 600)

    fills: list[Fill] = []

    side30 = btc_dir(open_row, row30)
    hedge30 = "DOWN" if side30 == "UP" else "UP"
    buy(fills, row30, side30, 4, "entry_btc_dir")
    buy(fills, row30, hedge30, 3, "entry_hedge")

    side240 = btc_dir(open_row, row240)
    hedge240 = "DOWN" if side240 == "UP" else "UP"
    if side240 == row240.leader:
        buy(fills, row240, side240, 3, "mid_agree_press")
        buy(fills, row240, hedge240, 1, "mid_agree_hedge")
    else:
        buy(fills, row240, side240, 2, "mid_disagree_main")
        buy(fills, row240, hedge240, 2, "mid_disagree_hedge")

    if btc_dir(open_row, row600) == row600.leader and abs(row600.up_price - row600.down_price) >= 0.08:
        buy(fills, row600, row600.leader, 1, "late_confirm_press")

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
    OUTDIR.mkdir(parents=True, exist_ok=True)
    windows = load_windows()

    all_fill_rows: list[dict[str, Any]] = []
    pool_summaries: list[dict[str, Any]] = []

    for pool_size in POOL_SIZES:
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
