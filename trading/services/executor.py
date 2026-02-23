import logging
from typing import Optional
from decimal import Decimal
import time
from django.utils import timezone
from django.conf import settings
from django.core.cache import cache

from api_keys.models import ApiKey
from trading.models import OrderLog, SleeveStrategy
from wallets.models import Sleeve

from .kraken_adapter import GLOBAL_RATE_LIMITER, KrakenAdapter
from .autotrade_console import push as _console_push

logger = logging.getLogger(__name__)


_KEY_CURSOR: dict[int, int] = {}

_PAIR_MIN_CACHE: dict[str, object] = {"ts": 0.0, "by_pair": {}}

_CONSOLE_THROTTLE: dict[str, float] = {}


def _pair_ordermin(adapter: KrakenAdapter, pair: str) -> Decimal:
    p = (pair or "").strip()
    if not p:
        return Decimal("0")

    now = time.time()
    try:
        last = float(_PAIR_MIN_CACHE.get("ts") or 0.0)
    except Exception:
        last = 0.0

    by_pair = _PAIR_MIN_CACHE.get("by_pair")
    if not isinstance(by_pair, dict):
        by_pair = {}
        _PAIR_MIN_CACHE["by_pair"] = by_pair

    # Cache for a few minutes to avoid calling AssetPairs every tick.
    if (now - last) < 300 and p in by_pair:
        try:
            return Decimal(str(by_pair.get(p) or 0))
        except Exception:
            return Decimal("0")

    try:
        meta_map = adapter.public_request("AssetPairs", params={"pair": p}).get("result", {})
    except Exception:
        meta_map = {}

    meta = None
    if isinstance(meta_map, dict):
        meta = meta_map.get(p)
        if meta is None:
            meta = next(iter(meta_map.values()), None)

    ordermin = Decimal("0")
    if isinstance(meta, dict) and meta.get("ordermin") is not None:
        try:
            ordermin = Decimal(str(meta.get("ordermin") or 0))
        except Exception:
            ordermin = Decimal("0")

    by_pair[p] = str(ordermin)
    _PAIR_MIN_CACHE["ts"] = now
    return ordermin


def _norm_asset(asset: str) -> str:
    a = (asset or "").strip().upper()
    if a in {"BTC"}:
        return "XBT"
    if a == "BT":
        return "XBT"
    if a.startswith("XX") or a.startswith("ZZ"):
        a = a[1:]
    if a.startswith("Z") and len(a) > 3:
        a = a[1:]
    return a


def _pick_active_key(user_id: int) -> Optional[ApiKey]:
    keys = list(ApiKey.objects.filter(user_id=user_id, is_active=True).order_by("created_at"))
    if not keys:
        return None
    idx = _KEY_CURSOR.get(user_id, 0) % len(keys)
    _KEY_CURSOR[user_id] = (idx + 1) % len(keys)
    return keys[idx]


def _console_push_throttled(user_id: int, sleeve_id: int, reason: str, msg: str, *, level: str = "info", every_s: int = 45) -> None:
    try:
        uid = int(user_id)
        sid = int(sleeve_id)
    except Exception:
        return
    r = (reason or "").strip()
    m = (msg or "").strip()
    if not r or not m:
        return
    try:
        now = float(time.time())
    except Exception:
        now = 0.0

    ttl = max(int(every_s or 1), 1)
    k_cache = f"autotrade_console_throttle:{uid}:{sid}:{r}"
    try:
        # Cross-process throttle (Huey + runserver + reloaders).
        # If key already exists, we have pushed recently and should skip.
        if not cache.add(k_cache, now, timeout=ttl):
            return
    except Exception:
        # Cache unavailable; fall back to per-process throttle.
        pass

    k = f"{uid}:{sid}:{r}"
    try:
        last = float(_CONSOLE_THROTTLE.get(k) or 0.0)
    except Exception:
        last = 0.0
    if last and now and (now - last) < float(ttl):
        return
    _CONSOLE_THROTTLE[k] = now
    try:
        _console_push(uid, m, level=level)
    except Exception:
        return


