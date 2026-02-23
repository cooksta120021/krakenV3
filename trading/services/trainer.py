import logging
import math
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Tuple

from django.conf import settings
from django.utils import timezone

from trading.models import MlCandle, MlMarketSnapshot, SleeveStrategy
from .kraken_adapter import KrakenAdapter, GLOBAL_RATE_LIMITER
from api_keys.models import ApiKey
from trading.services.autotrade_console import push as _console_push

logger = logging.getLogger(__name__)


def _pick_active_key(user_id: int):
    return ApiKey.objects.filter(user_id=user_id, is_active=True).order_by("created_at").first()


def _recompute_triggers_for_strategy(s: SleeveStrategy, price_now: Decimal) -> None:
    try:
        entry_drop = Decimal(str(getattr(s, "ml_entry_drop_pct", 0) or 0)) / Decimal("100")
    except Exception:
        entry_drop = Decimal("0")
    try:
        take_profit = Decimal(str(getattr(s, "ml_take_profit_pct", 0) or 0)) / Decimal("100")
    except Exception:
        take_profit = Decimal("0")
    try:
        stop_loss = Decimal(str(getattr(s, "ml_stop_loss_pct", 0) or 0)) / Decimal("100")
    except Exception:
        stop_loss = Decimal("0")

    try:
        if str(getattr(s, "mode", "") or "").startswith("buy"):
            s.ml_last_entry_trigger = (price_now * (Decimal("1") - entry_drop)).quantize(Decimal("0.0000000001"))
            s.ml_last_tp_trigger = (price_now * (Decimal("1") + take_profit)).quantize(Decimal("0.0000000001"))
            s.ml_last_sl_trigger = (price_now * (Decimal("1") + stop_loss)).quantize(Decimal("0.0000000001"))
        else:
            s.ml_last_entry_trigger = (price_now * (Decimal("1") - entry_drop)).quantize(Decimal("0.0000000001"))
            s.ml_last_tp_trigger = (price_now * (Decimal("1") + take_profit)).quantize(Decimal("0.0000000001"))
            s.ml_last_sl_trigger = (price_now * (Decimal("1") - stop_loss)).quantize(Decimal("0.0000000001"))
    except Exception:
        return


def refresh_ml_prices() -> Dict[str, int]:
    """Fast live update for ML strategies: ticker-only.

    Updates:
    - ml_last_price
    - ml_last_tick_at
    - ml_last_entry_trigger / ml_last_tp_trigger / ml_last_sl_trigger

    Does NOT recompute confidence/vars from candles (see refresh_ml_vars).
    """

    updated = 0
    strategies = list(
        SleeveStrategy.objects.select_related("sleeve", "sleeve__wallet")
        .filter(ml_active=True)
        .only(
            "id",
            "mode",
            "ml_pair",
            "ml_entry_drop_pct",
            "ml_take_profit_pct",
            "ml_stop_loss_pct",
            "ml_last_price",
            "ml_last_tick_at",
            "ml_last_entry_trigger",
            "ml_last_tp_trigger",
            "ml_last_sl_trigger",
            "ml_paused",
            "sleeve__wallet__user_id",
        )
    )
    if not strategies:
        return {"updated": 0}

    # Group by user_id so we can reuse a single adapter and avoid rate spikes.
    by_user: Dict[int, List[SleeveStrategy]] = {}
    for s in strategies:
        try:
            uid = int(s.sleeve.wallet.user_id)
        except Exception:
            continue
        by_user.setdefault(uid, []).append(s)

    for user_id, st_list in by_user.items():
        before_updated = updated
        key = _pick_active_key(user_id)
        if not key:
            continue
        adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER, user_id=user_id)

        # Fetch ticker once per pair.
        pair_prices: Dict[str, Decimal] = {}
        pair_errors: Dict[str, str] = {}
        for s in st_list:
            pair = (getattr(s, "ml_pair", "") or "").strip()
            if not pair:
                continue
            if pair in pair_prices:
                continue
            try:
                ticker = adapter.fetch_ticker(pair)
                pair_prices[pair] = _safe_decimal(ticker.get("price") or 0)
            except Exception as exc:
                pair_prices[pair] = Decimal("0")
                pair_errors[pair] = str(exc)

        now = timezone.now()
        for s in st_list:
            pair = (getattr(s, "ml_pair", "") or "").strip()
            if not pair:
                continue
            price_now = pair_prices.get(pair) or Decimal("0")
            s.ml_last_tick_at = now

            if price_now > 0:
                s.ml_last_price = price_now
                _recompute_triggers_for_strategy(s, price_now)
                if getattr(s, "ml_paused", False):
                    s.ml_last_reason = "paused_price_tick"
                elif not (getattr(s, "ml_last_reason", "") or "").strip():
                    s.ml_last_reason = "price_tick"
                fields = [
                    "ml_last_tick_at",
                    "ml_last_price",
                    "ml_last_entry_trigger",
                    "ml_last_tp_trigger",
                    "ml_last_sl_trigger",
                    "ml_last_reason",
                ]
            else:
                s.ml_last_reason = "price_tick_failed"
                fields = ["ml_last_tick_at", "ml_last_reason"]
                try:
                    err = pair_errors.get(pair)
                    if err:
                        _console_push(user_id, f"ml_price_tick_failed pair={pair}: {err}", level="warn")
                except Exception:
                    pass

            try:
                s.save(update_fields=fields)
                updated += 1
            except Exception:
                continue

    return {"updated": updated}


