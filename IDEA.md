## Project Idea: User-Managed API Trading Platform

### Goal
- Build a Django-based web app where users authenticate, register their own exchange API credentials (multiple keys for rotation), and automate spot trading on their chosen assets and stablecoin quote (e.g., ETH/USDT).
- Extend beyond the current ETH-focused script into a multi-asset, multi-user platform with dashboards, controls, and audit trails.

### What Exists Now (from `OUTLINE.md`)
- A Python script (`eth_trading_market.py`) automating ETH/USDT trading on Kraken with:
  - Buy/sell cycle management, trend lookbacks (1h/4h), trailing stops, and risk thresholds (stop-loss, take-profit, fee-aware P&L).
  - Price fetching via Kraken public APIs and private trade execution via `krakenex` with `.env` credentials.
  - State tracking: price history (8h), tracked lows/highs, cycle state (buy/sell), balances, and 4h trend queue.
  - Operational cadence: ~1.1s price polling, 30s buy checks, 15s sell checks, 60s logging.

### Platform Scope
1) **User & Auth**
   - Django auth, sessions, optional MFA.
   - Profile stores user-selected base asset(s) and quote wallets (stablecoins).

2) **API Key Management**
   - Users add multiple exchange API keys for rotation (round-robin or health-based fallback).
   - Secure storage (encrypted at rest), per-user separation, and key validity checks.

3) **Trading Engine**
   - Service layer wrapping exchange clients (start with Kraken; design for adapters to others).
   - Strategy executor inspired by the current ETH/USDT logic: buy/sell cycles, trend filters, trailing exits, and fee-aware profit checks.
   - Configurable strategy parameters per user/asset (thresholds, intervals, risk caps).

4) **Data & Observability**
   - Persist price snapshots, orders, fills, P&L, and strategy decisions for audits.
   - Dashboards: balances, open positions, recent trades, per-asset cycle state, and API key health.

5) **Machine Learning Add-On (next)**
   - Ingest historical OHLCV and executed trades; feature engineer trends/volatility/liquidity.
   - Use a starter scikit-learn workflow (e.g., train/validate a classifier or regression to estimate expected return or probability of profit over short horizons).
   - Optimize thresholds (buy/sell triggers, trailing offsets) via backtests and cross-validation instead of static constants.
   - Guardrails: out-of-sample evaluation, walk-forward testing, and risk caps; no “guaranteed profit” claims.

### Architecture Sketch
- **Django app**: auth, user CRUD, API key CRUD, strategy configs, dashboards.
- **Background workers** (Celery/RQ): polling prices, running strategy loops per user/asset, executing orders, recording telemetry.
- **Adapters**: exchange clients abstracted behind a common interface (place order, fetch ticker, balances, OHLC).
- **Storage**: Postgres (users, configs, runs, orders), Redis (queues, short-term state), encrypted secrets for API keys.

### Early Milestones
1) Port existing ETH/USDT cycle logic into a Django-managed worker with per-user configs.
2) Add API key management UI and rotation logic; test end-to-end with paper trading or sandbox.
3) Add multi-asset support and dashboards for balances and recent trades.
4) Add scikit-learn prototype to tune strategy thresholds using historical data and backtests.

### Risks / Considerations
- Exchange rate limits and error handling when rotating keys.
- Security of user-provided API keys; strict least privilege and encryption required.
- Regulatory and compliance review for “trading on behalf of users.”
- Model overfitting: ensure evaluation discipline; results are probabilistic, not guaranteed.
