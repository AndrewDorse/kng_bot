# Deploy (Docker)

**Production Docker for Paladin v7 lives only in the KNG3 mirror repo**, not in `kng_bot3`.

- Canonical checkout: see `KNG3_MIRROR.txt` (`MIRROR_LOCAL_PATH`, usually `C:\Users\Lenovo\Documents\Git\KNG3`).
- To refresh runtime files from `kng_bot3` into that repo:  
  `powershell -File deploy\sync_kng3_mirror.ps1`  
  Then merge any logging changes into **KNG3’s own** `main.py` (sync does not copy monolithic `main.py`).  
  Then commit and push **inside the KNG3 repo** (`docker compose up --build` there).

Do **not** reintroduce a second Docker tree under `kng_bot3/deploy/` for the same bot.
