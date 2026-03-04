from django.http import HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.views.generic import TemplateView
from django.db import models
from django.db import IntegrityError
from django.conf import settings
from django.core.cache import cache
from rest_framework import viewsets
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import json
import time
from datetime import datetime, timedelta

from accounts.permissions import IsApproved, IsOwnerOrAdmin
from accounts.views import ApprovalRequiredMixin
from .forms import SleeveForm, WalletForm
from .models import Sleeve, Wallet
from .serializers import SleeveSerializer, WalletSerializer
from trading.models import OrderLog, SleeveStrategy, SleevePerformanceRecord
from trading.services.kraken_adapter import KrakenAdapter, GLOBAL_RATE_LIMITER
from trading.services.autotrade_console import clear as _console_clear
from trading.services.autotrade_console import tail as _console_tail
from trading.services.trainer import refresh_ml_prices
from trading.services.market_scan import top_profit_candidates, profit_candidates_last_updated
from api_keys.models import ApiKey
from django.utils import timezone
from django.views import View

# reuse executor-like cursor for balance calls
_BAL_KEY_CURSOR: dict[int, int] = {}


_ASSET_PAIRS_CACHE: dict[str, object] = {"ts": 0.0, "by_quote": {}}


_PAIR_MIN_CACHE: dict[str, object] = {"ts": 0.0, "by_pair": {}}


def _pair_permission_cache_key(user_id: int, pair: str) -> str:
    try:
        uid = int(user_id)
    except Exception:
        uid = 0
    p = (pair or "").strip().upper()
    return f"kraken_pair_perm:{uid}:{p}"


def _pair_tradeable_for_user(adapter: KrakenAdapter, user_id: int, pair: str) -> bool:
    p = (pair or "").strip().upper()
    if not p:
        return False

    k = _pair_permission_cache_key(user_id, p)
    try:
        cached = cache.get(k)
    except Exception:
        cached = None
    if isinstance(cached, dict) and (cached.get("ok") is True):
        return True
    if isinstance(cached, dict) and (cached.get("ok") is False):
        # Only treat permission errors as hard blocks.
        err = str(cached.get("err") or "")
        if "EAccount:Invalid permissions" in err or "trading restricted" in err or "restricted for" in err:
            return False
        return True

    # Not cached: validate with Kraken (does not place an order).
    try:
        ordermin = _pair_ordermin(adapter, p)
    except Exception:
        ordermin = Decimal("0")
    if ordermin <= 0:
        return True

    try:
        adapter._private_request(
            "AddOrder",
            {
                "pair": p,
                "type": "buy",
                "ordertype": "market",
                "volume": float(ordermin),
                "validate": "true",
            },
            weight=1.0,
        )
        try:
            cache.set(k, {"ok": True, "ts": time.time(), "err": ""}, timeout=7 * 24 * 3600)
        except Exception:
            pass
        return True
    except Exception as exc:  # noqa: BLE001
        err_s = str(exc)
        try:
            cache.set(k, {"ok": False, "ts": time.time(), "err": err_s[:240]}, timeout=7 * 24 * 3600)
        except Exception:
            pass
        if "EAccount:Invalid permissions" in err_s or "trading restricted" in err_s or "restricted for" in err_s:
            try:
                from trading.services.executor import _mark_pair_restricted

                _mark_pair_restricted(int(user_id), p, err_s)
            except Exception:
                pass
            return False
        # Any other error (insufficient funds, etc) is not a permissions block.
        return True


def _pair_ordermin(adapter: KrakenAdapter, pair: str) -> Decimal:
    p = (pair or "").strip()
    if not p:
        return Decimal("0")
    now = time.time()
    by_pair = _PAIR_MIN_CACHE.get("by_pair")
    if isinstance(by_pair, dict):
        cached = by_pair.get(p)
        if cached is not None and (now - float(_PAIR_MIN_CACHE.get("ts") or 0.0)) < 900:
            try:
                v = Decimal(str(cached or 0))
                try:
                    cache.set(f"kraken_ordermin:{p}", str(v), timeout=24 * 3600)
                except Exception:
                    pass
                return v
            except Exception:
                return Decimal("0")

    ap = adapter.public_request("AssetPairs", params={"pair": p}, weight=1.0)
    meta_map = (ap.get("result") or {}) if isinstance(ap, dict) else {}
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

    if not isinstance(by_pair, dict):
        by_pair = {}
        _PAIR_MIN_CACHE["by_pair"] = by_pair
    by_pair[p] = str(ordermin)
    _PAIR_MIN_CACHE["ts"] = now
    try:
        cache.set(f"kraken_ordermin:{p}", str(ordermin), timeout=24 * 3600)
    except Exception:
        pass
    return ordermin


def _extract_ohlc_rows(payload: dict, pair: str) -> list[tuple[int, float]]:
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
    rows: list[tuple[int, float]] = []
    for row in series:
        if len(row) >= 5:
            try:
                ts = int(row[0])
                close = float(row[4])
            except Exception:
                continue
            rows.append((ts, close))
    return rows


def _asset_pairs_for_quote(adapter: KrakenAdapter, quote: str) -> tuple[dict[str, str], list[str]]:
    """Return (wsname->pair_code, wsname_list) for a quote (e.g., USD).

    Cached to keep wallets page fast; all calls go through KrakenAdapter rate limiter.
    """
    q = (quote or "").strip().upper()
    now = time.time()
    by_quote = _ASSET_PAIRS_CACHE.get("by_quote")
    if isinstance(by_quote, dict):
        cached = by_quote.get(q)
        if cached and (now - float(_ASSET_PAIRS_CACHE.get("ts") or 0.0)) < 900:
            ws_to_code = cached.get("ws_to_code") or {}
            ws_list = cached.get("ws_list") or []
            return dict(ws_to_code), list(ws_list)

    payload = adapter.public_request("AssetPairs", weight=1.0)
    result = payload.get("result") or {}
    ws_to_code: dict[str, str] = {}
    ws_list: list[str] = []
    for pair_code, meta in result.items():
        if not isinstance(meta, dict):
            continue
        if meta.get("status") != "online":
            continue
        wsname = (meta.get("wsname") or "").strip()
        if not wsname or "/" not in wsname:
            continue
        _base, _quote = wsname.split("/", 1)
        if (_quote or "").strip().upper() != q:
            continue
        ws_to_code[wsname.upper()] = str(pair_code)
        ws_list.append(wsname.upper())

    ws_list = sorted(set(ws_list))
    _ASSET_PAIRS_CACHE["ts"] = now
    if not isinstance(by_quote, dict):
        by_quote = {}
        _ASSET_PAIRS_CACHE["by_quote"] = by_quote
    by_quote[q] = {"ws_to_code": ws_to_code, "ws_list": ws_list}
    return ws_to_code, ws_list


def _resolve_pair_code(adapter: KrakenAdapter, base: str, quote: str) -> str:
    """Resolve Kraken pair code for base/quote (e.g., USDC / USD).

    Tries cached AssetPairs by quote, falls back to concatenation. Returns empty string if not found.
    """
    b = _norm_asset(base)
    q = _norm_asset(quote)
    if not b or not q:
        return ""
    try:
        ws_to_code, _ = _asset_pairs_for_quote(adapter, q)
    except Exception:
        ws_to_code = {}
    ws_key = f"{b}/{q}".upper()
    code = ws_to_code.get(ws_key)
    if code:
        return code
    # Fallback: Kraken sometimes uses direct concatenation
    return f"{b}{q}"


def _resolve_pair_code_any(adapter: KrakenAdapter, base: str, quote: str) -> str:
    """Resolve Kraken pair code for base/quote by scanning AssetPairs (bidirectional, normalized).

    Tries exact wsname match first, then normalized asset prefixes, finally concatenation.
    """
    b = _norm_asset(base)
    q = _norm_asset(quote)
    if not b or not q:
        return ""
    now = time.time()
    all_cached = _ASSET_PAIRS_CACHE.get("all")
    pairs_map: dict[str, str] = {}
    if isinstance(all_cached, dict) and (now - float(all_cached.get("ts") or 0.0)) < 900:
        pairs_map = all_cached.get("map") or {}
    if not pairs_map:
        try:
            payload = adapter.public_request("AssetPairs", weight=1.0)
            result = payload.get("result") or {}
        except Exception:
            result = {}
        for code, meta in (result or {}).items():
            if not isinstance(meta, dict):
                continue
            ws = (meta.get("wsname") or "").strip().upper()
            if "/" in ws:
                pairs_map[ws] = str(code)
        _ASSET_PAIRS_CACHE["all"] = {"ts": now, "map": pairs_map}

    # Exact wsname match
    ws_key = f"{b}/{q}".upper()
    if ws_key in pairs_map:
        return pairs_map[ws_key]
    # Try with common prefixes removed
    def _strip_pref(a: str) -> str:
        if a.startswith("X") or a.startswith("Z"):
            return a[1:]
        return a
    b2, q2 = _strip_pref(b), _strip_pref(q)
    ws_key2 = f"{b2}/{q2}".upper()
    if ws_key2 in pairs_map:
        return pairs_map[ws_key2]
    # Try reverse wsname (quote/base) if needed
    ws_rev = f"{q}/{b}".upper()
    if ws_rev in pairs_map:
        return pairs_map[ws_rev]
    ws_rev2 = f"{q2}/{b2}".upper()
    if ws_rev2 in pairs_map:
        return pairs_map[ws_rev2]
    # No match
    return ""


def _norm_asset(asset: str) -> str:
    a = (asset or "").strip().upper()
    # Common user alias
    if a in {"BTC"}:
        return "XBT"
    # Backwards-compat: earlier buggy normalization could turn XBT into BT.
    if a == "BT":
        return "XBT"
    # Kraken sometimes prefixes assets with an extra X/Z (e.g., XXBT, ZUSD)
    if a.startswith("XX") or a.startswith("ZZ"):
        a = a[1:]
    # Strip single leading Z for fiat like ZUSD->USD, but do NOT strip from canonical 3-4 char assets like XBT.
    if a.startswith("Z") and len(a) > 3:
        a = a[1:]
    return a


def _to_kraken_pair(base: str, quote: str) -> str:
    b = _norm_asset(base)
    q = _norm_asset(quote)
    # Kraken typical format: X<BASE>Z<QUOTE>
    return f"X{b}Z{q}"


def _norm_pair_input(raw: str, default_quote: str) -> str:
    s = (raw or "").strip().upper()
    # Allow friendly dropdown values like "BTC/USD (BITCOIN)" by stripping anything after whitespace or "(".
    if "(" in s:
        s = s.split("(", 1)[0]
    s = s.strip().replace(" ", "")
    if not s:
        return ""
    # If already looks like Kraken code (no slash and has X/Z markers), accept.
    if "/" not in s and ("Z" in s or s.startswith("X") or s.startswith("Z")) and len(s) >= 6:
        return s
    # Accept BASE/QUOTE
    if "/" in s:
        base, quote = s.split("/", 1)
        return _to_kraken_pair(base, quote)
    # Accept BASEQUOTE (like BTCUSD)
    q = _norm_asset(default_quote)
    if s.endswith(q) and len(s) > len(q):
        base = s[: -len(q)]
        return _to_kraken_pair(base, q)
    # Fallback: treat as base only, pair with wallet quote
    return _to_kraken_pair(s, default_quote)


_ASSET_NAMES: dict[str, str] = {
    "XBT": "Bitcoin",
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
    "ADA": "Cardano",
    "XRP": "XRP",
    "DOGE": "Dogecoin",
    "XDG": "Dogecoin",
}


