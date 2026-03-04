from django.conf import settings
from django.core.cache import cache
from django_huey import db_periodic_task, db_task
import time

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from .services.executor import run_active_sleeves
from .services.trainer import refresh_ml_prices, refresh_ml_vars
from .services.market_scan import refresh_profit_candidates
from .services.candle_cleanup import cleanup_orphan_ml_candles
from .services.order_sync import sync_pending_orders
from wallets.services.balance_refresh import refresh_wallet_balances
from trading.models import SleeveStrategy
from django.utils import timezone

from trading.models import OrderLog
from trading.services.kraken_adapter import GLOBAL_RATE_LIMITER, KrakenAdapter


def _every_n_minutes(n: int):
    n = max(int(n or 1), 1)

    def _validate(dt):
        return (dt.minute % n) == 0

    return _validate


def _every_n_seconds(n: int):
    n = max(int(n or 1), 1)

    def _validate(dt):
        return (dt.second % n) == 0

    return _validate


@db_periodic_task(_every_n_seconds(getattr(settings, "EXECUTOR_TICK_SECONDS", 1)))
def run_all_active_sleeves():
    """Huey periodic task to process active strategies."""
    try:
        cache.set("huey_heartbeat:executor", time.time(), timeout=120)
    except Exception:
        pass
    return run_active_sleeves()


@db_task()
def run_all_active_sleeves_now():
    return run_active_sleeves()


@db_periodic_task(_every_n_minutes(1))
def refresh_all_ml_vars():
    """Periodic task to refresh ML-generated vars for active ML sleeves.

    This is the heavy refresh (OHLC/candles). We run the scheduler tick every minute for
    reliability and apply adaptive throttling so the actual refresh frequency scales with load.
    """
    try:
        cache.set("huey_heartbeat:ml_vars", time.time(), timeout=300)
    except Exception:
        pass
    k_adapt = "ml_vars_refresh_adaptive"
    now = time.time()
    try:
        cached = cache.get(k_adapt) or {}
    except Exception:
        cached = {}
    throttle_s = 0
    try:
        throttle_s = int((cached or {}).get("throttle_s") or 0)
    except Exception:
        throttle_s = 0
    try:
        last_run = float((cached or {}).get("last_run") or 0.0)
    except Exception:
        last_run = 0.0

    if not throttle_s:
        # Conservative defaults.
        # Flash should refresh more often than quiet.
        base_flash = 120  # 2 min
        base_quiet = 300  # 5 min
        slow = 600  # 10 min under higher load

        try:
            active_qs = SleeveStrategy.objects.filter(ml_active=True)
            active_strats = int(active_qs.count())
            active_users = int(active_qs.values("sleeve__wallet__user_id").distinct().count())
            active_flash = int(active_qs.filter(mode__contains="flash").count())
        except Exception:
            active_strats = 0
            active_users = 0
            active_flash = 0

        load = active_strats + max(active_users - 1, 0)
        any_flash = active_flash > 0

        if load <= 2:
            throttle_s = base_flash if any_flash else base_quiet
        elif load <= 4:
            throttle_s = 180 if any_flash else 420
        elif load <= 6:
            throttle_s = 240 if any_flash else 480
        else:
            throttle_s = slow

    if last_run and (now - last_run) < float(throttle_s):
        return {"updated": 0}

    try:
        cache.set(k_adapt, {"throttle_s": int(throttle_s), "last_run": now}, timeout=3600)
    except Exception:
        pass

    return refresh_ml_vars()


