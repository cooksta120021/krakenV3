import logging
from typing import Optional
from decimal import Decimal, ROUND_DOWN
import time
from datetime import timedelta, datetime, time as dt_time
from django.utils import timezone
from django.conf import settings
from django.core.cache import cache

from api_keys.models import ApiKey
from trading.models import OrderLog, SleeveStrategy
from wallets.models import Sleeve

from .kraken_adapter import GLOBAL_RATE_LIMITER, KrakenAdapter
from .autotrade_console import push as _console_push

logger = logging.getLogger(__name__)


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    p = max(int(period or 0), 1)
    alpha = 2.0 / (p + 1.0)
    out: list[float] = []
    cur = float(values[0])
    out.append(cur)
    for v in values[1:]:
        cur = (alpha * float(v)) + ((1.0 - alpha) * cur)
        out.append(cur)
    return out


def _rsi(values: list[float], period: int = 14) -> float:
    p = max(int(period or 0), 1)
    if len(values) < p + 1:
        return 50.0
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(values)):
        chg = float(values[i]) - float(values[i - 1])
        gains.append(max(0.0, chg))
        losses.append(max(0.0, -chg))
    gains = gains[-p:]
    losses = losses[-p:]
    avg_gain = sum(gains) / float(p)
    avg_loss = sum(losses) / float(p)
    if avg_loss <= 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _macd_hist(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float]:
    if not values or len(values) < max(int(slow or 0), 1) + 2:
        return 0.0, 0.0
    ema_f = _ema(values, fast)
    ema_s = _ema(values, slow)
    macd = [float(a) - float(b) for a, b in zip(ema_f, ema_s)]
    sig = _ema(macd, signal)
    hist = [float(m) - float(s) for m, s in zip(macd, sig)]
    if len(hist) < 2:
        return float(hist[-1] if hist else 0.0), 0.0
    return float(hist[-1]), float(hist[-2])


def _ohlc_close_series(adapter: KrakenAdapter, pair: str, interval_min: int, want: int) -> list[float]:
    try:
        interval_min = int(interval_min or 0)
    except Exception:
        interval_min = 5
    if interval_min <= 0:
        interval_min = 5
    try:
        want = int(want or 0)
    except Exception:
        want = 120
    want = max(10, min(want, 720))

    k = f"ohlc_closes:{pair}:{interval_min}"
    cached = cache.get(k)
    if isinstance(cached, list) and cached:
        try:
            vals = [float(x) for x in cached if x is not None]
            if len(vals) >= 10:
                return vals[-want:]
        except Exception:
            pass

    payload = adapter.fetch_ohlc(pair, interval=interval_min)
    result = payload.get("result") or {}
    series = None
    if isinstance(result, dict):
        for k2, v2 in result.items():
            if k2 == "last":
                continue
            if k2 == pair or str(k2).strip() == str(pair).strip():
                series = v2
                break
        if series is None:
            series = next((v for kk, v in result.items() if kk != "last"), None)
    closes: list[float] = []
    if isinstance(series, list):
        for row in series[-want:]:
            try:
                closes.append(float(row[4]))
            except Exception:
                continue
    if closes:
        try:
            cache.set(k, closes[-want:], timeout=20)
        except Exception:
            pass
    return closes[-want:]


def _order_quote_cost(o: OrderLog) -> Decimal:
    """Quote amount excluding fee. Kraken 'cost' is quote for both buys and sells."""
    try:
        c = Decimal(str(getattr(o, "cost", 0) or 0))
    except Exception:
        c = Decimal("0")
    if c > 0:
        return c
    try:
        vol = Decimal(str(getattr(o, "vol_exec", 0) or 0))
    except Exception:
        vol = Decimal("0")
    try:
        px = Decimal(str(getattr(o, "price", 0) or 0))
    except Exception:
        px = Decimal("0")
    if vol > 0 and px > 0:
        try:
            return (vol * px).quantize(Decimal("0.00000001"))
        except Exception:
            return vol * px
    return Decimal("0")


def _order_quote_fee(o: OrderLog) -> Decimal:
    try:
        return Decimal(str(getattr(o, "fee", 0) or 0))
    except Exception:
        return Decimal("0")


def _pnl_quote_for_closed_sell_naive(sleeve: Sleeve, sell_o: OrderLog) -> Decimal | None:
    if not sell_o or str(getattr(sell_o, "status", "") or "").lower() != "closed":
        return None
    if str(getattr(sell_o, "side", "") or "").lower() != str(OrderLog.Side.SELL).lower():
        return None

    try:
        buy_o = (
            OrderLog.objects.filter(
                sleeve=sleeve,
                status="closed",
                side=OrderLog.Side.BUY,
                created_at__lt=sell_o.created_at,
            )
            .order_by("-created_at")
            .first()
        )
    except Exception:
        buy_o = None
    if not buy_o:
        return None

    try:
        sell_vol = Decimal(str(getattr(sell_o, "vol_exec", 0) or 0))
    except Exception:
        sell_vol = Decimal("0")
    try:
        buy_vol = Decimal(str(getattr(buy_o, "vol_exec", 0) or 0))
    except Exception:
        buy_vol = Decimal("0")
    if sell_vol <= 0 or buy_vol <= 0:
        return None

    ratio = Decimal("1")
    try:
        ratio = min(Decimal("1"), (sell_vol / buy_vol))
    except Exception:
        ratio = Decimal("1")

    buy_quote = (_order_quote_cost(buy_o) + _order_quote_fee(buy_o)) * ratio
    sell_quote = _order_quote_cost(sell_o) - _order_quote_fee(sell_o)
    try:
        return (sell_quote - buy_quote).quantize(Decimal("0.00000001"))
    except Exception:
        return sell_quote - buy_quote


def _realized_pnl_fifo_map(orders: list[OrderLog]) -> dict[int, Decimal]:
    """Return realized PnL per SELL order id using FIFO cost basis."""
    out: dict[int, Decimal] = {}
    buy_lots: list[dict[str, Decimal]] = []

    for o in orders:
        if not o or str(getattr(o, "status", "") or "").lower() != "closed":
            continue
        side = str(getattr(o, "side", "") or "").lower()
        try:
            qty = Decimal(str(getattr(o, "vol_exec", 0) or 0))
        except Exception:
            qty = Decimal("0")
        if qty <= 0:
            continue

        if side == str(OrderLog.Side.BUY).lower():
            try:
                total_quote = _order_quote_cost(o) + _order_quote_fee(o)
            except Exception:
                total_quote = Decimal("0")
            if total_quote <= 0:
                continue
            try:
                ppu = (total_quote / qty).quantize(Decimal("0.0000000001"))
            except Exception:
                ppu = total_quote / qty
            buy_lots.append({"qty": qty, "ppu": ppu})
            continue

        if side != str(OrderLog.Side.SELL).lower():
            continue

        try:
            sell_total_quote = _order_quote_cost(o) - _order_quote_fee(o)
        except Exception:
            sell_total_quote = Decimal("0")
        if sell_total_quote <= 0:
            continue
        try:
            sell_ppu = (sell_total_quote / qty).quantize(Decimal("0.0000000001"))
        except Exception:
            sell_ppu = sell_total_quote / qty

        remaining = qty
        pnl = Decimal("0")
        while remaining > 0 and buy_lots:
            lot = buy_lots[0]
            try:
                lot_qty = Decimal(str(lot.get("qty") or 0))
                lot_ppu = Decimal(str(lot.get("ppu") or 0))
            except Exception:
                lot_qty = Decimal("0")
                lot_ppu = Decimal("0")
            if lot_qty <= 0:
                buy_lots.pop(0)
                continue

            take = remaining if remaining <= lot_qty else lot_qty
            pnl += (sell_ppu - lot_ppu) * take
            remaining -= take
            lot_qty -= take
            lot["qty"] = lot_qty
            if lot_qty <= 0:
                buy_lots.pop(0)

        if remaining > 0:
            continue

        try:
            out[int(getattr(o, "id", 0) or 0)] = pnl.quantize(Decimal("0.00000001"))
        except Exception:
            try:
                out[int(getattr(o, "id", 0) or 0)] = pnl
            except Exception:
                pass

    return out


def _pnl_quote_for_closed_sell(sleeve: Sleeve, sell_o: OrderLog) -> Decimal | None:
    try:
        oid = int(getattr(sell_o, "id", 0) or 0)
    except Exception:
        oid = 0
    if not oid:
        return _pnl_quote_for_closed_sell_naive(sleeve, sell_o)

    try:
        lookback = timezone.now() - timedelta(days=30)
        rows = list(
            OrderLog.objects.filter(sleeve=sleeve, status="closed", created_at__gte=lookback)
            .exclude(txid="")
            .order_by("-created_at")[:1000]
        )
        rows.reverse()
        pnl_map = _realized_pnl_fifo_map(rows)
        v = pnl_map.get(oid)
        if v is not None:
            return v
    except Exception:
        pass
    return _pnl_quote_for_closed_sell_naive(sleeve, sell_o)