def _friendly_pair_options(ws_list: list[str]) -> list[str]:
    """Return a friendlier list of wsname pairs for datalist.

    Includes common aliases (BTC for XBT) and optional asset names.
    """
    out: list[str] = []
    seen: set[str] = set()
    for ws in ws_list:
        ws_u = (ws or "").strip().upper()
        if not ws_u or "/" not in ws_u:
            continue
        base, quote = ws_u.split("/", 1)
        base_norm = _norm_asset(base)
        quote_norm = _norm_asset(quote)
        # canonical
        label = f"{base_norm}/{quote_norm}"
        name = _ASSET_NAMES.get(base_norm)
        if name:
            label = f"{label} ({name.upper()})"
        if label not in seen:
            out.append(label)
            seen.add(label)

        # aliases
        if base_norm == "XBT":
            alias = f"BTC/{quote_norm}"
            alias_name = _ASSET_NAMES.get("BTC")
            if alias_name:
                alias = f"{alias} ({alias_name.upper()})"
            if alias not in seen:
                out.append(alias)
                seen.add(alias)

    return out


class WalletViewSet(viewsets.ModelViewSet):
    serializer_class = WalletSerializer
    permission_classes = [IsApproved, IsOwnerOrAdmin]

    def get_queryset(self):
        if self.request.user.role == "admin":
            return Wallet.objects.all()
        return Wallet.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class SleeveViewSet(viewsets.ModelViewSet):
    serializer_class = SleeveSerializer
    permission_classes = [IsApproved, IsOwnerOrAdmin]

    def get_queryset(self):
        if self.request.user.role == "admin":
            return Sleeve.objects.select_related("wallet", "wallet__user")
        return Sleeve.objects.select_related("wallet", "wallet__user").filter(wallet__user=self.request.user)

    def perform_create(self, serializer):
        serializer.save()


