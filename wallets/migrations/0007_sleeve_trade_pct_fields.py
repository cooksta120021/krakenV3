from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wallets", "0006_sleeve_trade_amount_base"),
    ]

    operations = [
        migrations.AddField(
            model_name="sleeve",
            name="trade_pct_mode",
            field=models.CharField(default="both", max_length=12),
        ),
        migrations.AddField(
            model_name="sleeve",
            name="trade_pct_both",
            field=models.DecimalField(decimal_places=3, default=0, max_digits=6),
        ),
        migrations.AddField(
            model_name="sleeve",
            name="trade_pct_buy",
            field=models.DecimalField(decimal_places=3, default=0, max_digits=6),
        ),
        migrations.AddField(
            model_name="sleeve",
            name="trade_pct_sell",
            field=models.DecimalField(decimal_places=3, default=0, max_digits=6),
        ),
    ]
