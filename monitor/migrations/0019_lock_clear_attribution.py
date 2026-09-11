from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("monitor", "0018_retention_cleanup"),
    ]

    operations = [
        migrations.AddField(
            model_name="lockalert",
            name="clear_reason",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="lockalert",
            name="cleared_by",
            field=models.CharField(blank=True, max_length=150),
        ),
    ]
