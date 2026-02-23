from django.contrib.auth import get_user_model
from rest_framework import serializers

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email", "role", "is_approved"]
        read_only_fields = ["id", "role", "is_approved"]


class UserApprovalSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "is_approved"]
        read_only_fields = ["id"]
