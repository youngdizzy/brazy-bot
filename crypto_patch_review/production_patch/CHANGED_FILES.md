# Changed / delivered files

## New (patch)
- production_patch/crypto_integration_shim.py
- production_patch/INSTALL.md
- production_patch/CHANGED_FILES.md

## Package (copy to /opt/brazy/)
- crypto_copy/__init__.py
- crypto_copy/executor.py          # shared buy/sell + live gate
- crypto_copy/tx_validate.py
- crypto_copy/risk_limits.py
- crypto_copy/order_state.py
- crypto_copy/rr.py
- crypto_copy/close_positions.py   # four close scopes
- crypto_copy/copy_engine.py
- crypto_copy/monitor.py
- crypto_copy/automation.py        # monitor, default off
- crypto_copy/pnl.py               # origin=copied
- crypto_copy/scan_ui.py           # origin=sniped
- crypto_copy/position_manager.py
- crypto_copy/storage.py
- crypto_copy/models.py
- crypto_copy/snipe.py
- crypto_copy/alerts.py
- crypto_copy/leaderboard.py
- crypto_copy/telegram_ui.py
- crypto_copy/requirements-crypto.txt
- crypto_copy/tests/*

## Workspace reference (optional merge, not required for shim)
- brazy_bot.py — full UI reference for positions_keyboard / stocks modes

## Do NOT overwrite on VPS
- .env
- .fernet_key
- data/
- brazy_bot_fixed_v3.py (edit hooks only; keep .bak)