@db_periodic_task(_every_n_seconds(1))
def refresh_all_ml_prices():
    """Fast periodic task: update ticker price + trigger targets for active ML sleeves."""
    try:
        cache.set("huey_heartbeat:ml_prices", time.time(), timeout=120)
    except Exception:
        pass
    # Run every second and throttle using shared cache to avoid missing modulo boundaries.
    # Adaptive throttle based on current active ML load.
    # Goal: run fastest when there are few active users/sleeves, slow down as load increases.
    k_adapt = "ml_price_tick_adaptive"
    now = time.time()
    try:
        cached = cache.get(k_adapt) or {}
    except Exception:
        cached = {}
    throttle_s = None
    try:
        throttle_s = int((cached or {}).get("throttle_s") or 0)
    except Exception:
        throttle_s = 0
    try:
        adapt_ts = float((cached or {}).get("ts") or 0.0)
    except Exception:
        adapt_ts = 0.0

    if (not throttle_s) or (now - adapt_ts) > 15:
        # Defaults (safe maximum speed under low load).
        # Flash is intended to react faster than quiet.
        base_fast_flash = 2
        base_fast_quiet = 6
        base_mid = 5
        base_slow = 8
        base_slower = 12
        base_slowest = 20

        try:
            active_qs = SleeveStrategy.objects.filter(ml_active=True)
            active_strats = int(active_qs.count())
            active_users = int(active_qs.values("sleeve__wallet__user_id").distinct().count())
            active_flash = int(active_qs.filter(mode__contains="flash").count())
            active_quiet = int(active_qs.filter(mode__contains="quiet").count())
        except Exception:
            active_strats = 0
            active_users = 0
            active_flash = 0
            active_quiet = 0

        # Heuristic: scale primarily by number of active strategies (sleeves/modes), with a
        # small penalty for multiple distinct users.
        load = active_strats + max(active_users - 1, 0)

        # If only quiet is running, be conservative. If any flash is active, allow faster ticks.
        any_flash = active_flash > 0
        if load <= 1:
            throttle_s = base_fast_flash if any_flash else base_fast_quiet
        elif load <= 2:
            throttle_s = 3 if any_flash else base_mid
        elif load <= 4:
            throttle_s = base_mid if any_flash else base_slow
        elif load <= 6:
            throttle_s = base_slow if any_flash else base_slower
        elif load <= 10:
            throttle_s = base_slower
        else:
            throttle_s = base_slowest

        try:
            cache.set(k_adapt, {"ts": now, "throttle_s": int(throttle_s)}, timeout=60)
        except Exception:
            pass

    throttle_s = max(int(throttle_s or 5), 1)
    k = "ml_price_tick_global"
    try:
        last = float(cache.get(k) or 0.0)
    except Exception:
        last = 0.0
    if last and (now - last) < float(throttle_s):
        return {"updated": 0}
    try:
        cache.set(k, now, timeout=60)
    except Exception:
        pass
    return refresh_ml_prices()


@db_task()
def refresh_all_ml_prices_now():
    return refresh_ml_prices()


@db_task()
def refresh_all_ml_vars_now():
    return refresh_ml_vars()


@db_periodic_task(_every_n_minutes(5))
def refresh_profit_candidates_cache():
    return refresh_profit_candidates(limit=5, quote="USD", scan_pairs_limit=40)


@db_task()
def refresh_profit_candidates_cache_now():
    return refresh_profit_candidates(limit=5, quote="USD", scan_pairs_limit=40)


@db_periodic_task(_every_n_minutes(2))
def refresh_wallet_balances_cache():
    return refresh_wallet_balances()


@db_task()
def refresh_wallet_balances_cache_now():
    return refresh_wallet_balances()


@db_periodic_task(_every_n_minutes(10))
def cleanup_orphan_ml_candles_task():
    return cleanup_orphan_ml_candles()


@db_task()
def cleanup_orphan_ml_candles_now():
    return cleanup_orphan_ml_candles()


@db_periodic_task(_every_n_minutes(1))
def sync_pending_orders_task():
    return sync_pending_orders(limit=40)


@db_task()
def sync_pending_orders_now():
    return sync_pending_orders(limit=100)