def _safe_decimal(val) -> Decimal:
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal("0")


def _extract_ohlc(payload: Dict[str, Any], pair: str) -> List[float]:
    result = payload.get("result", {})
    series = None
    if isinstance(result, dict):
        if pair in result and isinstance(result.get(pair), list):
            series = result.get(pair)
        else:
            # take first list-like value that is not the cursor
            for k, v in result.items():
                if k == "last":
                    continue
                if isinstance(v, list):
                    series = v
                    break
    if not series or not isinstance(series, list):
        return []
    closes = []
    for row in series:
        # Kraken OHLC: [time, open, high, low, close, vwap, volume, count]
        if len(row) >= 5:
            closes.append(float(row[4]))
    return closes


def _extract_ohlc_rows(payload: Dict[str, Any], pair: str) -> List[Tuple[int, float]]:
    result = payload.get("result", {})
    series = None
    if isinstance(result, dict):
        if pair in result and isinstance(result.get(pair), list):
            series = result.get(pair)
        else:
            for k, v in result.items():
                if k == "last":
                    continue
                if isinstance(v, list):
                    series = v
                    break
    if not series or not isinstance(series, list):
        return []
    rows: List[Tuple[int, float]] = []
    for row in series:
        if len(row) >= 5:
            try:
                ts = int(row[0])
                close = float(row[4])
            except Exception:
                continue
            rows.append((ts, close))
    return rows


def _fetch_ohlc_1h_paged_rows(
    adapter: KrakenAdapter,
    pair: str,
    since_ts: int | None = None,
    max_candles: int | None = None,
) -> List[Tuple[int, float]]:
    """Fetch ~`days` of 1h OHLC closes, paging using Kraken's `last` cursor."""
    if since_ts is None:
        lookback_days = int(getattr(settings, "ML_LOOKBACK_DAYS", 365) or 365)
        since_ts = int((datetime.utcnow() - timedelta(days=lookback_days)).timestamp())

    if max_candles is None:
        # Estimate a reasonable cap from since_ts (defaults to 365d if estimate fails).
        try:
            est_days = max(1, int((datetime.utcnow().timestamp() - int(since_ts)) / 86400))
        except Exception:
            est_days = 365
        max_candles = int(est_days * 24)

    out: List[Tuple[int, float]] = []
    cursor = since_ts
    # Allow enough pages for long lookbacks; Kraken may return limited candles per page.
    safety = 0
    max_pages = max(60, int((max_candles / 720)) + 20)
    while safety < max_pages and len(out) < int(max_candles * 1.2):
        safety += 1
        before_len = len(out)
        payload = adapter.fetch_ohlc(pair, interval=60, since=cursor)
        new_rows = _extract_ohlc_rows(payload, pair)
        if new_rows:
            out.extend(new_rows)
        result = payload.get("result", {})
        last = result.get("last")
        if last is None:
            break
        try:
            next_cursor = int(last)
        except Exception:
            break
        if next_cursor <= cursor:
            break
        cursor = next_cursor

        # If Kraken returned no new candles in this page, stop.
        if len(out) == before_len:
            break

    # Sort + de-dup by timestamp; keep the last close per ts.
    if out:
        out = sorted(out, key=lambda x: x[0])
        dedup: dict[int, float] = {}
        for ts, close in out:
            dedup[int(ts)] = float(close)
        out = [(ts, dedup[ts]) for ts in sorted(dedup.keys())]

    # keep the most-recent target window if we overshot
    if max_candles and len(out) > max_candles:
        out = out[-max_candles:]
    return out


