from django import forms

from .models import Sleeve, Wallet


class WalletForm(forms.ModelForm):
    class Meta:
        model = Wallet
        fields = ["currency"]


class SleeveForm(forms.ModelForm):
    class Meta:
        model = Sleeve
        fields = [
            "wallet",
            "type",
            "allocated_balance",
            "base_asset",
        ]


class WalletTradeableForm(forms.ModelForm):
    class Meta:
        model = Wallet
        fields = []
