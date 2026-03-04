from django.contrib import admin

from .models import Sleeve, Wallet


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ("user", "currency", "real_balance", "tradeable_balance", "reserve_wallet")
    list_filter = ("currency",)
    search_fields = ("user__username", "user__email")


@admin.register(Sleeve)
class SleeveAdmin(admin.ModelAdmin):
    list_display = ("wallet", "type", "allocated_balance")
    list_filter = ("type", "wallet__currency")
    search_fields = ("wallet__user__username", "wallet__user__email")
