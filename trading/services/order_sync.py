import logging
from decimal import Decimal
from typing import Optional

from django.utils import timezone

from api_keys.models import ApiKey
from trading.models import OrderLog
from wallets.models import Sleeve
from trading.services.kraken_adapter import GLOBAL_RATE_LIMITER, KrakenAdapter
from trading.services.autotrade_console import push as _console_push

logger = logging.getLogger(__name__)


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
    if a.startswith("X") and len(a) > 3 and a not in {"XBT"}:
        a = a[1:]
    return a


def _pick_active_key(user_id: int) -> Optional[ApiKey]:
    return ApiKey.objects.filter(user_id=user_id, is_active=True).order_by("created_at").first()


def _extract_first_txid(txid_val) -> str:
    if not txid_val:
        return ""
    if isinstance(txid_val, list) and txid_val:
        return str(txid_val[0])
    return str(txid_val)


def _fetch_order(adapter: KrakenAdapter, txid: str) -> tuple[str, Decimal, Decimal]:
    """Return (status, vol_exec, avg_price). status is one of: open, closed, canceled, expired, unknown."""
    txid = (txid or "").strip()
    if not txid:
        return "unknown", Decimal("0"), Decimal("0")

    # Prefer QueryOrders for a specific txid.
    try:
        payload = adapter._private_request("QueryOrders", {"txid": txid})
        result = payload.get("result") or {}
        if isinstance(result, dict) and txid in result:
            o = result.get(txid) or {}
            status = str(o.get("status") or "unknown")
            vol_exec = Decimal(str((o.get("vol_exec") or 0)))
            cost = Decimal(str((o.get("cost") or 0)))
            avg_price = Decimal("0")
            if vol_exec and vol_exec > 0 and cost and cost > 0:
                avg_price = (cost / vol_exec).quantize(Decimal("0.0000000001"))
            # Kraken may report status=open/closed/canceled/expired
            return status, vol_exec, avg_price
    except Exception:
        pass

    # 1) open orders
    try:
        payload = adapter._private_request("OpenOrders", {"txid": txid})
        result = payload.get("result") or {}
        open_map = result.get("open") or {}
        if isinstance(open_map, dict) and txid in open_map:
            o = open_map.get(txid) or {}
            vol_exec = Decimal(str((o.get("vol_exec") or 0)))
            return "open", vol_exec, Decimal("0")
    except Exception:
        pass

    # 2) closed orders
    try:
        payload = adapter._private_request("ClosedOrders", {"txid": txid})
        result = payload.get("result") or {}
        closed_map = result.get("closed") or {}
        if isinstance(closed_map, dict) and txid in closed_map:
            o = closed_map.get(txid) or {}
            status = str(o.get("status") or "closed")
            vol_exec = Decimal(str((o.get("vol_exec") or 0)))
            cost = Decimal(str((o.get("cost") or 0)))
            avg_price = Decimal("0")
            if vol_exec and vol_exec > 0 and cost and cost > 0:
                avg_price = (cost / vol_exec).quantize(Decimal("0.0000000001"))
            return status, vol_exec, avg_price
    except Exception:
        pass

    return "unknown", Decimal("0"), Decimal("0")


def sync_pending_orders(limit: int = 50) -> dict[str, int]:
    """Poll Kraken for pending/submitted orders and update OrderLog + isolated sleeve positions.

    Only updates Sleeve.position_base_qty when order is confirmed closed with executed volume.
    """
    pending = (
        OrderLog.objects.select_related("sleeve", "sleeve__wallet", "sleeve__wallet__user", "api_key")
        .exclude(txid="")
        .filter(status__in=["submitted", "open", "pending"])
        .order_by("-created_at")
    )

    checked = 0
    updated = 0
    for o in pending[: max(int(limit or 50), 1)]:
        checked += 1
        sleeve: Sleeve = o.sleeve
        user_id = sleeve.wallet.user_id

        try:
            _console_push(user_id, f"sync check order={o.id} sleeve={sleeve.id} txid={o.txid} status={o.status}")
        except Exception:
            pass

        key = _pick_active_key(user_id)
        if not key:
            continue
        adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER, user_id=user_id)

        # Fix legacy display fields (pair slicing) while we're here.
        try:
            want_base = _norm_asset(getattr(sleeve, "base_asset", "") or "")
            want_quote = _norm_asset(getattr(sleeve.wallet, "currency", "") or "")
            if (o.base_asset != want_base) or (o.quote_asset != want_quote):
                o.base_asset = want_base
                o.quote_asset = want_quote
                o.save(update_fields=["base_asset", "quote_asset"])
                updated += 1
        except Exception:
            pass

        status, vol_exec, avg_price = _fetch_order(adapter, o.txid)
        if status == "unknown":
            continue

        if status in {"open"}:
            if o.status != "open":
                o.status = "open"
                o.save(update_fields=["status"])
                updated += 1
                try:
                    _console_push(user_id, f"sync update order={o.id} txid={o.txid} status=open")
                except Exception:
                    pass
            continue

        # closed/canceled/expired
        status_fields: list[str] = []
        if o.status != status:
            o.status = status
            status_fields.append("status")
        if avg_price and avg_price > 0 and (not o.price or o.price <= 0):
            o.price = avg_price
            status_fields.append("price")
        if status_fields:
            o.save(update_fields=status_fields)
            updated += 1
            try:
                _console_push(user_id, f"sync update order={o.id} txid={o.txid} status={o.status} vol_exec={vol_exec} avg_price={avg_price}")
            except Exception:
                pass

        if status == "closed" and vol_exec > 0:
            try:
                pos = Decimal(str(getattr(sleeve, "position_base_qty", 0) or 0))
                if o.side == OrderLog.Side.BUY:
                    pos = (pos + vol_exec).quantize(Decimal("0.0000000001"))
                else:
                    pos = max(Decimal("0"), (pos - vol_exec).quantize(Decimal("0.0000000001")))
                sleeve.position_base_qty = pos
                sleeve.save(update_fields=["position_base_qty"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("Position update failed for sleeve %s: %s", sleeve.id, exc)

            try:
                _console_push(user_id, f"sync position sleeve={sleeve.id} position_base_qty={getattr(sleeve, 'position_base_qty', 0)}")
            except Exception:
                pass

            # Nudge strategies out of pending_fill
            try:
                for st in sleeve.strategies.filter(is_active=True):
                    if getattr(st, "ml_stage", "") == "pending_fill":
                        st.ml_stage = "waiting_buy" if pos <= 0 else "waiting_sell"
                        st.ml_last_action_at = timezone.now()
                        st.save(update_fields=["ml_stage", "ml_last_action_at"])
            except Exception:
                pass

    return {"checked": checked, "updated": updated}
