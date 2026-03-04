from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wallets", "0005_normalize_bt_to_xbt"),
    ]

    operations = [
        migrations.AddField(
            model_name="sleeve",
            name="trade_amount_base",
            field=models.DecimalField(decimal_places=10, default=0, max_digits=20),
        ),
    ]
