from rest_framework import serializers

from api_keys.serializers import ApiKeySerializer
from .models import OrderLog, SleeveStrategy


class SleeveStrategySerializer(serializers.ModelSerializer):
    class Meta:
        model = SleeveStrategy
        fields = ["id", "sleeve", "mode", "params", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_sleeve(self, sleeve):
        request = self.context.get("request")
        if request and request.user.role != "admin" and sleeve.wallet.user != request.user:
            raise serializers.ValidationError("Cannot attach strategy to another user's sleeve")
        return sleeve


class OrderLogSerializer(serializers.ModelSerializer):
    api_key = ApiKeySerializer(read_only=True)

    class Meta:
        model = OrderLog
        fields = [
            "id",
            "sleeve",
            "api_key",
            "side",
            "base_asset",
            "quote_asset",
            "amount",
            "price",
            "txid",
            "status",
            "error",
            "created_at",
        ]
        read_only_fields = fields
