from django.contrib import admin

from .models import OrderLog, SleeveStrategy, SleevePerformanceRecord


@admin.register(SleeveStrategy)
class SleeveStrategyAdmin(admin.ModelAdmin):
    list_display = ("sleeve", "mode", "is_active", "created_at")
    list_filter = ("mode", "is_active")
    search_fields = ("sleeve__wallet__user__username", "sleeve__wallet__currency")


@admin.register(OrderLog)
class OrderLogAdmin(admin.ModelAdmin):
    list_display = ("sleeve", "api_key", "side", "base_asset", "quote_asset", "price", "status", "created_at")
    list_filter = ("side", "status")
    search_fields = ("sleeve__wallet__user__username", "base_asset", "quote_asset")


@admin.register(SleevePerformanceRecord)
class SleevePerformanceRecordAdmin(admin.ModelAdmin):
    list_display = (
        "sleeve_id",
        "wallet_currency",
        "base_asset",
        "sleeve_type",
        "started_at",
        "ended_at",
        "realized_pnl_quote",
        "end_equity_est_quote",
        "trades",
    )
    list_filter = ("wallet_currency", "sleeve_type")
    search_fields = ("user__username", "base_asset", "wallet_currency")