def _risk_caps_for_sleeve(sleeve: Sleeve) -> tuple[bool, str, str]:
    """Return (blocked, reason, detail). Uses cache to reduce DB load."""
    try:
        sid = int(getattr(sleeve, "id", 0) or 0)
    except Exception:
        sid = 0
    if not sid:
        return False, "", ""

    k = f"risk_caps:{sid}"
    cached = cache.get(k)
    if isinstance(cached, dict) and "blocked" in cached:
        try:
            return bool(cached.get("blocked")), str(cached.get("reason") or ""), str(cached.get("detail") or "")
        except Exception:
            pass

    try:
        max_consec = int(getattr(settings, "EXECUTOR_RISK_MAX_CONSEC_LOSSES", 3) or 3)
    except Exception:
        max_consec = 3
    try:
        max_daily_loss = Decimal(str(getattr(settings, "EXECUTOR_RISK_MAX_DAILY_LOSS_QUOTE", 2) or 2))
    except Exception:
        max_daily_loss = Decimal("2")
    max_consec = max(1, min(int(max_consec or 3), 10))
    if max_daily_loss < 0:
        max_daily_loss = Decimal("0")

    # Daily realized PnL is computed from closed SELL legs matched to prior closed BUY.
    # This avoids treating an open BUY (unrealized) as a realized daily loss.
    try:
        day = timezone.localdate()
        naive_start = datetime.combine(day, dt_time.min)
        if bool(getattr(settings, "USE_TZ", True)):
            start = timezone.make_aware(naive_start, timezone.get_current_timezone())
        else:
            start = naive_start
    except Exception:
        start = timezone.now() - timedelta(hours=24)

    try:
        lookback = start - timedelta(days=30)
    except Exception:
        lookback = timezone.now() - timedelta(days=30)

    orders: list[OrderLog] = []
    try:
        orders = list(
            OrderLog.objects.filter(sleeve=sleeve, status="closed", created_at__gte=lookback)
            .exclude(txid="")
            .order_by("-created_at")[:1000]
        )
        orders.reverse()
    except Exception:
        orders = []

    pnl_map: dict[int, Decimal] = {}
    try:
        pnl_map = _realized_pnl_fifo_map(orders)
    except Exception:
        pnl_map = {}

    pnl_today = Decimal("0")
    for o in orders:
        if str(getattr(o, "side", "") or "").lower() != str(OrderLog.Side.SELL).lower():
            continue
        try:
            if o.created_at < start:
                continue
        except Exception:
            pass
        try:
            oid = int(getattr(o, "id", 0) or 0)
        except Exception:
            oid = 0
        p = pnl_map.get(oid)
        if p is None:
            p = _pnl_quote_for_closed_sell_naive(sleeve, o)
        if p is not None:
            pnl_today += p

    if max_daily_loss > 0 and pnl_today <= (Decimal("0") - max_daily_loss):
        out = {
            "blocked": True,
            "reason": "blocked_risk_daily_loss_cap",
            "detail": f"pnl_today={pnl_today} cap=-{max_daily_loss}",
        }
        try:
            cache.set(k, out, timeout=20)
        except Exception:
            pass
        return True, str(out["reason"]), str(out["detail"])

    # Consecutive losing trades: look at most recent closed sells and count negative PnL streak.
    consec_losses = 0
    sells_seen = 0
    for o in reversed(orders):
        if sells_seen >= max_consec:
            break
        if str(getattr(o, "side", "") or "").lower() != str(OrderLog.Side.SELL).lower():
            continue
        sells_seen += 1
        try:
            oid = int(getattr(o, "id", 0) or 0)
        except Exception:
            oid = 0
        p = pnl_map.get(oid)
        if p is None:
            p = _pnl_quote_for_closed_sell_naive(sleeve, o)
        if p is None:
            break
        if p < 0:
            consec_losses += 1
        else:
            break
    if consec_losses >= max_consec:
        out = {
            "blocked": True,
            "reason": "blocked_risk_consecutive_loss_cap",
            "detail": f"consec_losses={consec_losses} cap={max_consec}",
        }
        try:
            cache.set(k, out, timeout=20)
        except Exception:
            pass
        return True, str(out["reason"]), str(out["detail"])

    out = {"blocked": False, "reason": "", "detail": ""}
    try:
        cache.set(k, out, timeout=20)
    except Exception:
        pass
    return False, "", ""


_KEY_CURSOR: dict[int, int] = {}

_PAIR_MIN_CACHE: dict[str, object] = {"ts": 0.0, "by_pair": {}}

_PAIR_DEC_CACHE: dict[str, object] = {"ts": 0.0, "by_pair": {}}

_CONSOLE_THROTTLE: dict[str, float] = {}


def _last_filled_buy_for_sleeve(sleeve: Sleeve, pair: str) -> OrderLog | None:
    try:
        qs = (
            OrderLog.objects.filter(
                sleeve=sleeve,
                side=OrderLog.Side.BUY,
                status="closed",
                txid__gt="",
            )
            .order_by("-created_at")
        )
        return qs.first()
    except Exception:
        return None


def _entry_price_from_order(o: OrderLog | None) -> Decimal:
    if o is None:
        return Decimal("0")
    try:
        px = Decimal(str(getattr(o, "price", 0) or 0))
    except Exception:
        px = Decimal("0")
    if px > 0:
        return px
    try:
        v = Decimal(str(getattr(o, "vol_exec", 0) or 0))
        c = Decimal(str(getattr(o, "cost", 0) or 0))
    except Exception:
        v, c = Decimal("0"), Decimal("0")
    if v > 0 and c > 0:
        try:
            return (c / v).quantize(Decimal("0.0000000001"))
        except Exception:
            return Decimal("0")
    return Decimal("0")


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
            v = Decimal(str(by_pair.get(p) or 0))
            try:
                cache.set(f"kraken_ordermin:{p}", str(v), timeout=24 * 3600)
            except Exception:
                pass
            return v
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
    try:
        cache.set(f"kraken_ordermin:{p}", str(ordermin), timeout=24 * 3600)
    except Exception:
        pass
    return ordermin


def _pair_decimals(adapter: KrakenAdapter, pair: str) -> int:
    p = (pair or "").strip()
    if not p:
        return 0

    now = time.time()
    try:
        last = float(_PAIR_DEC_CACHE.get("ts") or 0.0)
    except Exception:
        last = 0.0

    by_pair = _PAIR_DEC_CACHE.get("by_pair")
    if not isinstance(by_pair, dict):
        by_pair = {}
        _PAIR_DEC_CACHE["by_pair"] = by_pair

    if (now - last) < 300 and p in by_pair:
        try:
            return int(by_pair.get(p) or 0)
        except Exception:
            return 0

    try:
        meta_map = adapter.public_request("AssetPairs", params={"pair": p}).get("result", {})
    except Exception:
        meta_map = {}

    meta = None
    if isinstance(meta_map, dict):
        meta = meta_map.get(p)
        if meta is None:
            meta = next(iter(meta_map.values()), None)

    dec = 0
    if isinstance(meta, dict) and meta.get("pair_decimals") is not None:
        try:
            dec = int(meta.get("pair_decimals") or 0)
        except Exception:
            dec = 0

    by_pair[p] = int(dec)
    _PAIR_DEC_CACHE["ts"] = now
    return int(dec)


def _round_pair_price(price: Decimal, decimals: int) -> Decimal:
    try:
        d = int(decimals or 0)
    except Exception:
        d = 0
    if d < 0:
        d = 0
    q = Decimal("1") if d == 0 else Decimal("1").scaleb(-d)
    try:
        return Decimal(str(price)).quantize(q, rounding=ROUND_DOWN)
    except Exception:
        return Decimal(str(price))


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