class WalletPageView(ApprovalRequiredMixin, TemplateView):
    template_name = "wallets/list.html"

    def _user_wallets(self):
        if self.request.user.role == "admin":
            return Wallet.objects.select_related("user").all()
        return Wallet.objects.select_related("user").filter(user=self.request.user)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # Keep messages persistent; live polling will update the UI without requiring a full refresh.
        err = self.request.session.get("ml_train_error")
        if isinstance(err, dict):
            ctx["ml_train_error"] = str(err.get("msg") or "")
        elif err:
            ctx["ml_train_error"] = str(err)
        else:
            ctx["ml_train_error"] = ""

        notice = self.request.session.get("ml_train_notice")
        msg = ""
        if isinstance(notice, dict):
            msg = str(notice.get("msg") or "")
        elif notice:
            msg = str(notice)
        ctx["ml_train_notice"] = msg
        wallets = self._user_wallets()
        ctx["wallets"] = wallets
        try:
            ctx["reserve_wallet_choices"] = list(wallets)
        except Exception:
            ctx["reserve_wallet_choices"] = []
        ctx["wallet_form"] = WalletForm()
        wallet_ids = [w.id for w in wallets]
        sleeves = Sleeve.objects.select_related("wallet", "wallet__user").filter(wallet_id__in=wallet_ids)
        ctx["sleeve_has_pending_orders"] = {}
        strategies = SleeveStrategy.objects.select_related("sleeve", "sleeve__wallet").filter(sleeve_id__in=[s.id for s in sleeves])
        strat_map = {}
        for st in strategies:
            strat_map.setdefault(st.sleeve_id, []).append(st)
        ctx["strategies"] = strat_map

        sleeve_buy_strategy: dict[int, SleeveStrategy] = {}
        sleeve_sell_strategy: dict[int, SleeveStrategy] = {}
        for sleeve_id, st_list in strat_map.items():
            for st in st_list:
                mode = (getattr(st, "mode", "") or "")
                if mode.startswith("buy") and sleeve_id not in sleeve_buy_strategy:
                    sleeve_buy_strategy[sleeve_id] = st
                if mode.startswith("sell") and sleeve_id not in sleeve_sell_strategy:
                    sleeve_sell_strategy[sleeve_id] = st
        ctx["sleeve_buy_strategy"] = sleeve_buy_strategy
        ctx["sleeve_sell_strategy"] = sleeve_sell_strategy

        sleeve_force_meta: dict[int, dict[str, str]] = {}
        try:
            key = ApiKey.objects.filter(user=self.request.user, is_active=True).order_by("created_at").first()
            if key:
                adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER, user_id=self.request.user.id)
                sleeve_by_id = {int(s.id): s for s in sleeves}
                for sleeve_id in [s.id for s in sleeves]:
                    st = sleeve_buy_strategy.get(sleeve_id) or sleeve_sell_strategy.get(sleeve_id)
                    pair = (getattr(st, "ml_pair", "") or "").strip() if st else ""
                    if not pair:
                        continue
                    ordermin = _pair_ordermin(adapter, pair)
                    min_quote = ""
                    min_pct = ""
                    try:
                        last_price = Decimal(str(getattr(st, "ml_last_price", 0) or 0))
                        if ordermin > 0 and last_price > 0:
                            min_quote_dec = (ordermin * last_price).quantize(Decimal("0.00000001"))
                            min_quote = str(min_quote_dec)

                            # Minimum % needed to meet Kraken ordermin at current price.
                            # Uses sleeve allocated (quote) with the same BUY fee buffer as executor.
                            try:
                                sleeve_obj = sleeve_by_id.get(int(sleeve_id))
                                alloc = Decimal(str(getattr(sleeve_obj, "allocated_balance", 0) or 0)) if sleeve_obj else Decimal("0")
                            except Exception:
                                alloc = Decimal("0")
                            try:
                                fee_buf_pct = Decimal(str(getattr(settings, "EXECUTOR_BUY_FEE_BUFFER_PCT", 0.5) or 0.5)) / Decimal("100")
                            except Exception:
                                fee_buf_pct = Decimal("0")
                            if fee_buf_pct < 0:
                                fee_buf_pct = Decimal("0")
                            if fee_buf_pct > Decimal("0.05"):
                                fee_buf_pct = Decimal("0.05")
                            alloc_eff = alloc
                            if fee_buf_pct > 0:
                                try:
                                    alloc_eff = (alloc * (Decimal("1") - fee_buf_pct)).quantize(Decimal("0.00000001"))
                                except Exception:
                                    alloc_eff = alloc

                            if alloc_eff > 0 and min_quote_dec > 0:
                                try:
                                    pct_need = (min_quote_dec / alloc_eff) * Decimal("100")
                                except Exception:
                                    pct_need = Decimal("0")
                                # Round UP to nearest 5% so the chosen % will satisfy ordermin.
                                try:
                                    step = Decimal("5")
                                    pct_need = (pct_need / step).to_integral_value(rounding=ROUND_UP) * step
                                except Exception:
                                    pass
                                if pct_need < 5:
                                    pct_need = Decimal("5")
                                if pct_need > 95:
                                    min_pct = ">95"
                                else:
                                    try:
                                        min_pct = format(pct_need.quantize(Decimal("0")), "f")
                                    except Exception:
                                        min_pct = str(pct_need)
                    except Exception:
                        min_quote = ""
                    sleeve_force_meta[sleeve_id] = {
                        "pair": pair,
                        "ordermin": format(ordermin, "f"),
                        "min_quote": min_quote,
                        "min_pct": min_pct,
                        "quote": str(getattr(st.sleeve.wallet, "currency", "") or ""),
                    }
        except Exception:
            sleeve_force_meta = {}
        ctx["sleeve_force_meta"] = sleeve_force_meta

        sleeve_ml_paused: dict[int, bool] = {}
        sleeve_ml_stage: dict[int, str] = {}

        sleeve_pos_map: dict[int, Decimal] = {}
        for s in sleeves:
            try:
                sleeve_pos_map[int(s.id)] = Decimal(str(getattr(s, "position_base_qty", 0) or 0))
            except Exception:
                sleeve_pos_map[int(s.id)] = Decimal("0")

        for sleeve_id, st_list in strat_map.items():
            paused_any = any(getattr(st, "ml_paused", False) for st in st_list)
            sleeve_ml_paused[sleeve_id] = paused_any
            # pick a representative stage in priority order
            stages = [str(getattr(st, "ml_stage", "") or "") for st in st_list if getattr(st, "ml_active", False)]
            statuses = [str(getattr(st, "ml_status", "") or "") for st in st_list if getattr(st, "ml_active", False)]
            stage = "idle"
            try:
                pos = sleeve_pos_map.get(int(sleeve_id)) or Decimal("0")
            except Exception:
                pos = Decimal("0")

            pos_eff = pos
            try:
                fm = (sleeve_force_meta or {}).get(int(sleeve_id)) or {}
                o = Decimal(str((fm or {}).get("ordermin") or 0))
            except Exception:
                o = Decimal("0")
            if o > 0 and pos > 0 and pos < o:
                pos_eff = Decimal("0")
            if paused_any:
                stage = "paused"
            elif any(s == "training" for s in statuses):
                stage = "training"
            elif any(s == "pending_fill" for s in stages):
                stage = "pending_fill"
            elif any(s == "placing_order" for s in stages):
                stage = "placing_order"
            elif any(s == "cooldown" for s in stages):
                stage = "cooldown"
            else:
                if any(s == "error" for s in stages):
                    stage = "error"
                else:
                    if pos_eff > 0:
                        if any(s == "waiting_sell" for s in stages):
                            stage = "waiting_sell"
                        elif any(s == "waiting_buy" for s in stages):
                            stage = "waiting_buy"
                    else:
                        if any(s == "waiting_buy" for s in stages):
                            stage = "waiting_buy"
                        elif any(s == "waiting_sell" for s in stages):
                            stage = "waiting_sell"
            sleeve_ml_stage[sleeve_id] = stage

        ctx["sleeve_ml_paused"] = sleeve_ml_paused
        ctx["sleeve_ml_stage"] = sleeve_ml_stage

        sleeve_ml_active: dict[int, bool] = {}
        for st in strategies:
            if st.ml_active:
                sleeve_ml_active[st.sleeve_id] = True
        ctx["sleeve_ml_active"] = sleeve_ml_active

        sleeve_trade_state: dict[int, str] = {}
        for s in sleeves:
            try:
                pos = Decimal(str(getattr(s, "position_base_qty", 0) or 0))
            except Exception:
                pos = Decimal("0")
            pos_eff = pos
            is_dust = False
            try:
                fm = (sleeve_force_meta or {}).get(int(s.id)) or {}
                o = Decimal(str((fm or {}).get("ordermin") or 0))
            except Exception:
                o = Decimal("0")
            if o > 0 and pos > 0 and pos < o:
                pos_eff = Decimal("0")
                is_dust = True
            if is_dust:
                sleeve_trade_state[s.id] = "dust"
            else:
                sleeve_trade_state[s.id] = "sell" if pos_eff > 0 else "buy"
        ctx["sleeve_trade_state"] = sleeve_trade_state
        ctx["ml_active_wallet"] = None
        for st in strategies:
            if st.ml_active:
                ctx["ml_active_wallet"] = st.sleeve.wallet_id
                break

        # Attempt balance refresh from user's active key(s)
        ctx["live_balances_available"] = False
        ctx["live_real"] = {}
        ctx["live_tradeable"] = {}
        ctx["live_keys"] = 0
        ctx["live_no_key"] = False
        ctx["top3_pairs"] = {}
        ctx["wallet_pair_options"] = {}
        cand = top_profit_candidates(limit=25, quote="USD", scan_pairs_limit=40)
        user = self.request.user
        try:
            from trading.services.executor import _get_restricted_pairs_map

            restricted = _get_restricted_pairs_map(int(user.id))
        except Exception:
            restricted = {}

        # Optional proactive permission validation using user's key.
        key = None
        try:
            key = ApiKey.objects.filter(user=user, is_active=True).order_by("created_at").first()
        except Exception:
            key = None
        perm_adapter = None
        if key:
            try:
                perm_adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER, user_id=user.id)
            except Exception:
                perm_adapter = None

        out_cand = []
        try:
            validate_budget = int(getattr(settings, "WALLETS_PAIR_PERMISSION_VALIDATE_BUDGET", 0) or 0)
        except Exception:
            validate_budget = 0
        validate_budget = max(0, min(validate_budget, 10))
        for c in (cand or []):
            try:
                p = str((c or {}).get("pair") or "").strip().upper()
            except Exception:
                p = ""
            if not p:
                continue
            if isinstance(restricted, dict) and p in restricted:
                continue
            if perm_adapter is not None and validate_budget > 0:
                validate_budget -= 1
                if not _pair_tradeable_for_user(perm_adapter, int(user.id), p):
                    continue
            out_cand.append(c)
            if len(out_cand) >= 3:
                break
        ctx["top_profit_candidates"] = out_cand
        ctx["profit_candidates_last_updated"] = profit_candidates_last_updated()
        if wallets:
            user = self.request.user
            keys = list(ApiKey.objects.filter(user=user, is_active=True).order_by("created_at"))
            ctx["live_keys"] = len(keys)
            if keys:
                idx = _BAL_KEY_CURSOR.get(user.id, 0) % len(keys)
                _BAL_KEY_CURSOR[user.id] = (idx + 1) % len(keys)
                key = keys[idx]
                try:
                    adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER, user_id=user.id)
                    bal = adapter.fetch_balances()
                    # Normalize common prefixes (ZUSD->USD, XXBT->XBT)
                    balance_map = {}
                    for asset, val in bal.items():
                        val_dec = Decimal(str(val))
                        balance_map[asset] = val_dec
                        balance_map[asset.upper()] = val_dec
                        if asset.startswith("Z") or asset.startswith("X"):
                            balance_map[asset[1:]] = val_dec
                            balance_map[asset[1:].upper()] = val_dec
                        if asset.startswith("XX") or asset.startswith("ZZ"):
                            balance_map[asset[1:]] = val_dec
                            balance_map[asset[1:].upper()] = val_dec
                    ctx["live_balances"] = balance_map
                    ctx["live_balances_available"] = True
                    ctx["live_balances_debug"] = balance_map
                    ctx["live_balances_json"] = json.dumps({k: str(v) for k, v in balance_map.items()})

                    # compute allocations per wallet
                    allocated = {w.id: Decimal("0") for w in wallets}
                    for s in sleeves:
                        allocated[s.wallet_id] = allocated.get(s.wallet_id, Decimal("0")) + s.allocated_balance
                    # map live real and tradeable (real - allocated)
                    for w in wallets:
                        real_val = balance_map.get(w.currency) or balance_map.get(w.currency.upper())
                        if real_val is None:
                            continue
                        ctx["live_real"][w.id] = real_val
                        tradeable_val = max(Decimal("0"), real_val - allocated.get(w.id, Decimal("0")))
                        ctx["live_tradeable"][w.id] = tradeable_val

                        # Persist latest known balances for fast dashboard loads.
                        if (w.real_balance != real_val) or (w.tradeable_balance != tradeable_val):
                            w.real_balance = real_val
                            w.tradeable_balance = tradeable_val
                            w.save(update_fields=["real_balance", "tradeable_balance"])
                        # simple top3 suggestion: pair wallet currency with three largest non-quote assets
                        quote_norm = _norm_asset(w.currency)
                        others = [
                            (asset, bal)
                            for asset, bal in balance_map.items()
                            if _norm_asset(asset) != quote_norm
                        ]
                        top3 = sorted(others, key=lambda kv: kv[1], reverse=True)[:3]
                        ctx["top3_pairs"][w.id] = [f"{_norm_asset(a)}/{quote_norm}" for a, _ in top3]

                        # Provide valid tradeable pairs for the wallet quote using Kraken AssetPairs.
                        try:
                            _ws_to_code, ws_list = _asset_pairs_for_quote(adapter, quote_norm)
                            ctx["wallet_pair_options"][w.id] = _friendly_pair_options(ws_list)
                        except Exception:
                            ctx["wallet_pair_options"][w.id] = []
                    ctx["live_balances_available"] = True
                except Exception as exc:
                    ctx["live_balances_error"] = True
                    ctx["live_balances_error_msg"] = str(exc)
                    ctx["live_balances_available"] = False
            else:
                ctx["live_no_key"] = True
        return ctx

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")
        def _clear_error():
            try:
                request.session.pop("ml_train_error", None)
            except Exception:
                pass

        def _archive_sleeve_performance(sleeve: Sleeve):
            try:
                w = getattr(sleeve, "wallet", None)
                user = getattr(w, "user", None)
                if not user:
                    return

                try:
                    rec = SleevePerformanceRecord.objects.filter(sleeve_id=int(sleeve.id)).first()
                except Exception:
                    rec = None
                if not rec:
                    rec = SleevePerformanceRecord(
                        user=user,
                        wallet=w,
                        wallet_currency=str(getattr(w, "currency", "") or ""),
                        sleeve_id=int(sleeve.id),
                        sleeve_type=str(getattr(sleeve, "type", "") or ""),
                        base_asset=str(getattr(sleeve, "base_asset", "") or ""),
                        started_at=timezone.now(),
                        start_allocated_quote=Decimal(str(getattr(sleeve, "allocated_balance", 0) or 0)),
                        start_position_base=Decimal(str(getattr(sleeve, "position_base_qty", 0) or 0)),
                    )

                try:
                    orders = list(
                        OrderLog.objects.filter(sleeve=sleeve, status="closed")
                        .only("created_at", "side", "amount", "price", "vol_exec", "cost", "fee", "base_asset", "quote_asset")
                        .order_by("created_at")
                    )
                except Exception:
                    orders = []

                pos = Decimal("0")
                cost = Decimal("0")
                realized = Decimal("0")
                net_quote = Decimal("0")
                last_px = Decimal("0")

                for o in orders:
                    try:
                        qty = Decimal(str(getattr(o, "vol_exec", 0) or 0))
                    except Exception:
                        qty = Decimal("0")
                    if qty <= 0:
                        try:
                            qty = Decimal(str(getattr(o, "amount", 0) or 0))
                        except Exception:
                            qty = Decimal("0")

                    try:
                        cost_quote = Decimal(str(getattr(o, "cost", 0) or 0))
                    except Exception:
                        cost_quote = Decimal("0")
                    try:
                        fee_quote = Decimal(str(getattr(o, "fee", 0) or 0))
                    except Exception:
                        fee_quote = Decimal("0")
                    try:
                        px = Decimal(str(getattr(o, "price", 0) or 0))
                    except Exception:
                        px = Decimal("0")
                    if cost_quote <= 0:
                        if qty <= 0 or px <= 0:
                            continue
                        cost_quote = (qty * px)
                    if qty <= 0:
                        continue

                    last_px = px
                    side = str(getattr(o, "side", "") or "").strip().lower()
                    avg = (cost / pos) if pos > 0 else Decimal("0")

                    if side == "buy":
                        pos = (pos + qty)
                        cost = (cost + cost_quote + fee_quote)
                        net_quote = (net_quote - (cost_quote + fee_quote))
                    elif side == "sell":
                        sell_qty = qty if qty <= pos else pos
                        if sell_qty > 0 and avg > 0:
                            proceeds = cost_quote
                            try:
                                proceeds = (cost_quote - fee_quote)
                            except Exception:
                                proceeds = cost_quote
                            realized = (realized + (proceeds - (avg * sell_qty)))
                            cost = max(Decimal("0"), (cost - (avg * sell_qty)))
                            pos = max(Decimal("0"), (pos - sell_qty))
                            net_quote = (net_quote + proceeds)

                if last_px <= 0:
                    try:
                        st = (
                            SleeveStrategy.objects.filter(sleeve=sleeve)
                            .exclude(ml_last_price__isnull=True)
                            .order_by("-updated_at")
                            .first()
                        )
                    except Exception:
                        st = None
                    if st:
                        try:
                            last_px = Decimal(str(getattr(st, "ml_last_price", 0) or 0))
                        except Exception:
                            last_px = Decimal("0")

                rec.wallet = w
                rec.wallet_currency = str(getattr(w, "currency", "") or "")
                rec.sleeve_type = str(getattr(sleeve, "type", "") or "")
                try:
                    rec.base_asset = str(getattr(sleeve, "base_asset", "") or "")
                except Exception:
                    pass

                try:
                    rec.ended_at = timezone.now()
                except Exception:
                    rec.ended_at = None

                rec.first_trade_at = orders[0].created_at if orders else None
                rec.last_trade_at = orders[-1].created_at if orders else None

                rec.end_allocated_quote = Decimal(str(getattr(sleeve, "allocated_balance", 0) or 0))
                rec.end_position_base = Decimal(str(getattr(sleeve, "position_base_qty", 0) or 0))

                rec.realized_pnl_quote = realized
                rec.net_quote_flow = net_quote
                rec.trades = int(len(orders))

                rec.last_price_quote_per_base = last_px
                rec.end_cash_est_quote = (rec.end_allocated_quote + net_quote)
                rec.end_equity_est_quote = (rec.end_cash_est_quote + (rec.end_position_base * last_px if last_px > 0 else Decimal("0")))

                try:
                    rec.save()
                except Exception:
                    try:
                        rec.save(update_fields=[
                            "wallet",
                            "wallet_currency",
                            "sleeve_type",
                            "base_asset",
                            "ended_at",
                            "first_trade_at",
                            "last_trade_at",
                            "end_allocated_quote",
                            "end_position_base",
                            "realized_pnl_quote",
                            "net_quote_flow",
                            "trades",
                            "last_price_quote_per_base",
                            "end_cash_est_quote",
                            "end_equity_est_quote",
                        ])
                    except Exception:
                        pass
            except Exception:
                return

        if action == "create_wallet":
            form = WalletForm(request.POST)
            if form.is_valid():
                wallet = form.save(commit=False)
                wallet.user = request.user
                wallet.save()
        elif action == "create_sleeve":
            form = SleeveForm(request.POST)
            if not form.is_valid():
                try:
                    request.session["ml_train_error"] = f"Cannot create sleeve: {form.errors.as_text()}"
                except Exception:
                    pass
                return HttpResponseRedirect(reverse("wallets-page"))

            sleeve = form.save(commit=False)
            if request.user.role != "admin" and sleeve.wallet.user != request.user:
                return HttpResponseRedirect(reverse("wallets-page"))
            try:
                if Sleeve.objects.filter(wallet=sleeve.wallet, type=sleeve.type).exists():
                    request.session["ml_train_error"] = (
                        f"Sleeve already exists for this wallet/type ({sleeve.wallet.currency} {sleeve.type}). "
                        "Delete the existing sleeve first."
                    )
                    return HttpResponseRedirect(reverse("wallets-page"))
            except Exception:
                pass

            try:
                sleeve.save()
            except IntegrityError:
                try:
                    request.session["ml_train_error"] = (
                        f"Cannot create sleeve: a sleeve already exists for this wallet/type ({sleeve.wallet.currency} {sleeve.type})."
                    )
                except Exception:
                    pass
                return HttpResponseRedirect(reverse("wallets-page"))

            try:
                SleevePerformanceRecord.objects.create(
                    user=sleeve.wallet.user,
                    wallet=sleeve.wallet,
                    wallet_currency=str(getattr(sleeve.wallet, "currency", "") or ""),
                    sleeve_id=int(sleeve.id),
                    sleeve_type=str(getattr(sleeve, "type", "") or ""),
                    base_asset=str(getattr(sleeve, "base_asset", "") or ""),
                    started_at=timezone.now(),
                    start_allocated_quote=Decimal(str(getattr(sleeve, "allocated_balance", 0) or 0)),
                    start_position_base=Decimal(str(getattr(sleeve, "position_base_qty", 0) or 0)),
                    end_allocated_quote=Decimal(str(getattr(sleeve, "allocated_balance", 0) or 0)),
                    end_position_base=Decimal(str(getattr(sleeve, "position_base_qty", 0) or 0)),
                )
            except Exception:
                pass
        elif action == "set_reserve_wallet":
            wallet_id = request.POST.get("wallet_id")
            reserve_wallet_id = (request.POST.get("reserve_wallet_id") or "").strip()
            try:
                wallet = Wallet.objects.select_related("user", "reserve_wallet").get(id=wallet_id)
            except Wallet.DoesNotExist:
                return HttpResponseRedirect(reverse("wallets-page"))
            if request.user.role != "admin" and wallet.user != request.user:
                return HttpResponseRedirect(reverse("wallets-page"))

            if reserve_wallet_id in {"", "none", "null"}:
                wallet.reserve_wallet = None
                wallet.save(update_fields=["reserve_wallet"])
                return HttpResponseRedirect(reverse("wallets-page"))

            try:
                rwid = int(reserve_wallet_id)
            except Exception:
                request.session["ml_train_error"] = "Invalid reserve wallet selection."
                return HttpResponseRedirect(reverse("wallets-page"))

            if int(wallet.id) == int(rwid):
                request.session["ml_train_error"] = "Reserve wallet cannot be the same as the trading wallet."
                return HttpResponseRedirect(reverse("wallets-page"))

            try:
                reserve_wallet = Wallet.objects.select_related("user").get(id=rwid)
            except Wallet.DoesNotExist:
                request.session["ml_train_error"] = "Reserve wallet not found."
                return HttpResponseRedirect(reverse("wallets-page"))

            if reserve_wallet.user_id != wallet.user_id:
                request.session["ml_train_error"] = "Reserve wallet must belong to the same user."
                return HttpResponseRedirect(reverse("wallets-page"))

            wallet.reserve_wallet = reserve_wallet
            wallet.save(update_fields=["reserve_wallet"])
        elif action == "move_to_reserve":
            wallet_id = request.POST.get("wallet_id")
            amount_raw = request.POST.get("amount") or "0"
            try:
                wallet = Wallet.objects.select_related("user", "reserve_wallet").get(id=wallet_id)
            except Wallet.DoesNotExist:
                return HttpResponseRedirect(reverse("wallets-page"))
            if request.user.role != "admin" and wallet.user != request.user:
                return HttpResponseRedirect(reverse("wallets-page"))
            if not wallet.reserve_wallet:
                request.session["ml_train_error"] = "Set a reserve wallet first."
                return HttpResponseRedirect(reverse("wallets-page"))
            reserve_wallet = wallet.reserve_wallet
            if reserve_wallet.user_id != wallet.user_id:
                request.session["ml_train_error"] = "Reserve wallet must belong to the same user."
                return HttpResponseRedirect(reverse("wallets-page"))
            try:
                amount = Decimal(str(amount_raw))
            except Exception:
                amount = Decimal("0")
            if amount <= 0:
                request.session["ml_train_error"] = "Enter an amount greater than 0 to move to reserve."
                return HttpResponseRedirect(reverse("wallets-page"))
            rate = Decimal("1")
            available = Decimal(str(wallet.tradeable_balance or 0))
            real_bal = Decimal(str(wallet.real_balance or 0))
            if amount > available or amount > real_bal:
                request.session["ml_train_error"] = "Not enough balance to move to reserve."
                return HttpResponseRedirect(reverse("wallets-page"))
            key = ApiKey.objects.filter(user_id=wallet.user_id, is_active=True).order_by("created_at").first()
            if not key:
                request.session["ml_train_error"] = "No active API key for this user to convert funds."
                return HttpResponseRedirect(reverse("wallets-page"))
            adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER, user_id=wallet.user_id)
            pair = _resolve_pair_code_any(adapter, reserve_wallet.currency, wallet.currency)
            if not pair:
                request.session["ml_train_error"] = "Could not resolve conversion pair."
                return HttpResponseRedirect(reverse("wallets-page"))
            try:
                order_resp = adapter._private_request(
                    "AddOrder",
                    {
                        "pair": pair,
                        "type": "buy",
                        "ordertype": "market",
                        "cost": str(amount),
                    },
                    weight=1.0,
                )
            except Exception as exc:
                request.session["ml_train_error"] = f"Conversion failed: {exc}"
                return HttpResponseRedirect(reverse("wallets-page"))

            # Provisional balance move; we don't block on price fetch
            wallet.tradeable_balance = available - amount
            wallet.real_balance = real_bal - amount
            reserve_wallet.real_balance = Decimal(str(reserve_wallet.real_balance or 0)) + amount
            wallet.save(update_fields=["tradeable_balance", "real_balance"])
            reserve_wallet.save(update_fields=["real_balance"])

            # Attempt to fetch executed volume for display; warn if missing
            exec_note = ""
            try:
                txid = None
                if isinstance(order_resp, dict):
                    descr = (order_resp.get("result") or {}).get("descr") or {}
                    txids = (order_resp.get("result") or {}).get("txid") or []
                    if txids:
                        txid = txids[0]
                ticker = adapter.fetch_ticker(pair)
                px_val = ticker.get("price") if isinstance(ticker, dict) else None
                px = Decimal(str(px_val or 0)) if px_val else Decimal("0")
                if px > 0 and amount > 0:
                    est_base = (Decimal(str(amount)) / px).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
                    exec_note = f" (est {est_base} {reserve_wallet.currency})"
                elif txid:
                    exec_note = f" (order {txid})"
            except Exception:
                exec_note = " (conversion placed; price unavailable)"

            request.session["ml_train_notice"] = f"Moved {amount} {wallet.currency} to reserve via market buy{exec_note}."
        elif action == "move_from_reserve":
            wallet_id = request.POST.get("wallet_id")
            amount_raw = request.POST.get("amount") or "0"
            try:
                wallet = Wallet.objects.select_related("user", "reserve_wallet").get(id=wallet_id)
            except Wallet.DoesNotExist:
                return HttpResponseRedirect(reverse("wallets-page"))
            if request.user.role != "admin" and wallet.user != request.user:
                return HttpResponseRedirect(reverse("wallets-page"))
            if not wallet.reserve_wallet:
                request.session["ml_train_error"] = "Set a reserve wallet first."
                return HttpResponseRedirect(reverse("wallets-page"))
            reserve_wallet = wallet.reserve_wallet
            if reserve_wallet.user_id != wallet.user_id:
                request.session["ml_train_error"] = "Reserve wallet must belong to the same user."
                return HttpResponseRedirect(reverse("wallets-page"))
            try:
                amount = Decimal(str(amount_raw))
            except Exception:
                amount = Decimal("0")
            if amount <= 0:
                request.session["ml_train_error"] = "Enter an amount greater than 0 to move from reserve."
                return HttpResponseRedirect(reverse("wallets-page"))
            rate = Decimal("1")
            reserve_real = Decimal(str(reserve_wallet.real_balance or 0))
            if amount > reserve_real:
                request.session["ml_train_error"] = "Not enough reserve balance."
                return HttpResponseRedirect(reverse("wallets-page"))
            key = ApiKey.objects.filter(user_id=wallet.user_id, is_active=True).order_by("created_at").first()
            if not key:
                request.session["ml_train_error"] = "No active API key for this user to convert funds."
                return HttpResponseRedirect(reverse("wallets-page"))
            adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER, user_id=wallet.user_id)
            pair = _resolve_pair_code_any(adapter, reserve_wallet.currency, wallet.currency)
            if not pair:
                request.session["ml_train_error"] = "Could not resolve conversion pair."
                return HttpResponseRedirect(reverse("wallets-page"))
            volume_base = amount.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            if volume_base <= 0:
                request.session["ml_train_error"] = "Calculated conversion volume is zero."
                return HttpResponseRedirect(reverse("wallets-page"))
            try:
                order_resp = adapter._private_request(
                    "AddOrder",
                    {
                        "pair": pair,
                        "type": "sell",
                        "ordertype": "market",
                        "volume": str(volume_base),
                    },
                    weight=1.0,
                )
            except Exception as exc:
                request.session["ml_train_error"] = f"Conversion failed: {exc}"
                return HttpResponseRedirect(reverse("wallets-page"))

            # Provisional credit back to trading wallet
            wallet.tradeable_balance = Decimal(str(wallet.tradeable_balance or 0)) + amount
            wallet.real_balance = Decimal(str(wallet.real_balance or 0)) + amount
            reserve_wallet.real_balance = reserve_real - volume_base
            wallet.save(update_fields=["tradeable_balance", "real_balance"])
            reserve_wallet.save(update_fields=["real_balance"])

            exec_note = ""
            try:
                txid = None
                if isinstance(order_resp, dict):
                    txids = (order_resp.get("result") or {}).get("txid") or []
                    if txids:
                        txid = txids[0]
                if txid:
                    exec_note = f" (order {txid})"
            except Exception:
                exec_note = " (conversion placed)"

            request.session["ml_train_notice"] = f"Moved {amount} {reserve_wallet.currency} back from reserve via market sell{exec_note}."

        elif action == "delete_wallet":
            wallet_id = request.POST.get("wallet_id")
            try:
                wallet = Wallet.objects.select_related("user").get(id=wallet_id)
            except Wallet.DoesNotExist:
                return HttpResponseRedirect(reverse("wallets-page"))
            if request.user.role != "admin" and wallet.user != request.user:
                return HttpResponseRedirect(reverse("wallets-page"))
            try:
                for s in Sleeve.objects.filter(wallet=wallet).select_related("wallet", "wallet__user"):
                    _archive_sleeve_performance(s)
            except Exception:
                pass
            wallet.delete()
        elif action == "delete_sleeve":
            sleeve_id = request.POST.get("sleeve_id")
            try:
                sleeve = Sleeve.objects.select_related("wallet", "wallet__user").get(id=sleeve_id)
            except Sleeve.DoesNotExist:
                return HttpResponseRedirect(reverse("wallets-page"))
            if request.user.role != "admin" and sleeve.wallet.user != request.user:
                return HttpResponseRedirect(reverse("wallets-page"))

            try:
                pos = Decimal(str(getattr(sleeve, "position_base_qty", 0) or 0))
            except Exception:
                pos = Decimal("0")
            risky = False
            try:
                risky = bool(pos > 0) or bool(
                    OrderLog.objects.filter(sleeve=sleeve, txid__gt="", status__in=["submitted", "open", "pending"]).exists()
                )
            except Exception:
                risky = bool(pos > 0)
            _archive_sleeve_performance(sleeve)
            sleeve.delete()
            if risky:
                request.session["ml_train_notice"] = {
                    "msg": (
                        "Deleted sleeve while it had an open position and/or pending orders. "
                        "Funds may still be held on Kraken; cancel/sell manually if needed."
                    ),
                    "ts": time.time(),
                }
        elif action == "topup_sleeve":
            sleeve_id = request.POST.get("sleeve_id")
            raw = (request.POST.get("topup_amount") or "").strip()
            try:
                sleeve = Sleeve.objects.select_related("wallet", "wallet__user").get(id=sleeve_id)
            except Sleeve.DoesNotExist:
                return HttpResponseRedirect(reverse("wallets-page"))
            if request.user.role != "admin" and sleeve.wallet.user != request.user:
                return HttpResponseRedirect(reverse("wallets-page"))

            wallet = sleeve.wallet
            tradeable = Decimal(str(getattr(wallet, "tradeable_balance", 0) or 0))
            real_val = None

            try:
                key = ApiKey.objects.filter(user=wallet.user, is_active=True).order_by("created_at").first()
            except Exception:
                key = None
            if key:
                try:
                    adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER, user_id=wallet.user_id)
                    bal = adapter.fetch_balances()
                    balance_map = {}
                    for asset, val in (bal or {}).items():
                        v = Decimal(str(val))
                        balance_map[asset] = v
                        balance_map[asset.upper()] = v
                        if asset.startswith("Z") or asset.startswith("X"):
                            balance_map[asset[1:]] = v
                            balance_map[asset[1:].upper()] = v
                        if asset.startswith("XX") or asset.startswith("ZZ"):
                            balance_map[asset[1:]] = v
                            balance_map[asset[1:].upper()] = v

                    rv = balance_map.get(wallet.currency) or balance_map.get(str(wallet.currency).upper())
                    if rv is not None:
                        real_val = rv
                        alloc_total = Sleeve.objects.filter(wallet_id=wallet.id).aggregate(
                            total=models.Sum("allocated_balance")
                        ).get("total") or Decimal("0")
                        tradeable = max(Decimal("0"), Decimal(str(real_val)) - Decimal(str(alloc_total)))
                except Exception:
                    pass

            if raw == "":
                request.session["ml_train_error"] = "Enter an amount to add, or click 'Add All'."
                return HttpResponseRedirect(reverse("wallets-page"))

            if raw.lower() == "all":
                add_amt = tradeable
            else:
                try:
                    add_amt = Decimal(str(raw or "0"))
                except Exception:
                    add_amt = Decimal("0")

            if add_amt < 0:
                add_amt = Decimal("0")
            if add_amt > tradeable:
                add_amt = tradeable

            if add_amt > 0:
                sleeve.allocated_balance = (Decimal(str(sleeve.allocated_balance or 0)) + add_amt).quantize(Decimal("0.00000001"))
                sleeve.save(update_fields=["allocated_balance"])

                try:
                    if real_val is not None:
                        wallet.real_balance = Decimal(str(real_val)).quantize(Decimal("0.00000001"))
                    wallet.tradeable_balance = max(Decimal("0"), tradeable - add_amt).quantize(Decimal("0.00000001"))
                    wallet.save(update_fields=["real_balance", "tradeable_balance"])
                except Exception:
                    pass

            request.session["ml_train_notice"] = {
                "msg": f"Added {format(add_amt, 'f')} {wallet.currency} tradeable to {wallet.currency} sleeve ({sleeve.type}).",
                "ts": time.time(),
            }
            _clear_error()
        elif action == "reset_sleeve_position":
            sleeve_id = request.POST.get("sleeve_id")
            try:
                sleeve = Sleeve.objects.select_related("wallet", "wallet__user").get(id=sleeve_id)
            except Sleeve.DoesNotExist:
                return HttpResponseRedirect(reverse("wallets-page"))
            if request.user.role != "admin" and sleeve.wallet.user != request.user:
                return HttpResponseRedirect(reverse("wallets-page"))
            sleeve.position_base_qty = Decimal("0")
            sleeve.save(update_fields=["position_base_qty"])
            request.session["ml_train_notice"] = {
                "msg": f"Reset sleeve position to 0 for {sleeve.wallet.currency} sleeve ({sleeve.type}).",
                "ts": time.time(),
            }
            _clear_error()
        elif action == "set_trade_amount":
            sleeve_id = request.POST.get("sleeve_id")
            raw = (request.POST.get("trade_amount_base") or "").strip()
            try:
                sleeve = Sleeve.objects.select_related("wallet", "wallet__user").get(id=sleeve_id)
            except Sleeve.DoesNotExist:
                return HttpResponseRedirect(reverse("wallets-page"))
            if request.user.role != "admin" and sleeve.wallet.user != request.user:
                return HttpResponseRedirect(reverse("wallets-page"))
            try:
                val = Decimal(str(raw or "0"))
            except Exception:
                val = Decimal("0")
            if val < 0:
                val = Decimal("0")

            try:
                key = ApiKey.objects.filter(user=sleeve.wallet.user, is_active=True).order_by("created_at").first()
            except Exception:
                key = None
            pair = ""
            try:
                st = SleeveStrategy.objects.filter(sleeve=sleeve, is_active=True, ml_active=True).order_by("mode").first()
                pair = (getattr(st, "ml_pair", "") or "").strip() if st else ""
            except Exception:
                pair = ""
            if key and pair:
                try:
                    adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER, user_id=sleeve.wallet.user_id)
                    ordermin = _pair_ordermin(adapter, pair)
                    if ordermin > 0 and val < ordermin:
                        val = ordermin
                except Exception:
                    pass

            sleeve.trade_amount_base = val
            sleeve.save(update_fields=["trade_amount_base"])
            val_disp = ""
            try:
                val_disp = format(val, "f")
            except Exception:
                val_disp = str(val)
            request.session["ml_train_notice"] = {
                "msg": f"Trade amount set to {val_disp} base units for {sleeve.wallet.currency} sleeve ({sleeve.type}).",
                "ts": time.time(),
            }
            _clear_error()
        elif action == "set_trade_pct":
            sleeve_id = request.POST.get("sleeve_id")
            mode = (request.POST.get("trade_pct_mode") or "").strip().lower()
            if mode not in {"both", "individual"}:
                mode = "both"

            def _pct(name: str) -> Decimal:
                raw = (request.POST.get(name) or "").strip()
                try:
                    v = Decimal(str(raw or "0"))
                except Exception:
                    v = Decimal("0")
                if v < 0:
                    v = Decimal("0")
                if v == 0:
                    return Decimal("0")
                if v < 5:
                    v = Decimal("5")
                if v > 95:
                    v = Decimal("95")
                # Floor to 5% increments so manual typing can't create odd values.
                try:
                    step = Decimal("5")
                    v = (v / step).to_integral_value(rounding=ROUND_DOWN) * step
                except Exception:
                    pass
                return v

            try:
                sleeve = Sleeve.objects.select_related("wallet", "wallet__user").get(id=sleeve_id)
            except Sleeve.DoesNotExist:
                return HttpResponseRedirect(reverse("wallets-page"))
            if request.user.role != "admin" and sleeve.wallet.user != request.user:
                return HttpResponseRedirect(reverse("wallets-page"))

            sleeve.trade_pct_mode = mode
            sleeve.trade_pct_both = _pct("trade_pct_both")
            sleeve.trade_pct_buy = _pct("trade_pct_buy")
            sleeve.trade_pct_sell = _pct("trade_pct_sell")
            sleeve.save(update_fields=["trade_pct_mode", "trade_pct_both", "trade_pct_buy", "trade_pct_sell"])
            request.session["ml_train_notice"] = {
                "msg": f"Trade % settings saved for {sleeve.wallet.currency} sleeve ({sleeve.type}).",
                "ts": time.time(),
            }
            _clear_error()
        elif action == "set_entry_mode":
            sleeve_id = request.POST.get("sleeve_id")
            mode = (request.POST.get("buy_entry_mode") or "").strip().lower()
            if mode not in {"dip", "momentum"}:
                mode = "dip"
            try:
                sleeve = Sleeve.objects.select_related("wallet", "wallet__user").get(id=sleeve_id)
            except Sleeve.DoesNotExist:
                return HttpResponseRedirect(reverse("wallets-page"))
            if request.user.role != "admin" and sleeve.wallet.user != request.user:
                return HttpResponseRedirect(reverse("wallets-page"))

            sleeve.buy_entry_mode = mode
            sleeve.save(update_fields=["buy_entry_mode"])
            request.session["ml_train_notice"] = {
                "msg": f"Entry mode set to {mode.upper()} for {sleeve.wallet.currency} sleeve ({sleeve.type}).",
                "ts": time.time(),
            }
            _clear_error()
        elif action == "set_limit_failures":
            sleeve_id = request.POST.get("sleeve_id")
            raw = (request.POST.get("limit_max_failures") or "").strip()
            try:
                sleeve = Sleeve.objects.select_related("wallet", "wallet__user").get(id=sleeve_id)
            except Sleeve.DoesNotExist:
                return HttpResponseRedirect(reverse("wallets-page"))
            if request.user.role != "admin" and sleeve.wallet.user != request.user:
                return HttpResponseRedirect(reverse("wallets-page"))

            try:
                val = int(float(raw or 0))
            except Exception:
                val = 0
            if val < 0:
                val = 0
            if val > 10:
                val = 10
            sleeve.limit_max_failures = val
            sleeve.save(update_fields=["limit_max_failures"])
            request.session["ml_train_notice"] = {
                "msg": f"Limit failures set to {val} for {sleeve.wallet.currency} sleeve ({sleeve.type}).",
                "ts": time.time(),
            }
            _clear_error()
        elif action in {"force_buy", "force_sell"}:
            sleeve_id = request.POST.get("sleeve_id")
            try:
                sleeve = Sleeve.objects.select_related("wallet", "wallet__user").get(id=sleeve_id)
            except Sleeve.DoesNotExist:
                return HttpResponseRedirect(reverse("wallets-page"))
            if request.user.role != "admin" and sleeve.wallet.user != request.user:
                return HttpResponseRedirect(reverse("wallets-page"))

            # Block if there's a pending order for this sleeve.
            if OrderLog.objects.filter(sleeve=sleeve, txid__gt="", status__in=["submitted", "open", "pending"]).exists():
                request.session["ml_train_error"] = "Cannot force trade while a pending order exists (pending_fill)."
                return HttpResponseRedirect(reverse("wallets-page"))

            key = ApiKey.objects.filter(user=sleeve.wallet.user, is_active=True).order_by("created_at").first()
            if not key:
                request.session["ml_train_error"] = "No active Kraken API key found. Add/activate a key first."
                return HttpResponseRedirect(reverse("wallets-page"))
            adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER, user_id=sleeve.wallet.user_id)

            is_buy = action == "force_buy"
            st = (
                SleeveStrategy.objects.filter(sleeve=sleeve, is_active=True, mode__startswith=("buy" if is_buy else "sell"))
                .order_by("mode")
                .first()
            )
            if not st or not (st.ml_pair or "").strip():
                request.session["ml_train_error"] = "No strategy/pair configured for this sleeve yet. Train ML first."
                return HttpResponseRedirect(reverse("wallets-page"))
            pair = (st.ml_pair or "").strip()

            try:
                ticker = adapter.fetch_ticker(pair)
                price_now = Decimal(str(ticker.get("price") or 0))
            except Exception as exc:  # noqa: BLE001
                request.session["ml_train_error"] = f"Ticker fetch failed for {pair}: {exc}"
                return HttpResponseRedirect(reverse("wallets-page"))
            if price_now <= 0:
                request.session["ml_train_error"] = f"No valid price for {pair}; cannot force trade."
                return HttpResponseRedirect(reverse("wallets-page"))

            if is_buy:
                alloc = Decimal(str(getattr(sleeve, "allocated_balance", 0) or 0))
                try:
                    fee_buf_pct = Decimal(str(getattr(settings, "EXECUTOR_BUY_FEE_BUFFER_PCT", 0.5) or 0.5)) / Decimal("100")
                except Exception:
                    fee_buf_pct = Decimal("0")
                if fee_buf_pct < 0:
                    fee_buf_pct = Decimal("0")
                try:
                    bal = adapter.fetch_balances()
                except Exception:
                    bal = {}
                balance_map = {}
                try:
                    for asset, val in (bal or {}).items():
                        v = Decimal(str(val))
                        balance_map[asset] = v
                        balance_map[asset.upper()] = v
                        if asset.startswith("Z") or asset.startswith("X"):
                            balance_map[asset[1:]] = v
                            balance_map[asset[1:].upper()] = v
                        if asset.startswith("XX") or asset.startswith("ZZ"):
                            balance_map[asset[1:]] = v
                            balance_map[asset[1:].upper()] = v
                except Exception:
                    balance_map = {}
                quote = str(getattr(sleeve.wallet, "currency", "") or "").strip().upper()
                try:
                    avail_quote = Decimal(str(balance_map.get(quote) or 0))
                except Exception:
                    avail_quote = Decimal("0")
                req_quote = alloc
                try:
                    req_quote = (alloc * (Decimal("1") + fee_buf_pct)).quantize(Decimal("0.00000001"))
                except Exception:
                    req_quote = alloc
                if avail_quote <= 0 or req_quote <= 0 or req_quote > (avail_quote + Decimal("0.00000001")):
                    request.session["ml_train_error"] = (
                        f"Force BUY blocked: insufficient {quote} on Kraken. Need ~{req_quote} {quote}, available {avail_quote}. "
                        "Deposit funds or lower allocated_balance."
                    )
                    return HttpResponseRedirect(reverse("wallets-page"))
                volume = float((alloc / price_now) if price_now > 0 else Decimal("0"))
            else:
                owned_qty = Decimal(str(getattr(sleeve, "position_base_qty", 0) or 0))
                if owned_qty <= 0:
                    request.session["ml_train_error"] = "Sleeve-owned position is 0; cannot force sell."
                    return HttpResponseRedirect(reverse("wallets-page"))
                try:
                    bal = adapter.fetch_balances()
                except Exception:
                    bal = {}
                balance_map = {}
                try:
                    for asset, val in (bal or {}).items():
                        v = Decimal(str(val))
                        balance_map[asset] = v
                        balance_map[asset.upper()] = v
                        if asset.startswith("Z") or asset.startswith("X"):
                            balance_map[asset[1:]] = v
                            balance_map[asset[1:].upper()] = v
                        if asset.startswith("XX") or asset.startswith("ZZ"):
                            balance_map[asset[1:]] = v
                            balance_map[asset[1:].upper()] = v
                except Exception:
                    balance_map = {}
                base = _norm_asset(getattr(sleeve, "base_asset", "") or "")
                try:
                    avail_base = Decimal(str(balance_map.get(base) or 0))
                except Exception:
                    avail_base = Decimal("0")
                if avail_base <= 0 or owned_qty > (avail_base + Decimal("0.0000000001")):
                    request.session["ml_train_error"] = (
                        f"Force SELL blocked: insufficient {base} on Kraken. Need {owned_qty}, available {avail_base}. "
                        "Sync balances or reduce position."
                    )
                    return HttpResponseRedirect(reverse("wallets-page"))
                volume = float(owned_qty)

            if volume <= 0:
                request.session["ml_train_error"] = "Computed volume is 0; cannot force trade."
                return HttpResponseRedirect(reverse("wallets-page"))

            # Enforce Kraken per-pair minimum order size.
            try:
                ordermin = _pair_ordermin(adapter, pair)
                if ordermin > 0 and Decimal(str(volume)) < ordermin:
                    min_quote = (ordermin * price_now).quantize(Decimal("0.00000001"))
                    request.session["ml_train_error"] = (
                        f"Force order volume too small for {pair}. Kraken minimum is {ordermin} base units "
                        f"(~{min_quote} {sleeve.wallet.currency} at current price). Increase allocated_balance."
                    )
                    return HttpResponseRedirect(reverse("wallets-page"))
            except Exception:
                pass

            try:
                result = adapter.place_order(side=("buy" if is_buy else "sell"), pair=pair, volume=volume, price=None)
                txid_val = result.get("txid")
                txid = ""
                if isinstance(txid_val, list) and txid_val:
                    txid = str(txid_val[0])
                elif txid_val:
                    txid = str(txid_val)
                OrderLog.objects.create(
                    sleeve=sleeve,
                    api_key=key,
                    side=(OrderLog.Side.BUY if is_buy else OrderLog.Side.SELL),
                    order_type="market",
                    base_asset=_norm_asset(getattr(sleeve, "base_asset", "") or ""),
                    quote_asset=_norm_asset(getattr(sleeve.wallet, "currency", "") or ""),
                    amount=volume,
                    price=price_now,
                    txid=txid,
                    status=result.get("status", "submitted"),
                    error="",
                )

                # Mark all strategies for sleeve as pending_fill so UI blocks until confirmed.
                SleeveStrategy.objects.filter(sleeve=sleeve, ml_active=True).update(
                    ml_stage="pending_fill",
                    ml_last_action_at=timezone.now(),
                    ml_last_reason=("force_buy" if is_buy else "force_sell"),
                )

                request.session["ml_train_notice"] = {
                    "msg": f"Forced {'BUY' if is_buy else 'SELL'} market order submitted for {pair} (txid: {txid or 'n/a'}).",
                    "ts": time.time(),
                }
                _clear_error()

                try:
                    from trading.services.order_sync import sync_pending_orders
                    from trading.tasks import sync_pending_orders_now

                    sync_pending_orders(limit=10)
                    sync_pending_orders_now()
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001
                err_s = str(exc)
                if "EAccount:Invalid permissions" in err_s or "trading restricted" in err_s or "restricted for" in err_s:
                    try:
                        from trading.services.executor import _mark_pair_restricted

                        _mark_pair_restricted(int(sleeve.wallet.user_id), pair, err_s)
                    except Exception:
                        pass
                    request.session["ml_train_error"] = (
                        f"Force order blocked for {pair}: {err_s}. "
                        "This pair is restricted for your region/account; it has been blocked to prevent retries."
                    )
                else:
                    request.session["ml_train_error"] = f"Force order failed for {pair}: {exc}"
                return HttpResponseRedirect(reverse("wallets-page"))

            return HttpResponseRedirect(reverse("wallets-page"))

        elif action == "set_trade_mode":
            sleeve_id = request.POST.get("sleeve_id")
            mode = (request.POST.get("trade_mode") or "").strip().lower()
            if mode not in {"auto", "custom"}:
                return HttpResponseRedirect(reverse("wallets-page"))
            try:
                sleeve = Sleeve.objects.select_related("wallet", "wallet__user").get(id=sleeve_id)
            except Sleeve.DoesNotExist:
                return HttpResponseRedirect(reverse("wallets-page"))
            if request.user.role != "admin" and sleeve.wallet.user != request.user:
                return HttpResponseRedirect(reverse("wallets-page"))

            sleeve.trade_mode = mode
            sleeve.save(update_fields=["trade_mode"])

            if mode == "custom":
                modes = [
                    SleeveStrategy.Mode.BUY_QUIET,
                    SleeveStrategy.Mode.SELL_QUIET,
                    SleeveStrategy.Mode.BUY_FLASH,
                    SleeveStrategy.Mode.SELL_FLASH,
                ]
                for m in modes:
                    if sleeve.type == Sleeve.Type.QUIET and "quiet" not in m:
                        continue
                    if sleeve.type == Sleeve.Type.FLASH and "flash" not in m:
                        continue
                    st, _ = SleeveStrategy.objects.get_or_create(sleeve=sleeve, mode=m)
                    st.ml_status = "custom" if st.ml_active else st.ml_status
                    if not getattr(st, "ml_stage", "") or st.ml_stage == "idle":
                        st.ml_stage = "waiting_buy" if str(st.mode).startswith("buy") else "waiting_sell"
                    st.save(update_fields=["ml_status", "ml_stage"])
                request.session["ml_train_notice"] = {
                    "msg": f"Trade mode set to CUSTOM for {sleeve.wallet.currency} sleeve ({sleeve.type}).",
                    "ts": time.time(),
                }
            else:
                request.session["ml_train_notice"] = {
                    "msg": f"Trade mode set to AUTO for {sleeve.wallet.currency} sleeve ({sleeve.type}).",
                    "ts": time.time(),
                }
            _clear_error()

        elif action == "set_custom_triggers":
            strategy_id = request.POST.get("strategy_id")
            try:
                st = SleeveStrategy.objects.select_related("sleeve", "sleeve__wallet", "sleeve__wallet__user").get(id=strategy_id)
            except SleeveStrategy.DoesNotExist:
                return HttpResponseRedirect(reverse("wallets-page"))
            if request.user.role != "admin" and st.sleeve.wallet.user != request.user:
                return HttpResponseRedirect(reverse("wallets-page"))

            if getattr(st.sleeve, "trade_mode", "auto") != "custom":
                request.session["ml_train_error"] = "Cannot set custom triggers while sleeve is in AUTO mode."
                return HttpResponseRedirect(reverse("wallets-page"))

            def _dec(name: str) -> Decimal:
                raw = (request.POST.get(name) or "").strip()
                if raw == "":
                    return Decimal("0")
                return Decimal(str(raw))

            def _int(name: str) -> int:
                raw = (request.POST.get(name) or "").strip()
                if raw == "":
                    return 0
                return int(float(raw))

            params = dict(st.params or {})
            params["entry_drop_pct"] = str(_dec("entry_drop_pct"))
            params["take_profit_pct"] = str(_dec("take_profit_pct"))
            params["stop_loss_pct"] = str(_dec("stop_loss_pct"))
            params["position_size_pct"] = str(_dec("position_size_pct"))
            params["cooldown_seconds"] = _int("cooldown_seconds")
            st.params = params
            st.save(update_fields=["params"])
            request.session["ml_train_notice"] = {
                "msg": f"Custom triggers saved for {st.sleeve.wallet.currency} sleeve ({st.sleeve.type}) {st.mode}.",
                "ts": time.time(),
            }
            _clear_error()

        elif action in {"train_ml", "stop_ml", "pause_ml", "resume_ml"}:
            # Existing ML action handler continues below in this file.
            # (Kept here to preserve behavior; logic is implemented in this same view.)
            return self._handle_ml_actions(request)

        return HttpResponseRedirect(reverse("wallets-page"))

    def _handle_ml_actions(self, request):
        action = request.POST.get("action")
        sleeve_id = request.POST.get("sleeve_id")
        pair = request.POST.get("pair", "").strip()

        def _clear_error():
            try:
                request.session.pop("ml_train_error", None)
            except Exception:
                pass

        try:
            sleeve = Sleeve.objects.select_related("wallet", "wallet__user").get(id=sleeve_id)
        except Sleeve.DoesNotExist:
            return HttpResponseRedirect(reverse("wallets-page"))
        if request.user.role != "admin" and sleeve.wallet.user != request.user:
            return HttpResponseRedirect(reverse("wallets-page"))
        if action == "pause_ml":
            strategies = SleeveStrategy.objects.filter(sleeve=sleeve, ml_active=True)
            for st in strategies:
                st.ml_paused = True
                st.ml_status = "paused"
                st.ml_stage = "paused"
                st.save(update_fields=["ml_paused", "ml_status", "ml_stage"])
            request.session["ml_train_notice"] = {
                "msg": f"ML paused for {sleeve.wallet.currency} sleeve ({sleeve.type}).",
                "ts": time.time(),
            }
            _clear_error()
            return HttpResponseRedirect(reverse("wallets-page"))

        if action == "resume_ml":
            strategies = SleeveStrategy.objects.filter(sleeve=sleeve, ml_active=True)
            for st in strategies:
                st.ml_paused = False
                st.ml_status = "training"
                st.ml_stage = "training"
                st.ml_confidence = Decimal("0")
                st.ml_entry_drop_pct = Decimal("0")
                st.ml_take_profit_pct = Decimal("0")
                st.ml_stop_loss_pct = Decimal("0")
                st.ml_position_size_pct = Decimal("0")
                st.ml_cooldown_seconds = 0
                st.ml_candles_1h = 0
                st.ml_last_trained_at = None
                st.save(update_fields=[
                    "ml_paused",
                    "ml_status",
                    "ml_stage",
                    "ml_confidence",
                    "ml_entry_drop_pct",
                    "ml_take_profit_pct",
                    "ml_stop_loss_pct",
                    "ml_position_size_pct",
                    "ml_cooldown_seconds",
                    "ml_candles_1h",
                    "ml_last_trained_at",
                ])
            request.session["ml_train_notice"] = {
                "msg": f"ML resumed (retraining) for {sleeve.wallet.currency} sleeve ({sleeve.type}).",
                "ts": time.time(),
            }
            _clear_error()
            try:
                from trading.tasks import refresh_all_ml_vars_now

                refresh_all_ml_vars_now()
            except Exception:
                pass

            try:
                from trading.tasks import refresh_all_ml_prices_now

                refresh_all_ml_prices_now()
            except Exception:
                pass
            return HttpResponseRedirect(reverse("wallets-page"))

        # enforce single ML wallet per user on start
        if action == "train_ml":
            existing = SleeveStrategy.objects.filter(
                sleeve__wallet__user=request.user,
                ml_active=True,
            ).exclude(sleeve__wallet_id=sleeve.wallet_id)
            if existing.exists():
                request.session["ml_train_error"] = "ML is already active on another wallet for this user. Stop ML there first."
                return HttpResponseRedirect(reverse("wallets-page"))
            quote = sleeve.wallet.currency
            quote_norm = _norm_asset(quote)
            base = sleeve.base_asset or "XBT"
            base_norm = _norm_asset(base)
            target_pair = _to_kraken_pair(base_norm, quote_norm)

            if pair:
                raw = (pair or "").strip()
                resolved = ""
                try:
                    key = ApiKey.objects.filter(user=request.user, is_active=True).order_by("created_at").first()
                    if key:
                        adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER, user_id=request.user.id)
                        ws_to_code, _ws_list = _asset_pairs_for_quote(adapter, quote_norm)
                        if "/" in raw:
                            raw_u = raw.strip().upper()
                            if "(" in raw_u:
                                raw_u = raw_u.split("(", 1)[0]
                            raw_u = raw_u.strip()
                            if " " in raw_u:
                                raw_u = raw_u.split(" ", 1)[0]
                            if "/" in raw_u:
                                b, q = raw_u.split("/", 1)
                                b = "XBT" if b == "BTC" else b
                                q = _norm_asset(q)
                                b = _norm_asset(b)
                                raw_u = f"{b}/{q}"
                            resolved = ws_to_code.get(raw_u, "")
                except Exception:
                    resolved = ""
                if not resolved:
                    resolved = _norm_pair_input(raw, default_quote=quote_norm)
                if resolved:
                    target_pair = resolved

            try:
                key = ApiKey.objects.filter(user=request.user, is_active=True).order_by("created_at").first()
                if not key:
                    request.session["ml_train_error"] = "No active Kraken API key found. Add/activate a key first."
                    return HttpResponseRedirect(reverse("wallets-page"))
                adapter = KrakenAdapter(
                    key.public_key,
                    key.private_key,
                    rate_limiter=GLOBAL_RATE_LIMITER,
                    user_id=request.user.id,
                )

                ws_to_code, _ws_list = _asset_pairs_for_quote(adapter, quote_norm)
                desired_ws = f"{base_norm}/{quote_norm}".upper()
                resolved_code = ws_to_code.get(desired_ws, "")

                if not resolved_code:
                    candidates = [ws for ws in ws_to_code.keys() if ws.startswith(f"{base_norm}/")]
                    if len(candidates) == 1:
                        resolved_code = ws_to_code.get(candidates[0], "")
                    elif candidates:
                        show = ", ".join(candidates[:8])
                        request.session["ml_train_error"] = (
                            f"Asset {base_norm} exists, but Kraken has multiple pairs for quote {quote_norm}: {show}. "
                            "Pick the exact pair from suggestions (top of Wallets page) and try again."
                        )
                        return HttpResponseRedirect(reverse("wallets-page"))
                    else:
                        request.session["ml_train_error"] = (
                            f"Kraken does not list an online pair for {base_norm}/{quote_norm}. "
                            "Pick a valid pair from suggestions and try again."
                        )
                        return HttpResponseRedirect(reverse("wallets-page"))

                target_pair = resolved_code

                ticker = adapter.fetch_ticker(target_pair)
                price_now = Decimal(str(ticker.get("price") or 0))
                since_ts = int((datetime.utcnow() - timedelta(days=5)).timestamp())
                payload_1h = adapter.fetch_ohlc(target_pair, interval=60, since=since_ts)
                rows_1h = _extract_ohlc_rows(payload_1h, target_pair)
                if len(rows_1h) < 50:
                    payload_15m = adapter.fetch_ohlc(target_pair, interval=15, since=since_ts)
                    rows_15m = _extract_ohlc_rows(payload_15m, target_pair)
                    if len(rows_15m) >= 200:
                        rows_1h = [(ts - (ts % 3600), close) for ts, close in rows_15m]
                if price_now <= 0 or not rows_1h:
                    request.session["ml_train_error"] = (
                        f"Kraken returned no usable market data for pair {target_pair}. "
                        "This usually means the pair code is invalid or the asset is too new for OHLC/ticker. "
                        "Pick a different pair and try again."
                    )
                    return HttpResponseRedirect(reverse("wallets-page"))

                try:
                    ordermin = _pair_ordermin(adapter, target_pair)
                    cur_amt = Decimal(str(getattr(sleeve, "trade_amount_base", 0) or 0))
                    if ordermin > 0 and (cur_amt <= 0 or cur_amt < ordermin):
                        sleeve.trade_amount_base = ordermin
                        sleeve.save(update_fields=["trade_amount_base"])
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001
                request.session["ml_train_error"] = f"Kraken validation failed for {target_pair}: {exc}"
                return HttpResponseRedirect(reverse("wallets-page"))

            modes = [
                SleeveStrategy.Mode.BUY_QUIET,
                SleeveStrategy.Mode.SELL_QUIET,
                SleeveStrategy.Mode.BUY_FLASH,
                SleeveStrategy.Mode.SELL_FLASH,
            ]
            for m in modes:
                if sleeve.type == Sleeve.Type.QUIET and "quiet" not in m:
                    continue
                if sleeve.type == Sleeve.Type.FLASH and "flash" not in m:
                    continue
                st, _ = SleeveStrategy.objects.get_or_create(sleeve=sleeve, mode=m)
                st.ml_active = True
                st.ml_pair = target_pair
                st.ml_status = "training"
                st.ml_stage = "training"
                st.ml_paused = False
                st.save(update_fields=["ml_active", "ml_pair", "ml_status", "ml_stage", "ml_paused"])
            request.session["ml_train_notice"] = {"msg": f"ML training started for {target_pair}.", "ts": time.time()}
            _clear_error()
            try:
                from trading.tasks import refresh_all_ml_vars_now

                refresh_all_ml_vars_now()
            except Exception:
                pass

            try:
                from trading.tasks import refresh_all_ml_prices_now

                refresh_all_ml_prices_now()
            except Exception:
                pass
            return HttpResponseRedirect(reverse("wallets-page"))

        if action == "stop_ml":
            SleeveStrategy.objects.filter(sleeve=sleeve, ml_active=True).update(
                ml_active=False,
                ml_status="idle",
                ml_stage="idle",
                ml_paused=False,
            )
            request.session["ml_train_notice"] = {
                "msg": f"ML stopped for {sleeve.wallet.currency} sleeve ({sleeve.type}).",
                "ts": time.time(),
            }
            _clear_error()
            return HttpResponseRedirect(reverse("wallets-page"))

        return HttpResponseRedirect(reverse("wallets-page"))


