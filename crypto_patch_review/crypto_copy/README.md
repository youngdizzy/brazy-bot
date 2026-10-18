# Brazy Bot — Crypto Copy-Trading (FOMO / KOLScan style)

## What this is
Solana-side package for Brazy Bot:
- FOMO-style leaderboard (24h / 7d / 30d, 8 traders, Next)
- Trader profiles (Copy Trader / Get Alerts / Back / Menu)
- Alert modes: first buy, all buys, buys and sells (notify only)
- Copy wizard with review + confirm (never auto-starts)
- Copy list shows **YOUR** PnL / win rate, not the leader’s
- Paper/sim copy engine by default; live OFF unless explicitly enabled

## Install on VPS
```bash
cd /opt/brazy
# place package next to brazy_bot.py:
#   crypto_copy/   (this folder)
#   brazy_bot.py   (updated)

source venv/bin/activate
pip install -r requirements.txt
# optional for live later: solders cryptography

# .env
# COPY_LIVE_ENABLED=false          # keep false until you accept risk
# SOLANA_TRACKER_API_KEY=          # optional real leaderboard
# DATA_SECRET=long-random-string

systemctl restart brazy
```

## Telegram flow
1. Sol desk → **FOMO**
2. Pick period → trader → **Copy Trader** or **Get Alerts**
3. Wizard → existing or custom rules → REVIEW → CONFIRM
4. **Copy list** = your fills / PnL / win rate
5. **Kill switch ON** pauses all copy for you

## Live execution status
**NOT enabled by default.**  
`CopyEngine` paper-simulates fills. Broadcasting requires:
1. `COPY_LIVE_ENABLED=true`
2. A verified Jupiter quote + sign + send path using your existing burner key
3. Your explicit go-ahead after small-size tests

Do not set live true until those are tested.

## Tests
```bash
cd /opt/brazy
source venv/bin/activate
python -m pytest crypto_copy/tests/test_copy_core.py -q
```

## Data
Per-user under `data/copy/<telegram_id>.json` (subscriptions, fills, alerts, kill switch).

## Live execution (Jupiter)

Env:
```
COPY_LIVE_ENABLED=false
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
JUPITER_QUOTE_URL=https://quote-api.jup.ag/v6/quote
JUPITER_SWAP_URL=https://quote-api.jup.ag/v6/swap
SOL_USD_HINT=150
```

Enable only after:
1. `/wallet key <burner>` imported
2. Paper copy list looks correct
3. Small SOL on burner
4. `COPY_LIVE_ENABLED=true` + restart
5. Copy subscription activated with paper=false (confirm wizard while live flag on)

Commands:
- `/copypoll` — one poll cycle of source wallets
- Kill switch via Copy list UI

Files:
- `executor.py` — Jupiter quote/swap/sign/send/confirm
- `monitor.py` — RPC wallet signature poll + classify
- `snipe.py` — DexScreener new pair filters
