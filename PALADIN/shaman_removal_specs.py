"""Shared removal keys for ``organize_shaman_v1_rule_lists.py`` (week WR < 60%)."""

from __future__ import annotations

# 15m: (family, pattern_key, pred, week_wr, hits, wins) — REPORT_15m_rules_last_week.md
REMOVED_15M_SPEC: list[tuple[str, str, str, float, int, int]] = [
    ("B_rg_suffix_lastTok", "RR|last=RSm", "G", 0.5833, 12, 7),
    ("B_rg_suffix_lastTok", "GRGR|last=RSl", "G", 0.5833, 12, 7),
    ("B_rg_suffix_lastTok", "RGRG|last=GLl", "R", 0.5714, 7, 4),
    ("D_rg_rng_lastTok", "GG|last=GSl|rng=n", "R", 0.5000, 24, 12),
    ("D_rg_rng_lastTok", "RGG|last=GMl|rng=w", "R", 0.5000, 10, 5),
    ("D_rg_rng_lastTok", "GRGG|last=GSl|rng=n", "R", 0.5000, 6, 3),
    ("D_rg_rng_lastTok", "GRGR|last=RSl|rng=n", "G", 0.5000, 6, 3),
    ("B_rg_suffix_lastTok", "GGRGR|last=RSl", "R", 0.5000, 4, 2),
    ("C_rg_suffix_last2Tok", "GR|t-1=GSl|t=RMl", "G", 0.4444, 9, 4),
    ("A_token_chain", "GSl>RMl", "G", 0.4444, 9, 4),
    ("D_rg_rng_lastTok", "GRG|last=GLh|rng=W", "G", 0.4286, 7, 3),
    ("D_rg_rng_lastTok", "RGG|last=GSl|rng=n", "R", 0.4167, 12, 5),
    ("A_token_chain", "RLh>RLh", "G", 0.3333, 6, 2),
    ("C_rg_suffix_last2Tok", "RR|t-1=RLh|t=RLh", "G", 0.3333, 6, 2),
]

# 5m: REPORT_5m_rules_last_week.md (week WR < 60%)
REMOVED_5M_SPEC: list[tuple[str, str, str, float, int, int]] = [
    ("D_rg_rng_lastTok", "GRRGG|last=GLl|rng=W", "G", 0.2500, 4, 1),
    ("B_rg_suffix_lastTok", "RGRGR|last=RSl", "G", 0.4615, 13, 6),
    ("C_rg_suffix_last2Tok", "GGG|t-1=GSl|t=GSl", "R", 0.4737, 19, 9),
    ("D_rg_rng_lastTok", "GGRRR|last=RLh|rng=W", "G", 0.5000, 10, 5),
    ("B_rg_suffix_lastTok", "RGGRG|last=GMl", "R", 0.5000, 12, 6),
    ("D_rg_rng_lastTok", "RGRG|last=GLl|rng=W", "G", 0.5000, 14, 7),
    ("C_rg_suffix_last2Tok", "GG|t-1=GSl|t=GLl", "G", 0.5000, 20, 10),
    ("A_token_chain", "GSl>GLl", "G", 0.5000, 20, 10),
    ("D_rg_rng_lastTok", "GRGR|last=RSl|rng=n", "G", 0.5000, 22, 11),
    ("D_rg_rng_lastTok", "GGRG|last=GSl|rng=n", "R", 0.5161, 31, 16),
    ("B_rg_suffix_lastTok", "GRGR|last=RSl", "G", 0.5172, 29, 15),
    ("B_rg_suffix_lastTok", "GRGRR|last=RSl", "G", 0.5333, 15, 8),
    ("D_rg_rng_lastTok", "GRG|last=GLl|rng=W", "G", 0.5500, 20, 11),
    ("D_rg_rng_lastTok", "GG|last=GMh|rng=W", "R", 0.5556, 9, 5),
    ("B_rg_suffix_lastTok", "GRGGG|last=GSl", "R", 0.5556, 18, 10),
    ("B_rg_suffix_lastTok", "GGGR|last=RLl", "G", 0.5556, 27, 15),
    ("B_rg_suffix_lastTok", "RGRG|last=GSl", "G", 0.5556, 27, 15),
    ("C_rg_suffix_last2Tok", "RGR|t-1=GMl|t=RSl", "G", 0.5625, 16, 9),
    ("B_rg_suffix_lastTok", "GGRGR|last=RSl", "G", 0.5625, 16, 9),
    ("D_rg_rng_lastTok", "RRGRR|last=RLl|rng=W", "G", 0.5714, 7, 4),
    ("D_rg_rng_lastTok", "GRR|last=RMl|rng=n", "R", 0.5789, 19, 11),
    ("D_rg_rng_lastTok", "RG|last=GMm|rng=w", "R", 0.5833, 12, 7),
    ("D_rg_rng_lastTok", "GGR|last=RLl|rng=W", "G", 0.5833, 24, 14),
    ("C_rg_suffix_last2Tok", "RRR|t-1=RSl|t=RSl", "R", 0.5909, 22, 13),
    ("B_rg_suffix_lastTok", "RR|last=RSm", "G", 0.5926, 27, 16),
    ("D_rg_rng_lastTok", "GRR|last=RLl|rng=W", "R", 0.5926, 27, 16),
]
