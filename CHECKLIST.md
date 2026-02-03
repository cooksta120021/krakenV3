# Platform Implementation Checklist

> No trading logic code here; reference existing script for strategy details. Use this as a high-level, checkable guide.

Legend: `[ ]` not started, `[-]` in progress, `[x]` done.

**Expected timeframe (using Windsurf AI):** ~2–3 days (~16–24 hours) of focused effort to wire backend scaffolding, vault accounting, ML signal plumbing, and UI/controls (excluding exchange sandbox/live burn-in). Add 1–2 extra days (~8–12 hours) for sandbox soak + tweaks.

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
- [ ] Tradeable Coins management: user-set amount distinct from Total Coins; allocations to sleeves subtract from Tradeable Coins; unallocate to restore; prevent trading above remaining Tradeable Coins; show remaining tradable balance.
- [ ] Track per-sleeve allocated amounts (Quiet, Flash) and recompute remaining Tradeable Coins accordingly.
- [ ] Detect wallet balance increases (poll or manual refresh) and update Total Coins; auto-increase Tradeable Coins by new funds (with user cap/adjustment controls); funding adjustments must not affect sleeve P&L (profits/losses tracked only from trading).
- [ ] (Optional) Alert/log when Tradeable Coins auto-increase: show new Total, new Tradeable, remaining tradable balance, and per-sleeve allocations.
- [ ] Allocation changes allowed; take effect on next buy if positions are open.
- [ ] Pause/kill-switch: pause waits for sell exit unless user manually closes on exchange.

## 5) Strategy Integration (reuse existing script logic)
- [ ] Wrap existing ETH/USDT cycle logic into service/worker with per-user, per-vault configs.
- [ ] Map per-vault quote asset to allowed trading pairs; skip if unavailable.
- [ ] Respect fee/slippage thresholds in scripts using sleeve’s Tradeable Coins.
- [ ] Flash sweep: on full position close (sell), send profit back to Tradeable Coins.
- [ ] Ensure bots run headless/background; continue when user logs off. Start/stop controlled by user/admin actions.
- [ ] Universal cycle: one loop can include all active user bots; ensure per-user isolation of state and API keys.
- [ ] Mode toggle per sleeve: manual (rule-based only) vs ML-assisted (direction/outcome/regressor influence). Default? user-selectable.
- [ ] Fallback rules: if ML unavailable, revert to manual rule-based signals.

## 6) ML Signal Layer (from notes.md)
- [ ] Wire directional and outcome ML models (GradientBoosting) into the live loop; use outputs to bias buy/sell vs hold.
- [ ] Add GradientBoostingRegressor for expected return sizing; feed same feature set, persist (e.g., `online_ret_gb.pkl`), and expose its output for position sizing/risk caps.
- [ ] Replace simulated data loader with real OHLCV (Kraken/CCXT) for both live and backfill.
- [ ] Add backfill step: feed historical candles to directional trainer and historical closed-trade outcomes to outcome trainer; trigger force retrain before live.
- [ ] Ensure features match `SignalPredictor` (price/volume changes, RSI delta, EMA spreads, trend strength, ATR proxy, volume surge flag, trend flags, RSI).
- [ ] Persist models (`online_dir_gb.pkl`, `online_gb.pkl`) and set 15m retrain cadence on rolling buffer (e.g., last 500 samples).
- [ ] Expose per-sleeve variables/flags derived from ML signals for the trading scripts (no trading code here):
    - Directional classifier: probability_up/probability_down; decision flag buy/hold/sell thresholds.
    - Outcome classifier: probability_win; confidence score to gate trades.
    - Return regressor: expected_return; use for position size/risk caps.
    - Combined signal: final_action suggestion + confidence; emit to sleeves as vars only.
- [ ] Define default thresholds (adjustable): e.g., directional buy ≥ 0.55, sell ≤ 0.45; outcome gate ≥ 0.55; expected_return min for sizing.

## 7) Workers & Scheduling
- [ ] Background workers run per user/asset/sleeve loops (price polling, buy/sell checks, orders, telemetry).
- [ ] Ensure rate-limit handling and retries per exchange adapter.

## 8) Data & Observability
- [ ] Persist: price snapshots, orders/fills, P&L, strategy decisions, allocations, profit returns from Flash.
- [ ] Track per-sleeve P&L, fees, realized/unrealized profit, balances.
- [ ] Record allocation history and pauses/resumes.
- [ ] Admin dashboard: audit log for approvals, bot start/stop, API key changes, pauses/resumes.

## 9) Dashboards & UI
- [ ] Vault view per wallet: Total Coins, Tradeable Coins, Quiet/Flash balances, allocations.
- [ ] Sleeve views: current positions, P&L (realized/unrealized), fees, recent trades.
- [ ] Graphs: per-sleeve balance over time; realized vs unrealized P&L; cumulative Flash profit returned; fees over time; Tradeable vs Total over time.
- [ ] Controls: set Tradeable Coins, allocate Quiet/Flash, pause/resume sleeves, rotate keys, select pairs.

## 10) Risk & Controls
- [ ] Define caps later (drawdown, position sizing, daily loss) per sleeve; placeholder config fields.
- [ ] Reserve buffer optional: allow user to cap Tradeable Coins below Total Coins (e.g., 5–20%).
- [ ] Profit optimization knobs (to tune later):
    - Position sizing tied to expected_return and confidence.
    - Trailing exits and dynamic take-profit/stop-loss per sleeve style.
    - Profit sweep cadence (Flash already on sell) and optional periodic Quiet sweeps if desired.
    - Trade frequency throttle and cooldowns to avoid chop.
    - Volatility-aware sizing/entry filters.
    - Loss-triggered cooldowns before next entry.
    - Trading windows per asset/quote to avoid illiquid sessions.

## 11) Roles & Access Control
- [ ] Roles: Admin (all privileges), Mod (support), Member (self-service).
- [ ] Member approval flow: registration requires Admin/Mod approval; unapproved users see waiting page.
- [ ] Admin capabilities: manage all APIs (add/remove), change any user email/password (including own), start/stop any bot.
- [ ] Mod capabilities: approve members; start/stop bots for Members; remove Member API keys; assist with password/login. No access to Admin APIs.
- [ ] Member capabilities: manage own email/password and API keys; start/stop own bot only.
- [ ] Enforce RBAC in UI and API endpoints; audit actions (approvals, bot start/stop, key changes).

## 11) Testing & Sandbox
- [ ] Paper trading/sandbox run per vault to verify flows before live.
- [ ] Health checks on key rotation and pair availability.

## 12) ML Add-On (later, optional beyond current models)
- [ ] Backfill additional OHLCV + executed trades; extra feature engineering.
- [ ] Train/validate alternative models for thresholds; backtest and cross-validate.
- [ ] Guardrails: out-of-sample evaluation, walk-forward testing.
