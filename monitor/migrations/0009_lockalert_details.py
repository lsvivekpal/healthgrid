from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0008_monitorlease"),
    ]

    operations = [
        migrations.AddField(
            model_name="lockalert",
            name="blocked_user",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="lockalert",
            name="blocking_user",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="lockalert",
            name="last_notified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="lockalert",
            name="waiting_seconds",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
