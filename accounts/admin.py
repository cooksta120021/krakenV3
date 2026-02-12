from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    fieldsets = BaseUserAdmin.fieldsets + (
        (
            "Platform",
            {
                "fields": (
                    "mfa_enabled",
                    "preferred_quotes",
                    "role",
                    "is_approved",
                    "extra_permissions",
                )
            },
        ),
    )
    list_display = BaseUserAdmin.list_display + ("role", "is_approved", "mfa_enabled")
    list_filter = BaseUserAdmin.list_filter + ("role", "is_approved", "mfa_enabled")
    search_fields = BaseUserAdmin.search_fields + ("role",)

# Register your models here.
