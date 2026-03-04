from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings


class Migration(migrations.Migration):

    dependencies = [
        ("wallets", "0009_sleeve_limit_max_failures"),
        ("trading", "0012_orderlog_order_type_backfill"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SleevePerformanceRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("wallet_currency", models.CharField(default="", max_length=10)),
                ("sleeve_id", models.IntegerField(unique=True)),
                ("sleeve_type", models.CharField(default="", max_length=10)),
                ("base_asset", models.CharField(default="", max_length=20)),
                ("started_at", models.DateTimeField()),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("first_trade_at", models.DateTimeField(blank=True, null=True)),
                ("last_trade_at", models.DateTimeField(blank=True, null=True)),
                ("start_allocated_quote", models.DecimalField(decimal_places=10, default=0, max_digits=20)),
                ("end_allocated_quote", models.DecimalField(decimal_places=10, default=0, max_digits=20)),
                ("start_position_base", models.DecimalField(decimal_places=10, default=0, max_digits=20)),
                ("end_position_base", models.DecimalField(decimal_places=10, default=0, max_digits=20)),
                ("realized_pnl_quote", models.DecimalField(decimal_places=10, default=0, max_digits=20)),
                ("net_quote_flow", models.DecimalField(decimal_places=10, default=0, max_digits=20)),
                ("trades", models.IntegerField(default=0)),
                ("last_price_quote_per_base", models.DecimalField(decimal_places=10, default=0, max_digits=20)),
                ("end_cash_est_quote", models.DecimalField(decimal_places=10, default=0, max_digits=20)),
                ("end_equity_est_quote", models.DecimalField(decimal_places=10, default=0, max_digits=20)),
                (
                    "user",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sleeve_performance", to=settings.AUTH_USER_MODEL),
                ),
                (
                    "wallet",
                    models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="sleeve_performance", to="wallets.wallet"),
                ),
            ],
            options={
                "ordering": ["-started_at"],
            },
        ),
    ]