def _observed_fee_pct_for_sleeve(sleeve_id: int, base_asset: str, quote_asset: str) -> Decimal:
    try:
        sid = int(sleeve_id)
    except Exception:
        return Decimal("0")

    b = _norm_asset(base_asset)
    q = _norm_asset(quote_asset)
    if not b or not q:
        return Decimal("0")

    k = f"observed_fee_pct:{sid}:{b}:{q}"
    cached = cache.get(k)
    if cached is not None:
        try:
            return Decimal(str(cached))
        except Exception:
            return Decimal("0")

    rows = list(
        OrderLog.objects.filter(
            sleeve_id=sid,
            status="closed",
            base_asset=b,
            quote_asset=q,
            cost__gt=0,
            fee__gt=0,
        )
        .only("cost", "fee")
        .order_by("-created_at")[:25]
    )
    if not rows:
        try:
            cache.set(k, "0", timeout=600)
        except Exception:
            pass
        return Decimal("0")

    acc = Decimal("0")
    n = 0
    for r in rows:
        try:
            c = Decimal(str(getattr(r, "cost", 0) or 0))
            f = Decimal(str(getattr(r, "fee", 0) or 0))
        except Exception:
            continue
        if c > 0 and f >= 0:
            try:
                acc += (f / c)
                n += 1
            except Exception:
                continue

    if n <= 0:
        out = Decimal("0")
    else:
        out = (acc / Decimal(str(n))) * Decimal("100")
        if out < 0:
            out = Decimal("0")
        if out > Decimal("5"):
            out = Decimal("5")

    try:
        cache.set(k, str(out), timeout=600)
    except Exception:
        pass
    return out


def _conservative_fee_pct_for_order_type(order_type: str) -> Decimal:
    try:
        assume_taker_for_limit = bool(getattr(settings, "EXECUTOR_ASSUME_TAKER_FOR_LIMIT_ORDERS", True))
    except Exception:
        assume_taker_for_limit = True

    try:
        taker_default = getattr(settings, "EXECUTOR_EST_TAKER_FEE_PCT", None)
    except Exception:
        taker_default = None
    try:
        maker_default = getattr(settings, "EXECUTOR_EST_MAKER_FEE_PCT", None)
    except Exception:
        maker_default = None

    try:
        maker_fee_pct = Decimal(str(getattr(settings, "KRAKEN_MAX_MAKER_FEE_PCT", maker_default if maker_default is not None else 0.25) or 0.25))
    except Exception:
        maker_fee_pct = Decimal("0.25")
    try:
        taker_fee_pct = Decimal(str(getattr(settings, "KRAKEN_MAX_TAKER_FEE_PCT", taker_default if taker_default is not None else 0.40) or 0.40))
    except Exception:
        taker_fee_pct = Decimal("0.40")

    ot = str(order_type or "").strip().lower()
    if ot == "market":
        return max(Decimal("0"), taker_fee_pct)
    if ot == "limit":
        return max(Decimal("0"), taker_fee_pct if assume_taker_for_limit else maker_fee_pct)
    return max(Decimal("0"), taker_fee_pct)


def _limit_max_failures_for_sleeve(sleeve: Sleeve, strategy: SleeveStrategy | None = None) -> int:
    v = None
    try:
        # Ensure we honor the latest value even if the Sleeve instance is stale/deferred.
        v = (
            Sleeve.objects.filter(id=int(getattr(sleeve, "id", 0) or 0))
            .values_list("limit_max_failures", flat=True)
            .first()
        )
    except Exception:
        v = None

    if v is None:
        try:
            v = getattr(sleeve, "limit_max_failures", None)
        except Exception:
            v = None

    if v is None and strategy is not None:
        try:
            v = (getattr(strategy, "params", None) or {}).get("limit_max_failures")
        except Exception:
            v = None

    try:
        out = int(v)
    except Exception:
        out = 2
    if out < 0:
        out = 0
    if out > 10:
        out = 10
    return out


def _restricted_pairs_key(user_id: int) -> str:
    try:
        uid = int(user_id)
    except Exception:
        uid = 0
    return f"kraken_restricted_pairs:{uid}"


def _get_restricted_pairs_map(user_id: int) -> dict:
    k = _restricted_pairs_key(user_id)
    try:
        m = cache.get(k)
    except Exception:
        m = None
    return m if isinstance(m, dict) else {}


def _is_pair_restricted(user_id: int, pair: str) -> bool:
    p = str(pair or "").strip().upper()
    if not p:
        return False
    m = _get_restricted_pairs_map(user_id)
    return p in m


def _mark_pair_restricted(user_id: int, pair: str, reason: str) -> None:
    p = str(pair or "").strip().upper()
    if not p:
        return
    k = _restricted_pairs_key(user_id)
    m = _get_restricted_pairs_map(user_id)
    try:
        ts = float(time.time())
    except Exception:
        ts = 0.0
    m[p] = {"ts": ts, "reason": str(reason or "").strip()[:200]}
    try:
        cache.set(k, m, timeout=30 * 24 * 3600)
    except Exception:
        pass


