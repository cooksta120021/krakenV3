from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user with optional MFA flag."""

    mfa_enabled = models.BooleanField(default=False)

    # Preferred quote assets (e.g., USD, USDC, USDT). Keep simple list for now.
    preferred_quotes = models.JSONField(default=list, blank=True)

    # RBAC role: admin, mod, member
    role = models.CharField(
        max_length=16,
        choices=(
            ("admin", "Admin"),
            ("mod", "Mod"),
            ("member", "Member"),
        ),
        default="member",
    )

    # Approval flow: unapproved users restricted until approved by admin/mod
    is_approved = models.BooleanField(default=False)

    # Optional list of permissions/grants per user for future fine-grain RBAC
    extra_permissions = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:  # pragma: no cover - display convenience
        return self.username

# Create your models here.
