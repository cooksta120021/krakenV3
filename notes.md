## Live trading loop
- File: `kraken_bot_modular/main.py`
- Supports stop_event for web-controlled stop.
- Directional + outcome ML models are loaded and used in the loop.

## Models (scikit-learn GradientBoosting)
- Outcome model (profit-driven):
  - Label: win/lose when a trade closes (pnl > 0 => 1 else 0).
  - Retrain cadence: ~15 minutes on buffered last 500 samples.
  - Persistence: `ml_models/online_gb.pkl`.
- Directional model (immediate decisions):
  - Label: next-candle up/down (updated every candle).
  - Retrain cadence: ~15 minutes on buffered last 500 samples.
  - Persistence: `ml_models/online_dir_gb.pkl`.
- Features (from `SignalPredictor`): price change, volume change, rsi delta, ema spreads, trend strength, atr proxy, volume surge flag, trend up/down flags, rsi.
- Decision logic:
  - Base strategy signal from `generate_enhanced_signal`.
  - Directional model can flip HOLD to BUY (≥0.55) or SELL (≤0.45).
  - Outcome model confidence is blended with rule-based confidence for stability.

## Data and indicators
- Price data loader is currently simulated (`data/data_loader.py`). Replace with real OHLCV fetch to train on historical + live data.
- Indicators: EMA(9/21/50), RSI(14), basic feature engineering in `SignalPredictor`.

## “Train on historic and current values”
- Current state: live loop trains online (buffer + 15m retrains). Models persist between runs.
- To include true historical data: plug real OHLCV into `load_price_data`, then run a pre-loop backfill that feeds historical candles into the directional trainer and historical closed-trade outcomes into the outcome trainer (call `add_sample` then `maybe_retrain(force=True)` once implemented). At present, backfill hook is not yet added.

## Files touched/added
- `kraken_bot_modular/web_app.py` – FastAPI UI.
- `kraken_bot_modular/main.py` – run loop with stop_event + two trainers.
- `kraken_bot_modular/ml/signal_predictor.py` – feature extraction, confidence blending, returns features.
- `kraken_bot_modular/ml/online_trainer.py` – buffer + periodic retrains + persistence.
- `README.md` – usage and learning notes.
- `NOTES.md` (this file) – condensed requirements & behavior.

## Next suggested steps
1) Replace simulated data loader with real OHLCV fetch (Kraken/CCXT) so historical backfill is meaningful.
2) Add backfill function to pre-train both models before starting live loop (use a `force` retrain flag in trainer).
3) Adjust directional thresholds or let the directional model fully drive buy/sell if desired.
