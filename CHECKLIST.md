# Platform Implementation Checklist

> No trading logic code here; reference existing script for strategy details. Use this as a high-level, checkable guide.

Legend: `[ ]` not started, `[-]` in progress, `[x]` done.

## 1) Foundations
- [ ] Confirm tech stack: Django app, background workers (Celery/RQ), Postgres, Redis, encrypted secrets.
- [ ] Set up environment configs (.env) and secret storage strategy.
- [ ] Define per-user base/quote wallet selection model (supports multiple wallets per user).

## 2) Auth & Users
- [ ] Implement Django auth with sessions; optional MFA flag.
- [ ] User profile stores selected wallets (USD, USDC, USDT, etc.) and permissions.

## 3) API Keys
- [ ] Secure API key storage (encryption at rest); per-user separation.
- [ ] Key validation checks on add/rotate.
- [ ] Per-Core-Vault key rotation when multiple keys exist; track health/rotation rules.

## 4) Core Vault Model (per asset wallet)
- [ ] Create Core Vault per wallet/asset; show **Total Coins** and **Tradeable Coins**.
- [ ] Support Tradeable Coins = 0 without affecting holdings (sleeves idle).
- [ ] Enforce quote constraint: only pairs quoted in the Core Vault’s asset; stay idle if none available.
- [ ] Track Quiet Sleeve balance and Flash Sleeve balance within Tradeable Coins.
- [ ] Profit flow: Quiet keeps profits; Flash returns profits to Tradeable Coins.
- [ ] Allocation changes allowed; take effect on next buy if positions are open.
- [ ] Pause/kill-switch: pause waits for sell exit unless user manually closes on exchange.

## 5) Strategy Integration (reuse existing script logic)
- [ ] Wrap existing ETH/USDT cycle logic into service/worker with per-user, per-vault configs.
- [ ] Map per-vault quote asset to allowed trading pairs; skip if unavailable.
- [ ] Respect fee/slippage thresholds in scripts using sleeve’s Tradeable Coins.
- [ ] Flash sweep: on full position close (sell), send profit back to Tradeable Coins.

## 6) Workers & Scheduling
- [ ] Background workers run per user/asset/sleeve loops (price polling, buy/sell checks, orders, telemetry).
- [ ] Ensure rate-limit handling and retries per exchange adapter.

## 7) Data & Observability
- [ ] Persist: price snapshots, orders/fills, P&L, strategy decisions, allocations, profit returns from Flash.
- [ ] Track per-sleeve P&L, fees, realized/unrealized profit, balances.
- [ ] Record allocation history and pauses/resumes.

## 8) Dashboards & UI
- [ ] Vault view per wallet: Total Coins, Tradeable Coins, Quiet/Flash balances, allocations.
- [ ] Sleeve views: current positions, P&L (realized/unrealized), fees, recent trades.
- [ ] Graphs: per-sleeve balance over time; realized vs unrealized P&L; cumulative Flash profit returned; fees over time; Tradeable vs Total over time.
- [ ] Controls: set Tradeable Coins, allocate Quiet/Flash, pause/resume sleeves, rotate keys, select pairs.

## 9) Risk & Controls
- [ ] Define caps later (drawdown, position sizing, daily loss) per sleeve; placeholder config fields.
- [ ] Reserve buffer optional: allow user to cap Tradeable Coins below Total Coins (e.g., 5–20%).

## 10) Testing & Sandbox
- [ ] Paper trading/sandbox run per vault to verify flows before live.
- [ ] Health checks on key rotation and pair availability.

## 11) ML Add-On (later)
- [ ] Backfill OHLCV + executed trades; feature engineering.
- [ ] Train/validate simple model for thresholds; backtest and cross-validate.
- [ ] Guardrails: out-of-sample evaluation, walk-forward testing.
