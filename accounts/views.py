from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView
from decimal import Decimal
import json
from django.views.generic.edit import FormView
from django.contrib.auth.forms import UserCreationForm
from django.urls import reverse_lazy
from django.shortcuts import redirect
from rest_framework import mixins, viewsets

from .permissions import IsAdmin, IsSelfOrAdmin
from .serializers import UserApprovalSerializer, UserSerializer
from wallets.models import Wallet, Sleeve
from api_keys.models import ApiKey
from trading.services.market_scan import get_cached_profit_candidates, profit_candidates_last_updated
from trading.models import SleeveStrategy
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
        wallets = Wallet.objects.filter(user=user, sleeves__isnull=False).distinct().prefetch_related("sleeves")
        active_wallets = list(wallets)
        ctx["active_wallets"] = active_wallets
        keys = list(ApiKey.objects.filter(user=user, is_active=True).order_by("created_at"))
        ctx["top_profit_candidates"] = get_cached_profit_candidates(limit=5, quote="USD", max_age_seconds=900)
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


class SignupView(FormView):
    template_name = "registration/signup.html"
    form_class = UserCreationForm
    success_url = reverse_lazy("login")

    def form_valid(self, form):
        form.save()
        return super().form_valid(form)


class WaitingApprovalView(LoginRequiredMixin, TemplateView):
    template_name = "waiting_approval.html"
