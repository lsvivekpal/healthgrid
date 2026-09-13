from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0027_dashboardlink"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationsettings",
            name="report_retention_days",
            field=models.PositiveSmallIntegerField(
                choices=[(2, "2 days"), (3, "3 days"), (7, "7 days")],
                default=7,
                help_text="Days to keep generated weekly report CSV payloads in the database",
            ),
        ),
    ]
