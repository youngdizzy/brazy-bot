# Brazy Bot — Production Integration Guide

## Access note
This package was built in `/home/workdir/artifacts`.
`/opt/brazy` was **not accessible** from the build environment.
You must apply this package on the VPS yourself.

## What is implemented (code)
1. Single executor path: quote → validate → simulate → sign → send → confirm (`executor.py`)
2. Order state machine (`order_state.py`) — never marks filled on quote alone
3. Min 1.5:1 R:R (`rr.py`) + risk gate
4. Position manager TP/SL/trail evaluate (`position_manager.py`)
5. Copy monitor service (`automation.py`) — starts only if `AUTO_COPY_MONITOR=true`
6. Scan + snipe UI; origin `sniped` / `copied` tagging
7. Four close controls: individual + copied + sniped + all
8. Live **disabled** unless `COPY_LIVE_ENABLED=true`

## Architecture map
```
UI button / command
  → brazy_bot handlers
    → scan_ui / copy_engine / close_positions
      → risk_limits + rr
        → executor.execute_swap / buy_token_sol / sell_token_pct
          → Jupiter quote/swap → tx_validate → sim → sign → RPC send → confirm
            → order state CONFIRMED | FAILED | UNKNOWN_REQUIRES_RECONCILIATION
              → pnl / sol_positions (origin tagged)
```

Copy path:
```
automation tick OR /copypoll
  → monitor wallet sigs
    → process_event → execute_live (if not paper)
      → same executor
```

## Deploy on VPS
```bash
# 1) backup
sudo systemctl stop brazy
cd /opt/brazy
cp -a brazy_bot_fixed_v3.py brazy_bot_fixed_v3.py.bak.$(date +%Y%m%d%H%M)
cp -a data data.bak.$(date +%Y%m%d%H%M)

# 2) install package
unzip -o /path/to/brazy_production_ready.zip -d /opt/brazy/
# merge crypto_copy into /opt/brazy/crypto_copy

# 3) deps
source venv/bin/activate
pip install -r crypto_copy/requirements-crypto.txt

# 4) env — DO NOT overwrite .env with template; merge keys only
# COPY_LIVE_ENABLED=false
# AUTO_COPY_MONITOR=false

# 5) wire entrypoint
# Option A: import crypto_copy from fixed_v3 and register handlers from brazy_bot.py
# Option B: point systemd ExecStart to a merged brazy_bot.py after diff review

# 6) test
PYTHONPATH=/opt/brazy python -m pytest /opt/brazy/crypto_copy/tests/ -q

# 7) start
sudo systemctl start brazy
sudo systemctl status brazy
```

## Rollback
```bash
sudo systemctl stop brazy
cp -a brazy_bot_fixed_v3.py.bak.TIMESTAMP brazy_bot_fixed_v3.py
# restore data.bak if needed
sudo systemctl start brazy
```

## Live smoke (YOU only, tiny cap)
1. Set `COPY_LIVE_ENABLED=true` and `SOL_MAX_BUY=0.001`
2. Restart
3. One manual tiny buy
4. Confirm signature on explorer
5. Set live false again until satisfied

## Not verified from build host
- On-chain live swap
- VPS systemd integration
- Production wallet decrypt path identity with this package