def run_active_sleeves():
    """Run one pass over all active sleeve strategies.

    This is intentionally simple and synchronous; Celery beat can trigger it periodically,
    and Celery workers execute it. Extend with per-user queues or concurrency later.
    """
    strategies = (
        SleeveStrategy.objects.select_related("sleeve", "sleeve__wallet", "sleeve__wallet__user")
        .filter(is_active=True)
    )

    # Adaptive global limiter: adjust based on number of distinct active users
    active_user_ids = {s.sleeve.wallet.user_id for s in strategies}
    GLOBAL_RATE_LIMITER.set_active_users(len(active_user_ids) or 1)

    for strategy in strategies:
        sleeve: Sleeve = strategy.sleeve
        user = sleeve.wallet.user
        user_id = getattr(user, "id", None)

        # Throttle per strategy to avoid hitting Kraken too frequently.
        # Run the executor task every second, but only do the full tick at:
        # - BUY: every ~EXECUTOR_BUY_TICK_SECONDS
        # - SELL: every ~EXECUTOR_SELL_TICK_SECONDS
        try:
            owned_qty_pre = Decimal(str(getattr(sleeve, "position_base_qty", 0) or 0))
        except Exception:
            owned_qty_pre = Decimal("0")
        side_pre = OrderLog.Side.BUY if str(getattr(strategy, "mode", "") or "").startswith("buy") else OrderLog.Side.SELL
        sleeve_type = str(getattr(sleeve, "type", "") or "").lower()
        is_flash = sleeve_type == "flash" or "flash" in str(getattr(strategy, "mode", "") or "")

        if is_flash:
            want_buy = int(getattr(settings, "EXECUTOR_BUY_TICK_SECONDS_FLASH", getattr(settings, "EXECUTOR_BUY_TICK_SECONDS", 4)) or 4)
            want_sell = int(getattr(settings, "EXECUTOR_SELL_TICK_SECONDS_FLASH", getattr(settings, "EXECUTOR_SELL_TICK_SECONDS", 1)) or 1)
        else:
            want_buy = int(getattr(settings, "EXECUTOR_BUY_TICK_SECONDS_QUIET", getattr(settings, "EXECUTOR_BUY_TICK_SECONDS", 4)) or 15)
            want_sell = int(getattr(settings, "EXECUTOR_SELL_TICK_SECONDS_QUIET", getattr(settings, "EXECUTOR_SELL_TICK_SECONDS", 1)) or 5)

        want_sec = want_buy
        if side_pre == OrderLog.Side.SELL and owned_qty_pre > 0:
            want_sec = want_sell
        want_sec = max(int(want_sec or 1), 1)

        # IMPORTANT: do NOT throttle off ml_last_tick_at because price ticks also update it.
        # Use a dedicated cache key so the executor always runs at the intended cadence.
        now_ts = time.time()
        k_exec = f"executor_tick:{int(getattr(strategy, 'id', 0) or 0)}"
        try:
            last_ts = float(cache.get(k_exec) or 0.0)
        except Exception:
            last_ts = 0.0
        if last_ts and (now_ts - last_ts) < float(want_sec):
            continue
        try:
            cache.set(k_exec, now_ts, timeout=max(60, int(want_sec * 5)))
        except Exception:
            pass

        key = _pick_active_key(user.id)
        if not key:
            logger.warning("No active API key for user %s; skipping sleeve %s", user, sleeve)
            try:
                _console_push(user_id, f"skip sleeve={sleeve.id} no_active_api_key", level="warn")
            except Exception:
                pass
            continue

        adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER)

        # Pair must be explicitly selected; do not fallback to a default.
        pair = (strategy.ml_pair or "").strip() or (strategy.params.get("pair") if strategy.params else "")
        pair = (pair or "").strip()
        if not pair:
            logger.warning("No pair selected for sleeve %s strategy %s; skipping", sleeve.id, strategy.id)
            try:
                _console_push(user_id, f"skip sleeve={sleeve.id} no_pair_selected", level="warn")
            except Exception:
                pass
            continue

        is_custom = getattr(sleeve, "trade_mode", "auto") == "custom"
        use_ml = bool(strategy.ml_active) and not is_custom

        # Confidence gating: quiet should be strict (~90%+), flash should be more aggressive.
        if use_ml:
            try:
                conf = float(getattr(strategy, "ml_confidence", 0) or 0)
            except Exception:
                conf = 0.0
            mode_str = str(getattr(strategy, "mode", "") or "")
            if "quiet" in mode_str:
                min_conf = float(getattr(settings, "ML_CONFIDENCE_MIN_QUIET", 0.90) or 0.90)
            else:
                min_conf = float(getattr(settings, "ML_CONFIDENCE_MIN_FLASH", 0.55) or 0.55)
            if conf < min_conf:
                side_gate = OrderLog.Side.BUY if mode_str.startswith("buy") else OrderLog.Side.SELL
                want_stage = "waiting_buy" if side_gate == OrderLog.Side.BUY else "waiting_sell"
                strategy.ml_last_tick_at = timezone.now()
                strategy.ml_last_reason = "blocked_confidence_low"
                try:
                    strategy.save(update_fields=["ml_last_tick_at", "ml_last_reason"])
                except Exception:
                    pass
                if getattr(strategy, "ml_stage", "") != want_stage:
                    strategy.ml_stage = want_stage
                    try:
                        strategy.save(update_fields=["ml_stage"])
                    except Exception:
                        pass
                trained_ts = 0.0
                try:
                    trained_ts = float(getattr(getattr(strategy, "ml_last_trained_at", None), "timestamp", lambda: 0.0)())
                except Exception:
                    trained_ts = 0.0

                trained_key = "0"
                try:
                    trained_key = str(int(trained_ts or 0.0))
                except Exception:
                    trained_key = "0"

                _console_push_throttled(
                    user_id,
                    sleeve.id,
                    f"blocked_confidence_low:{mode_str}:{trained_key}",
                    f"blocked sleeve={sleeve.id} mode={mode_str} pair={(strategy.ml_pair or '').strip()} reason=blocked_confidence_low conf={conf:.4f} min={min_conf:.2f}",
                    level="warn",
                    every_s=3600,
                )
                continue

        if (use_ml or is_custom) and getattr(strategy, "ml_paused", False):
            if getattr(strategy, "ml_stage", "") != "paused":
                strategy.ml_stage = "paused"
                strategy.save(update_fields=["ml_stage"])
            try:
                _console_push(user_id, f"paused sleeve={sleeve.id} pair={pair}")
            except Exception:
                pass
            continue

        # enforce cooldown for ML
        cooldown_seconds = int(strategy.ml_cooldown_seconds or 0) if use_ml else int(
            (strategy.params or {}).get("cooldown_seconds", 0) or 0
        )
        if (use_ml or is_custom) and cooldown_seconds and strategy.ml_last_action_at:
            delta = timezone.now() - strategy.ml_last_action_at
            if delta.total_seconds() < cooldown_seconds:
                if getattr(strategy, "ml_stage", "") != "cooldown":
                    strategy.ml_stage = "cooldown"
                    strategy.save(update_fields=["ml_stage"])
                try:
                    _console_push(user_id, f"cooldown sleeve={sleeve.id} pair={pair} remaining={int(cooldown_seconds - delta.total_seconds())}s")
                except Exception:
                    pass
                continue

        # fetch current price for volume/targets
        try:
            ticker = adapter.fetch_ticker(pair)
            price_now = Decimal(str(ticker.get("price") or 0))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ticker fetch failed for %s: %s", pair, exc)
            try:
                _console_push(user_id, f"error sleeve={sleeve.id} pair={pair} ticker_fetch_failed: {exc}", level="error")
            except Exception:
                pass
            continue
        if price_now <= 0:
            try:
                _console_push(user_id, f"skip sleeve={sleeve.id} pair={pair} price_now<=0", level="warn")
            except Exception:
                pass
            continue

        # side
        side = OrderLog.Side.BUY if strategy.mode.startswith("buy") else OrderLog.Side.SELL

        owned_qty = Decimal(str(getattr(sleeve, "position_base_qty", 0) or 0))

        if use_ml or is_custom:
            strategy.ml_last_tick_at = timezone.now()
            strategy.ml_last_price = price_now

        # If we have a pending order for this sleeve, do not flip stages/modes.
        pending_exists = OrderLog.objects.filter(
            sleeve=sleeve,
            txid__gt="",
            status__in=["submitted", "open", "pending"],
        ).exists()
        if pending_exists and (use_ml or is_custom):
            strategy.ml_last_reason = "pending_fill"
            strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
            if getattr(strategy, "ml_stage", "") != "pending_fill":
                strategy.ml_stage = "pending_fill"
                strategy.save(update_fields=["ml_stage"])
            try:
                _console_push(user_id, f"pending_fill sleeve={sleeve.id} pair={pair}")
            except Exception:
                pass
            continue

        # Isolated default behavior:
        # - If we own nothing: we are in buy mode, sell strategies should not run.
        # - If we own something: buy strategies should not run, sell strategies can run.
        if (use_ml or is_custom):
            if owned_qty <= 0 and side == OrderLog.Side.SELL:
                strategy.ml_last_reason = "blocked_sell_no_position"
                strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                if getattr(strategy, "ml_stage", "") != "waiting_buy":
                    strategy.ml_stage = "waiting_buy"
                    strategy.save(update_fields=["ml_stage"])
                try:
                    _console_push(user_id, f"blocked sleeve={sleeve.id} pair={pair} reason=blocked_sell_no_position", level="warn")
                except Exception:
                    pass
                continue
            if owned_qty > 0 and side == OrderLog.Side.BUY:
                strategy.ml_last_reason = "blocked_buy_position_open"
                strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                if getattr(strategy, "ml_stage", "") != "waiting_sell":
                    strategy.ml_stage = "waiting_sell"
                    strategy.save(update_fields=["ml_stage"])
                try:
                    _console_push(user_id, f"blocked sleeve={sleeve.id} pair={pair} reason=blocked_buy_position_open", level="warn")
                except Exception:
                    pass
                continue

        if use_ml or is_custom:
            want_stage = "waiting_buy" if side == OrderLog.Side.BUY else "waiting_sell"
            if getattr(strategy, "ml_stage", "") != want_stage:
                strategy.ml_stage = want_stage
                strategy.save(update_fields=["ml_stage"])

        # derive volume from allocated_balance and position size
        alloc = strategy.sleeve.allocated_balance or Decimal("0")
        if use_ml:
            size_pct = Decimal(str(strategy.ml_position_size_pct))
        else:
            size_pct = Decimal(str((strategy.params or {}).get("position_size_pct", 0) or 0))
        if side == OrderLog.Side.SELL and (use_ml or is_custom):
            # Sell based on base holdings.
            volume = float((owned_qty * size_pct / Decimal("100")) if size_pct > 0 else Decimal("0"))
        else:
            vol_quote = alloc * size_pct / Decimal("100")
            volume = float((vol_quote / price_now) if price_now > 0 else Decimal("0"))

        # price targets
        if use_ml:
            entry_drop = Decimal(str(strategy.ml_entry_drop_pct)) / Decimal("100")
            take_profit = Decimal(str(strategy.ml_take_profit_pct)) / Decimal("100")
            stop_loss = Decimal(str(strategy.ml_stop_loss_pct)) / Decimal("100")
            if side == OrderLog.Side.BUY:
                price = float(price_now * (Decimal("1") - entry_drop))
            else:
                price = float(price_now * (Decimal("1") + take_profit))
        else:
            price = strategy.params.get("price") if strategy.params else None

        if use_ml or is_custom:
            entry_drop2 = Decimal(str((strategy.ml_entry_drop_pct if use_ml else (strategy.params or {}).get("entry_drop_pct", 0)) or 0)) / Decimal("100")
            take_profit2 = Decimal(str((strategy.ml_take_profit_pct if use_ml else (strategy.params or {}).get("take_profit_pct", 0)) or 0)) / Decimal("100")
            stop_loss2 = Decimal(str((strategy.ml_stop_loss_pct if use_ml else (strategy.params or {}).get("stop_loss_pct", 0)) or 0)) / Decimal("100")
            strategy.ml_last_entry_trigger = (price_now * (Decimal("1") - entry_drop2)).quantize(Decimal("0.0000000001"))
            strategy.ml_last_tp_trigger = (price_now * (Decimal("1") + take_profit2)).quantize(Decimal("0.0000000001"))
            strategy.ml_last_sl_trigger = (
                (price_now * (Decimal("1") + stop_loss2)) if side == OrderLog.Side.BUY else (price_now * (Decimal("1") - stop_loss2))
            ).quantize(Decimal("0.0000000001"))

        if volume <= 0:
            try:
                _console_push(user_id, f"skip sleeve={sleeve.id} pair={pair} volume<=0", level="warn")
            except Exception:
                pass
            continue

        # Enforce Kraken minimum order size (Option B): bump volume up to ordermin.
        # If the bumped minimum would exceed sleeve funds/position, skip the trade and surface why.
        if use_ml or is_custom:
            try:
                ordermin = _pair_ordermin(adapter, pair)
            except Exception:
                ordermin = Decimal("0")

            try:
                vol_base_dec = Decimal(str(volume))
            except Exception:
                vol_base_dec = Decimal("0")

            bumped = vol_base_dec
            if ordermin > 0 and vol_base_dec < ordermin:
                bumped = ordermin

            if side == OrderLog.Side.SELL:
                # Sell based on base holdings (sleeve isolated position).
                if bumped > owned_qty:
                    strategy.ml_last_reason = "blocked_sell_min_exceeds_position"
                    strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                    if getattr(strategy, "ml_stage", "") != "waiting_sell":
                        strategy.ml_stage = "waiting_sell"
                        strategy.save(update_fields=["ml_stage"])
                    try:
                        _console_push(user_id, f"blocked sleeve={sleeve.id} pair={pair} reason=blocked_sell_min_exceeds_position bumped={bumped} owned={owned_qty}", level="warn")
                    except Exception:
                        pass
                    continue
            else:
                # Buy based on quote allocation.
                try:
                    req_quote = (bumped * price_now)
                except Exception:
                    req_quote = Decimal("0")
                if req_quote > alloc:
                    strategy.ml_last_reason = "blocked_buy_min_exceeds_allocated"
                    strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                    if getattr(strategy, "ml_stage", "") != "waiting_buy":
                        strategy.ml_stage = "waiting_buy"
                        strategy.save(update_fields=["ml_stage"])
                    try:
                        _console_push(user_id, f"blocked sleeve={sleeve.id} pair={pair} reason=blocked_buy_min_exceeds_allocated bumped={bumped} req_quote={req_quote} alloc={alloc}", level="warn")
                    except Exception:
                        pass
                    continue

            volume = float(bumped)

        # exit logic: if ml_active and side is sell, ensure TP/SL zones; for buy, check if price already above TP etc.
        if use_ml or is_custom:
            entry_drop = Decimal(str((strategy.ml_entry_drop_pct if use_ml else (strategy.params or {}).get("entry_drop_pct", 0)) or 0)) / Decimal("100")
            take_profit = Decimal(str((strategy.ml_take_profit_pct if use_ml else (strategy.params or {}).get("take_profit_pct", 0)) or 0)) / Decimal("100")
            stop_loss = Decimal(str((strategy.ml_stop_loss_pct if use_ml else (strategy.params or {}).get("stop_loss_pct", 0)) or 0)) / Decimal("100")
            if side == OrderLog.Side.SELL:
                # if price is already below SL, exit at market
                sl_trigger = price_now * (Decimal("1") - stop_loss)
                tp_trigger = price_now * (Decimal("1") + take_profit)
                if price_now <= sl_trigger or price_now >= tp_trigger:
                    price = None  # market
                    if getattr(strategy, "ml_stage", "") != "placing_order":
                        strategy.ml_stage = "placing_order"
                        strategy.save(update_fields=["ml_stage"])
                    try:
                        _console_push(user_id, f"auto_trade sleeve={sleeve.id} pair={pair} side={side} ml_stage=placing_order", level="info")
                    except Exception:
                        pass
                else:
                    strategy.ml_last_reason = "waiting_sell_not_in_exit_zone"
                    strategy.save(update_fields=[
                        "ml_last_tick_at",
                        "ml_last_price",
                        "ml_last_reason",
                        "ml_last_tp_trigger",
                        "ml_last_sl_trigger",
                    ])
                    if getattr(strategy, "ml_stage", "") != "waiting_sell":
                        strategy.ml_stage = "waiting_sell"
                        strategy.save(update_fields=["ml_stage"])
                    _console_push_throttled(
                        user_id,
                        sleeve.id,
                        "waiting_sell_not_in_exit_zone",
                        f"waiting sleeve={sleeve.id} pair={pair} reason=waiting_sell_not_in_exit_zone",
                        level="info",
                    )
                    continue
            else:
                # buy: if price moved unfavorably beyond SL band, skip
                sl_trigger = price_now * (Decimal("1") + stop_loss)
                if price_now >= sl_trigger:
                    strategy.ml_last_reason = "waiting_buy_price_above_sl_band"
                    strategy.save(update_fields=[
                        "ml_last_tick_at",
                        "ml_last_price",
                        "ml_last_reason",
                        "ml_last_entry_trigger",
                        "ml_last_tp_trigger",
                        "ml_last_sl_trigger",
                    ])
                    if getattr(strategy, "ml_stage", "") != "waiting_buy":
                        strategy.ml_stage = "waiting_buy"
                        strategy.save(update_fields=["ml_stage"])
                    _console_push_throttled(
                        user_id,
                        sleeve.id,
                        "waiting_buy_price_above_sl_band",
                        f"waiting sleeve={sleeve.id} pair={pair} reason=waiting_buy_price_above_sl_band",
                        level="info",
                    )
                    continue
                # if price already dropped beyond entry target, go market
                entry_trigger = price_now * (Decimal("1") - entry_drop)
                if price_now <= entry_trigger:
                    price = None
                    if getattr(strategy, "ml_stage", "") != "placing_order":
                        strategy.ml_stage = "placing_order"
                        strategy.save(update_fields=["ml_stage"])
                else:
                    strategy.ml_last_reason = "waiting_buy_price_not_at_entry"
                    strategy.save(update_fields=[
                        "ml_last_tick_at",
                        "ml_last_price",
                        "ml_last_reason",
                        "ml_last_entry_trigger",
                        "ml_last_tp_trigger",
                        "ml_last_sl_trigger",
                    ])
                    if getattr(strategy, "ml_stage", "") != "waiting_buy":
                        strategy.ml_stage = "waiting_buy"
                        strategy.save(update_fields=["ml_stage"])
                    _console_push_throttled(
                        user_id,
                        sleeve.id,
                        "waiting_buy_price_not_at_entry",
                        f"waiting sleeve={sleeve.id} pair={pair} reason=waiting_buy_price_not_at_entry",
                        level="info",
                    )
                    continue

        try:
            result = adapter.place_order(side=side, pair=pair, volume=volume, price=price)
            txid_val = result.get("txid")
            txid = ""
            if isinstance(txid_val, list) and txid_val:
                txid = str(txid_val[0])
            elif txid_val:
                txid = str(txid_val)
            OrderLog.objects.create(
                sleeve=sleeve,
                api_key=key,
                side=side,
                base_asset=_norm_asset(getattr(sleeve, "base_asset", "") or ""),
                quote_asset=_norm_asset(getattr(sleeve.wallet, "currency", "") or ""),
                amount=volume,
                price=price if price is not None else price_now,
                txid=txid,
                status=result.get("status", "submitted"),
                error="",
            )
            if use_ml or is_custom:
                strategy.ml_last_action_at = timezone.now()
                strategy.ml_stage = "pending_fill" if txid else (
                    "cooldown" if cooldown_seconds else strategy.ml_stage
                )
                strategy.save(update_fields=["ml_last_action_at", "ml_stage"])
            try:
                _console_push(
                    user_id,
                    f"order sleeve={sleeve.id} pair={pair} side={side} volume={volume} price={'market' if price is None else price} txid={txid} status={result.get('status', '')}",
                )
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            logger.exception("Order attempt failed for sleeve %s", sleeve)
            if use_ml or is_custom:
                strategy.ml_stage = "error"
                strategy.save(update_fields=["ml_stage"])
            try:
                _console_push(user_id, f"error sleeve={sleeve.id} pair={pair} order_failed: {exc}", level="error")
            except Exception:
                pass
            OrderLog.objects.create(
                sleeve=sleeve,
                api_key=key,
                side=side,
                base_asset=_norm_asset(getattr(sleeve, "base_asset", "") or ""),
                quote_asset=_norm_asset(getattr(sleeve.wallet, "currency", "") or ""),
                amount=volume,
                price=price if price is not None else price_now,
                status="error",
                error=str(exc),
            )

    return True
