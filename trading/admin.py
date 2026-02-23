from django.contrib import admin

from .models import OrderLog, SleeveStrategy


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
