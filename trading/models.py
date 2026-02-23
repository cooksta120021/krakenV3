from django.conf import settings
from django.db import models

from wallets.models import Sleeve
from api_keys.models import ApiKey


class SleeveStrategy(models.Model):
    class Mode(models.TextChoices):
        BUY_QUIET = "buy_quiet", "Quiet Buy (conservative/longterm)"
        SELL_QUIET = "sell_quiet", "Quiet Sell (conservative/longterm)"
        BUY_FLASH = "buy_flash", "Flash Buy (aggressive/shortterm)"
        SELL_FLASH = "sell_flash", "Flash Sell (aggressive/shortterm)"

    sleeve = models.ForeignKey(Sleeve, on_delete=models.CASCADE, related_name="strategies")
    mode = models.CharField(max_length=20, choices=Mode.choices)
    params = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    ml_active = models.BooleanField(default=False)
    ml_confidence = models.DecimalField(max_digits=5, decimal_places=4, default=0)
    ml_pair = models.CharField(max_length=20, blank=True, default="")
    ml_entry_drop_pct = models.DecimalField(max_digits=6, decimal_places=3, default=0)
    ml_take_profit_pct = models.DecimalField(max_digits=6, decimal_places=3, default=0)
    ml_stop_loss_pct = models.DecimalField(max_digits=6, decimal_places=3, default=0)
    ml_position_size_pct = models.DecimalField(max_digits=6, decimal_places=3, default=0)
    ml_cooldown_seconds = models.IntegerField(default=0)
    ml_candles_1h = models.IntegerField(default=0)
    ml_last_trained_at = models.DateTimeField(null=True, blank=True)
    ml_last_action_at = models.DateTimeField(null=True, blank=True)
    ml_last_tick_at = models.DateTimeField(null=True, blank=True)
    ml_last_price = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    ml_last_reason = models.CharField(max_length=200, blank=True, default="")
    ml_last_entry_trigger = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    ml_last_tp_trigger = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    ml_last_sl_trigger = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    ml_status = models.CharField(max_length=30, default="idle")
    ml_paused = models.BooleanField(default=False)
    ml_stage = models.CharField(max_length=30, default="idle")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("sleeve", "mode")
        ordering = ["sleeve", "mode"]

    def __str__(self):
        return f"{self.sleeve} {self.mode}"


class OrderLog(models.Model):
    class Side(models.TextChoices):
        BUY = "buy", "Buy"
        SELL = "sell", "Sell"

    sleeve = models.ForeignKey(Sleeve, on_delete=models.CASCADE, related_name="orders")
    api_key = models.ForeignKey(ApiKey, on_delete=models.PROTECT, related_name="orders")
    side = models.CharField(max_length=4, choices=Side.choices)
    base_asset = models.CharField(max_length=20)
    quote_asset = models.CharField(max_length=20)
    amount = models.DecimalField(max_digits=20, decimal_places=10)
    price = models.DecimalField(max_digits=20, decimal_places=10)
    txid = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(max_length=20, default="pending")
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.sleeve} {self.side} {self.base_asset}/{self.quote_asset}"


class MlCandle(models.Model):
    pair = models.CharField(max_length=20)
    interval_minutes = models.IntegerField(default=60)
    ts = models.BigIntegerField()
    close = models.DecimalField(max_digits=20, decimal_places=10)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("pair", "interval_minutes", "ts")
        ordering = ["pair", "interval_minutes", "ts"]

    def __str__(self):
        return f"MlCandle({self.pair}/{self.interval_minutes}m@{self.ts})"


class ProfitCandidatesCache(models.Model):
    quote = models.CharField(max_length=10, default="USD")
    items = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"ProfitCandidatesCache({self.quote})"


class MlMarketSnapshot(models.Model):
    strategy = models.ForeignKey(SleeveStrategy, on_delete=models.CASCADE, related_name="ml_snapshots")
    pair = models.CharField(max_length=20)
    lookback_days = models.IntegerField(default=0)
    candles = models.IntegerField(default=0)
    closes = models.JSONField(default=list, blank=True)
    price_now = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
