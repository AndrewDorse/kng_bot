#!/usr/bin/env python3
"""
PALADIN: analyze bucket snapshots (prices + inventory), settlement ROIs, imbalance,
and profit-lock — pure logic for live control and offline simulation.

See STRATEGY_CORE.md for strategy rules; this module implements measurable checks
and helpers. It does not place orders (wire that in trader / sim harness).
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

Side = Literal["up", "down"]


@dataclass(slots=True, frozen=True)
class PaladinParams:
    """Tunable knobs; defaults mirror STRATEGY_CORE.md."""

    disbalance_abs_shares: float = 5.0
    disbalance_fraction_of_max: float = 0.25
    roi_lock_min_each: float = 0.10
    profit_lock_usdc_each_scenario: float = 5.0
    min_clip_shares: float = 5.0
    # ROI profit-lock only after each leg has at least this many shares (STRATEGY_CORE).
    profit_lock_min_shares_per_side: float = 20.0


@dataclass(slots=True, frozen=True)
class BucketSnapshot:
    """One row of incoming telemetry (e.g. per 30s bucket within a 15m window)."""

    bucket_label: str
    pm_up: float
    pm_down: float
    size_up: float
    size_down: float
    avg_up: float
    avg_down: float
    roi_if_up: float | None = None
    roi_if_down: float | None = None

    @property
    def seconds_start(self) -> int | None:
        return parse_bucket_start_seconds(self.bucket_label)


def parse_bucket_start_seconds(bucket_label: str) -> int | None:
    """Parse labels like '000-029' -> 0, '690-719' -> 690."""
    raw = bucket_label.strip()
    if "-" not in raw:
        return None
    left, _, _ = raw.partition("-")
    try:
        return int(left)
    except ValueError:
        return None


def total_cost_usdc(size_up: float, avg_up: float, size_down: float, avg_down: float) -> float:
    return float(size_up) * float(avg_up) + float(size_down) * float(avg_down)


def pnl_if_up_usdc(size_up: float, avg_up: float, size_down: float, avg_down: float) -> float:
    """Settlement PnL in USDC if UP wins (YES pays $1, NO pays $0)."""
    return float(size_up) * 1.0 - total_cost_usdc(size_up, avg_up, size_down, avg_down)


def pnl_if_down_usdc(size_up: float, avg_up: float, size_down: float, avg_down: float) -> float:
    """Settlement PnL in USDC if DOWN wins."""
    return float(size_down) * 1.0 - total_cost_usdc(size_up, avg_up, size_down, avg_down)


def roi_if_up(size_up: float, avg_up: float, size_down: float, avg_down: float) -> float:
    c = total_cost_usdc(size_up, avg_up, size_down, avg_down)
    if c <= 0:
        return 0.0
    return pnl_if_up_usdc(size_up, avg_up, size_down, avg_down) / c


def roi_if_down(size_up: float, avg_up: float, size_down: float, avg_down: float) -> float:
    c = total_cost_usdc(size_up, avg_up, size_down, avg_down)
    if c <= 0:
        return 0.0
    return pnl_if_down_usdc(size_up, avg_up, size_down, avg_down) / c


def max_disbalance_shares(size_up: float, size_down: float, p: PaladinParams) -> float:
    """Lesser of ~5 shares or 25% of largest leg (STRATEGY_CORE)."""
    m = max(float(size_up), float(size_down), 0.0)
    if m <= 0:
        return float(p.disbalance_abs_shares)
    return min(p.disbalance_abs_shares, p.disbalance_fraction_of_max * m)


def share_imbalance(size_up: float, size_down: float) -> float:
    """Signed: positive => more UP than DOWN."""
    return float(size_up) - float(size_down)


def imbalance_within_band(size_up: float, size_down: float, p: PaladinParams) -> bool:
    return abs(share_imbalance(size_up, size_down)) <= max_disbalance_shares(size_up, size_down, p) + 1e-9


def smaller_side(size_up: float, size_down: float) -> Side:
    return "up" if float(size_up) <= float(size_down) else "down"


def profit_lock_triggered(
    size_up: float,
    avg_up: float,
    size_down: float,
    avg_down: float,
    p: PaladinParams,
) -> tuple[bool, str]:
    """
    STRATEGY_CORE: stop when (roi_up >= min AND roi_down >= min, with min book size) OR
    (pnl_if_up >= $ each AND pnl_if_down >= $ each).
    """
    r_up = roi_if_up(size_up, avg_up, size_down, avg_down)
    r_dn = roi_if_down(size_up, avg_up, size_down, avg_down)
    u_pnl = pnl_if_up_usdc(size_up, avg_up, size_down, avg_down)
    d_pnl = pnl_if_down_usdc(size_up, avg_up, size_down, avg_down)

    min_s = float(p.profit_lock_min_shares_per_side)
    roi_book_ok = float(size_up) >= min_s - 1e-9 and float(size_down) >= min_s - 1e-9
    if roi_book_ok and r_up >= p.roi_lock_min_each and r_dn >= p.roi_lock_min_each:
        return True, (
            f"roi_lock both>={p.roi_lock_min_each:.0%} (up={r_up:.4f} down={r_dn:.4f}) "
            f"with each leg>={min_s:.0f} sh"
        )
    usd_thr = float(p.profit_lock_usdc_each_scenario)
    if (
        math.isfinite(usd_thr)
        and usd_thr > 0
        and u_pnl >= usd_thr
        and d_pnl >= usd_thr
    ):
        return (
            True,
            f"usd_lock both>=${usd_thr:.2f} (if_up=${u_pnl:.2f} if_down=${d_pnl:.2f})",
        )
    return False, "active"


@dataclass(slots=True, frozen=True)
class PaladinMetrics:
    bucket_label: str
    pm_up: float
    pm_down: float
    size_up: float
    size_down: float
    avg_up: float
    avg_down: float
    total_cost_usdc: float
    pnl_if_up_usdc: float
    pnl_if_down_usdc: float
    roi_if_up: float
    roi_if_down: float
    share_imbalance: float
    max_disbalance_shares: float
    imbalance_ok: bool
    profit_locked: bool
    profit_lock_reason: str


def analyze_snapshot(row: BucketSnapshot, p: PaladinParams | None = None) -> PaladinMetrics:
    p = p or PaladinParams()
    su, sd = float(row.size_up), float(row.size_down)
    au, ad = float(row.avg_up), float(row.avg_down)
    tc = total_cost_usdc(su, au, sd, ad)
    pu = pnl_if_up_usdc(su, au, sd, ad)
    pd = pnl_if_down_usdc(su, au, sd, ad)
    ru = roi_if_up(su, au, sd, ad)
    rd = roi_if_down(su, au, sd, ad)
    imb = share_imbalance(su, sd)
    max_d = max_disbalance_shares(su, sd, p)
    locked, reason = profit_lock_triggered(su, au, sd, ad, p)
    return PaladinMetrics(
        bucket_label=row.bucket_label,
        pm_up=float(row.pm_up),
        pm_down=float(row.pm_down),
        size_up=su,
        size_down=sd,
        avg_up=au,
        avg_down=ad,
        total_cost_usdc=tc,
        pnl_if_up_usdc=pu,
        pnl_if_down_usdc=pd,
        roi_if_up=ru,
        roi_if_down=rd,
        share_imbalance=imb,
        max_disbalance_shares=max_d,
        imbalance_ok=abs(imb) <= max_d + 1e-9,
        profit_locked=locked,
        profit_lock_reason=reason,
    )


def _f(row: dict[str, str], key: str) -> float:
    return float(row[key].strip())


def row_dict_to_snapshot(d: dict[str, str]) -> BucketSnapshot:
    roi_u = d.get("roi_if_up")
    roi_d = d.get("roi_if_down")
    return BucketSnapshot(
        bucket_label=d["bucket"].strip(),
        pm_up=_f(d, "pm_up"),
        pm_down=_f(d, "pm_down"),
        size_up=_f(d, "size_up"),
        size_down=_f(d, "size_down"),
        avg_up=_f(d, "avg_up"),
        avg_down=_f(d, "avg_down"),
        roi_if_up=float(roi_u) if roi_u not in (None, "") else None,
        roi_if_down=float(roi_d) if roi_d not in (None, "") else None,
    )


def load_bucket_csv(path: Path) -> list[BucketSnapshot]:
    out: list[BucketSnapshot] = []
    with path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            if not row.get("bucket"):
                continue
            out.append(row_dict_to_snapshot(row))
    return out


def iter_bucket_csv(path: Path) -> Iterator[BucketSnapshot]:
    with path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            if not row.get("bucket"):
                continue
            yield row_dict_to_snapshot(row)


@dataclass(slots=True, frozen=True)
class ControlHint:
    """Non-binding suggestion for sim / wiring; refine with price-improvement rules."""

    allow_new_inventory: bool
    rebalance_side: Side | None
    note: str


def control_hint(m: PaladinMetrics, p: PaladinParams | None = None) -> ControlHint:
    """
    Map metrics to coarse control: when locked, do not add; else if skew exceeds band,
    favor buying the smaller side (rebuild other side per STRATEGY_CORE).
    """
    p = p or PaladinParams()
    if m.profit_locked:
        return ControlHint(False, None, m.profit_lock_reason)
    if m.imbalance_ok:
        return ControlHint(True, None, "imbalance_ok")
    side = smaller_side(m.size_up, m.size_down)
    return ControlHint(
        True,
        side,
        f"rebalance_toward_{side} (|imb|={abs(m.share_imbalance):.2f} > max={m.max_disbalance_shares:.2f})",
    )


def assert_roi_close(
    computed: float,
    expected: float | None,
    *,
    tol: float = 0.002,
) -> None:
    if expected is None or math.isnan(expected):
        return
    if abs(computed - expected) > tol:
        raise AssertionError(f"roi mismatch: got {computed:.6f} expected {expected:.6f} (tol={tol})")


def validate_snapshot_against_row(row: BucketSnapshot) -> dict[str, Any]:
    """Optional: compare engine ROIs to precomputed columns in CSV."""
    m = analyze_snapshot(row)
    out: dict[str, Any] = {
        "bucket": row.bucket_label,
        "roi_if_up": m.roi_if_up,
        "roi_if_down": m.roi_if_down,
    }
    assert_roi_close(m.roi_if_up, row.roi_if_up)
    assert_roi_close(m.roi_if_down, row.roi_if_down)
    return out


def apply_buy_fill(
    size_up: float,
    avg_up: float,
    size_down: float,
    avg_down: float,
    *,
    side: Side,
    add_shares: float,
    fill_price: float,
) -> tuple[float, float, float, float]:
    """
    Update inventory after a BUY on `side` at `fill_price` for `add_shares` (simulation).

    Returns new (size_up, avg_up, size_down, avg_down).
    """
    if add_shares <= 0:
        return (size_up, avg_up, size_down, avg_down)
    if side == "up":
        new_sz = size_up + add_shares
        new_avg = (size_up * avg_up + add_shares * fill_price) / new_sz if new_sz > 0 else 0.0
        return (new_sz, new_avg, size_down, avg_down)
    new_sz = size_down + add_shares
    new_avg = (size_down * avg_down + add_shares * fill_price) / new_sz if new_sz > 0 else 0.0
    return (size_up, avg_up, new_sz, new_avg)


def metrics_to_dict(m: PaladinMetrics) -> dict[str, Any]:
    return {
        "bucket": m.bucket_label,
        "pm_up": m.pm_up,
        "pm_down": m.pm_down,
        "size_up": m.size_up,
        "size_down": m.size_down,
        "avg_up": m.avg_up,
        "avg_down": m.avg_down,
        "total_cost_usdc": m.total_cost_usdc,
        "pnl_if_up_usdc": m.pnl_if_up_usdc,
        "pnl_if_down_usdc": m.pnl_if_down_usdc,
        "roi_if_up": m.roi_if_up,
        "roi_if_down": m.roi_if_down,
        "share_imbalance": m.share_imbalance,
        "max_disbalance_shares": m.max_disbalance_shares,
        "imbalance_ok": m.imbalance_ok,
        "profit_locked": m.profit_locked,
        "profit_lock_reason": m.profit_lock_reason,
    }
