---
title: ML trading variable plan
status: complete
---

Scope
- Per user, only one wallet can have ML active at a time. Within that wallet, up to two sleeves (quiet/flash) can be ML-driven. Switching wallets stops previous ML but keeps saved vars on those sleeves. (Done)
- Universal scripts; vars are per user/sleeve and read live by executor (no hardcoding). (Done)

Data
- Historical: Kraken OHLC, 1h candles, ~1 year for chosen pair (using since). (Done)
- Live: Kraken ticker for chosen pair. (Done)
- Wallet selection: show top-3 suggestions per wallet (by balance), but user may choose any pair matching wallet quote. (Done)

Signals & model
- Features: recent returns, volatility, drawdown, volume; optional momentum confirmation. (Done)
- Model: logistic regression on returns to propose entry/TP/SL/size; confirm via heuristic (momentum/vol thresholds). (Done)
- Confidence: derived from validation slice; must pass threshold before enabling live vars. (Done)

Vars to store per sleeve
- pair, mode (quiet/flash), entry_drop_pct, take_profit_pct, stop_loss_pct, position_size_pct, cooldown_seconds, confidence, last_updated. (Done)
- Quiet: stricter thresholds/lower size/longer cooldown. Flash: more responsive/higher size/shorter cooldown. (Done)

Execution wiring
- Executor reads vars from DB each cycle for that sleeve; uses sleeve’s user API key. (Done)
- Cooldown enforced after buy/sell to avoid fee churn; ml_last_action tracked. (Done)
- Ticker pulled live each cycle; volume derived from allocated * position_size_pct. (Done)
- TP/SL entry checks and zero-volume guards added. (Done)
- If ML inactive: allow manual edits. If active: vars locked; stop button unlocks. (Done)

Training flow
- Trigger from wallet page per sleeve: user chooses pair (top-3 or custom) → “Train & Set (ML)”. (Done)
- Training pulls 1h OHLC (Kraken OHLC) + latest ticker; computes signals (returns/vol/drawdown) → logistic score → entry/TP/SL/size/cooldown per mode; sets confidence; starts trading automatically. (Done)
- Periodic refresh task refresh_all_ml_vars re-fetches OHLC+ticker and updates vars for active ML sleeves; enforces single active wallet per user (skips others without clearing vars). (Done)

UI
- Wallet page: per sleeve shows pair selection, Train/Set ML button, active badge with confidence + vars, stop ML button; manual edit disabled when ML active. (Done)
- Dashboard: top 20 assets per wallet; indicate which wallet has ML active. (Done)

Safety/limits
- Enforce one-ML-wallet-per-user. Reject start if another wallet is active; stop prior to switch. (Done)
- Cap history fetch to top-3 pairs plus chosen pair; limit refresh frequency. (Done)
- Handle Kraken errors gracefully; show status. (Done)

Status summary
- Core ML trainer implemented (Kraken OHLC + ticker, signals/logistic, per-mode vars, confidence). (Done)
- Executor reads ML vars, applies cooldown and TP/SL logic, updates last_action. (Done)
- Celery tasks: run_all_active_sleeves and refresh_all_ml_vars wired. (Done)
- Wallet UI ML controls and dashboard indicator in place; single-ML-wallet enforced; vars persist per wallet; manual edits locked when ML active. (Done)
- All scoped items implemented. (Done)
