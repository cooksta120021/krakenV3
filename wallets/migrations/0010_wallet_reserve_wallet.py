from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("wallets", "0009_sleeve_limit_max_failures"),
    ]

    operations = [
        migrations.AddField(
            model_name="wallet",
            name="reserve_wallet",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="reserved_by_wallets",
                to="wallets.wallet",
            ),
        ),
    ]
