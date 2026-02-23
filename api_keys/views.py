from django.urls import reverse_lazy
from django.views.generic import ListView
from django.views.generic.edit import FormMixin
from rest_framework import viewsets

from accounts.permissions import IsApproved, IsOwnerOrAdmin
from accounts.views import ApprovalRequiredMixin
from trading.services.kraken_adapter import KrakenAdapter, GLOBAL_RATE_LIMITER
from .forms import ApiKeyForm
from .models import ApiKey
from .serializers import ApiKeySerializer


class ApiKeyViewSet(viewsets.ModelViewSet):
    serializer_class = ApiKeySerializer
    permission_classes = [IsApproved, IsOwnerOrAdmin]

    def get_queryset(self):
        if self.request.user.role == "admin":
            return ApiKey.objects.all()
        return ApiKey.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class ApiKeyListCreateView(ApprovalRequiredMixin, FormMixin, ListView):
    template_name = "api_keys/list.html"
    form_class = ApiKeyForm
    context_object_name = "keys"
    success_url = reverse_lazy("api-keys-page")

    def get_queryset(self):
        if self.request.user.role == "admin":
            return ApiKey.objects.select_related("user").all()
        return ApiKey.objects.filter(user=self.request.user)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        health = {}
        for key in ctx.get("keys", []):
            if not key.is_active:
                health[key.id] = {"status": "inactive", "detail": "Not active"}
                continue
            try:
                adapter = KrakenAdapter(key.public_key, key.private_key, rate_limiter=GLOBAL_RATE_LIMITER, user_id=key.user_id)
                adapter.fetch_balances()
                health[key.id] = {"status": "ok", "detail": "Healthy"}
            except Exception as exc:  # noqa: BLE001
                health[key.id] = {"status": "error", "detail": str(exc)}
        ctx["key_health"] = health
        return ctx

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            api_key = form.save(commit=False)
            api_key.user = request.user
            api_key.save()
            return self.form_valid(form)
        # handle delete/update actions
        action = request.POST.get("action")
        if action == "delete_key":
            key_id = request.POST.get("key_id")
            ApiKey.objects.filter(id=key_id, user=request.user).delete()
            return self.form_valid(form)
        if action == "update_key":
            key_id = request.POST.get("key_id")
            try:
                key = ApiKey.objects.get(id=key_id)
                if request.user.role != "admin" and key.user != request.user:
                    return self.form_invalid(form)
                key.name = request.POST.get("name", key.name)
                key.is_active = request.POST.get("is_active") == "on"
                key.save()
                return self.form_valid(form)
            except ApiKey.DoesNotExist:
                return self.form_invalid(form)
        return self.form_invalid(form)
