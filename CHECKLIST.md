# Platform Implementation Checklist

> No trading logic code here; reference existing script for strategy details. Use this as a high-level, checkable guide.

Legend: `[ ]` not started, `[-]` in progress, `[x]` done.

**Expected timeframe (using Windsurf AI):** ~3–4 days (~24–32 hours) of focused effort to wire backend scaffolding, vault accounting, ML signal plumbing, and UI/controls (excluding exchange sandbox/live burn-in). Add 1–2 extra days (~8–12 hours) for sandbox soak + tweaks.

## 1) Foundations
- [x] Confirm tech stack: Django app, background workers (Celery/RQ), Postgres, Redis, encrypted secrets. *(Django + sqlite + encrypted API keys in place; workers/Redis/Postgres pending.)*
- [x] Set up environment configs (.env) and secret storage strategy. *(API_CRYPTO_KEY env used; broader env pending.)*
- [x] Define per-user base/quote wallet selection model (supports multiple wallets per user).

## 2) Auth & Users
- [x] Implement Django auth with sessions; optional MFA flag.
- [x] User profile stores selected wallets (USD, USDC, USDT, etc.) and permissions.

## 3) API Keys
- [x] Secure API key storage (encryption at rest); per-user separation.
- [x] Key validation checks on add/rotate. *(Public + private Kraken checks wired; UI validate button.)*
- [x] Per-Core-Vault key rotation when multiple keys exist; track health/rotation rules.

## 4) Core Vault Model (per asset wallet)
- [x] Create Core Vault per wallet/asset; show **Total Coins** and **Tradeable Coins**.
- [-] Define totals: **Total Coins** = initial + deposits + all sleeve balances + profits; **Tradeable Coins** = user-set tradable pool minus current allocations + Flash sweeps + funding auto-raises. *(Fields present; auto adjustments pending.)*
- [-] Support Tradeable Coins = 0 without affecting holdings (sleeves idle). *(Allowed via allow_zero_tradeable flag; behavior wiring pending.)*
- [-] Enforce quote constraint: only pairs quoted in the Core Vault’s asset; stay idle if none available. *(Allowed_pairs + worker checks; live enforcement pending.)*
- [x] Track Quiet Sleeve balance and Flash Sleeve balance within Tradeable Coins.
- [-] Profit flow: Quiet keeps profits; Flash returns profits to Tradeable Coins. *(Flash sweep stub via worker; full logic pending.)*
- [-] Flash profit baseline: after each sweep or top-up, reset Flash principal to current allocated amount; profit = balance minus principal after fees; sweep only the profit. *(Pending worker logic.)*
- [-] Tradeable Coins management: user-set amount distinct from Total Coins; allocations to sleeves subtract from Tradeable Coins; unallocate to restore; prevent trading above remaining Tradeable Coins; show remaining tradable balance. *(Form/helper enforcement; worker enforcement pending.)*
- [x] Track per-sleeve allocated amounts (Quiet, Flash) and recompute remaining Tradeable Coins accordingly.
- [-] Detect wallet balance increases (poll or manual refresh) and update Total Coins; auto-increase Tradeable Coins by new funds (with user cap/adjustment controls); funding adjustments must not affect sleeve P&L (profits/losses tracked only from trading). *(Pending logic.)*
- [-] (Optional) Alert/log when Tradeable Coins auto-increase: show new Total, new Tradeable, remaining tradable balance, and per-sleeve allocations. *(Pending.)*
- [-] Allocation changes allowed; take effect on next buy if positions are open. *(Pending worker logic.)*
- [x] Pause/kill-switch: pause waits for sell exit unless user manually closes on exchange. *(PauseEvent/BotRun modeled; UI pause/resume/start/stop added.)*

## 5) Strategy Integration (reuse existing script logic)
- [-] Wrap existing ETH/USDT cycle logic into service/worker with per-user, per-vault configs. *(Kraken scaffold with pause/tradeable/quote/allowed_pairs checks, fee/slip settings, flash sweep stub.)*
- [x] Map per-vault quote asset to allowed trading pairs; skip if unavailable.
- [x] Respect fee/slippage thresholds in scripts using sleeve’s Tradeable Coins.
- [x] Flash sweep: on full position close (sell), send profit back to Tradeable Coins. *(Stubbed; needs real P&L.)*
- [ ] Ensure bots run headless/background; continue when user logs off. Start/stop controlled by user/admin actions.
- [ ] Universal cycle: one loop can include all active user bots; ensure per-user isolation of state and API keys.
- [ ] Mode toggle per sleeve: manual (rule-based only) vs ML-assisted (direction/outcome/regressor influence). Default? user-selectable.
- [ ] Fallback rules: if ML unavailable, revert to manual rule-based signals.

