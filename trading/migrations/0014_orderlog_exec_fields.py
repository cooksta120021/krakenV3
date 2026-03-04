from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("trading", "0013_sleeveperformancerecord"),
    ]

    operations = [
        migrations.AddField(
            model_name="orderlog",
            name="vol_exec",
            field=models.DecimalField(decimal_places=10, default=0, max_digits=20),
        ),
        migrations.AddField(
            model_name="orderlog",
            name="cost",
            field=models.DecimalField(decimal_places=10, default=0, max_digits=20),
        ),
        migrations.AddField(
            model_name="orderlog",
            name="fee",
            field=models.DecimalField(decimal_places=10, default=0, max_digits=20),
        ),
    ]