def _fetch_ohlc_paged_rows(
    adapter: KrakenAdapter,
    pair: str,
    interval_minutes: int,
    since_ts: int | None = None,
    max_candles: int | None = None,
) -> List[Tuple[int, float]]:
    if since_ts is None:
        lookback_days = int(getattr(settings, "ML_LOOKBACK_DAYS", 365) or 365)
        since_ts = int((datetime.utcnow() - timedelta(days=lookback_days)).timestamp())

    interval_minutes = int(interval_minutes or 60)
    if interval_minutes <= 0:
        interval_minutes = 60

    if max_candles is None:
        try:
            est_days = max(1, int((datetime.utcnow().timestamp() - int(since_ts)) / 86400))
        except Exception:
            est_days = 365
        per_day = int((24 * 60) / interval_minutes)
        max_candles = int(est_days * per_day)

    out: List[Tuple[int, float]] = []
    cursor = since_ts
    safety = 0
    max_pages = max(60, int((max_candles / 720)) + 20)
    while safety < max_pages and len(out) < int(max_candles * 1.2):
        safety += 1
        before_len = len(out)
        payload = adapter.fetch_ohlc(pair, interval=interval_minutes, since=cursor)
        new_rows = _extract_ohlc_rows(payload, pair)
        if new_rows:
            out.extend(new_rows)
        result = payload.get("result", {})
        last = result.get("last") if isinstance(result, dict) else None
        if last is None:
            break
        try:
            next_cursor = int(last)
        except Exception:
            break
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(out) == before_len:
            break

    if out:
        out = sorted(out, key=lambda x: x[0])
        dedup: dict[int, float] = {}
        for ts, close in out:
            dedup[int(ts)] = float(close)
        out = [(ts, dedup[ts]) for ts in sorted(dedup.keys())]

    if max_candles and len(out) > max_candles:
        out = out[-max_candles:]
    return out


def _lookback_days_for_strategy(s: SleeveStrategy) -> int:
    mode = (getattr(s, "mode", "") or "").lower()
    if "flash" in mode:
        return int(getattr(settings, "ML_LOOKBACK_DAYS_FLASH", getattr(settings, "ML_LOOKBACK_DAYS", 365)) or 180)
    return int(getattr(settings, "ML_LOOKBACK_DAYS_QUIET", getattr(settings, "ML_LOOKBACK_DAYS", 365)) or 365)


def _interval_minutes_for_strategy(s: SleeveStrategy) -> int:
    mode = (getattr(s, "mode", "") or "").lower()
    return 15 if "flash" in mode else 60


def _fetch_ohlc_1h_paged(adapter: KrakenAdapter, pair: str, since_ts: int | None = None) -> List[float]:
    rows = _fetch_ohlc_1h_paged_rows(adapter, pair, since_ts)
    return [close for _, close in rows]


def _aggregate_rows_to_1h(rows: List[Tuple[int, float]]) -> List[Tuple[int, float]]:
    if not rows:
        return []
    # Ensure sorted.
    rows = sorted(rows, key=lambda x: x[0])
    buckets: dict[int, float] = {}
    for ts, close in rows:
        try:
            hour_ts = int(ts) - (int(ts) % 3600)
        except Exception:
            continue
        # Use last close within the hour.
        buckets[hour_ts] = float(close)
    out = [(ts, buckets[ts]) for ts in sorted(buckets.keys())]
    return out


