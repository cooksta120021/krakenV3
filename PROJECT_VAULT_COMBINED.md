# Project Overview + Vault Model

## Platform Summary (from IDEA.md)
- Django web app where users authenticate, register multiple exchange API keys, and automate spot trading for chosen assets/quotes.
- Evolves current ETH-focused script into a multi-asset, multi-user platform with dashboards, controls, and audit trails.
- Key layers: auth, API key rotation, trading engine, data/observability, and optional ML tuning for strategy thresholds.
- Architecture: Django app + background workers (Celery/RQ) + exchange adapters + Postgres/Redis + encrypted key storage.
- Early milestones: port ETH/USDT logic to workers, add key rotation UI, add multi-asset dashboards, then add ML backtests/tuning.
- Risks: exchange limits, API-key security, regulatory review, model overfitting.

## Current Script Outline (from OUTLINE.md)
- `eth_trading_market.py` automates ETH/USDT with buy/sell cycles, trend filters (1h/4h), trailing exits, and fee-aware P&L.
- Uses Kraken public/private endpoints via `requests` and `krakenex`; loads creds from `.env` (`KRAKEN_API_KEY`, `KRAKEN_PRIVATE_KEY`).
- State tracked: price history (8h), lows/highs, cycle state, balances, 4h trend queue.
- Cadence: ~1.1s price fetch; buy checks every 30s; sell checks every 15s; logging every 60s.
- Key triggers: 1h momentum ≥1.25% to buy (with 4h uptrend ≥0.2%); stop-loss at -1.6% net; trailing sell after ≥0.1% net gain; skip sells during ≥1.1% 1h uptrend.

## Vault Model (new)
Goal: track the true asset total while allocating trade capital into two strategy sleeves. All trading happens through the sleeves; the main vault only allocates/reclaims funds.

If **Tradeable Coins = 0**, Core Vault still reports **Total Coins** but leaves sleeves idle; no interference with holdings.

### Entities
- **Per-Asset Core Vault** (one per wallet/asset, e.g., USD, USDC, USDT)
  - Tracks **Total Coins**: all coins on that wallet/asset (traded or not).
  - Tracks **Tradeable Coins**: coins the user elects to put at risk for trading (user-chosen; no automatic reserve holdback).
  - Breaks out **Tradeable Coins** into **Quiet Sleeve Balance** and **Flash Sleeve Balance**; allocates and collects profit from the Flash Sleeve.
- **Quiet Sleeve** (conservative / low-vol)
  - Allocated from Tradeable Coins; keeps its profits to compound its own capacity. Has its own balance and P&L.
  - Trades only with its own allocated amount; cannot send to Flash Sleeve directly.
- **Flash Sleeve** (aggressive / high-vol)
  - Allocated from Tradeable Coins; **returns profits back to Tradeable Coins** while keeping only its original allocation. Has its own balance and P&L.
  - Trades only with its own allocated amount; cannot send to Quiet Sleeve directly.

### Naming Options
Locked-in: **Core Vault / Quiet Sleeve / Flash Sleeve**

- **Allocation:** Core Vault assigns user-selected Tradeable Coins across sleeves (Quiet/Flash) or just one sleeve. Example: Total Coins = 20.01253; user sets Tradeable Coins = 15; allocate 10 to Quiet and 5 to Flash.
- **Isolation:** Quiet and Flash cannot transfer between each other; only Core Vault can move funds to/from sleeves.
- **Trading:** All orders execute within a sleeve’s balance. No direct trading from the Core Vault.
- **Quote constraint:** Each Core Vault trades only pairs quoted in its wallet asset (e.g., USD Core Vault trades USD-quoted pairs only). Users can add multiple wallets/Core Vaults as needed.
- **Pair availability:** If no pairs exist for a Core Vault’s quote asset, its sleeves stay idle.
- **API keys:** If a user adds multiple API key sets, rotate them per Core Vault (per asset wallet) according to health/rotation rules.
- **Profit handling:**
  - Quiet Sleeve: keeps profits; its trading balance grows.
  - Flash Sleeve: sends profits back to Tradeable Coins; retains only its initial allocation.
- **Rebalancing:** Core Vault may re-allocate Tradeable Coins across sleeves based on strategy or risk controls.

### Simple Status Diagram
```
Core Vault
  Total Coins: [all coins]
  Tradeable Coins: [coins available for sleeves]
    Quiet Sleeve balance: [amount]
    Flash Sleeve balance: [amount]
  Allocations:
    Quiet Sleeve: [amount]
    Flash Sleeve: [amount]

Rules:
- Trades only occur in sleeves.
- Quiet ↔ Flash transfers are disallowed.
- Profit flow: Quiet keeps profits; Flash profits return to Tradeable Coins.
```

### Data to Track
- Total Coins (by asset)
- Tradeable Coins (by asset)
- Tradeable breakdown: Quiet Sleeve balance, Flash Sleeve balance (per asset)
- Sleeves: per-sleeve P&L, balances, fees, realized/unrealized profit (for graphs)
- Profit returned from Flash Sleeve to Tradeable Coins
- Allocation history/audit (who/when/how much)

### Reporting / Graphs
- Per-sleeve balance over time (Quiet, Flash)
- Per-sleeve realized vs. unrealized P&L over time
- Cumulative profit returned from Flash to Tradeable Coins
- Fees per sleeve over time
- Tradeable Coins vs. Total Coins over time (per asset)

### Open Questions
- **Tradeable vs Total Coins (reserve buffer?):** Yes—optionally cap Tradeable Coins below Total Coins to maintain a safety reserve (e.g., 5–20%) for fees, slippage, and protection against aggressive sleeve drawdowns. Reserve size can flex with volatility or user risk settings.
- **Flash profit sweep frequency:** Sweep = on full position close (sell) in Flash; move profit back to Tradeable Coins immediately.
- **Fee handling per sleeve:** Track fees separately per sleeve since each runs its own script/strategy (long-term conservative vs short-term aggressive); fee/slippage thresholds will be handled in the trade scripts using the sleeve’s available Tradeable Coins.
- **Risk caps per sleeve (drawdown/position sizing/daily loss):** To be defined later; should align with each sleeve’s risk profile and controls.
- **Allocation changes during open positions:** Allowed, but new allocations take effect on the next buy (existing positions continue with prior allocation).
- **Pause / kill-switch:** Can pause a sleeve/Core Vault mid-run; trades wait for a sell to exit unless the user manually closes on the exchange.
