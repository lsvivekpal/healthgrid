from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0014_lock_notification_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="rdsinstance",
            name="control_password_encrypted",
            field=models.CharField(blank=True, help_text="Fernet-encrypted control role password", max_length=512),
        ),
        migrations.AddField(
            model_name="rdsinstance",
            name="control_username",
            field=models.CharField(blank=True, help_text="Optional dedicated monitoring/kill role; falls back to DB login username", max_length=255),
        ),
    ]
