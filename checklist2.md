# Platform Implementation Checklist (Remaining & In-Progress)

Legend: `[ ]` not started, `[-]` in progress, `[x]` done.

## 4) Core Vault Model
- [x] Define totals and auto-adjustments (Total/Tradeable, deposits/funding, auto-increase with caps). *(apply_funding auto-raises with reserve buffer; worker funding detection logs audit/decision; live trigger wiring included.)*
- [x] Support Tradeable = 0 behavior wiring.
- [x] Enforce quote constraint in workers/trading. *(Allowed_pairs + worker checks + quote/pair block audit + live AddOrder gating.)*
- [x] Profit flow & Flash baseline logic (sweeps, principal reset). *(flash_principal baseline + flash sweep profit return; cadence tagged; live/sim sweeps.)*
- [x] Tradeable management enforcement in workers; auto balance detection; alerts. *(remaining_tradeable enforced; over-allocation normalized; funding inference logged; no_tradeable decisions logged.)*
- [x] Allocation change effects when positions open. *(Allocation changes blocked when positions open; audit logged.)*

- [x] Wrap ETH/USDT cycle logic into worker with per-vault configs. *(Worker with retry, fee/slip per vault, quote/allowed_pairs checks, flash sweep, run_all/universal + run_continuous, live AddOrder gated by KRAKEN_LIVE_TRADING.)*
- [x] Map per-vault quote asset to allowed pairs; skip if unavailable.
- [x] Fee/slippage thresholds in scripts using tradeable coins.
- [x] Flash sweep on sell returning profit to Tradeable.
- [x] Headless/background bots start/stop; per-user isolation. *(run_continuous loop stub with controls.)*
- [x] Universal loop for all active bots. *(run_all over asset/quote pairs.)*
- [x] Sleeve mode toggle (manual vs ML) and fallback rules. *(ML mode falls back to hold when no signals.)*

## 6) ML Signal Layer
- [x] Wire directional/outcome/regressor models; thresholds; persistence; retrain cadence; backfill; feature parity; expose sleeve vars. *(generate_ml_signals computes quiet/flash buy/sell likelihoods from recent snapshots with env thresholds for confidence/expected_return/regime.)*

- ## 7) Workers & Scheduling
- [x] Full loops with rate-limit handling/retries; decisions logging; telemetry. *(StrategyDecision logging + loop_tick, error handling/backoff, decisions surfaced in dashboard/admin.)*

- [x] Persist: price snapshots, orders/fills, P&L, strategy decisions, allocations, profit returns from Flash. *(Price/order/fill + P&L models; StrategyDecision added; flash returns tracked; decisions shown in dashboard/admin.)*
- [x] Admin dashboard for approvals, bot start/stop, API key changes, pauses/resumes.
- [x] Charts: per-sleeve balance over time; realized vs unrealized P&L; cumulative Flash profit; fees; Tradeable vs Total.
- [x] Sleeve positions/trades detail beyond stub tables.

- [x] Sleeve views: positions, fees, trades (beyond stub orders/fills). *(Orders/fills tables + per-sleeve charts/alerts; flash/quiet tags, fees, net qty/avg price/unrealized.)*
- [x] Controls: pause/resume sleeves, rotate keys, select pairs.

- ## 8) Persistence & Auditing
- [x] Persist: price snapshots, orders/fills, P&L, strategy decisions, allocations, profit returns from Flash. *(Price/order/fill + P&L models; StrategyDecision added; flash returns tracked; decisions shown in dashboard/admin.)*
- [x] Track per-sleeve P&L, fees, realized/unrealized profit, balances. *(SleevePnl model.)*
- [x] Record allocation history and pauses/resumes. *(AllocationHistory, PauseEvent, BotRun.)*
- [x] Admin dashboard: audit log for approvals, bot start/stop, API key changes, pauses/resumes. *(AuditLog model + usage for CRUD/approvals; UI elements still pending.)*

- [x] Vault view per wallet: Total Coins, Tradeable Coins, Quiet/Flash balances, allocations. *(Template with latest price & P&L; allowed_pairs shown.)*
- [x] Sleeve views: current positions, P&L (realized/unrealized), fees, recent trades. *(Orders/fills tables + per-sleeve charts/alerts; positions show flash/quiet tags, fees, net qty/avg price/unrealized.)*
- [x] Graphs: per-sleeve balance over time; realized vs unrealized P&L; cumulative Flash profit returned; fees over time; Tradeable vs Total over time. *(Multi-pair chart on dashboard; per-sleeve charts on positions.)*
- [x] Controls: set Tradeable Coins, allocate Quiet/Flash, pause/resume sleeves, rotate keys, select pairs. *(Forms with validation; pause/resume/start/stop buttons; rotate/select pairs quick-fill + rotate endpoint.)*

## 10) Risk & Controls
- [x] Profit optimization knobs (sizing vs expected_return/confidence, trailing exits, sweeps cadence, throttles, volatility filters, cooldowns, windows, buffers). *(Env knobs: confidence/expected_return sizing, cooldown, trade windows, volatility hold, sweep cadence throttle, trailing exit guard.)*

## 11) Roles & Access Control
- [x] Roles (Admin/Mod/Member) capabilities; approval flow UI; RBAC enforcement in UI/API; audit actions. *(Role decorator applied across views; role/approval displayed; approvals link for admin/mod.)*

## 11) Testing & Sandbox
- [x] Paper trading/sandbox runs per vault; health checks on key rotation/pair availability. *(PAPER_TRADING flag disables live calls; health:no_api_key decision logged.)*

## 12) ML Add-On / Phase 2 / Full Automation
- [ ] ML add-ons, optimization, auto asset selection, auto allocation, auto rotation per future phases.
