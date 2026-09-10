from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0015_control_credentials"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notificationsettings",
            name="channel_webhook_url",
            field=models.URLField(blank=True, help_text="Shared Teams channel Workflow webhook", max_length=2048),
        ),
        migrations.AlterField(
            model_name="rdsinstance",
            name="owner_teams_webhook_url",
            field=models.URLField(blank=True, help_text="Teams Workflow webhook for this database owner", max_length=2048),
        ),
    ]
