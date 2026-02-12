from django import forms

from .models import ApiKey, CoreVault, Wallet

KRAKEN_STABLECOINS = {
    "DAI",
    "EURC",
    "EURQ",
    "USDG",
    "PYUSD",
    "RLUSD",
    "EUROP",
    "EURR",
    "USDR",
    "USDT",
    "EURT",
    "TUSD",
    "USDC",
    "USDD",
    "USDQ",
    "USDS",
    # fiat
    "USD",
    "EUR",
    "GBP",
    "CAD",
    "AUD",
    "CHF",
    "JPY",
}

ALLOWED_ASSETS = KRAKEN_STABLECOINS


class WalletForm(forms.ModelForm):
    def clean(self):
        cleaned = super().clean()
        asset = cleaned.get("asset") or ""
        if asset and asset not in ALLOWED_ASSETS:
            raise forms.ValidationError("Asset must be a supported fiat/stablecoin on Kraken.")
        return cleaned

    class Meta:
        model = Wallet
        fields = ["name", "asset"]


class CoreVaultForm(forms.ModelForm):
    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user:
            self.fields["wallet"].queryset = Wallet.objects.filter(owner=user)

    class Meta:
        model = CoreVault
        fields = [
            "wallet",
            "asset",
            "quote_asset",
            "total_coins",
            "tradeable_coins",
            "quiet_allocation",
            "flash_allocation",
            "reserve_buffer_pct",
            "allow_zero_tradeable",
            "fee_rate",
            "slippage_pct",
            "allowed_pairs",
            "max_drawdown_pct",
            "max_daily_loss_pct",
            "max_position_pct",
        ]
        widgets = {
            "allowed_pairs": forms.Textarea(attrs={"rows": 2, "placeholder": "Comma-separated pairs e.g. ETHUSDT,BTCUSDT"}),
        }

    def clean(self):
        cleaned = super().clean()
        total = cleaned.get("total_coins") or 0
        tradeable = cleaned.get("tradeable_coins") or 0
        quiet = cleaned.get("quiet_allocation") or 0
        flash = cleaned.get("flash_allocation") or 0
        pairs_raw = cleaned.get("allowed_pairs") or []
        quote = cleaned.get("quote_asset") or ""

        if tradeable < 0 or total < 0 or quiet < 0 or flash < 0:
            raise forms.ValidationError("Values must be non-negative.")

        if tradeable > total and total > 0:
            raise forms.ValidationError("Tradeable Coins cannot exceed Total Coins.")

        if quiet + flash > tradeable:
            raise forms.ValidationError("Allocations (Quiet + Flash) cannot exceed Tradeable Coins.")

        # quote must be a Kraken stablecoin
        if quote and quote not in KRAKEN_STABLECOINS:
            raise forms.ValidationError("Quote asset must be a supported stablecoin on Kraken.")

        # normalize allowed_pairs if provided as comma string
        if isinstance(pairs_raw, str):
            cleaned["allowed_pairs"] = [p.strip() for p in pairs_raw.split(",") if p.strip()]

        for pair in cleaned.get("allowed_pairs", []):
            if not pair.isalnum():
                raise forms.ValidationError("Allowed pairs must be alphanumeric (e.g., ETHUSDT).")
            if len(pair) < 6 or len(pair) > 12:
                raise forms.ValidationError("Allowed pair length seems invalid.")

        return cleaned


class ApiKeyForm(forms.ModelForm):
    key_plain = forms.CharField(max_length=255, required=False)
    secret_plain = forms.CharField(max_length=255, required=False, widget=forms.PasswordInput)

    class Meta:
        model = ApiKey
        fields = ["label", "exchange", "is_active", "rotation_index", "health_status", "last_error"]

    def clean(self):
        cleaned = super().clean()
        if not self.instance.pk and (not cleaned.get("key_plain") or not cleaned.get("secret_plain")):
            raise forms.ValidationError("API key and secret are required when creating a key.")
        return cleaned
