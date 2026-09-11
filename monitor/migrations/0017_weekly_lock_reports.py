from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("monitor", "0016_webhook_url_length"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationsettings",
            name="last_weekly_report_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="notificationsettings",
            name="report_base_url",
            field=models.URLField(
                blank=True,
                help_text="Public dashboard URL used to create signed CSV links for Teams",
                max_length=2048,
            ),
        ),
        migrations.AddField(
            model_name="notificationsettings",
            name="weekly_report_day",
            field=models.PositiveSmallIntegerField(
                default=0, help_text="0=Monday through 6=Sunday"
            ),
        ),
        migrations.AddField(
            model_name="notificationsettings",
            name="weekly_report_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="notificationsettings",
            name="weekly_report_hour",
            field=models.PositiveSmallIntegerField(
                default=9, help_text="Hour in IST, 0-23"
            ),
        ),
        migrations.CreateModel(
            name="LockReport",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("file_name", models.CharField(max_length=255)),
                ("csv_content", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
            ],
            options={
                "indexes": [models.Index(fields=["expires_at"], name="monitor_loc_expires_ed1911_idx")],
            },
        ),
    ]
