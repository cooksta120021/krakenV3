from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "Access",
            {
                "fields": (
                    "role",
                    "is_approved",
                )
            },
        ),
    )

    list_display = (
        "username",
        "email",
        "role",
        "is_approved",
        "is_staff",
        "is_active",
    )
    list_filter = ("role", "is_approved", "is_staff", "is_superuser", "is_active")
    search_fields = ("username", "email")

# Register your models here.
