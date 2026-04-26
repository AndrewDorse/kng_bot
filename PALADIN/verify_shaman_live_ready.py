#!/usr/bin/env python3
"""Preflight: live rules JSON shape, dedupe keys, no removal-spec collisions, engine splits 5m/15m."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[1]
PAL = REPO / "PALADIN"
LIVE = PAL / "shaman_v1_rules.json"

REQUIRED = ("timeframe", "family", "pattern_key", "pred", "wr", "n")


def main() -> int:
    sys.path.insert(0, str(REPO))
    from shaman_removal_specs import REMOVED_15M_SPEC, REMOVED_5M_SPEC  # noqa: PLC0415

    rows: list[dict] = json.loads(LIVE.read_text(encoding="utf-8"))
    keys = [
        (
            str(r["timeframe"]).strip(),
            str(r["family"]).strip(),
            str(r["pattern_key"]).strip(),
            str(r["pred"]).strip(),
        )
        for r in rows
    ]
    dup = [k for k, v in Counter(keys).items() if v > 1]
    if dup:
        print(f"FAIL: {len(dup)} duplicate keys (first {dup[0]})", file=sys.stderr)
        return 1

    bad_spec: list[tuple] = []
    for r in rows:
        tf = str(r["timeframe"]).strip()
        sk = (
            str(r["family"]).strip(),
            str(r["pattern_key"]).strip(),
            str(r["pred"]).strip(),
        )
        if tf == "5m" and sk in {(a[0], a[1], a[2]) for a in REMOVED_5M_SPEC}:
            bad_spec.append((tf,) + sk)
        if tf == "15m" and sk in {(a[0], a[1], a[2]) for a in REMOVED_15M_SPEC}:
            bad_spec.append((tf,) + sk)
    if bad_spec:
        print(f"FAIL: {len(bad_spec)} live rows match shaman_removal_specs", file=sys.stderr)
        return 1

    for i, r in enumerate(rows):
        miss = [k for k in REQUIRED if k not in r]
        if miss:
            print(f"FAIL: row {i} missing {miss}", file=sys.stderr)
            return 1
        try:
            float(r["wr"])
            int(r["n"])
        except (TypeError, ValueError) as exc:
            print(f"FAIL: row {i} wr/n invalid: {exc}", file=sys.stderr)
            return 1

    cfg = MagicMock()
    cfg.shaman_v1_rules_path = ""
    from shaman_v1_engine import ShamanV1Engine  # noqa: PLC0415

    eng = ShamanV1Engine(cfg, MagicMock(), MagicMock())
    n5 = len(eng._rules_5m)
    n15 = len(eng._rules_15m)
    if n5 + n15 != len(rows):
        print(f"FAIL: split {n5}+{n15} != {len(rows)}", file=sys.stderr)
        return 1

    print(f"OK: {len(rows)} live rules ({n5} x 5m, {n15} x 15m); dedupe + spec + schema + engine split")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
