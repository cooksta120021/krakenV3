# eth_trading_market.py — Outline

## Purpose
- Automates ETH/USDT spot trading on Kraken with buy/sell cycles, momentum and trailing logic, and logging.

## Dependencies
- Standard: `time`, `os`, `collections.deque`.
- Third-party: `requests` (direct Kraken HTTP), `krakenex` (Kraken API client), `dotenv.load_dotenv` (environment loading).

## Environment & Constants
- Loads `.env` via `load_dotenv()`.
- Env vars: `KRAKEN_API_KEY`, `KRAKEN_PRIVATE_KEY`.
- Trading pair: `TRADING_PAIR = 'ETHUSDT'`.
- Fee assumption: `FEE_RATE = 0.004` (0.4% per trade).

## Global State
- `price_history`: deque storing `(timestamp, price)` up to 8 hours.
- Flags/values: `holding_eth`, `buy_price`, `peak_price`, `eth_amount_held`, `current_cycle`, `last_cycle_check`, `last_cycle_log`.
- Tracking lows/highs: `tracking_low`, `is_tracking_low`, `tracking_high`, `is_tracking_high`.
- Trend: `trend_4hr_queue` (deque maxlen=4 for hourly closes), `last_trend4_update`.

## Helper / Utility Functions
- `debug_log(msg)`: prints timestamped messages.
- `get_usdt_balance(api)`: private `Balance`; returns USDT float or 0.
- `get_eth_balance(api)`: private `Balance`; sums `XETH` and `ETH.F`; returns float or 0.
- `buy_eth(api, usdt_amount)`: public `Ticker` for `TRADING_PAIR`; uses ask price; buys market with 99.6% of provided USDT; returns status and ask price.
- `sell_eth(api, eth_amount)`: checks ETH balance; private `AddOrder` market sell; handles insufficient funds; returns status.
- `get_last_buy_price(api)`: private `ClosedOrders` (with trades) to recover last closed buy price for `TRADING_PAIR`.
- `fetch_price_data(max_retries=3, initial_delay=1.0, backoff_factor=2.0)`: direct HTTP GET `https://api.kraken.com/0/public/Ticker?pair=ETHUSDT`; averages ask/bid for current price; returns `(current_price, low_24h)` with retry/backoff.
- `check_cycle_and_set_state(api)`: sets `current_cycle` to `buy` if USDT ≥ 10 else `sell`; recovers `buy_price` from history when selling; sets `holding_eth` accordingly.
- `get_price_n_hours_ago(price_history, now, hours)`: finds price closest to target timestamp.
- `get_1hr_low(price_history, now)`: min price within last hour.
- `init_trend_4hr(api)`: public `OHLC` (interval 60); seeds `trend_4hr_queue` with last 4 closes (from last 5 candles).

## Main Execution Flow (`if __name__ == "__main__":`)
1. Instantiate `krakenex.API`, set `api.key` and `api.secret`.
2. Initialize 4hr trend queue and cycle state.
3. Set timers and `PRICE_FETCH_INTERVAL = 1.1s`.
4. Infinite loop:
   - Enforce min delay between price fetches.
   - Fetch `(current_price, low_24h)` via `fetch_price_data`; skip on failure.
   - Append to `price_history`; prune older than 8h.
   - Every 60s log cycle, current price, and 8h low.
   - Derive `price_1hr_ago`, `one_hr_low`; skip if unavailable.
   - Hourly update `trend_4hr_queue`; compute `trend_4hr` (current / mean of queue) when full; default 1.0 otherwise.
   - Compute `trend_1hr = current_price / price_1hr_ago`.

### Buy Cycle Logic (`current_cycle == 'buy'`, checks every 30s)
- Immediate buy trigger: `trend_1hr ≥ 1.0125` (1.25% rise) with USDT ≥ 10.
- Precondition: require `trend_4hr ≥ 1.002`; otherwise skip.
- If USDT < 10, re-check cycle state.
- Track 8h low: `low_8hr = min(price_history)`, `price_ratio = current_price / low_8hr`.
  - When `price_ratio ≤ 1.02`: start/update `tracking_low`.
  - Buy when price rises >0.2% from tracked low (`current_price > tracking_low * 1.002`).
  - Reset tracking when `price_ratio > 1.02`.
- On successful buy: set `holding_eth=True`, `current_cycle='sell'`, `peak_price=buy_price`.

### Sell Cycle Logic (`current_cycle == 'sell'`, checks every 15s)
- Skip selling if upward momentum: `trend_1hr ≥ 1.011`.
- If `eth_balance < 0.005`: reset to buy cycle and clear tracking.
- When `buy_price` known:
  - `price_change = (current_price - buy_price) / buy_price`.
  - `profit = price_change - (FEE_RATE * 2)`.
  - Stop-loss: if `profit ≤ -0.016` (net loss >1.6%), attempt market sell.
  - Trailing take-profit: if `profit ≥ 0.001` (≥0.1% after fees), start/update `tracking_high`; sell when price drops from tracked high.
  - Reset high tracking if profit falls below 0.8% threshold used for tracking.
- If no `buy_price`, log inability to compute profit.
- Loop sleeps 1s each iteration; KeyboardInterrupt logs stop message.

## External API Touchpoints
- Kraken private: `Balance`, `AddOrder`, `ClosedOrders` (via `krakenex.API.query_private`).
- Kraken public (krakenex): `Ticker`, `OHLC` (via `api.query_public`).
- Kraken public (direct HTTP): `https://api.kraken.com/0/public/Ticker?pair=ETHUSDT`.

# Kraken API Credentials
# Rename this file to .env and fill in your API keys

# API Key (public)
KRAKEN_API_KEY="MZ5ZDIA8SMmEGJHoz87PB5NI7X0wTVlfGoNB2TlJxl88dzH9lNwYJTw+"

# Private Key (keep this secure!)
KRAKEN_PRIVATE_KEY="Dby316utsV/obpdAeVNQAFM7rfRL6Zrqmm8Mny6lq6VjU/oKo0jJp45s0ck+vAMQ0pBkwoN9NxnPCYYDa3UzoQ=="


## Key Thresholds & Parameters
- Minimum USDT to trade: 10.
- Minimum ETH to sell: 0.005.
- Fee assumption: 0.4% per trade (subtracted twice in profit calc).
- Price fetch interval: 1.1s; log interval: 60s; buy check cadence: 30s; sell check cadence: 15s; trend queue update: 1h.
- Buy triggers: 1h spike ≥1.25% or uptick >0.2% after being within 2% of 8h low with 4h uptrend ≥0.2%.
- Sell triggers: stop-loss at -1.6% net; trailing sell after ≥0.1% net gain when price falls from tracked high; skip sells during ≥1.1% 1h uptrend.

## Data Tracked per Cycle
- Prices over 8h window, 1h lookback, 1h low, 8h low.
- 4h trend queue (last four hourly closes).
- Current cycle state (buy/sell), balances, tracked lows/highs, buy price, profit estimation.
