# Production integration — brazy_bot_fixed_v3.py

Workspace entry `brazy_bot.py` is **not** interchangeable with `/opt/brazy/brazy_bot_fixed_v3.py`.
This patch adds `crypto_copy/` + a thin shim. Production file stays the entrypoint.

## Defaults (safe)
- COPY_LIVE_ENABLED=false
- AUTO_COPY_MONITOR=false
- No mainnet sends from install

## Steps

```bash
# on VPS
sudo systemctl stop brazy
cd /opt/brazy

# 1) backup
cp -a brazy_bot_fixed_v3.py brazy_bot_fixed_v3.py.bak.$(date +%Y%m%d%H%M)
cp -a data data.bak.$(date +%Y%m%d%H%M) 2>/dev/null || true
# NEVER overwrite .env / .fernet_key

# 2) copy package
unzip -o brazy_vps_integration_patch.zip
# results in crypto_copy/, crypto_integration_shim.py, INSTALL.md

# 3) deps
source venv/bin/activate
pip install -r crypto_copy/requirements-crypto.txt

# 4) edit brazy_bot_fixed_v3.py — minimal hooks (see below)

# 5) tests
export COPY_LIVE_ENABLED=false
export AUTO_COPY_MONITOR=false
PYTHONPATH=/opt/brazy python -m pytest crypto_copy/tests/ -q

# 6) start
sudo systemctl start brazy
sudo systemctl status brazy
```

## Minimal hooks in brazy_bot_fixed_v3.py

Near other imports:
```python
from crypto_integration_shim import (
    register_crypto_handlers,
    maybe_start_copy_monitor,
    handle_crypto_callback,
    CRYPTO_CALLBACK_PREFIXES,
)
```

Inside `main()` after `app = Application.builder()...`:
```python
register_crypto_handlers(app)
```

Before `app.run_polling(...)`:
```python
maybe_start_copy_monitor(
    data_dir=Path("data"),
    owner_ids=OWNER_IDS if "OWNER_IDS" in dir() else [],
    credential_provider=lambda uid: (
        get_user_profile(uid).get("sol_burner_key"),
        get_user_profile(uid).get("sol_wallet"),
    ),
)
```

Inside existing `button_handler` / callback dispatch, **early**:
```python
if any(data.startswith(p) for p in (
    "pos_close_scope_", "pos_cconf_", "pos_view_", "pos_iclose_", "pos_idone_"
)):
    handled = await handle_crypto_callback(
        query, data, user_id, profile,
        update_user_profile, get_user_profile,
        record_crypto_close=record_crypto_close if "record_crypto_close" in dir() else None,
        context=context,
    )
    if handled:
        return
```

Optional: copy `positions_keyboard` rows from workspace `brazy_bot.py` if production lacks the four close buttons.

## Rollback
```bash
sudo systemctl stop brazy
cp -a brazy_bot_fixed_v3.py.bak.TIMESTAMP brazy_bot_fixed_v3.py
# optionally restore data.bak
sudo systemctl start brazy
```

## Origins
- Copy fills: `crypto_copy/pnl.py` → origin=`copied`
- Snipe fills: `scan_ui.last_fill` + handler → origin=`sniped`

## Still blocks safe live
1. Production must set COPY_LIVE_ENABLED=true deliberately
2. Jupiter API key recommended
3. solders installed
4. Wallet decrypt must match public key
5. Mainnet path not verified in build environment
6. Manual merge of UI keyboards if production positions UI differs
