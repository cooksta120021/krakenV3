from django.conf import settings
from django.db import models
from django.utils import timezone
from urllib.request import urlopen
import json
from decimal import Decimal

from .utils import decrypt_value, encrypt_value, kraken_private_request

User = settings.AUTH_USER_MODEL


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Wallet(TimeStampedModel):
    """Per-user wallet selection (supports multiple)."""

    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="wallets")
    name = models.CharField(max_length=64)
    asset = models.CharField(max_length=16)  # e.g., USD, USDC, USDT

    class Meta:
        unique_together = ("owner", "name")

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.owner}::{self.name} ({self.asset})"


class ApiKey(TimeStampedModel):
    """Encrypted API keys per user."""

    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_keys")
    label = models.CharField(max_length=64)
    exchange = models.CharField(max_length=32, default="kraken")
    key_encrypted = models.TextField()
    secret_encrypted = models.TextField()
    is_active = models.BooleanField(default=True)
    last_checked = models.DateTimeField(null=True, blank=True)
    rotation_index = models.PositiveIntegerField(default=0)
    health_status = models.CharField(max_length=32, default="unknown")
    last_error = models.CharField(max_length=255, blank=True)

    def set_secret(self, key_plain: str, secret_plain: str):
        """Encrypt API credentials before save."""

        self.key_encrypted = encrypt_value(key_plain)
        self.secret_encrypted = encrypt_value(secret_plain)

    def get_secret(self):
        """Decrypt API credentials when needed."""

        return decrypt_value(self.key_encrypted), decrypt_value(self.secret_encrypted)

    def validate_keys(self) -> bool:
        """Validate via Kraken public system status (no auth); replace with private ping later."""

        status = "unknown"
        error = ""
        if not self.is_active:
            status = "inactive"
        else:
            try:
                with urlopen("https://api.kraken.com/0/public/SystemStatus", timeout=5) as resp:
                    data = json.loads(resp.read().decode())
                    if data.get("error") == []:
                        result = data.get("result", {})
                        status_val = result.get("status", "online")
                        status = "healthy" if status_val == "online" else status_val
                    else:
                        status = "degraded"
                        error = "Kraken status error"
            except Exception as exc:  # pragma: no cover - network
                status = "error"
                error = str(exc)

        self.health_status = status
        self.last_error = error
        self.last_checked = timezone.now()
        self.save(update_fields=["health_status", "last_error", "last_checked"])
        return self.health_status == "healthy"

    def validate_private(self) -> bool:
        """Stub for private validation using decrypted keys; replace with real exchange call."""

        key, secret = self.get_secret()
        if not key or not secret:
            self.mark_health("error", "missing credentials")
            return False
        try:
            kraken_private_request(key, secret, path="/0/private/Balance")
            self.mark_health("healthy")
            return True
        except Exception as exc:
            self.mark_health("error", str(exc))
            return False

    def rotate_key(self, new_key: str, new_secret: str):
        """Stub rotation: set new secrets and log event."""

        self.set_secret(new_key, new_secret)
        self.rotation_success()

    def mark_health(self, status: str, error: str | None = None):
        self.health_status = status
        self.last_error = error or ""
        self.last_checked = timezone.now()
        self.save(update_fields=["health_status", "last_error", "last_checked"])

    def rotation_success(self):
        KeyRotationEvent.objects.create(api_key=self, status="rotated")

    @classmethod
    def next_for_owner(cls, owner):
        """Round-robin active keys for owner; falls back to single active key."""

        active = list(
            cls.objects.filter(owner=owner, is_active=True).order_by("rotation_index", "created_at")
        )
        if not active:
            return None
        if len(active) == 1:
            return active[0]

        # pick first, then bump its rotation_index to end
        key = active[0]
        max_index = max(k.rotation_index for k in active)
        key.rotation_index = max_index + 1
        key.save(update_fields=["rotation_index"])
        return key

    class Meta:
        unique_together = ("owner", "label")

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.owner}::{self.label}"