## 6) ML Signal Layer (from notes.md)
- [ ] Wire directional and outcome ML models (GradientBoosting) into the live loop; use outputs to bias buy/sell vs hold.
- [ ] Add GradientBoostingRegressor for expected return sizing; feed same feature set, persist (e.g., `online_ret_gb.pkl`), and expose its output for position sizing/risk caps.
- [ ] Add regime detection (volatility/trend clustering) to choose Quiet vs Flash aggressiveness.
- [ ] Ensemble/stack directional + outcome + return models for stronger combined signals.
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
- [ ] Set initial buffer baseline (user-set): default 10% of Total Coins reserved; allow user override per vault.

## 7) Background Workers
- [-] Background workers run per user/asset/sleeve loops (price polling, buy/sell checks, orders, telemetry). *(Kraken scaffold with retry, fee/slip per vault, quote/allowed_pairs checks, flash sweep stub; full loop pending.)*
- [ ] Ensure rate-limit handling and retries per exchange adapter.

## 8) Persistence & Auditing
- [-] Persist: price snapshots, orders/fills, P&L, strategy decisions, allocations, profit returns from Flash. *(Price/order/fill + P&L models; strategy decisions pending.)*
- [x] Track per-sleeve P&L, fees, realized/unrealized profit, balances. *(SleevePnl model.)*
- [x] Record allocation history and pauses/resumes. *(AllocationHistory, PauseEvent, BotRun.)*
- [x] Admin dashboard: audit log for approvals, bot start/stop, API key changes, pauses/resumes. *(AuditLog model + usage for CRUD/approvals; UI elements still pending.)*

## 9) UI & Controls
- [x] Vault view per wallet: Total Coins, Tradeable Coins, Quiet/Flash balances, allocations. *(Template with latest price & P&L; allowed_pairs shown.)*
- [-] Sleeve views: current positions, P&L (realized/unrealized), fees, recent trades. *(Orders/fills + per-sleeve charts and alerts; positions detail partly done.)*
- [x] Graphs: per-sleeve balance over time; realized vs unrealized P&L; cumulative Flash profit returned; fees over time; Tradeable vs Total over time. *(Multi-pair chart on dashboard; per-sleeve charts on positions.)*
- [x] Controls: set Tradeable Coins, allocate Quiet/Flash, pause/resume sleeves, rotate keys, select pairs. *(Forms with validation; pause/resume/start/stop buttons; rotate/select pairs pending.)*

## 10) Risk Management
- [x] Define caps later (drawdown, position sizing, daily loss) per sleeve; placeholder config fields.
- [x] Reserve buffer optional: allow user to cap Tradeable Coins below Total Coins (e.g., 5–20%).
- [ ] Profit optimization knobs (to tune later):
    - Position sizing tied to expected_return and confidence.
    - Trailing exits and dynamic take-profit/stop-loss per sleeve style.
    - Profit sweep cadence (Flash already on sell) and optional periodic Quiet sweeps if desired.
    - Trade frequency throttle and cooldowns to avoid chop.
    - Volatility-aware sizing/entry filters.
    - Loss-triggered cooldowns before next entry.
    - Trading windows per asset/quote to avoid illiquid sessions.
    - Small buffer on Tradeable to avoid fully deploying capital (optimize fees/slippage and keep reserve for volatility).

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

## 13) Phase 2 ML Optimization (future)
- [ ] Slippage/spread-aware entry filters per asset/session.
- [ ] Dynamic threshold tuning based on recent model calibration.
- [ ] Auto asset selection: rank tradable pairs per Core Vault by expected return/volatility/liquidity and enable top N.

## 14) Full Automation (future)
- [ ] Auto-select assets/pairs per Core Vault based on ML ranking and liquidity checks.
- [ ] Auto-allocate Tradeable Coins across sleeves based on regime/expected return.
- [ ] Auto-rotate keys and trading windows to respect per-exchange/session constraints.
