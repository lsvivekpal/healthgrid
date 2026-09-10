from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0006_owner_teams_webhook"),
    ]

    operations = [
        migrations.CreateModel(
            name="NotificationSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("channel_webhook_url", models.URLField(blank=True, help_text="Shared Teams channel Workflow webhook")),
                ("threshold_seconds", models.PositiveIntegerField(default=120)),
                ("interval_seconds", models.PositiveIntegerField(default=30)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Notification settings",
                "verbose_name_plural": "Notification settings",
            },
        ),
    ]
