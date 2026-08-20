from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0005_lockalert_owner_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="rdsinstance",
            name="owner_teams_webhook_url",
            field=models.URLField(blank=True, help_text="Teams Workflow webhook for this database owner"),
        ),
        migrations.RemoveField(
            model_name="rdsinstance",
            name="owner_email",
        ),
    ]
