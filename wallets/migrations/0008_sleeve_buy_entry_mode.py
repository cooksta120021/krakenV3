from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wallets", "0007_sleeve_trade_pct_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="sleeve",
            name="buy_entry_mode",
            field=models.CharField(default="dip", max_length=12),
        ),
    ]