def _store_rolling_candles(
    pair: str,
    interval_minutes: int,
    rows: List[Tuple[int, float]],
    keep_days: int = 30,
) -> None:
    if not rows:
        return
    interval_minutes = int(interval_minutes or 60)
    objs = [
        MlCandle(pair=pair, interval_minutes=interval_minutes, ts=ts, close=Decimal(str(close)))
        for ts, close in rows
    ]
    MlCandle.objects.bulk_create(objs, ignore_conflicts=True)
    cutoff = int((datetime.utcnow() - timedelta(days=int(keep_days or 30))).timestamp())
    MlCandle.objects.filter(pair=pair, interval_minutes=interval_minutes, ts__lt=cutoff).delete()


def _compute_signals(closes: List[float], interval_minutes: int = 60) -> Dict[str, float]:
    if len(closes) < 50:
        return {}
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1] != 0]
    if not returns:
        return {}
    # recent windows
    interval_minutes = int(interval_minutes or 60)
    if interval_minutes <= 0:
        interval_minutes = 60
    per_day = int((24 * 60) / interval_minutes)
    win_short = returns[-per_day:]  # ~1d
    win_mid = returns[-per_day * 7 :]  # ~1w
    mean_short = sum(win_short) / len(win_short)
    vol_short = (sum((r - mean_short) ** 2 for r in win_short) / len(win_short)) ** 0.5
    mean_mid = sum(win_mid) / len(win_mid)
    vol_mid = (sum((r - mean_mid) ** 2 for r in win_mid) / len(win_mid)) ** 0.5 if len(win_mid) > 0 else 0
    # drawdown
    peak = closes[0]
    max_dd = 0.0
    for c in closes:
        if c > peak:
            peak = c
        dd = (peak - c) / peak if peak else 0
        if dd > max_dd:
            max_dd = dd
    return {
        "mean_short": mean_short,
        "vol_short": vol_short,
        "mean_mid": mean_mid,
        "vol_mid": vol_mid,
        "drawdown": max_dd,
    }


def _logistic(x):
    return 1 / (1 + math.exp(-x))


def _score_signals(sig: Dict[str, float]) -> float:
    # simple weighted logistic score
    z = (
        8 * sig.get("mean_short", 0)
        - 3 * sig.get("vol_short", 0)
        + 4 * sig.get("mean_mid", 0)
        - 2 * sig.get("vol_mid", 0)
        - 1.5 * sig.get("drawdown", 0)
    )
    return _logistic(z)


def _vars_for_mode(prob: float, mode: str) -> Dict[str, Decimal]:
    # map probability into thresholds; quieter is stricter
    if mode in (SleeveStrategy.Mode.BUY_QUIET, SleeveStrategy.Mode.SELL_QUIET):
        entry = max(Decimal("0.45"), Decimal(str((1 - prob) * 1.6)))
        tp = Decimal("0.90") + Decimal(str(prob * 0.9))
        sl = max(Decimal("0.35"), Decimal(str((1 - prob) * 1.2)))
        size = Decimal("2.0") + Decimal(str(prob * 3))
        cooldown = 360
    else:
        entry = max(Decimal("0.05"), Decimal(str((1 - prob) * 0.45)))
        tp = Decimal("0.35") + Decimal(str(prob * 0.75))
        sl = max(Decimal("0.12"), Decimal(str((1 - prob) * 0.5)))
        size = Decimal("6.0") + Decimal(str(prob * 8))
        cooldown = 45
    return {
        "entry_drop_pct": entry.quantize(Decimal("0.01")),
        "take_profit_pct": tp.quantize(Decimal("0.01")),
        "stop_loss_pct": sl.quantize(Decimal("0.01")),
        "position_size_pct": size.quantize(Decimal("0.1")),
        "cooldown_seconds": cooldown,
    }