class CoreVault(TimeStampedModel):
    """Vault per asset wallet with tradeable vs total tracking."""

    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="core_vaults")
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="core_vaults")
    asset = models.CharField(max_length=16)  # asset held (e.g., ETH)
    quote_asset = models.CharField(max_length=16)  # quote constraint for pairs
    total_coins = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    tradeable_coins = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    quiet_allocation = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    flash_allocation = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    paused = models.BooleanField(default=False)
    reserve_buffer_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)  # optional reserve below total
    allow_zero_tradeable = models.BooleanField(default=True)
    fee_rate = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("0.004"))
    slippage_pct = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("0.001"))
    allowed_pairs = models.JSONField(default=list, blank=True)

    # Risk knobs / placeholders per checklist
    max_drawdown_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    max_daily_loss_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    max_position_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    trading_window = models.JSONField(default=dict, blank=True)  # session constraints

    def remaining_tradeable(self):
        return self.tradeable_coins - (self.quiet_allocation + self.flash_allocation)

    def can_allocate(self, amount):
        return amount <= self.remaining_tradeable()

    def apply_funding(self, new_total: Decimal):
        """Auto-raise total/tradeable when deposits land, respecting reserve buffer."""

        if new_total is None:
            return
        delta = new_total - self.total_coins
        if delta <= 0:
            return
        self.total_coins = new_total
        buffer = (new_total * (self.reserve_buffer_pct or Decimal("0"))) / Decimal("100")
        target_tradeable = new_total - buffer
        add_tradeable = max(Decimal("0"), target_tradeable - self.tradeable_coins)
        if add_tradeable > 0:
            self.tradeable_coins += add_tradeable
        self.save(update_fields=["total_coins", "tradeable_coins"])

    def __str__(self) -> str:  # pragma: no cover
        return f"Vault {self.asset}/{self.quote_asset} for {self.owner}"


class Sleeve(TimeStampedModel):
    SLEEVE_CHOICES = (
        ("quiet", "Quiet"),
        ("flash", "Flash"),
    )

    vault = models.ForeignKey(CoreVault, on_delete=models.CASCADE, related_name="sleeves")
    sleeve_type = models.CharField(max_length=16, choices=SLEEVE_CHOICES)
    allocated_amount = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    profit_retained = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    profit_returned = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    flash_principal = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    mode = models.CharField(max_length=16, default="manual")  # manual vs ml
    is_active = models.BooleanField(default=True)
    expected_return = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    confidence = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    regime = models.CharField(max_length=32, blank=True)  # volatility/trend regime hint

    class Meta:
        unique_together = ("vault", "sleeve_type")

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.sleeve_type} sleeve for {self.vault}"


class AllocationHistory(TimeStampedModel):
    vault = models.ForeignKey(CoreVault, on_delete=models.CASCADE, related_name="allocation_history")
    sleeve = models.ForeignKey(Sleeve, on_delete=models.CASCADE, related_name="allocation_history")
    allocated_amount = models.DecimalField(max_digits=20, decimal_places=8)
    tradeable_remaining = models.DecimalField(max_digits=20, decimal_places=8)
    note = models.CharField(max_length=255, blank=True)

    def log_action(self, actor: User | None, action: str, metadata: dict | None = None):
        AuditLog.objects.create(
            actor=actor,
            action=f"allocation:{action}",
            target_type="sleeve",
            target_id=str(self.sleeve_id),
            metadata=metadata or {},
        )


class VaultPnl(TimeStampedModel):
    vault = models.ForeignKey(CoreVault, on_delete=models.CASCADE, related_name="pnl_snapshots")
    realized = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    unrealized = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    fees = models.DecimalField(max_digits=20, decimal_places=8, default=0)

    @classmethod
    def latest_for_vaults(cls, vault_ids):
        latest = {}
        qs = (
            cls.objects.filter(vault_id__in=vault_ids)
            .order_by("vault_id", "-created_at")
            .values("vault_id", "realized", "unrealized", "fees", "created_at")
        )
        for row in qs:
            if row["vault_id"] not in latest:
                latest[row["vault_id"]] = row
        return latest


