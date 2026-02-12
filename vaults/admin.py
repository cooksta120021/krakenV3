from django.contrib import admin

from .models import (
    ApiKey,
    AllocationHistory,
    ApprovalRequest,
    AuditLog,
    BotRun,
    CoreVault,
    KeyRotationEvent,
    PauseEvent,
    Sleeve,
    Wallet,
)


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ("owner", "name", "asset", "created_at")
    list_filter = ("asset", "created_at")
    search_fields = ("owner__username", "name")


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("owner", "label", "exchange", "is_active", "health_status", "last_checked")
    list_filter = ("exchange", "is_active", "health_status")
    search_fields = ("owner__username", "label")


@admin.register(CoreVault)
class CoreVaultAdmin(admin.ModelAdmin):
    list_display = (
        "owner",
        "wallet",
        "asset",
        "quote_asset",
        "total_coins",
        "tradeable_coins",
        "quiet_allocation",
        "flash_allocation",
        "paused",
    )
    list_filter = ("asset", "quote_asset", "paused")
    search_fields = ("owner__username", "wallet__name")


@admin.register(Sleeve)
class SleeveAdmin(admin.ModelAdmin):
    list_display = (
        "vault",
        "sleeve_type",
        "allocated_amount",
        "profit_retained",
        "profit_returned",
        "mode",
        "is_active",
    )
    list_filter = ("sleeve_type", "is_active", "mode")
    search_fields = ("vault__owner__username",)


@admin.register(AllocationHistory)
class AllocationHistoryAdmin(admin.ModelAdmin):
    list_display = ("vault", "sleeve", "allocated_amount", "tradeable_remaining", "created_at")
    list_filter = ("created_at",)


@admin.register(BotRun)
class BotRunAdmin(admin.ModelAdmin):
    list_display = ("sleeve", "status", "started_by", "started_at", "stopped_at")
    list_filter = ("status", "started_at")


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(admin.ModelAdmin):
    list_display = ("user", "reviewer", "status", "created_at")
    list_filter = ("status", "created_at")


@admin.register(PauseEvent)
class PauseEventAdmin(admin.ModelAdmin):
    list_display = ("vault", "sleeve", "action", "actor", "created_at")
    list_filter = ("action", "created_at")


@admin.register(KeyRotationEvent)
class KeyRotationEventAdmin(admin.ModelAdmin):
    list_display = ("api_key", "status", "created_at")
    list_filter = ("status", "created_at")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("actor", "action", "target_type", "target_id", "created_at")
    list_filter = ("action", "created_at")

# Register your models here.
