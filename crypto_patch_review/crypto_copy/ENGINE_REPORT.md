# Brazy Bot — Solana Engine Audit & Implementation Report

## Environment note
This workspace is `/home/workdir/artifacts`. There is **no** `/opt/brazy` and **no** `brazy_bot_fixed_v3.py` or `solana_executor.py` here.
Active code paths inspected:
- `brazy_bot.py`
- `crypto_copy/*` (executor, monitor, copy_engine, scan_ui, storage, snipe)

## Findings confirmed (against this repo)
| Claim | Status here |
|-------|-------------|
| solana_executor.py → NOT_AUTHORIZED | File **absent** in this workspace |
| Jupiter preview-only in bot | Live path exists in `crypto_copy/executor.py` but **gated** by `COPY_LIVE_ENABLED` (default false) |
| /copyadd stores wallets | Yes (profile + copy storage); watcher via `monitor.py` + `/copypoll` |
| Scheduler = reports only | Yes — no crypto scan job in brazy_bot scheduler |
| crypto_copy incomplete | Partially true — now extended with validation + risk gates |

## What was implemented / changed
1. `crypto_copy/tx_validate.py` — decode versioned tx, allowlist programs, reject blocked/unknown floods
2. `crypto_copy/risk_limits.py` — pre-sign checks: size, daily loss, exposure, open positions, slippage, impact, kill switch
3. `crypto_copy/executor.py` — validate **before** simulate/sign; Jupiter `api.jup.ag` + lite fallback
4. `crypto_copy/storage.py` — kill switch **fail-closed** if unreadable
5. `crypto_copy/copy_engine.py` — risk snapshot in `execute_live`
6. Tests: `tests/test_live_gates.py` + existing suite

## Real execution path (code)
```
event → validate_event → risk check → (live?) Jupiter quote → swap tx
  → tx_validate → simulate → sign → send → confirm → fill/PnL
```
Paper path unchanged when `sub.paper=True` or live flag off.

## Risk controls before signing
- COPY_LIVE_ENABLED must be true
- Burner secret + pubkey required for live
- Kill switch (fail closed if storage error)
- max trade SOL, max exposure, max concurrent, daily loss
- slippage bps, price impact %
- Instruction/program validation
- Simulation must pass
- Sell only held token amount

## Tests
```
21 passed
```
All mocked — **no mainnet, no real funds**.

## Still blocking live trading (by design + ops)
1. `COPY_LIVE_ENABLED` must stay **false** until you enable it on VPS
2. Need `JUPITER_API_KEY` (portal.jup.ag) for reliable quotes
3. Need `solders` installed on VPS
4. Continuous watcher: use `/copypoll` or cron (not auto-enabled)
5. Mainnet path **not verified** in this environment (no broadcast performed)
6. If production uses `brazy_bot_fixed_v3.py` / `solana_executor.py`, those files are **not in this workspace** — merge this `crypto_copy` package into that tree manually

## Deploy (manual — you run)
```bash
cd /opt/brazy   # your VPS
# copy crypto_copy/ and merge brazy_bot.py handlers as needed
source venv/bin/activate
pip install solders requests pytest
export COPY_LIVE_ENABLED=false
python -m pytest crypto_copy/tests/ -q
# only after review:
# export COPY_LIVE_ENABLED=true
# systemctl restart brazy
```
