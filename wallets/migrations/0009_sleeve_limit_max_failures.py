from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wallets", "0008_sleeve_buy_entry_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="sleeve",
            name="limit_max_failures",
            field=models.IntegerField(default=2),
        ),
    ]
