# Brazy Bot — Final Integration Audit (pre-VPS)

## 1. Three close controls — implementation paths

| Control | UI label / callback | Selection | Confirm | Execute | PnL/state |
|--------|---------------------|-----------|---------|---------|-----------|
| Close All Copied | `Close All Copied (N)` → `pos_close_scope_copied` | `filter_open(..., SCOPE_COPIED)` origin=copied or leader_wallet | `pos_cconf_copied_track` / `_live` | `close_tracker` or `close_live_each` → `executor.sell_token_pct` | `record_crypto_close` + status=closed |
| Close All Sniped | `Close All Sniped (N)` → `pos_close_scope_sniped` | `filter_open(..., SCOPE_SNIPED)` origin=sniped | `pos_cconf_sniped_*` | same | same |
| CLOSE ALL | `CLOSE ALL (N)` → `pos_close_scope_all` | all open bot-tracked (id/origin) | `pos_cconf_all_*` | same | same |

**Files / functions**
- UI keyboard: `brazy_bot.py` → `positions_keyboard`
- Callbacks: `brazy_bot.py` → `_button_dispatch` (`pos_close_scope_*`, `pos_cconf_*`)
- Selection + sell loop: `crypto_copy/close_positions.py` → `filter_open`, `confirm_text`, `close_tracker`, `close_live_each`
- Live sell: `crypto_copy/executor.py` → `sell_token_pct` → `execute_swap`
- Copy origin tagging: `crypto_copy/pnl.py` → `apply_fill_to_positions` sets `origin: "copied"`

Does **not** delete copy-trader subscriptions (`storage` subscriptions untouched).

## 2. Changed files (this workspace)
- `brazy_bot.py`
- `crypto_copy/close_positions.py`
- `crypto_copy/pnl.py`
- `crypto_copy/automation.py`
- `crypto_copy/executor.py`
- `crypto_copy/tx_validate.py`
- `crypto_copy/risk_limits.py`
- `crypto_copy/scan_ui.py`
- `crypto_copy/tests/test_live_gates.py`
- `crypto_copy/tests/test_copy_core.py`

## 3. Tests
```bash
cd /home/workdir/artifacts
PYTHONPATH=. python3 -m pytest crypto_copy/tests/ -q
```
**Mocked only** — no mainnet, no real keys.

Coverage includes: scope isolation copied vs sniped, CLOSE ALL count, pending double-sell block, live disabled, kill switch, duplicate events.

## 4. Mocked vs real integration
| Verified in workspace | Not verified here |
|----------------------|-------------------|
| Unit tests for close scopes | Production systemd unit |
| Live flag defaults false | `brazy_bot_fixed_v3.py` content |
| Kill switch fail-closed | Real Jupiter/RPC |
| Automation starts only if AUTO_COPY_MONITOR | Wallet on VPS |

## 5. Production integration (manual)

**Reality:** workspace entry is `brazy_bot.py`. VPS historically used `brazy_bot_fixed_v3.py`. **Do not overwrite production blindly.**

### Ordered steps
1. SSH to VPS; `cd /opt/brazy`
2. Backup:
   ```bash
   cp -a brazy_bot_fixed_v3.py brazy_bot_fixed_v3.py.bak.$(date +%Y%m%d)
   cp -a data data.bak.$(date +%Y%m%d)
   ```
3. Copy package:
   ```bash
   # from this zip
   rsync -av crypto_copy/ /opt/brazy/crypto_copy/
   ```
4. **Either:**
   - A) Point systemd `ExecStart` to a merged entry that imports the same handlers, **or**
   - B) Diff-merge close/automation handlers from workspace `brazy_bot.py` into `brazy_bot_fixed_v3.py`
5. Dependencies:
   ```bash
   source venv/bin/activate
   pip install solders requests pytest
   ```
6. Env (keep disabled):
   ```
   COPY_LIVE_ENABLED=false
   AUTO_COPY_MONITOR=false
   AUTO_SCAN_DISCOVER=false
   ```
7. Tests on VPS:
   ```bash
   PYTHONPATH=/opt/brazy python -m pytest /opt/brazy/crypto_copy/tests/ -q
   ```
8. Restart only after review:
   ```bash
   systemctl restart brazy
   ```

## 6. Rollback
```bash
systemctl stop brazy
cp -a brazy_bot_fixed_v3.py.bak.YYYYMMDD brazy_bot_fixed_v3.py
# restore data.bak if profiles damaged
systemctl start brazy
```

## 7. Remaining blockers
1. Production entry file not accessible in this workspace — merge required
2. `COPY_LIVE_ENABLED` / mainnet path unproven on real chain
3. Continuous monitor needs `AUTO_COPY_MONITOR=true` + owner user ids wired
4. Sniped origin must be set when opening snipe positions in production entry (workspace tags copy; ensure snipe path sets `origin: "sniped"`)
5. Jupiter API key recommended for production rate limits

## Four distinct operations
1. **Scanning** — discover tokens (no trade)
2. **Trader discovery** — FOMO/KOLScan (no trade)
3. **Copy monitoring** — detect leader txs (no trade until execute)
4. **Trade execution** — Jupiter path only when live + checks pass

Defaults: live **off**, auto-monitor **off**.
