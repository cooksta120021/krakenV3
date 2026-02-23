from django.conf import settings
from django.db import models


class Wallet(models.Model):
    class Currency(models.TextChoices):
        USD = "USD", "USD"
        EUR = "EUR", "EUR"
        GBP = "GBP", "GBP"
        CAD = "CAD", "CAD"
        CHF = "CHF", "CHF"
        JPY = "JPY", "JPY"
        AUD = "AUD", "AUD"
        USDT = "USDT", "USDT"
        USDC = "USDC", "USDC"
        DAI = "DAI", "DAI"
        USDP = "USDP", "USDP"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallets")
    currency = models.CharField(max_length=10, choices=Currency.choices)
    real_balance = models.DecimalField(max_digits=18, decimal_places=8, default=0)
    tradeable_balance = models.DecimalField(max_digits=18, decimal_places=8, default=0)

    class Meta:
        unique_together = ("user", "currency")
        ordering = ["user", "currency"]

    def __str__(self):
        return f"{self.user} {self.currency} wallet"


class Sleeve(models.Model):
    class Type(models.TextChoices):
        QUIET = "quiet", "Quiet"
        FLASH = "flash", "Flash"

    class TradeMode(models.TextChoices):
        AUTO = "auto", "Auto"
        CUSTOM = "custom", "Custom"

    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="sleeves")
    type = models.CharField(max_length=10, choices=Type.choices)
    trade_mode = models.CharField(max_length=10, choices=TradeMode.choices, default=TradeMode.AUTO)
    allocated_balance = models.DecimalField(max_digits=18, decimal_places=8, default=0)
    base_asset = models.CharField(max_length=20, blank=True)
    position_base_qty = models.DecimalField(max_digits=18, decimal_places=10, default=0)

    class Meta:
        unique_together = ("wallet", "type")
        ordering = ["wallet", "type"]

    def __str__(self):
        return f"{self.wallet.currency} {self.type} sleeve"
