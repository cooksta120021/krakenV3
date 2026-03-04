from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView
from decimal import Decimal
from datetime import timedelta
import json
from django.views.generic.edit import FormView
from django.contrib.auth.forms import UserCreationForm
from django.urls import reverse_lazy
from django.shortcuts import redirect
from django.http import JsonResponse
from django.utils import timezone
from rest_framework import mixins, viewsets
from django.views import View

from .permissions import IsAdmin, IsSelfOrAdmin
from .serializers import UserApprovalSerializer, UserSerializer
from wallets.models import Wallet, Sleeve
from api_keys.models import ApiKey
from trading.services.market_scan import top_profit_candidates, profit_candidates_last_updated
from trading.models import SleeveStrategy, OrderLog
from trading.services.kraken_adapter import KrakenAdapter, GLOBAL_RATE_LIMITER

User = get_user_model()


class MeViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = UserSerializer
    permission_classes = [IsSelfOrAdmin]

    def get_object(self):
        return self.request.user


class UserApprovalViewSet(mixins.UpdateModelMixin, viewsets.GenericViewSet):
    queryset = User.objects.all()
    serializer_class = UserApprovalSerializer
    permission_classes = [IsAdmin]


class ApprovalRequiredMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        user = request.user
        if user.is_authenticated and (user.role == "admin" or user.is_approved):
            return super().dispatch(request, *args, **kwargs)
        if user.is_authenticated:
            return redirect("waiting-approval")
        return super().dispatch(request, *args, **kwargs)


class DashboardView(ApprovalRequiredMixin, TemplateView):
    template_name = "dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user

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
        ctx["ml_active_wallet"] = None
        active_ml = (
            SleeveStrategy.objects.select_related("sleeve", "sleeve__wallet")
            .filter(sleeve__wallet__user=user, ml_active=True)
            .order_by("sleeve__wallet_id")
            .first()
        )
        if active_ml:
            ctx["ml_active_wallet"] = active_ml.sleeve.wallet_id
        wallets = Wallet.objects.filter(user=user).distinct().prefetch_related("sleeves")
        active_wallets = list(wallets)
        ctx["active_wallets"] = active_wallets

        selected_wallet_id = None
        try:
            raw = (self.request.GET.get("wallet") or "").strip()
            selected_wallet_id = int(raw) if raw else None
        except Exception:
            selected_wallet_id = None
        if not selected_wallet_id:
            try:
                selected_wallet_id = int(ctx.get("ml_active_wallet") or 0) or None
            except Exception:
                selected_wallet_id = None
        if not selected_wallet_id and active_wallets:
            try:
                selected_wallet_id = int(active_wallets[0].id)
            except Exception:
                selected_wallet_id = None
        ctx["selected_wallet_id"] = selected_wallet_id

        selected_wallet_currency = None
        if selected_wallet_id:
            try:
                for w in active_wallets:
                    if int(getattr(w, "id", 0) or 0) == int(selected_wallet_id):
                        selected_wallet_currency = str(getattr(w, "currency", "") or "")
                        break
            except Exception:
                selected_wallet_currency = None
        ctx["selected_wallet_currency"] = selected_wallet_currency
        try:
            ctx["selected_wallet_sleeves"] = list(
                Sleeve.objects.filter(wallet_id=selected_wallet_id)
                .only("id", "type", "base_asset")
                .order_by("id")
            ) if selected_wallet_id else []
        except Exception:
            ctx["selected_wallet_sleeves"] = []
        keys = list(ApiKey.objects.filter(user=user, is_active=True).order_by("created_at"))
        cand = top_profit_candidates(limit=20, quote="USD", scan_pairs_limit=40)
        try:
            from trading.services.executor import _get_restricted_pairs_map

            restricted = _get_restricted_pairs_map(int(user.id))
        except Exception:
            restricted = {}
        out_cand = []
        for c in (cand or []):
            try:
                p = str((c or {}).get("pair") or "").strip().upper()
            except Exception:
                p = ""
            if p and isinstance(restricted, dict) and p in restricted:
                continue
            out_cand.append(c)
            if len(out_cand) >= 5:
                break
        ctx["top_profit_candidates"] = out_cand
        ctx["profit_candidates_last_updated"] = profit_candidates_last_updated()
        ctx["has_active_key"] = bool(keys)

        # Dashboard suggestion: top 3 pairs per wallet currency based on largest non-quote balances.
        ctx["top3_pairs_by_wallet_currency"] = {}
        if keys and active_wallets:
            key = keys[0]
            try:
                adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER, user_id=user.id)
                bal = adapter.fetch_balances()
            except Exception:
                bal = {}
            try:
                balance_map = {}
                for asset, val in (bal or {}).items():
                    val_dec = Decimal(str(val))
                    balance_map[asset] = val_dec
                    balance_map[asset.upper()] = val_dec
                    if asset.startswith("Z") or asset.startswith("X"):
                        balance_map[asset[1:]] = val_dec
                        balance_map[asset[1:].upper()] = val_dec
                    if asset.startswith("XX") or asset.startswith("ZZ"):
                        balance_map[asset[1:]] = val_dec
                        balance_map[asset[1:].upper()] = val_dec
            except Exception:
                balance_map = {}

            for w in active_wallets:
                quote_norm = _norm_asset(w.currency)
                others = [
                    (asset, amt)
                    for asset, amt in balance_map.items()
                    if _norm_asset(asset) != quote_norm
                ]
                top3 = sorted(others, key=lambda kv: kv[1], reverse=True)[:3]
                if top3:
                    ctx["top3_pairs_by_wallet_currency"][w.currency] = [f"{_norm_asset(a)}/{quote_norm}" for a, _ in top3]

        return ctx