def run_active_sleeves():
    """Run one pass over all active sleeve strategies.

    This is intentionally simple and synchronous; a scheduler triggers it periodically.
    Extend with per-user queues or concurrency later.
    """
    strategies = list(
        SleeveStrategy.objects.select_related("sleeve", "sleeve__wallet", "sleeve__wallet__user")
        .filter(is_active=True)
    )

    active_user_ids = {s.sleeve.wallet.user_id for s in strategies}
    GLOBAL_RATE_LIMITER.set_active_users(len(active_user_ids) or 1)

    by_sleeve: dict[int, dict[str, object]] = {}
    for st in strategies:
        sid = int(getattr(st, "sleeve_id", 0) or 0)
        if not sid:
            continue
        bucket = by_sleeve.setdefault(sid, {"sleeve": st.sleeve, "buy": None, "sell": None})
        m = str(getattr(st, "mode", "") or "")
        if m.startswith("buy"):
            bucket["buy"] = st
        elif m.startswith("sell"):
            bucket["sell"] = st

    local_seen: set[int] = set()
    for sleeve_id, bucket in by_sleeve.items():
        if sleeve_id in local_seen:
            continue
        local_seen.add(sleeve_id)
        sleeve = bucket.get("sleeve")
        if not isinstance(sleeve, Sleeve):
            continue
        buy_strategy = bucket.get("buy")
        sell_strategy = bucket.get("sell")
        user = sleeve.wallet.user
        user_id = getattr(user, "id", None)

        try:
            owned_qty_pre = Decimal(str(getattr(sleeve, "position_base_qty", 0) or 0))
        except Exception:
            owned_qty_pre = Decimal("0")

        try:
            cycle_override = str(cache.get(f"sleeve_cycle_override:{int(sleeve_id)}") or "").strip().lower()
        except Exception:
            cycle_override = ""
        if cycle_override not in {"buy", "sell"}:
            cycle_override = ""

        strategy = sell_strategy if owned_qty_pre > 0 else buy_strategy
        if cycle_override == "buy" and isinstance(buy_strategy, SleeveStrategy):
            strategy = buy_strategy
        elif cycle_override == "sell" and isinstance(sell_strategy, SleeveStrategy):
            strategy = sell_strategy
        if not isinstance(strategy, SleeveStrategy):
            continue

        # Throttle per strategy to avoid hitting Kraken too frequently.
        # Run the executor task every second, but only do the full tick at:
        # - BUY: every ~EXECUTOR_BUY_TICK_SECONDS
        # - SELL: every ~EXECUTOR_SELL_TICK_SECONDS
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

        now_ts = time.time()
        k_lock = f"executor_lock_sleeve:{int(sleeve_id)}"
        try:
            if not cache.add(k_lock, now_ts, timeout=max(10, int(want_sec * 2))):
                continue
        except Exception:
            pass

        k_exec = f"executor_tick_sleeve:{int(sleeve_id)}"
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

        # Risk caps: block new BUY entries after either cap is hit.
        # This is applied to the BUY strategy only so SELL exits can still unwind positions.
        if (use_ml or is_custom) and side_pre == OrderLog.Side.BUY:
            try:
                blocked, reason, detail = _risk_caps_for_sleeve(sleeve)
            except Exception:
                blocked, reason, detail = False, "", ""
            if blocked and reason:
                try:
                    buy_st = buy_strategy if isinstance(buy_strategy, SleeveStrategy) else strategy
                    buy_st.ml_paused = True
                    buy_st.ml_status = "paused"
                    buy_st.ml_stage = "paused"
                    buy_st.ml_last_reason = reason
                    buy_st.ml_last_tick_at = timezone.now()
                    buy_st.save(update_fields=[
                        "ml_paused",
                        "ml_status",
                        "ml_stage",
                        "ml_last_reason",
                        "ml_last_tick_at",
                    ])
                except Exception:
                    pass
                _console_push_throttled(
                    user_id,
                    sleeve.id,
                    reason,
                    f"blocked sleeve={sleeve.id} pair={pair} reason={reason} {detail}",
                    level="warn",
                    every_s=60,
                )
                continue

        try:
            dust_ordermin = _pair_ordermin(adapter, pair) if (use_ml or is_custom) else Decimal("0")
        except Exception:
            dust_ordermin = Decimal("0")
        if (
            owned_qty_pre > 0
            and dust_ordermin > 0
            and owned_qty_pre < dust_ordermin
            and isinstance(buy_strategy, SleeveStrategy)
        ):
            strategy = buy_strategy
            side_pre = OrderLog.Side.BUY
            pair = (strategy.ml_pair or "").strip() or (strategy.params.get("pair") if strategy.params else "")
            pair = (pair or "").strip()
            if not pair:
                continue
            use_ml = bool(strategy.ml_active) and not is_custom
            sleeve_type = str(getattr(sleeve, "type", "") or "").lower()
            is_flash = sleeve_type == "flash" or "flash" in str(getattr(strategy, "mode", "") or "")
            try:
                dust_ordermin = _pair_ordermin(adapter, pair) if (use_ml or is_custom) else Decimal("0")
            except Exception:
                dust_ordermin = Decimal("0")

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
        owned_qty_eff = owned_qty
        if dust_ordermin > 0 and owned_qty > 0 and owned_qty < dust_ordermin:
            owned_qty_eff = Decimal("0")
        if cycle_override == "buy":
            owned_qty_eff = Decimal("0")

        if use_ml or is_custom:
            strategy.ml_last_tick_at = timezone.now()
            strategy.ml_last_price = price_now

        # If we have a pending order for this sleeve, do not flip stages/modes.
        # We optionally retry limit orders (cancel + replace) and only fall back to market after
        # N failures configured per sleeve.
        pending_qs = OrderLog.objects.filter(
            sleeve=sleeve,
            txid__gt="",
            status__in=["submitted", "open", "pending"],
        ).order_by("-created_at")
        pending_order = pending_qs.first()
        if (not pending_order) and (use_ml or is_custom):
            # Clear retry counters when nothing is pending so the next trade starts fresh.
            try:
                cache.delete(f"limit_market_fallback_attempts:{int(sleeve.id)}:buy")
                cache.delete(f"limit_market_fallback_attempts:{int(sleeve.id)}:sell")
            except Exception:
                pass

        if pending_order and (use_ml or is_custom):
            try:
                timeout_s = int(getattr(settings, "EXECUTOR_LIMIT_TIMEOUT_SECONDS", 30) or 30)
            except Exception:
                timeout_s = 30
            timeout_s = max(int(timeout_s or 1), 1)

            # Per sleeve: max number of failed LIMIT attempts before switching to MARKET for this trade.
            max_failures = _limit_max_failures_for_sleeve(sleeve, strategy)

            age_s = 0
            try:
                created = getattr(pending_order, "created_at", None)
                if created:
                    age_s = int((timezone.now() - created).total_seconds())
            except Exception:
                age_s = 0

            side_s = str(getattr(pending_order, "side", "") or "").strip().lower()
            if side_s not in {"buy", "sell"}:
                side_s = "buy"
            k_attempts = f"limit_market_fallback_attempts:{int(sleeve.id)}:{side_s}"
            try:
                attempts = int(cache.get(k_attempts) or 0)
            except Exception:
                attempts = 0

            pending_type = str(getattr(pending_order, "order_type", "") or "").strip().lower() or "market"

            # Only apply retry/fallback to pending LIMIT orders.
            if pending_type != "limit":
                strategy.ml_last_reason = "pending_fill"
                strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                if getattr(strategy, "ml_stage", "") != "pending_fill":
                    strategy.ml_stage = "pending_fill"
                    strategy.save(update_fields=["ml_stage"])
                try:
                    _console_push(user_id, f"pending_fill sleeve={sleeve.id} pair={pair} type={pending_type}")
                except Exception:
                    pass
                continue

            if age_s >= timeout_s:
                # Only fallback if we can safely cancel and there is no partial fill we know about.
                try:
                    cancel = adapter.cancel_order(getattr(pending_order, "txid", "") or "")
                    canceled = int(cancel.get("count") or 0)
                except Exception:
                    canceled = 0

                if canceled > 0:
                    try:
                        pending_order.status = "canceled"
                        pending_order.save(update_fields=["status"])
                    except Exception:
                        pass

                    new_failures = int(attempts + 1)
                    try:
                        cache.set(k_attempts, int(new_failures), timeout=3600)
                    except Exception:
                        pass

                    try:
                        vol_f = float(getattr(pending_order, "amount", 0) or 0)
                    except Exception:
                        vol_f = 0.0
                    if vol_f > 0:
                        if max_failures and new_failures >= max_failures:
                            # Switch this trade to MARKET.
                            try:
                                result = adapter.place_order(side=side_s, pair=pair, volume=vol_f, price=None)
                                txid_val = result.get("txid")
                                txid = ""
                                if isinstance(txid_val, list) and txid_val:
                                    txid = str(txid_val[0])
                                elif txid_val:
                                    txid = str(txid_val)
                                OrderLog.objects.create(
                                    sleeve=sleeve,
                                    api_key=key,
                                    side=side_s,
                                    order_type="market",
                                    base_asset=_norm_asset(getattr(sleeve, "base_asset", "") or ""),
                                    quote_asset=_norm_asset(getattr(sleeve.wallet, "currency", "") or ""),
                                    amount=vol_f,
                                    price=price_now,
                                    txid=txid,
                                    status=str(result.get("status", "submitted") or "submitted"),
                                    error="",
                                )
                                strategy.ml_last_reason = "pending_fill_market_fallback"
                            except Exception as exc:  # noqa: BLE001
                                strategy.ml_last_reason = "pending_fill_market_fallback_failed"
                                try:
                                    _console_push(user_id, f"error sleeve={sleeve.id} pair={pair} market_fallback_failed: {exc}", level="error")
                                except Exception:
                                    pass
                        else:
                            # Retry LIMIT for this trade.
                            try:
                                limit_price = float(getattr(pending_order, "price", 0) or 0)
                            except Exception:
                                limit_price = 0.0
                            if limit_price <= 0:
                                limit_price = float(price_now)
                            try:
                                decs = _pair_decimals(adapter, pair)
                            except Exception:
                                decs = 0
                            limit_price = float(_round_pair_price(Decimal(str(limit_price)), decs))
                            try:
                                result = adapter.place_order(side=side_s, pair=pair, volume=vol_f, price=limit_price)
                                txid_val = result.get("txid")
                                txid = ""
                                if isinstance(txid_val, list) and txid_val:
                                    txid = str(txid_val[0])
                                elif txid_val:
                                    txid = str(txid_val)
                                OrderLog.objects.create(
                                    sleeve=sleeve,
                                    api_key=key,
                                    side=side_s,
                                    order_type="limit",
                                    base_asset=_norm_asset(getattr(sleeve, "base_asset", "") or ""),
                                    quote_asset=_norm_asset(getattr(sleeve.wallet, "currency", "") or ""),
                                    amount=vol_f,
                                    price=Decimal(str(limit_price)),
                                    txid=txid,
                                    status=str(result.get("status", "submitted") or "submitted"),
                                    error="",
                                )
                                strategy.ml_last_reason = "pending_fill_limit_retry"
                            except Exception as exc:  # noqa: BLE001
                                strategy.ml_last_reason = "pending_fill_limit_retry_failed"
                                try:
                                    _console_push(user_id, f"error sleeve={sleeve.id} pair={pair} limit_retry_failed: {exc}", level="error")
                                except Exception:
                                    pass

                    strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                    if getattr(strategy, "ml_stage", "") != "pending_fill":
                        strategy.ml_stage = "pending_fill"
                        strategy.save(update_fields=["ml_stage"])
                    continue

            strategy.ml_last_reason = "pending_fill"
            if age_s >= timeout_s and max_failures and attempts >= max_failures:
                strategy.ml_last_reason = "pending_fill_timeout_max_failures"
            strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
            if getattr(strategy, "ml_stage", "") != "pending_fill":
                strategy.ml_stage = "pending_fill"
                strategy.save(update_fields=["ml_stage"])
            try:
                _console_push(user_id, f"pending_fill sleeve={sleeve.id} pair={pair} age={age_s}s failures={attempts}/{max_failures}")
            except Exception:
                pass
            continue

        # Isolated default behavior:
        # - If we own nothing: we are in buy mode, sell strategies should not run.
        # - If we own something: buy strategies should not run, sell strategies can run.
        if (use_ml or is_custom):
            if owned_qty_eff <= 0 and side == OrderLog.Side.SELL:
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
            if owned_qty_eff > 0 and side == OrderLog.Side.BUY:
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

        if (use_ml or is_custom) and is_flash:
            try:
                max_trades_h = int(getattr(settings, "EXECUTOR_FLASH_MAX_TRADES_PER_HOUR", 12) or 12)
            except Exception:
                max_trades_h = 12
            max_trades_h = max(int(max_trades_h or 0), 0)
            if max_trades_h:
                try:
                    since = timezone.now() - timedelta(hours=1)
                    trades_1h = OrderLog.objects.filter(sleeve=sleeve, created_at__gte=since).exclude(status="canceled").count()
                except Exception:
                    trades_1h = 0
                if trades_1h >= max_trades_h:
                    strategy.ml_last_reason = "blocked_flash_max_trades_per_hour"
                    strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                    if getattr(strategy, "ml_stage", "") != "cooldown":
                        strategy.ml_stage = "cooldown"
                        strategy.save(update_fields=["ml_stage"])
                    _console_push_throttled(
                        user_id,
                        sleeve.id,
                        "blocked_flash_max_trades_per_hour",
                        f"blocked sleeve={sleeve.id} pair={pair} reason=blocked_flash_max_trades_per_hour trades_1h={trades_1h} cap={max_trades_h}",
                        level="warn",
                        every_s=60,
                    )
                    continue

        alloc = strategy.sleeve.allocated_balance or Decimal("0")

        # For BUY orders, leave a little headroom for fees/holds so "100%" sizing doesn't
        # attempt to spend the full allocated quote and get rejected by Kraken.
        try:
            fee_buf_pct = Decimal(str(getattr(settings, "EXECUTOR_BUY_FEE_BUFFER_PCT", 0.5) or 0.5)) / Decimal("100")
        except Exception:
            fee_buf_pct = Decimal("0")
        if fee_buf_pct < 0:
            fee_buf_pct = Decimal("0")
        if fee_buf_pct > Decimal("0.05"):
            fee_buf_pct = Decimal("0.05")
        alloc_eff = alloc
        if side == OrderLog.Side.BUY and fee_buf_pct > 0:
            try:
                alloc_eff = (alloc * (Decimal("1") - fee_buf_pct)).quantize(Decimal("0.00000001"))
            except Exception:
                alloc_eff = alloc

        pct_mode = str(getattr(sleeve, "trade_pct_mode", "") or "").strip().lower() or "both"
        if pct_mode not in {"both", "individual"}:
            pct_mode = "both"
        try:
            pct_both = Decimal(str(getattr(sleeve, "trade_pct_both", 0) or 0))
        except Exception:
            pct_both = Decimal("0")
        try:
            pct_buy = Decimal(str(getattr(sleeve, "trade_pct_buy", 0) or 0))
        except Exception:
            pct_buy = Decimal("0")
        try:
            pct_sell = Decimal(str(getattr(sleeve, "trade_pct_sell", 0) or 0))
        except Exception:
            pct_sell = Decimal("0")

        pct = Decimal("0")
        if pct_mode == "both":
            pct = pct_both
        else:
            pct = pct_buy if side == OrderLog.Side.BUY else pct_sell
        if pct < 0:
            pct = Decimal("0")
        if pct > 100:
            pct = Decimal("100")

        try:
            fixed_base = Decimal(str(getattr(sleeve, "trade_amount_base", 0) or 0))
        except Exception:
            fixed_base = Decimal("0")

        if pct > 0:
            if side == OrderLog.Side.SELL and (use_ml or is_custom):
                volume = float((owned_qty * pct / Decimal("100")) if owned_qty > 0 else Decimal("0"))
            else:
                vol_quote = alloc_eff * pct / Decimal("100")
                volume = float((vol_quote / price_now) if price_now > 0 else Decimal("0"))
        elif fixed_base > 0:
            vol_base_dec = fixed_base
            if side == OrderLog.Side.SELL and (use_ml or is_custom):
                if owned_qty <= 0:
                    vol_base_dec = Decimal("0")
                elif vol_base_dec > owned_qty:
                    vol_base_dec = owned_qty
            else:
                try:
                    req_quote = vol_base_dec * price_now
                except Exception:
                    req_quote = Decimal("0")
                # Allow a tiny tolerance to avoid blocking due to precision drift.
                try:
                    alloc_tol = Decimal("0.00000001")
                except Exception:
                    alloc_tol = Decimal("0")
                if req_quote > (alloc_eff + alloc_tol):
                    strategy.ml_last_reason = "blocked_buy_trade_amount_exceeds_allocated"
                    strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                    if getattr(strategy, "ml_stage", "") != "waiting_buy":
                        strategy.ml_stage = "waiting_buy"
                        strategy.save(update_fields=["ml_stage"])
                    _console_push_throttled(
                        user_id,
                        sleeve.id,
                        "blocked_buy_trade_amount_exceeds_allocated",
                        f"blocked sleeve={sleeve.id} pair={pair} reason=blocked_buy_trade_amount_exceeds_allocated trade_amount_base={vol_base_dec} req_quote={req_quote} alloc={alloc}",
                        level="warn",
                        every_s=60,
                    )
                    continue

            volume = float(vol_base_dec)
        else:
            if use_ml:
                size_pct = Decimal(str(strategy.ml_position_size_pct))
            else:
                size_pct = Decimal(str((strategy.params or {}).get("position_size_pct", 0) or 0))
            if side == OrderLog.Side.SELL and (use_ml or is_custom):
                volume = float((owned_qty * size_pct / Decimal("100")) if size_pct > 0 else Decimal("0"))
            else:
                vol_quote = alloc_eff * size_pct / Decimal("100")
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

        if (use_ml or is_custom) and side == OrderLog.Side.BUY:
            mode_s = str(getattr(strategy, "mode", "") or "").lower()
            if "flash" in mode_s:
                try:
                    tp_pct = Decimal(str((strategy.ml_take_profit_pct if use_ml else (strategy.params or {}).get("take_profit_pct", 0)) or 0))
                except Exception:
                    tp_pct = Decimal("0")

                observed_fee_pct = Decimal("0")
                try:
                    observed_fee_pct = _observed_fee_pct_for_sleeve(
                        int(sleeve.id),
                        str(getattr(sleeve, "base_asset", "") or ""),
                        str(getattr(sleeve.wallet, "currency", "") or ""),
                    )
                except Exception:
                    observed_fee_pct = Decimal("0")

                entry_fee_pct = _conservative_fee_pct_for_order_type("limit")
                exit_fee_pct = _conservative_fee_pct_for_order_type("limit")
                if observed_fee_pct > 0:
                    entry_fee_pct = max(entry_fee_pct, observed_fee_pct)
                    exit_fee_pct = max(exit_fee_pct, observed_fee_pct)
                try:
                    est_slip_pct = Decimal(str(getattr(settings, "EXECUTOR_EST_SLIPPAGE_PCT", 0.10) or 0.10))
                except Exception:
                    est_slip_pct = Decimal("0.10")
                min_tp_pct = (entry_fee_pct + exit_fee_pct) + est_slip_pct
                try:
                    min_tp_pct = Decimal(str(getattr(settings, "EXECUTOR_FLASH_MIN_TAKE_PROFIT_PCT", min_tp_pct) or min_tp_pct))
                except Exception:
                    pass
                if min_tp_pct < 0:
                    min_tp_pct = Decimal("0")
                if tp_pct > 0 and min_tp_pct > 0 and tp_pct < min_tp_pct:
                    strategy.ml_last_reason = "blocked_flash_take_profit_below_fees"
                    strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                    if getattr(strategy, "ml_stage", "") != "waiting_buy":
                        strategy.ml_stage = "waiting_buy"
                        strategy.save(update_fields=["ml_stage"])
                    _console_push_throttled(
                        user_id,
                        sleeve.id,
                        "blocked_flash_take_profit_below_fees",
                        f"blocked sleeve={sleeve.id} pair={pair} reason=blocked_flash_take_profit_below_fees tp_pct={tp_pct} min_tp_pct={min_tp_pct}",
                        level="warn",
                        every_s=120,
                    )
                    continue

        if volume <= 0:
            try:
                _console_push(user_id, f"skip sleeve={sleeve.id} pair={pair} volume<=0", level="warn")
            except Exception:
                pass
            continue

        # Reconcile SELL volume against actual Kraken base balance to avoid EOrder:Insufficient funds.
        # Sleeve position can drift if orders were filled partially, canceled manually, or balance changed externally.
        if (use_ml or is_custom) and side == OrderLog.Side.SELL:
            base_asset = _norm_asset(getattr(sleeve, "base_asset", "") or "")
            if not base_asset and "/" in pair:
                base_asset = _norm_asset(pair.split("/", 1)[0])
            if base_asset:
                k_bal = f"kraken_bal_base:{int(user_id)}:{base_asset}"
                bal_val = cache.get(k_bal)
                if bal_val is None:
                    try:
                        bal_map = adapter.fetch_balances()
                        cand = bal_map.get(base_asset)
                        if cand is None:
                            cand = bal_map.get(base_asset.upper())
                        if cand is None:
                            cand = bal_map.get("X" + base_asset)
                        if cand is None:
                            cand = bal_map.get("Z" + base_asset)
                        if cand is None:
                            cand = bal_map.get("XX" + base_asset)
                        if cand is None:
                            cand = bal_map.get("ZZ" + base_asset)
                        bal_val = float(cand) if cand is not None else 0.0
                    except Exception:
                        bal_val = 0.0
                    try:
                        cache.set(k_bal, float(bal_val), timeout=15)
                    except Exception:
                        pass

                try:
                    avail_base = Decimal(str(bal_val or 0))
                except Exception:
                    avail_base = Decimal("0")

                if avail_base <= 0:
                    # No base asset at exchange; treat sleeve as flat.
                    sleeve.position_base_qty = Decimal("0")
                    try:
                        sleeve.save(update_fields=["position_base_qty"])
                    except Exception:
                        pass
                    strategy.ml_last_reason = "blocked_sell_insufficient_funds_no_base"
                    strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                    if getattr(strategy, "ml_stage", "") != "waiting_buy":
                        strategy.ml_stage = "waiting_buy"
                        strategy.save(update_fields=["ml_stage"])
                    _console_push_throttled(
                        user_id,
                        sleeve.id,
                        "blocked_sell_insufficient_funds_no_base",
                        f"blocked sleeve={sleeve.id} pair={pair} reason=blocked_sell_insufficient_funds_no_base avail={avail_base}",
                        level="warn",
                        every_s=60,
                    )
                    continue

                try:
                    vol_base_dec = Decimal(str(volume))
                except Exception:
                    vol_base_dec = Decimal("0")
                if vol_base_dec > avail_base:
                    vol_base_dec = avail_base
                    volume = float(vol_base_dec)

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
                    if ordermin > 0 and owned_qty > 0 and owned_qty < ordermin:
                        if isinstance(buy_strategy, SleeveStrategy):
                            buy_strategy.ml_last_reason = "dust_position_below_ordermin"
                            buy_strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                            if getattr(buy_strategy, "ml_stage", "") != "waiting_buy":
                                buy_strategy.ml_stage = "waiting_buy"
                                buy_strategy.save(update_fields=["ml_stage"])

                        strategy.ml_last_reason = "dust_position_below_ordermin"
                        strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                        if getattr(strategy, "ml_stage", "") != "waiting_buy":
                            strategy.ml_stage = "waiting_buy"
                            strategy.save(update_fields=["ml_stage"])

                        _console_push_throttled(
                            user_id,
                            sleeve.id,
                            "dust_position_below_ordermin",
                            f"blocked sleeve={sleeve.id} pair={pair} reason=dust_position_below_ordermin ordermin={ordermin} owned={owned_qty}",
                            level="warn",
                            every_s=60,
                        )
                        continue

                    strategy.ml_last_reason = "blocked_sell_min_exceeds_position"
                    strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                    if getattr(strategy, "ml_stage", "") != "waiting_sell":
                        strategy.ml_stage = "waiting_sell"
                        strategy.save(update_fields=["ml_stage"])
                    _console_push_throttled(
                        user_id,
                        sleeve.id,
                        "blocked_sell_min_exceeds_position",
                        f"blocked sleeve={sleeve.id} pair={pair} reason=blocked_sell_min_exceeds_position bumped={bumped} owned={owned_qty}",
                        level="warn",
                        every_s=60,
                    )
                    continue
            else:
                # Buy based on quote allocation.
                try:
                    req_quote = (bumped * price_now)
                except Exception:
                    req_quote = Decimal("0")
                # Allow a tiny tolerance to avoid blocking due to precision drift.
                try:
                    alloc_tol = Decimal("0.00000001")
                except Exception:
                    alloc_tol = Decimal("0")
                if req_quote > (alloc_eff + alloc_tol):
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
                if take_profit <= 0 and stop_loss <= 0:
                    strategy.ml_last_reason = "blocked_sell_no_tp_sl_configured"
                    strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                    if getattr(strategy, "ml_stage", "") != "waiting_sell":
                        strategy.ml_stage = "waiting_sell"
                        strategy.save(update_fields=["ml_stage"])
                    _console_push_throttled(
                        user_id,
                        sleeve.id,
                        "blocked_sell_no_tp_sl_configured",
                        f"blocked sleeve={sleeve.id} pair={pair} reason=blocked_sell_no_tp_sl_configured",
                        level="warn",
                        every_s=120,
                    )
                    continue

                # Avoid instant churn right after a BUY fill.
                try:
                    min_hold_s = int(getattr(settings, "EXECUTOR_MIN_HOLD_SECONDS", 45) or 45)
                except Exception:
                    min_hold_s = 45
                min_hold_s = max(int(min_hold_s or 0), 0)
                if min_hold_s > 0:
                    last_buy = _last_filled_buy_for_sleeve(sleeve, pair)
                    if last_buy is not None:
                        try:
                            age_s = int((timezone.now() - last_buy.created_at).total_seconds())
                        except Exception:
                            age_s = 0
                        if age_s >= 0 and age_s < min_hold_s:
                            strategy.ml_last_reason = "cooldown_min_hold_after_buy"
                            strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                            if getattr(strategy, "ml_stage", "") != "cooldown":
                                strategy.ml_stage = "cooldown"
                                strategy.save(update_fields=["ml_stage"])
                            _console_push_throttled(
                                user_id,
                                sleeve.id,
                                "cooldown_min_hold_after_buy",
                                f"cooldown sleeve={sleeve.id} pair={pair} reason=cooldown_min_hold_after_buy remaining={max(0, min_hold_s - age_s)}s",
                                level="info",
                                every_s=30,
                            )
                            continue

                # Use entry-cost-basis triggers when possible (prevents weird immediate exits from tick-based triggers).
                entry_px = Decimal("0")
                try:
                    last_buy = _last_filled_buy_for_sleeve(sleeve, pair)
                    entry_px = _entry_price_from_order(last_buy)
                except Exception:
                    entry_px = Decimal("0")

                if entry_px > 0 and (take_profit > 0 or stop_loss > 0):
                    tp_trigger = (entry_px * (Decimal("1") + max(Decimal("0"), take_profit))).quantize(Decimal("0.0000000001"))
                    sl_trigger = (entry_px * (Decimal("1") - max(Decimal("0"), stop_loss))).quantize(Decimal("0.0000000001"))
                else:
                    try:
                        sl_trigger = Decimal(str(getattr(strategy, "ml_last_sl_trigger", 0) or 0))
                    except Exception:
                        sl_trigger = Decimal("0")
                    try:
                        tp_trigger = Decimal(str(getattr(strategy, "ml_last_tp_trigger", 0) or 0))
                    except Exception:
                        tp_trigger = Decimal("0")
                limit_price_dec = Decimal("0")
                if tp_trigger and price_now >= tp_trigger:
                    limit_price_dec = tp_trigger
                elif sl_trigger and price_now <= sl_trigger:
                    limit_price_dec = sl_trigger

                if limit_price_dec and limit_price_dec > 0:
                    try:
                        decs = _pair_decimals(adapter, pair)
                    except Exception:
                        decs = 0
                    price = float(_round_pair_price(limit_price_dec, decs))
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
                try:
                    sl_trigger = Decimal(str(getattr(strategy, "ml_last_sl_trigger", 0) or 0))
                except Exception:
                    sl_trigger = Decimal("0")
                if sl_trigger and price_now >= sl_trigger:
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

                try:
                    entry_trigger = Decimal(str(getattr(strategy, "ml_last_entry_trigger", 0) or 0))
                except Exception:
                    entry_trigger = Decimal("0")
                mode = str(getattr(sleeve, "buy_entry_mode", "") or "").strip().lower()

                if not entry_trigger:
                    if mode == "momentum":
                        # Momentum should require a move ABOVE a reference price.
                        ref_px = price_now
                        try:
                            interval_min = int(getattr(settings, "EXECUTOR_MOMENTUM_OHLC_INTERVAL_MIN", 5) or 5)
                        except Exception:
                            interval_min = 5
                        try:
                            closes = _ohlc_close_series(adapter, pair, interval_min, 120)
                            if closes:
                                ref_px = Decimal(str(closes[-1]))
                        except Exception:
                            pass
                        entry_trigger = (ref_px * (Decimal("1") + entry_drop)).quantize(Decimal("0.0000000001"))
                    else:
                        entry_trigger = (price_now * (Decimal("1") - entry_drop)).quantize(Decimal("0.0000000001"))

                if mode == "momentum" and side == OrderLog.Side.BUY:
                    try:
                        gate_flash = bool(getattr(settings, "EXECUTOR_MOMENTUM_INDICATOR_GATING_FLASH", True))
                    except Exception:
                        gate_flash = True
                    try:
                        gate_quiet = bool(getattr(settings, "EXECUTOR_MOMENTUM_INDICATOR_GATING_QUIET", True))
                    except Exception:
                        gate_quiet = True
                    apply_gate = (is_flash and gate_flash) or ((not is_flash) and gate_quiet)
                    if apply_gate:
                        if is_flash:
                            try:
                                interval_min = int(getattr(settings, "EXECUTOR_MOMENTUM_OHLC_INTERVAL_MIN_FLASH", getattr(settings, "EXECUTOR_MOMENTUM_OHLC_INTERVAL_MIN", 5)) or 5)
                            except Exception:
                                interval_min = 5
                            try:
                                rsi_n = int(getattr(settings, "EXECUTOR_MOMENTUM_RSI_PERIOD_FLASH", getattr(settings, "EXECUTOR_MOMENTUM_RSI_PERIOD", 14)) or 14)
                            except Exception:
                                rsi_n = 14
                            try:
                                rsi_min = float(getattr(settings, "EXECUTOR_MOMENTUM_RSI_MIN_FLASH", getattr(settings, "EXECUTOR_MOMENTUM_RSI_MIN", 40)) or 40)
                            except Exception:
                                rsi_min = 40.0
                            try:
                                rsi_max = float(getattr(settings, "EXECUTOR_MOMENTUM_RSI_MAX_FLASH", getattr(settings, "EXECUTOR_MOMENTUM_RSI_MAX", 72)) or 72)
                            except Exception:
                                rsi_max = 72.0
                            try:
                                require_rising = bool(getattr(settings, "EXECUTOR_MOMENTUM_MACD_REQUIRE_RISING_FLASH", getattr(settings, "EXECUTOR_MOMENTUM_MACD_REQUIRE_RISING", True)))
                            except Exception:
                                require_rising = True
                            try:
                                confirm_bars = int(getattr(settings, "EXECUTOR_MOMENTUM_MACD_CONFIRM_BARS_FLASH", 1) or 1)
                            except Exception:
                                confirm_bars = 1
                        else:
                            try:
                                interval_min = int(getattr(settings, "EXECUTOR_MOMENTUM_OHLC_INTERVAL_MIN_QUIET", getattr(settings, "EXECUTOR_MOMENTUM_OHLC_INTERVAL_MIN", 15)) or 15)
                            except Exception:
                                interval_min = 15
                            try:
                                rsi_n = int(getattr(settings, "EXECUTOR_MOMENTUM_RSI_PERIOD_QUIET", getattr(settings, "EXECUTOR_MOMENTUM_RSI_PERIOD", 14)) or 14)
                            except Exception:
                                rsi_n = 14
                            try:
                                rsi_min = float(getattr(settings, "EXECUTOR_MOMENTUM_RSI_MIN_QUIET", 45) or 45)
                            except Exception:
                                rsi_min = 45.0
                            try:
                                rsi_max = float(getattr(settings, "EXECUTOR_MOMENTUM_RSI_MAX_QUIET", 68) or 68)
                            except Exception:
                                rsi_max = 68.0
                            try:
                                require_rising = bool(getattr(settings, "EXECUTOR_MOMENTUM_MACD_REQUIRE_RISING_QUIET", getattr(settings, "EXECUTOR_MOMENTUM_MACD_REQUIRE_RISING", True)))
                            except Exception:
                                require_rising = True
                            try:
                                confirm_bars = int(getattr(settings, "EXECUTOR_MOMENTUM_MACD_CONFIRM_BARS_QUIET", 2) or 2)
                            except Exception:
                                confirm_bars = 2

                        if interval_min <= 0:
                            interval_min = 5 if is_flash else 15
                        if rsi_n <= 0:
                            rsi_n = 14
                        confirm_bars = max(1, min(int(confirm_bars or 1), 3))

                        closes = []
                        try:
                            closes = _ohlc_close_series(adapter, pair, interval_min, 120)
                        except Exception:
                            closes = []
                        if closes:
                            rsi_val = _rsi(closes, period=rsi_n)
                            macd_hist_now, macd_hist_prev = _macd_hist(closes)

                            if rsi_val > rsi_max:
                                strategy.ml_last_reason = "blocked_buy_momentum_rsi_overheated"
                                strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                                if getattr(strategy, "ml_stage", "") != "waiting_buy":
                                    strategy.ml_stage = "waiting_buy"
                                    strategy.save(update_fields=["ml_stage"])
                                _console_push_throttled(
                                    user_id,
                                    sleeve.id,
                                    "blocked_buy_momentum_rsi_overheated",
                                    f"blocked sleeve={sleeve.id} pair={pair} reason=blocked_buy_momentum_rsi_overheated rsi={rsi_val:.1f} max={rsi_max}",
                                    level="info",
                                    every_s=60,
                                )
                                continue
                            if rsi_val < rsi_min:
                                strategy.ml_last_reason = "blocked_buy_momentum_rsi_too_low"
                                strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                                if getattr(strategy, "ml_stage", "") != "waiting_buy":
                                    strategy.ml_stage = "waiting_buy"
                                    strategy.save(update_fields=["ml_stage"])
                                _console_push_throttled(
                                    user_id,
                                    sleeve.id,
                                    "blocked_buy_momentum_rsi_too_low",
                                    f"blocked sleeve={sleeve.id} pair={pair} reason=blocked_buy_momentum_rsi_too_low rsi={rsi_val:.1f} min={rsi_min}",
                                    level="info",
                                    every_s=60,
                                )
                                continue

                            macd_ok = (macd_hist_now > 0)
                            if confirm_bars >= 2:
                                macd_ok = macd_ok and (macd_hist_prev > 0)
                            if require_rising:
                                macd_ok = macd_ok and (macd_hist_now >= macd_hist_prev)
                            if not macd_ok:
                                strategy.ml_last_reason = "blocked_buy_momentum_macd_not_bullish"
                                strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                                if getattr(strategy, "ml_stage", "") != "waiting_buy":
                                    strategy.ml_stage = "waiting_buy"
                                    strategy.save(update_fields=["ml_stage"])
                                _console_push_throttled(
                                    user_id,
                                    sleeve.id,
                                    "blocked_buy_momentum_macd_not_bullish",
                                    f"blocked sleeve={sleeve.id} pair={pair} reason=blocked_buy_momentum_macd_not_bullish hist={macd_hist_now:.8f} prev={macd_hist_prev:.8f}",
                                    level="info",
                                    every_s=60,
                                )
                                continue

                hit = (price_now >= entry_trigger) if mode == "momentum" else (price_now <= entry_trigger)
                if hit:
                    limit_price_dec = entry_trigger
                    if mode == "momentum":
                        # Keep limit orders but make them marketable (avoid placing below current price).
                        limit_price_dec = (price_now * (Decimal("1") + Decimal("0.001"))).quantize(Decimal("0.0000000001"))
                    try:
                        decs = _pair_decimals(adapter, pair)
                    except Exception:
                        decs = 0
                    price = float(_round_pair_price(limit_price_dec, decs)) if limit_price_dec > 0 else float(price_now)
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
            place_price = price
            log_price = Decimal(str(price_now))
            if price is not None:
                try:
                    decs = _pair_decimals(adapter, pair)
                except Exception:
                    decs = 0
                rounded = _round_pair_price(Decimal(str(price)), decs)
                place_price = float(rounded)
                log_price = rounded

            # BUY-side preflight: if Kraken doesn't have enough quote balance, skip the order
            # and keep the strategy in waiting_buy (prevents repeated Insufficient funds errors).
            if (use_ml or is_custom) and side == OrderLog.Side.BUY:
                quote_asset = _norm_asset(getattr(sleeve.wallet, "currency", "") or "")
                if quote_asset:
                    k_qbal = f"kraken_bal_quote:{int(user_id)}:{quote_asset}"
                    q_val = cache.get(k_qbal)
                    if q_val is None:
                        try:
                            bal_map = adapter.fetch_balances()
                            cand = bal_map.get(quote_asset)
                            if cand is None:
                                cand = bal_map.get(quote_asset.upper())
                            if cand is None:
                                cand = bal_map.get("X" + quote_asset)
                            if cand is None:
                                cand = bal_map.get("Z" + quote_asset)
                            if cand is None:
                                cand = bal_map.get("XX" + quote_asset)
                            if cand is None:
                                cand = bal_map.get("ZZ" + quote_asset)
                            q_val = float(cand) if cand is not None else 0.0
                        except Exception:
                            q_val = 0.0
                        try:
                            cache.set(k_qbal, float(q_val), timeout=15)
                        except Exception:
                            pass
                    try:
                        avail_quote = Decimal(str(q_val or 0))
                    except Exception:
                        avail_quote = Decimal("0")
                    try:
                        vol_dec = Decimal(str(volume))
                    except Exception:
                        vol_dec = Decimal("0")
                    px_dec = log_price if price is not None else price_now
                    try:
                        req_quote = (vol_dec * px_dec).quantize(Decimal("0.00000001"))
                    except Exception:
                        req_quote = Decimal("0")

                    if avail_quote <= 0 or req_quote <= 0 or req_quote > (avail_quote + Decimal("0.00000001")):
                        strategy.ml_last_reason = "blocked_buy_insufficient_funds_quote_balance"
                        strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                        if getattr(strategy, "ml_stage", "") != "waiting_buy":
                            strategy.ml_stage = "waiting_buy"
                            strategy.save(update_fields=["ml_stage"])
                        _console_push_throttled(
                            user_id,
                            sleeve.id,
                            "blocked_buy_insufficient_funds_quote_balance",
                            f"blocked sleeve={sleeve.id} pair={pair} reason=blocked_buy_insufficient_funds_quote_balance req_quote={req_quote} avail={avail_quote} alloc={alloc}",
                            level="warn",
                            every_s=60,
                        )
                        continue
            if (use_ml or is_custom) and _is_pair_restricted(int(user_id), pair):
                try:
                    strategy.ml_paused = True
                    strategy.ml_last_reason = "blocked_pair_restricted"
                    strategy.ml_status = "paused"
                    strategy.ml_stage = "paused"
                    strategy.save(update_fields=["ml_paused", "ml_last_reason", "ml_status", "ml_stage", "ml_last_tick_at", "ml_last_price"])
                except Exception:
                    pass
                _console_push_throttled(
                    user_id,
                    sleeve.id,
                    "blocked_pair_restricted",
                    f"blocked sleeve={sleeve.id} pair={pair} reason=blocked_pair_restricted",
                    level="warn",
                    every_s=300,
                )
                continue

            result = adapter.place_order(side=side, pair=pair, volume=volume, price=place_price)
            txid_val = result.get("txid")
            txid = ""
            if isinstance(txid_val, list) and txid_val:
                txid = str(txid_val[0])
            elif txid_val:
                txid = str(txid_val)

            status_str = str(result.get("status", "submitted") or "submitted")
            log_row = OrderLog.objects.create(
                sleeve=sleeve,
                api_key=key,
                side=side,
                order_type=("market" if price is None else "limit"),
                base_asset=_norm_asset(getattr(sleeve, "base_asset", "") or ""),
                quote_asset=_norm_asset(getattr(sleeve.wallet, "currency", "") or ""),
                amount=volume,
                price=log_price,
                txid=txid,
                status=status_str,
                error="",
            )

            # If Kraken reports it as closed immediately, populate exec fields now.
            # Otherwise order_sync will fill in vol_exec/cost/fee later.
            exec_qty = None
            if status_str == "closed" and txid:
                try:
                    payload = adapter._private_request("QueryOrders", {"txid": txid})
                    result_map = payload.get("result") or {}
                    o = result_map.get(txid) if isinstance(result_map, dict) else None
                    if isinstance(o, dict):
                        try:
                            v = Decimal(str(o.get("vol_exec") or 0))
                        except Exception:
                            v = Decimal("0")
                        try:
                            c = Decimal(str(o.get("cost") or 0))
                        except Exception:
                            c = Decimal("0")
                        try:
                            f = Decimal(str(o.get("fee") or 0))
                        except Exception:
                            f = Decimal("0")
                        avg_price = Decimal("0")
                        if v > 0 and c > 0:
                            try:
                                avg_price = (c / v).quantize(Decimal("0.0000000001"))
                            except Exception:
                                avg_price = Decimal("0")

                        upd: list[str] = []
                        if v >= 0 and getattr(log_row, "vol_exec", None) != v:
                            log_row.vol_exec = v
                            upd.append("vol_exec")
                        if c >= 0 and getattr(log_row, "cost", None) != c:
                            log_row.cost = c
                            upd.append("cost")
                        if f >= 0 and getattr(log_row, "fee", None) != f:
                            log_row.fee = f
                            upd.append("fee")
                        if avg_price > 0:
                            log_row.price = avg_price
                            upd.append("price")
                        if upd:
                            log_row.save(update_fields=upd)
                        if v > 0:
                            exec_qty = v
                except Exception:
                    pass

            # If the order is already closed, update isolated sleeve position immediately.
            # Otherwise order_sync will update it when the txid transitions to closed.
            if status_str == "closed":
                try:
                    if exec_qty is not None:
                        vol_dec = Decimal(str(exec_qty))
                    else:
                        vol_dec = Decimal(str(volume))
                except Exception:
                    vol_dec = Decimal("0")
                if vol_dec > 0:
                    try:
                        pos = Decimal(str(getattr(sleeve, "position_base_qty", 0) or 0))
                    except Exception:
                        pos = Decimal("0")
                    if side == OrderLog.Side.BUY:
                        pos = (pos + vol_dec).quantize(Decimal("0.0000000001"))
                    else:
                        pos = max(Decimal("0"), (pos - vol_dec).quantize(Decimal("0.0000000001")))
                    sleeve.position_base_qty = pos
                    try:
                        sleeve.save(update_fields=["position_base_qty"])
                    except Exception:
                        pass

            if use_ml or is_custom:
                strategy.ml_last_action_at = timezone.now()
                strategy.ml_stage = "pending_fill" if txid else ("cooldown" if cooldown_seconds else strategy.ml_stage)
                strategy.save(update_fields=["ml_last_action_at", "ml_stage"])

            try:
                _console_push(
                    user_id,
                    f"order sleeve={sleeve.id} pair={pair} side={side} volume={volume} order_type={'market' if price is None else 'limit'} txid={txid} status={status_str}",
                )
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("Order placement failed for sleeve=%s pair=%s: %s", sleeve.id, pair, exc)
            err_s = str(exc)
            if "EAccount:Invalid permissions" in err_s or "trading restricted" in err_s or "restricted for" in err_s:
                try:
                    _mark_pair_restricted(int(user_id), pair, err_s)
                except Exception:
                    pass
                try:
                    strategy.ml_paused = True
                    strategy.ml_last_reason = "blocked_pair_restricted"
                    strategy.ml_status = "paused"
                    strategy.ml_stage = "paused"
                    strategy.save(update_fields=["ml_paused", "ml_last_tick_at", "ml_last_price", "ml_last_reason", "ml_status", "ml_stage"])
                except Exception:
                    pass
                _console_push_throttled(
                    user_id,
                    sleeve.id,
                    "blocked_pair_restricted",
                    f"blocked sleeve={sleeve.id} pair={pair} reason=blocked_pair_restricted err={err_s}",
                    level="error",
                    every_s=600,
                )
                continue
            if (use_ml or is_custom) and side == OrderLog.Side.SELL and "Insufficient funds" in err_s:
                try:
                    strategy.ml_last_reason = "blocked_sell_insufficient_funds"
                    strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                    if getattr(strategy, "ml_stage", "") != "waiting_sell":
                        strategy.ml_stage = "waiting_sell"
                        strategy.save(update_fields=["ml_stage"])
                except Exception:
                    pass
            if (use_ml or is_custom) and side == OrderLog.Side.BUY and "Insufficient funds" in err_s:
                try:
                    strategy.ml_last_reason = "blocked_buy_insufficient_funds"
                    strategy.save(update_fields=["ml_last_tick_at", "ml_last_price", "ml_last_reason"])
                    if getattr(strategy, "ml_stage", "") != "waiting_buy":
                        strategy.ml_stage = "waiting_buy"
                        strategy.save(update_fields=["ml_stage"])
                except Exception:
                    pass
            elif use_ml or is_custom:
                try:
                    strategy.ml_stage = "error"
                    strategy.save(update_fields=["ml_stage"])
                except Exception:
                    pass
            try:
                _console_push(user_id, f"error sleeve={sleeve.id} pair={pair} order_failed: {exc}", level="error")
            except Exception:
                pass
            try:
                OrderLog.objects.create(
                    sleeve=sleeve,
                    api_key=key,
                    side=side,
                    order_type=("market" if price is None else "limit"),
                    base_asset=_norm_asset(getattr(sleeve, "base_asset", "") or ""),
                    quote_asset=_norm_asset(getattr(sleeve.wallet, "currency", "") or ""),
                    amount=volume,
                    price=Decimal(str(price_now)) if price is None else Decimal(str(price)),
                    txid="",
                    status="error",
                    error=str(exc),
                )
            except Exception:
                pass
            continue
