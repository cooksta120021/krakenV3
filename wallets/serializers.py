from rest_framework import serializers

from .models import Sleeve, Wallet


class WalletSerializer(serializers.ModelSerializer):
    reserve_wallet_currency = serializers.CharField(source="reserve_wallet.currency", read_only=True)

    class Meta:
        model = Wallet
        fields = ["id", "currency", "real_balance", "tradeable_balance", "reserve_wallet", "reserve_wallet_currency"]
        read_only_fields = ["id"]

    def create(self, validated_data):
        user = self.context["request"].user
        return Wallet.objects.create(user=user, **validated_data)


class SleeveSerializer(serializers.ModelSerializer):
    wallet_currency = serializers.CharField(source="wallet.currency", read_only=True)

    class Meta:
        model = Sleeve
        fields = [
            "id",
            "wallet",
            "wallet_currency",
            "type",
            "allocated_balance",
            "trade_amount_base",
            "trade_pct_mode",
            "trade_pct_both",
            "trade_pct_buy",
            "trade_pct_sell",
            "buy_entry_mode",
            "limit_max_failures",
        ]
        read_only_fields = ["id", "wallet_currency"]

    def validate_wallet(self, wallet):
        request = self.context.get("request")
        if request and request.user.role != "admin" and wallet.user != request.user:
            raise serializers.ValidationError("Cannot assign sleeve to another user's wallet")
        return wallet