class DashboardWalletPnlView(ApprovalRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        user = request.user
        raw = (request.GET.get("wallet_id") or "").strip()
        try:
            wallet_id = int(raw)
        except Exception:
            return JsonResponse({"ok": False, "error": "invalid_wallet_id"}, status=400)

        raw_sleeve = (request.GET.get("sleeve_id") or "").strip()
        sleeve_id = None
        if raw_sleeve:
            try:
                sleeve_id = int(raw_sleeve)
            except Exception:
                return JsonResponse({"ok": False, "error": "invalid_sleeve_id"}, status=400)

        raw_days = (request.GET.get("days") or "").strip()
        days = None
        if raw_days:
            try:
                days = int(raw_days)
            except Exception:
                days = None
        if days is not None and days <= 0:
            days = None

        try:
            wallet = Wallet.objects.get(id=wallet_id, user=user)
        except Wallet.DoesNotExist:
            return JsonResponse({"ok": False, "error": "wallet_not_found"}, status=404)

        sleeves_qs = Sleeve.objects.filter(wallet=wallet)
        if sleeve_id:
            sleeves_qs = sleeves_qs.filter(id=sleeve_id)
        sleeves = list(sleeves_qs.only("id"))
        sleeve_ids = [int(s.id) for s in sleeves]
        if not sleeve_ids:
            return JsonResponse({"ok": True, "wallet_id": wallet_id, "points": [], "summary": {"realized_pnl": "0", "net_quote": "0"}})

        orders_qs = OrderLog.objects.filter(sleeve_id__in=sleeve_ids, status="closed")
        if days is not None:
            try:
                orders_qs = orders_qs.filter(created_at__gte=(timezone.now() - timedelta(days=int(days))))
            except Exception:
                pass
        orders = list(orders_qs.only("sleeve_id", "created_at", "side", "amount", "price", "vol_exec", "cost", "fee").order_by("created_at"))

        pos_by_sleeve: dict[int, Decimal] = {int(sid): Decimal("0") for sid in sleeve_ids}
        cost_by_sleeve: dict[int, Decimal] = {int(sid): Decimal("0") for sid in sleeve_ids}

        realized = Decimal("0")
        net_quote = Decimal("0")
        points: list[dict[str, str]] = []

        for o in orders:
            try:
                sid = int(getattr(o, "sleeve_id", 0) or 0)
            except Exception:
                continue
            if sid not in pos_by_sleeve:
                continue

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

            side = str(getattr(o, "side", "") or "").strip().lower()

            pos = pos_by_sleeve.get(sid) or Decimal("0")
            cost = cost_by_sleeve.get(sid) or Decimal("0")
            avg = (cost / pos) if pos > 0 else Decimal("0")

            if side == "buy":
                pos = (pos + qty)
                # Include fees in basis.
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
            else:
                continue

            pos_by_sleeve[sid] = pos
            cost_by_sleeve[sid] = cost

            try:
                ts = int(getattr(o, "created_at", None).timestamp()) if getattr(o, "created_at", None) else 0
            except Exception:
                ts = 0

            points.append(
                {
                    "ts": str(ts),
                    "realized_pnl": str(realized.quantize(Decimal("0.00000001"))),
                    "net_quote": str(net_quote.quantize(Decimal("0.00000001"))),
                }
            )

        summary = {
            "realized_pnl": str(realized.quantize(Decimal("0.00000001"))),
            "net_quote": str(net_quote.quantize(Decimal("0.00000001"))),
            "trades": str(len(points)),
        }
        return JsonResponse({"ok": True, "wallet_id": wallet_id, "sleeve_id": sleeve_id, "days": days, "currency": str(getattr(wallet, "currency", "") or ""), "points": points[-500:], "summary": summary})


class SignupView(FormView):
    template_name = "registration/signup.html"
    form_class = UserCreationForm
    success_url = reverse_lazy("login")

    def form_valid(self, form):
        form.save()
        return super().form_valid(form)


class WaitingApprovalView(LoginRequiredMixin, TemplateView):
    template_name = "waiting_approval.html"
