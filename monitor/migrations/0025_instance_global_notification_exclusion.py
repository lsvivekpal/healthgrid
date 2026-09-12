from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0024_replication_slot_access"),
    ]

    operations = [
        migrations.AddField(
            model_name="rdsinstance",
            name="exclude_from_global_notifications",
            field=models.BooleanField(
                default=False,
                help_text="Do not send real-time lock/query alerts to the shared/global Teams channel; weekly reports remain included",
            ),
        ),
    ]