class SleevePnl(TimeStampedModel):
    sleeve = models.ForeignKey(Sleeve, on_delete=models.CASCADE, related_name="pnl_snapshots")
    realized = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    unrealized = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    fees = models.DecimalField(max_digits=20, decimal_places=8, default=0)

    @classmethod
    def latest_for_sleeves(cls, sleeve_ids):
        latest = {}
        qs = (
            cls.objects.filter(sleeve_id__in=sleeve_ids)
            .order_by("sleeve_id", "-created_at")
            .values("sleeve_id", "realized", "unrealized", "fees", "created_at")
        )
        for row in qs:
            if row["sleeve_id"] not in latest:
                latest[row["sleeve_id"]] = row
        return latest


class BotRun(TimeStampedModel):
    """Track bot start/stop and pauses per sleeve."""

    sleeve = models.ForeignKey(Sleeve, on_delete=models.CASCADE, related_name="bot_runs")
    started_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="bot_runs_started")
    stopped_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="bot_runs_stopped")
    started_at = models.DateTimeField(default=timezone.now)
    stopped_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, default="running")
    note = models.CharField(max_length=255, blank=True)


class PriceSnapshot(TimeStampedModel):
    """Store OHLCV snapshots per asset/pair for observability/backfill."""

    asset = models.CharField(max_length=16)
    quote = models.CharField(max_length=16)
    open = models.DecimalField(max_digits=20, decimal_places=8)
    high = models.DecimalField(max_digits=20, decimal_places=8)
    low = models.DecimalField(max_digits=20, decimal_places=8)
    close = models.DecimalField(max_digits=20, decimal_places=8)
    volume = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    timestamp = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["asset", "quote", "timestamp"])]


class Order(TimeStampedModel):
    """Track orders and fills per sleeve."""

    sleeve = models.ForeignKey(Sleeve, on_delete=models.CASCADE, related_name="orders")
    side = models.CharField(max_length=4)  # buy/sell
    amount = models.DecimalField(max_digits=20, decimal_places=8)
    price = models.DecimalField(max_digits=20, decimal_places=8)
    status = models.CharField(max_length=16, default="open")
    exchange_order_id = models.CharField(max_length=128, blank=True)
    filled_amount = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    fees = models.DecimalField(max_digits=20, decimal_places=8, default=0)


class TradeFill(TimeStampedModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="fills")
    price = models.DecimalField(max_digits=20, decimal_places=8)
    amount = models.DecimalField(max_digits=20, decimal_places=8)
    fee = models.DecimalField(max_digits=20, decimal_places=8, default=0)


class StrategyDecision(TimeStampedModel):
    sleeve = models.ForeignKey(Sleeve, on_delete=models.CASCADE, related_name="decisions")
    action = models.CharField(max_length=32)
    confidence = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)


class ApprovalRequest(TimeStampedModel):
    """Member approval flow for registration and escalations."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="approval_requests")
    reviewer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="approvals_reviewed")
    status = models.CharField(
        max_length=16,
        choices=(
            ("pending", "Pending"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
        ),
        default="pending",
    )
    note = models.CharField(max_length=255, blank=True)


class PauseEvent(TimeStampedModel):
    """Pause/resume events for vaults or sleeves."""

    vault = models.ForeignKey(CoreVault, on_delete=models.CASCADE, related_name="pause_events")
    sleeve = models.ForeignKey(Sleeve, on_delete=models.CASCADE, null=True, blank=True, related_name="pause_events")
    action = models.CharField(max_length=16, choices=(("pause", "Pause"), ("resume", "Resume")))
    reason = models.CharField(max_length=255, blank=True)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="pause_events")


class KeyRotationEvent(TimeStampedModel):
    api_key = models.ForeignKey(ApiKey, on_delete=models.CASCADE, related_name="rotation_events")
    status = models.CharField(max_length=16, default="queued")
    note = models.CharField(max_length=255, blank=True)
    error = models.CharField(max_length=255, blank=True)


class AuditLog(TimeStampedModel):
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs")
    action = models.CharField(max_length=64)
    target_type = models.CharField(max_length=64)
    target_id = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
