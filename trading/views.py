from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView
from rest_framework import mixins, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsApproved, IsOwnerOrAdmin
from accounts.views import ApprovalRequiredMixin
from .models import OrderLog, SleeveStrategy
from .serializers import OrderLogSerializer, SleeveStrategySerializer
from .services.kraken_adapter import GLOBAL_RATE_LIMITER, KrakenAdapter


class SleeveStrategyViewSet(viewsets.ModelViewSet):
    serializer_class = SleeveStrategySerializer
    permission_classes = [IsApproved, IsOwnerOrAdmin]

    def get_queryset(self):
        qs = SleeveStrategy.objects.select_related("sleeve", "sleeve__wallet", "sleeve__wallet__user")
        if self.request.user.role == "admin":
            return qs
        return qs.filter(sleeve__wallet__user=self.request.user)


class RateStatusView(APIView):
    permission_classes = [IsApproved]

    def get(self, request):
        snap_fn = getattr(GLOBAL_RATE_LIMITER, "snapshot", None)
        data = snap_fn() if callable(snap_fn) else {"mode": "unknown"}
        return Response(data)


class KrakenAssetsView(APIView):
    permission_classes = [IsApproved]

    def get(self, request):
        adapter = KrakenAdapter("", "", rate_limiter=GLOBAL_RATE_LIMITER)
        payload = adapter.public_request("Assets", weight=1.0)
        result = payload.get("result") or {}
        assets: list[str] = []
        for _code, meta in result.items():
            if not isinstance(meta, dict):
                continue
            alt = (meta.get("altname") or "").strip()
            if alt:
                assets.append(alt.upper())
        # Common alias: Kraken uses XBT but users often type BTC.
        if "XBT" in assets and "BTC" not in assets:
            assets.append("BTC")
        assets = sorted(set(assets))
        return Response({"assets": assets})


class RateMeterPageView(ApprovalRequiredMixin, TemplateView):
    template_name = "rate_meter.html"


class OrderLogViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = OrderLogSerializer
    permission_classes = [IsApproved, IsOwnerOrAdmin]

    def get_queryset(self):
        qs = OrderLog.objects.select_related("sleeve", "sleeve__wallet", "sleeve__wallet__user", "api_key")
        if self.request.user.role == "admin":
            return qs
        return qs.filter(sleeve__wallet__user=self.request.user)
