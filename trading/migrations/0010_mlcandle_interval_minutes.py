from django.db import migrations, models


def _ensure_mlcandle_table(apps, schema_editor):
    """Make sure the MlCandle table exists and has interval_minutes.

    Some early versions of this project had MlCandle in models.py but not in migrations,
    so Django's migration state may not include it. We make this migration resilient.
    """
    connection = schema_editor.connection
    table = "trading_mlcandle"
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=%s",
                [table],
            )
            exists = cursor.fetchone() is not None

            if not exists:
                cursor.execute(
                    """
                    CREATE TABLE trading_mlcandle (
                        id integer NOT NULL PRIMARY KEY AUTOINCREMENT,
                        pair varchar(20) NOT NULL,
                        interval_minutes integer NOT NULL DEFAULT 60,
                        ts bigint NOT NULL,
                        close decimal(20,10) NOT NULL,
                        created_at datetime NOT NULL
                    )
                    """
                )

            # Add column if missing.
            cursor.execute(f"PRAGMA table_info({table})")
            cols = [str(r[1]) for r in cursor.fetchall() or []]
            if "interval_minutes" not in cols:
                cursor.execute(
                    "ALTER TABLE trading_mlcandle ADD COLUMN interval_minutes integer NOT NULL DEFAULT 60"
                )

            # Backfill (defensive).
            try:
                cursor.execute("UPDATE trading_mlcandle SET interval_minutes=60 WHERE interval_minutes IS NULL")
            except Exception:
                pass

            # Ensure unique index for (pair, interval_minutes, ts).
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS trading_mlcandle_pair_interval_ts_uniq ON trading_mlcandle(pair, interval_minutes, ts)"
            )

    except Exception:
        # If anything goes wrong, let migrate surface the failure.
        raise


class Migration(migrations.Migration):

    dependencies = [
        ("trading", "0009_sleevestrategy_executor_notify"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(_ensure_mlcandle_table, reverse_code=migrations.RunPython.noop),
            ],
            state_operations=[
                migrations.CreateModel(
                    name="MlCandle",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("pair", models.CharField(max_length=20)),
                        ("interval_minutes", models.IntegerField(default=60)),
                        ("ts", models.BigIntegerField()),
                        ("close", models.DecimalField(decimal_places=10, max_digits=20)),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                    ],
                    options={
                        "ordering": ["pair", "interval_minutes", "ts"],
                        "unique_together": {("pair", "interval_minutes", "ts")},
                    },
                ),
            ],
        ),
    ]
