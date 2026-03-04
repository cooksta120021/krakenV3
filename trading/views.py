from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView
from rest_framework import mixins, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView
from django.core.cache import cache
import time

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
        data = None
        try:
            cached = cache.get("kraken_rate:last_snapshot")
        except Exception:
            cached = None
        if isinstance(cached, dict) and cached:
            data = dict(cached)
        else:
            snap_fn = getattr(GLOBAL_RATE_LIMITER, "snapshot", None)
            if callable(snap_fn):
                try:
                    data = snap_fn()
                except Exception:
                    data = {"mode": "unknown"}
            else:
                data = {"mode": "unknown"}

        # Normalize expected keys so UI meters/badges stay stable.
        if not isinstance(data, dict):
            data = {"mode": "unknown"}
        data.setdefault("tokens", 0.0)
        data.setdefault("capacity", 1.0)
        data.setdefault("rate_per_sec", 0.0)
        data.setdefault("mode", "unknown")

        snap_age_s = 0.0
        try:
            if data.get("ts"):
                snap_age_s = max(0.0, float(time.time()) - float(data.get("ts") or 0.0))
        except Exception:
            snap_age_s = 0.0

        try:
            now_s = int(time.time())
        except Exception:
            now_s = 0

        calls_60 = 0.0
        credits_60 = 0.0
        calls_5 = 0.0
        credits_5 = 0.0
        if now_s:
            for ts in range(max(0, now_s - 59), now_s + 1):
                try:
                    calls_60 += float(cache.get(f"kraken_live:{ts}:calls") or 0)
                except Exception:
                    pass
                try:
                    credits_60 += float(cache.get(f"kraken_live:{ts}:credits") or 0)
                except Exception:
                    pass

            for ts in range(max(0, now_s - 4), now_s + 1):
                try:
                    calls_5 += float(cache.get(f"kraken_live:{ts}:calls") or 0)
                except Exception:
                    pass
                try:
                    credits_5 += float(cache.get(f"kraken_live:{ts}:credits") or 0)
                except Exception:
                    pass

        try:
            last = cache.get("kraken_live:last")
        except Exception:
            last = None

        data = dict(data)
        data["live_calls_60s"] = float(calls_60)
        data["live_credits_60s"] = float(credits_60)
        data["live_calls_5s"] = float(calls_5)
        data["live_credits_5s"] = float(credits_5)
        data["snapshot_age_s"] = float(snap_age_s)
        data["live_last"] = last if isinstance(last, dict) else {}
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
