from django.db import migrations


def backfill_order_type(apps, schema_editor):
    OrderLog = apps.get_model("trading", "OrderLog")
    # Existing rows were effectively market orders (we stored a display price either way)
    OrderLog.objects.filter(order_type="").update(order_type="market")


class Migration(migrations.Migration):

    dependencies = [
        ("trading", "0011_orderlog_order_type"),
    ]

    operations = [
        migrations.RunPython(backfill_order_type, migrations.RunPython.noop),
    ]