@db_task()
def startup_backfill_orderlog_exec_fields():
    k_lock = "startup_backfill:orderlog_exec_fields:running"
    try:
        if not cache.add(k_lock, 1, timeout=3600):
            return {"skipped": True, "reason": "already_running"}
    except Exception:
        pass

    days = int(getattr(settings, "STARTUP_ORDERLOG_BACKFILL_DAYS", 30) or 30)
    limit = max(int(getattr(settings, "STARTUP_ORDERLOG_BACKFILL_LIMIT", 300) or 300), 1)
    batch = max(min(int(getattr(settings, "STARTUP_ORDERLOG_BACKFILL_BATCH", 20) or 20), 50), 1)

    qs_cost = (
        OrderLog.objects.select_related("api_key", "sleeve", "sleeve__wallet")
        .exclude(txid="")
        .filter(status="closed")
        .filter(cost=0)
        .order_by("-created_at")
    )
    qs_fee = (
        OrderLog.objects.select_related("api_key", "sleeve", "sleeve__wallet")
        .exclude(txid="")
        .filter(status="closed")
        .filter(fee=0)
        .order_by("-created_at")
    )
    qs_vol = (
        OrderLog.objects.select_related("api_key", "sleeve", "sleeve__wallet")
        .exclude(txid="")
        .filter(status="closed")
        .filter(vol_exec=0)
        .order_by("-created_at")
    )

    if days and days > 0:
        cutoff = timezone.now() - timedelta(days=days)
        qs_cost = qs_cost.filter(created_at__gte=cutoff)
        qs_fee = qs_fee.filter(created_at__gte=cutoff)
        qs_vol = qs_vol.filter(created_at__gte=cutoff)

    cand_map = {}
    for o in list(qs_cost[:limit]) + list(qs_fee[:limit]) + list(qs_vol[:limit]):
        cand_map[int(o.id)] = o
    orders = list(cand_map.values())
    orders.sort(key=lambda r: r.created_at, reverse=True)
    orders = orders[:limit]
    if not orders:
        return {"checked": 0, "updated": 0, "calls": 0}

    grouped = defaultdict(list)
    for o in orders:
        try:
            grouped[int(o.api_key_id)].append(o)
        except Exception:
            continue

    checked = 0
    updated = 0
    calls = 0

    for api_key_id, rows in grouped.items():
        key = rows[0].api_key
        if not key:
            continue
        try:
            user_id = int(rows[0].sleeve.wallet.user_id)
        except Exception:
            user_id = None

        adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER, user_id=user_id)

        by_txid = {}
        for r in rows:
            t = str(getattr(r, "txid", "") or "").strip()
            if t:
                by_txid[t] = r
        txids = list(by_txid.keys())
        for i in range(0, len(txids), batch):
            chunk = txids[i : i + batch]
            if not chunk:
                continue
            try:
                payload = adapter._private_request("QueryOrders", {"txid": ",".join(chunk)}, weight=1.0)
                calls += 1
            except Exception:
                continue

            result = payload.get("result") or {}
            if not isinstance(result, dict):
                continue

            for txid, meta in result.items():
                checked += 1
                if not isinstance(meta, dict):
                    continue
                match = by_txid.get(str(txid))
                if not match:
                    continue

                try:
                    vol_exec = Decimal(str(meta.get("vol_exec") or 0))
                except Exception:
                    vol_exec = Decimal("0")
                try:
                    cost = Decimal(str(meta.get("cost") or 0))
                except Exception:
                    cost = Decimal("0")
                try:
                    fee = Decimal(str(meta.get("fee") or 0))
                except Exception:
                    fee = Decimal("0")

                avg_price = Decimal("0")
                if vol_exec > 0 and cost > 0:
                    try:
                        avg_price = (cost / vol_exec).quantize(Decimal("0.0000000001"))
                    except Exception:
                        avg_price = Decimal("0")

                update_fields = []
                if vol_exec >= 0 and match.vol_exec != vol_exec:
                    match.vol_exec = vol_exec
                    update_fields.append("vol_exec")
                if cost >= 0 and match.cost != cost:
                    match.cost = cost
                    update_fields.append("cost")
                if fee >= 0 and match.fee != fee:
                    match.fee = fee
                    update_fields.append("fee")
                if avg_price > 0 and match.price != avg_price:
                    match.price = avg_price
                    update_fields.append("price")

                if update_fields:
                    try:
                        match.save(update_fields=update_fields)
                        updated += 1
                    except Exception:
                        pass

    try:
        cache.set("startup_backfill:orderlog_exec_fields:last", {"ts": time.time(), "checked": checked, "updated": updated, "calls": calls}, timeout=86400)
    except Exception:
        pass
    return {"checked": checked, "updated": updated, "calls": calls}