def refresh_ml_vars() -> Dict[str, int]:
    updated = 0
    stored_pairs: set[tuple[str, int]] = set()
    strategies = list(
        SleeveStrategy.objects.select_related("sleeve", "sleeve__wallet", "sleeve__wallet__user")
        .filter(ml_active=True)
    )

    # Group strategies by user -> wallet so we can enforce "one active wallet per user" while
    # still allowing multiple sleeves/strategies on that chosen wallet.
    by_user: Dict[int, Dict[int, List[SleeveStrategy]]] = {}
    for s in strategies:
        by_user.setdefault(s.sleeve.wallet.user_id, {}).setdefault(s.sleeve.wallet_id, []).append(s)

    for user_id, wallets_map in by_user.items():
        if not wallets_map:
            continue
        # Choose a single wallet per user (stable order) to refresh.
        wallet_id = sorted(wallets_map.keys())[0]
        wallet_strategies = wallets_map[wallet_id]

        key = _pick_active_key(user_id)
        if not key:
            logger.warning("No active key for user %s; skipping ML refresh", user_id)
            continue
        adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER, user_id=user_id)

        # Cache market data per pair+interval+lookback so buy/sell can reuse the same fetch.
        data_cache: Dict[tuple[str, int, int], tuple[List[Tuple[int, float]], List[float], Decimal]] = {}

        for s in wallet_strategies:
            if getattr(s, "ml_paused", False):
                if s.ml_status != "paused" or getattr(s, "ml_stage", "") != "paused":
                    s.ml_status = "paused"
                    s.ml_stage = "paused"
                    s.save(update_fields=["ml_status", "ml_stage"])
                continue

            # In custom mode, do not overwrite user-provided triggers.
            try:
                if getattr(s.sleeve, "trade_mode", "auto") == "custom":
                    if s.ml_status != "custom":
                        s.ml_status = "custom"
                        if not getattr(s, "ml_stage", "") or s.ml_stage == "idle":
                            s.ml_stage = "waiting_buy" if str(s.mode).startswith("buy") else "waiting_sell"
                        s.save(update_fields=["ml_status", "ml_stage"])
                    continue
            except Exception:
                pass

            pair = (s.ml_pair or "").strip()
            if not pair:
                logger.warning("No ML pair selected for sleeve %s; skipping ML refresh", s.sleeve_id)
                continue

            lookback_days = _lookback_days_for_strategy(s)
            interval_minutes = _interval_minutes_for_strategy(s)
            cache_key = (pair, int(interval_minutes), int(lookback_days))
            if cache_key not in data_cache:
                try:
                    since_ts = int((datetime.utcnow() - timedelta(days=lookback_days)).timestamp())
                    per_day = int((24 * 60) / int(interval_minutes or 60))
                    candle_rows = _fetch_ohlc_paged_rows(
                        adapter,
                        pair,
                        interval_minutes=interval_minutes,
                        since_ts=since_ts,
                        max_candles=int(lookback_days * per_day),
                    )
                    closes = [close for _, close in candle_rows]

                    # Fallback for very-new coins: for quiet (60m), fetch 15m and aggregate to 1h.
                    if interval_minutes == 60 and len(closes) < 50:
                        try:
                            per_day_15m = int((24 * 60) / 15)
                            out_15m = _fetch_ohlc_paged_rows(
                                adapter,
                                pair,
                                interval_minutes=15,
                                since_ts=since_ts,
                                max_candles=int(lookback_days * per_day_15m),
                            )
                            agg_1h = _aggregate_rows_to_1h(out_15m)
                            closes_agg = [c for _, c in agg_1h]
                            if len(closes_agg) >= len(closes):
                                candle_rows = agg_1h
                                closes = closes_agg
                        except Exception:
                            pass

                    ticker = adapter.fetch_ticker(pair)
                    price_now = _safe_decimal(ticker.get("price") or 0)
                    data_cache[cache_key] = (candle_rows, closes, price_now)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ML refresh data fail for %s: %s", pair, exc)
                    continue

                # Persist rolling candle history once per pair (append new candles, prune old).
                stored_key = (pair, int(interval_minutes))
                if stored_key not in stored_pairs:
                    try:
                        _store_rolling_candles(
                            pair=pair,
                            interval_minutes=interval_minutes,
                            rows=data_cache[cache_key][0],
                            keep_days=int(lookback_days or 30),
                        )
                        stored_pairs.add(stored_key)
                    except Exception:
                        pass

            candle_rows, closes, price_now = data_cache.get(cache_key, ([], [], Decimal("0")))
            if price_now <= 0 or not closes:
                if s.ml_status != "error" or getattr(s, "ml_stage", "") != "error":
                    s.ml_status = "error"
                    s.ml_stage = "error"
                    s.ml_candles_1h = int(len(closes) if closes else 0)
                    s.save(update_fields=["ml_status", "ml_stage", "ml_candles_1h"])
                continue

            if len(closes) < 50:
                if s.ml_status != "error" or getattr(s, "ml_stage", "") != "error":
                    s.ml_status = "error"
                    s.ml_stage = "error"
                    s.ml_candles_1h = int(len(closes))
                    s.save(update_fields=["ml_status", "ml_stage", "ml_candles_1h"])
                continue

            sig = _compute_signals(closes, interval_minutes=_interval_minutes_for_strategy(s))
            if not sig:
                if s.ml_status != "error" or getattr(s, "ml_stage", "") != "error":
                    s.ml_status = "error"
                    s.ml_stage = "error"
                    s.ml_candles_1h = int(len(closes))
                    s.save(update_fields=["ml_status", "ml_stage", "ml_candles_1h"])
                continue
            prob = _score_signals(sig)
            vars_out = _vars_for_mode(prob, s.mode)

            try:
                MlMarketSnapshot.objects.create(
                    strategy=s,
                    pair=pair,
                    lookback_days=lookback_days,
                    candles=int(len(closes)),
                    closes=closes[-500:],
                    price_now=price_now,
                )
            except Exception:
                pass

            s.ml_entry_drop_pct = vars_out["entry_drop_pct"]
            s.ml_take_profit_pct = vars_out["take_profit_pct"]
            s.ml_stop_loss_pct = vars_out["stop_loss_pct"]
            s.ml_position_size_pct = vars_out["position_size_pct"]
            s.ml_cooldown_seconds = int(vars_out["cooldown_seconds"])
            s.ml_candles_1h = int(len(closes))
            s.ml_confidence = Decimal(str(prob)).quantize(Decimal("0.0001"))
            s.ml_last_trained_at = timezone.now()
            s.ml_last_tick_at = timezone.now()
            s.ml_last_price = price_now

            # Pre-compute expected trigger prices so the UI has immediate numbers.
            try:
                entry_drop = Decimal(str(s.ml_entry_drop_pct or 0)) / Decimal("100")
            except Exception:
                entry_drop = Decimal("0")
            try:
                take_profit = Decimal(str(s.ml_take_profit_pct or 0)) / Decimal("100")
            except Exception:
                take_profit = Decimal("0")
            try:
                stop_loss = Decimal(str(s.ml_stop_loss_pct or 0)) / Decimal("100")
            except Exception:
                stop_loss = Decimal("0")

            try:
                if str(getattr(s, "mode", "") or "").startswith("buy"):
                    s.ml_last_entry_trigger = (price_now * (Decimal("1") - entry_drop)).quantize(Decimal("0.0000000001"))
                    s.ml_last_tp_trigger = (price_now * (Decimal("1") + take_profit)).quantize(Decimal("0.0000000001"))
                    s.ml_last_sl_trigger = (price_now * (Decimal("1") + stop_loss)).quantize(Decimal("0.0000000001"))
                else:
                    # sell
                    s.ml_last_entry_trigger = (price_now * (Decimal("1") - entry_drop)).quantize(Decimal("0.0000000001"))
                    s.ml_last_tp_trigger = (price_now * (Decimal("1") + take_profit)).quantize(Decimal("0.0000000001"))
                    s.ml_last_sl_trigger = (price_now * (Decimal("1") - stop_loss)).quantize(Decimal("0.0000000001"))
            except Exception:
                pass

            # Set a default reason so blank reasons don't confuse the dashboard.
            s.ml_last_reason = "trained"
            s.ml_status = "active"
            s.ml_stage = "waiting_buy" if str(s.mode).startswith("buy") else "waiting_sell"
            s.save(update_fields=[
                "ml_entry_drop_pct",
                "ml_take_profit_pct",
                "ml_stop_loss_pct",
                "ml_position_size_pct",
                "ml_cooldown_seconds",
                "ml_candles_1h",
                "ml_confidence",
                "ml_last_trained_at",
                "ml_last_tick_at",
                "ml_last_price",
                "ml_last_reason",
                "ml_last_entry_trigger",
                "ml_last_tp_trigger",
                "ml_last_sl_trigger",
                "ml_status",
                "ml_stage",
            ])
            updated += 1
    return {"updated": updated}