class WalletLiveStatusView(ApprovalRequiredMixin, TemplateView):
    """Lightweight JSON endpoint for live UI updates on /wallets/.

    DB-only: no Kraken calls.
    """

    def get(self, request, *args, **kwargs):
        user = request.user

        # Ensure ML price/trigger targets stay live while the page is open.
        # This is ticker-only and throttled per user to avoid spamming Kraken.
        try:
            uid = int(getattr(user, "id", 0) or 0)
        except Exception:
            uid = 0
        if uid:
            try:
                throttle_s = int(getattr(settings, "ML_PRICE_TICK_SECONDS", 5) or 5)
            except Exception:
                throttle_s = 5
            throttle_s = max(int(throttle_s or 1), 1)
            k = f"ml_price_tick_throttle:{uid}"
            now_ts = time.time()
            try:
                last = float(cache.get(k) or 0.0)
            except Exception:
                last = 0.0
            if (now_ts - last) >= float(throttle_s):
                try:
                    cache.set(k, now_ts, timeout=60)
                except Exception:
                    pass
                try:
                    refresh_ml_prices()
                except Exception:
                    pass

        def _msg_val(key: str) -> dict[str, object]:
            v = request.session.get(key)
            if isinstance(v, dict):
                return {"msg": str(v.get("msg") or ""), "ts": float(v.get("ts") or 0.0)}
            if v:
                return {"msg": str(v), "ts": 0.0}
            return {"msg": "", "ts": 0.0}

        sleeves = list(
            Sleeve.objects.select_related("wallet")
            .filter(wallet__user=user)
            .only("id", "position_base_qty", "wallet__currency")
        )
        sleeve_ids = [s.id for s in sleeves]

        # Strategy info per sleeve (DB-only fields that are already persisted by ML refresh/executor).
        strat_rows = list(
            SleeveStrategy.objects.filter(sleeve_id__in=sleeve_ids, is_active=True)
            .only(
                "id",
                "sleeve_id",
                "mode",
                "ml_active",
                "ml_pair",
                "ml_confidence",
                "ml_last_tick_at",
                "ml_last_trained_at",
                "ml_last_price",
                "ml_last_reason",
                "ml_last_entry_trigger",
                "ml_last_tp_trigger",
                "ml_last_sl_trigger",
            )
        )
        by_sleeve: dict[int, dict[str, SleeveStrategy]] = {}
        for st in strat_rows:
            m = str(getattr(st, "mode", "") or "")
            if not m:
                continue
            bucket = by_sleeve.setdefault(int(st.sleeve_id), {})
            if m.startswith("buy") and "buy" not in bucket:
                bucket["buy"] = st
            if m.startswith("sell") and "sell" not in bucket:
                bucket["sell"] = st

        # Recent orders per sleeve.
        orders_by_sleeve: dict[int, list[dict[str, object]]] = {int(sid): [] for sid in sleeve_ids}
        try:
            live_recent_minutes = int(getattr(settings, "RECENT_ORDERS_WINDOW_MINUTES", 60) or 60)
        except Exception:
            live_recent_minutes = 60
        if live_recent_minutes < 1:
            live_recent_minutes = 1
        live_cutoff_dt = timezone.now() - timedelta(minutes=live_recent_minutes)
        o_qs = (
            OrderLog.objects.filter(sleeve_id__in=sleeve_ids, created_at__gte=live_cutoff_dt)
            .only("sleeve_id", "created_at", "side", "order_type", "base_asset", "quote_asset", "price", "status", "error")
            .order_by("-created_at")
        )
        for o in o_qs[:500]:
            sid = int(o.sleeve_id)
            cur = orders_by_sleeve.get(sid)
            if cur is None:
                continue
            if len(cur) >= 5:
                continue
            cur.append(
                {
                    "ts": float(o.created_at.timestamp()) if getattr(o, "created_at", None) else 0.0,
                    "side": str(getattr(o, "side", "") or ""),
                    "type": str(getattr(o, "order_type", "") or ""),
                    "pair": f"{getattr(o, 'base_asset', '')}/{getattr(o, 'quote_asset', '')}",
                    "price": str(getattr(o, "price", "") or ""),
                    "status": str(getattr(o, "status", "") or ""),
                    "error": str(getattr(o, "error", "") or ""),
                }
            )

        pending_ids = set(
            OrderLog.objects.filter(sleeve_id__in=sleeve_ids, txid__gt="", status__in=["submitted", "open", "pending"])
            .values_list("sleeve_id", flat=True)
            .distinct()
        )

        # Per-sleeve cash estimate inferred from closed orders (DB-only).
        # cash_remaining = allocated_balance - sum(buy_cost+buy_fee) + sum(sell_cost-sell_fee)
        flow_map: dict[int, dict[str, Decimal]] = {int(sid): {"buy_cost": Decimal("0"), "buy_fee": Decimal("0"), "sell_cost": Decimal("0"), "sell_fee": Decimal("0")} for sid in sleeve_ids}
        try:
            flow_rows = (
                OrderLog.objects.filter(sleeve_id__in=sleeve_ids, status="closed")
                .values("sleeve_id")
                .annotate(
                    buy_cost=models.Sum(models.Case(models.When(side=OrderLog.Side.BUY, then="cost"), default=Decimal("0"), output_field=models.DecimalField())),
                    buy_fee=models.Sum(models.Case(models.When(side=OrderLog.Side.BUY, then="fee"), default=Decimal("0"), output_field=models.DecimalField())),
                    sell_cost=models.Sum(models.Case(models.When(side=OrderLog.Side.SELL, then="cost"), default=Decimal("0"), output_field=models.DecimalField())),
                    sell_fee=models.Sum(models.Case(models.When(side=OrderLog.Side.SELL, then="fee"), default=Decimal("0"), output_field=models.DecimalField())),
                )
            )
            for r in flow_rows:
                try:
                    sid = int(r.get("sleeve_id") or 0)
                except Exception:
                    sid = 0
                if not sid:
                    continue
                try:
                    flow_map[sid] = {
                        "buy_cost": Decimal(str(r.get("buy_cost") or 0)),
                        "buy_fee": Decimal(str(r.get("buy_fee") or 0)),
                        "sell_cost": Decimal(str(r.get("sell_cost") or 0)),
                        "sell_fee": Decimal(str(r.get("sell_fee") or 0)),
                    }
                except Exception:
                    flow_map[sid] = flow_map.get(sid) or flow_map[int(sleeve_ids[0])]
        except Exception:
            pass

        stage_by_sleeve: dict[int, str] = {s.id: "idle" for s in sleeves}
        # Keep both buy/sell strategy stages so we can pick the correct one for the sleeve's current cycle.
        stages_by_sleeve: dict[int, list[str]] = {int(s.id): [] for s in sleeves}
        stage_pair_by_sleeve: dict[int, dict[str, str]] = {int(s.id): {"buy": "idle", "sell": "idle"} for s in sleeves}
        for st in SleeveStrategy.objects.filter(sleeve_id__in=sleeve_ids, is_active=True).only("sleeve_id", "ml_stage", "ml_active", "mode"):
            if not getattr(st, "ml_active", False):
                continue
            sid = int(getattr(st, "sleeve_id", 0) or 0)
            if not sid:
                continue
            stg = (st.ml_stage or "").strip() or "idle"
            stages_by_sleeve.setdefault(sid, []).append(stg)
            m = str(getattr(st, "mode", "") or "")
            if m.startswith("sell"):
                stage_pair_by_sleeve.setdefault(sid, {"buy": "idle", "sell": "idle"})["sell"] = stg
            elif m.startswith("buy"):
                stage_pair_by_sleeve.setdefault(sid, {"buy": "idle", "sell": "idle"})["buy"] = stg

        pos_by_sleeve: dict[int, Decimal] = {}
        for s in sleeves:
            try:
                pos_by_sleeve[int(s.id)] = Decimal(str(getattr(s, "position_base_qty", 0) or 0))
            except Exception:
                pos_by_sleeve[int(s.id)] = Decimal("0")

        for sid in sleeve_ids:
            st_list = stages_by_sleeve.get(int(sid), [])
            pos = pos_by_sleeve.get(int(sid)) or Decimal("0")
            pos_eff = pos
            is_dust = False
            if pos > 0:
                try:
                    st_bucket = by_sleeve.get(int(sid), {})
                    sell_st = st_bucket.get("sell")
                    pair_for_min = (getattr(sell_st, "ml_pair", "") or "").strip() if sell_st else ""
                    if not pair_for_min:
                        buy_st = st_bucket.get("buy")
                        pair_for_min = (getattr(buy_st, "ml_pair", "") or "").strip() if buy_st else ""
                    if pair_for_min:
                        cached = cache.get(f"kraken_ordermin:{pair_for_min}")
                        o = Decimal(str(cached or 0))
                    else:
                        o = Decimal("0")
                except Exception:
                    o = Decimal("0")
                if o > 0 and pos < o:
                    pos_eff = Decimal("0")
                    is_dust = True
            stage = "idle"

            try:
                cycle_override = str(cache.get(f"sleeve_cycle_override:{int(sid)}") or "").strip().lower()
            except Exception:
                cycle_override = ""
            if cycle_override not in {"buy", "sell"}:
                cycle_override = ""
            active_is_sell = (cycle_override == "sell") if cycle_override else (pos_eff > 0)

            # Global override stages always win.
            if "pending_fill" in st_list:
                stage = "pending_fill"
            elif "placing_order" in st_list:
                stage = "placing_order"
            elif "cooldown" in st_list:
                stage = "cooldown"
            elif "training" in st_list:
                stage = "training"
            else:
                pair_stg = stage_pair_by_sleeve.get(int(sid), {"buy": "idle", "sell": "idle"})
                active_stage = pair_stg.get("sell") if active_is_sell else pair_stg.get("buy")
                active_stage = (active_stage or "").strip() or "idle"
                if active_stage == "error":
                    stage = "error"
                elif active_stage in {"waiting_buy", "waiting_sell"}:
                    stage = active_stage
                else:
                    # If strategy stage is missing/idle, infer based on position.
                    stage = "waiting_sell" if active_is_sell else "waiting_buy"

            stage_by_sleeve[int(sid)] = stage

        sleeve_payload: dict[str, object] = {}
        for s in sleeves:
            stage = stage_by_sleeve.get(s.id) or "idle"
            pending = (s.id in pending_ids) or (stage == "pending_fill")
            pos = Decimal(str(getattr(s, "position_base_qty", 0) or 0))
            pos_eff = pos
            is_dust = False
            if pos > 0:
                try:
                    st_bucket = by_sleeve.get(int(s.id), {})
                    sell_st = st_bucket.get("sell")
                    pair_for_min = (getattr(sell_st, "ml_pair", "") or "").strip() if sell_st else ""
                    if not pair_for_min:
                        buy_st = st_bucket.get("buy")
                        pair_for_min = (getattr(buy_st, "ml_pair", "") or "").strip() if buy_st else ""
                    if pair_for_min:
                        cached = cache.get(f"kraken_ordermin:{pair_for_min}")
                        o = Decimal(str(cached or 0))
                    else:
                        o = Decimal("0")
                except Exception:
                    o = Decimal("0")
                if o > 0 and pos < o:
                    pos_eff = Decimal("0")
                    is_dust = True

            try:
                cycle_override = str(cache.get(f"sleeve_cycle_override:{int(s.id)}") or "").strip().lower()
            except Exception:
                cycle_override = ""
            if cycle_override not in {"buy", "sell"}:
                cycle_override = ""
            active_is_sell = (cycle_override == "sell") if cycle_override else (pos_eff > 0)

            # Pick the "active" strategy for display based on isolated position.
            st_bucket = by_sleeve.get(int(s.id), {})
            st = st_bucket.get("sell") if active_is_sell else st_bucket.get("buy")
            live_pair = (getattr(st, "ml_pair", "") or "").strip() if st else ""
            live_price = str(getattr(st, "ml_last_price", "") or "") if st else ""
            live_reason = str(getattr(st, "ml_last_reason", "") or "") if st else ""
            live_entry = str(getattr(st, "ml_last_entry_trigger", "") or "") if st else ""
            live_tp = str(getattr(st, "ml_last_tp_trigger", "") or "") if st else ""
            live_sl = str(getattr(st, "ml_last_sl_trigger", "") or "") if st else ""
            live_mode = str(getattr(st, "mode", "") or "") if st else ""
            live_conf = ""
            try:
                live_conf = str(getattr(st, "ml_confidence", "") or "") if st else ""
            except Exception:
                live_conf = ""
            live_tick = float(getattr(getattr(st, "ml_last_tick_at", None), "timestamp", lambda: 0.0)()) if st and getattr(st, "ml_last_tick_at", None) else 0.0
            live_trained = float(getattr(getattr(st, "ml_last_trained_at", None), "timestamp", lambda: 0.0)()) if st and getattr(st, "ml_last_trained_at", None) else 0.0

            # Quote "value" helpers (user-facing): position_value = base_qty * price.
            pos_value = ""
            alloc_value = ""
            equity_value = ""
            pnl_value = ""
            try:
                px = Decimal(str(live_price or 0))
            except Exception:
                px = Decimal("0")

            try:
                alloc_quote = Decimal(str(getattr(s, "allocated_balance", 0) or 0))
            except Exception:
                alloc_quote = Decimal("0")
            try:
                alloc_value = str(alloc_quote.quantize(Decimal("0.00000001")))
            except Exception:
                alloc_value = str(alloc_quote)

            if px > 0:
                try:
                    pos_value = str((pos * px).quantize(Decimal("0.00000001")))
                except Exception:
                    pos_value = ""

            # Sleeve-only equity/pnl inferred from order flows + current position value
            try:
                flows = flow_map.get(int(s.id)) or {"buy_cost": Decimal("0"), "buy_fee": Decimal("0"), "sell_cost": Decimal("0"), "sell_fee": Decimal("0")}
                cash_rem = alloc_quote - (flows.get("buy_cost", Decimal("0")) + flows.get("buy_fee", Decimal("0"))) + (flows.get("sell_cost", Decimal("0")) - flows.get("sell_fee", Decimal("0")))
            except Exception:
                cash_rem = alloc_quote
            try:
                pos_quote = Decimal(str(pos_value or 0))
            except Exception:
                pos_quote = Decimal("0")
            equity = cash_rem + pos_quote
            pnl = equity - alloc_quote
            try:
                equity_value = str(equity.quantize(Decimal("0.00000001")))
            except Exception:
                equity_value = str(equity)
            try:
                pnl_q = pnl.quantize(Decimal("0.00000001"))
            except Exception:
                pnl_q = pnl
            try:
                sign = "+" if pnl_q > 0 else "-" if pnl_q < 0 else ""
                pnl_value = f"{sign}{str(abs(pnl_q))}"
            except Exception:
                pnl_value = ""

            sleeve_payload[str(s.id)] = {
                "stage": stage,
                "pending": bool(pending),
                "position": str(pos),
                "trade_state": (
                    cycle_override
                    if cycle_override
                    else ("dust" if is_dust else ("sell" if pos_eff > 0 else "buy"))
                ),
                "can_force_buy": (not pending),
                "can_force_sell": (not pending) and (not is_dust) and (active_is_sell or (pos_eff > 0)),
                "strategy_mode": live_mode,
                "confidence": live_conf,
                "pair": live_pair,
                "price": live_price,
                "reason": live_reason,
                "entry": live_entry,
                "tp": live_tp,
                "sl": live_sl,
                "last_tick": live_tick,
                "last_trained": live_trained,
                "orders": orders_by_sleeve.get(int(s.id), []),
                "position_value": pos_value,
                "allocated_value": alloc_value,
                "equity_value": equity_value,
                "pnl_value": pnl_value,
            }

        console_lines = _console_tail(getattr(user, "id", 0), limit=80)

        try:
            huey_exec_ts = float(cache.get("huey_heartbeat:executor") or 0.0)
        except Exception:
            huey_exec_ts = 0.0
        try:
            huey_prices_ts = float(cache.get("huey_heartbeat:ml_prices") or 0.0)
        except Exception:
            huey_prices_ts = 0.0

        ml_vars_last_run = 0.0
        ml_vars_throttle_s = 0
        ml_vars_next_in = 0.0
        try:
            adapt = cache.get("ml_vars_refresh_adaptive") or {}
        except Exception:
            adapt = {}
        try:
            ml_vars_last_run = float((adapt or {}).get("last_run") or 0.0)
        except Exception:
            ml_vars_last_run = 0.0
        try:
            ml_vars_throttle_s = int((adapt or {}).get("throttle_s") or 0)
        except Exception:
            ml_vars_throttle_s = 0
        try:
            if ml_vars_last_run and ml_vars_throttle_s:
                ml_vars_next_in = max(0.0, (ml_vars_last_run + float(ml_vars_throttle_s)) - time.time())
        except Exception:
            ml_vars_next_in = 0.0

        return JsonResponse(
            {
                "ts": time.time(),
                "ml_train_error": _msg_val("ml_train_error"),
                "ml_train_notice": _msg_val("ml_train_notice"),
                "sleeves": sleeve_payload,
                "autotrade_console": console_lines,
                "ml_vars_refresh": {
                    "last_run": ml_vars_last_run,
                    "throttle_s": ml_vars_throttle_s,
                    "next_in": ml_vars_next_in,
                },
                "huey": {
                    "executor": huey_exec_ts,
                    "ml_prices": huey_prices_ts,
                },
            }
        )


class AutoTradeConsoleClearView(ApprovalRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        user = request.user
        try:
            uid = int(getattr(user, "id", 0) or 0)
        except Exception:
            uid = 0
        if uid:
            try:
                _console_clear(uid)
            except Exception:
                pass
        try:
            cache.delete("wallets-page:live_balances_available")
        except Exception:
            pass
        # Expire notices/errors quickly (15s)
        try:
            if "ml_train_notice" in request.session:
                request.session.set_expiry(15)
            if "ml_train_error" in request.session:
                request.session.set_expiry(15)
        except Exception:
            pass
        return JsonResponse({"ok": True})


class TradingHelpPageView(ApprovalRequiredMixin, TemplateView):
    template_name = "help/trading_help.html"
