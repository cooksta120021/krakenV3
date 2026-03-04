from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("trading", "0010_mlcandle_interval_minutes"),
    ]

    operations = [
        migrations.AddField(
            model_name="orderlog",
            name="order_type",
            field=models.CharField(default="market", max_length=10),
        ),
    ]
