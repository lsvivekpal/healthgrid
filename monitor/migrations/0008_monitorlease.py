from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0007_notificationsettings"),
    ]

    operations = [
        migrations.CreateModel(
            name="MonitorLease",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("owner_id", models.CharField(blank=True, max_length=64)),
                ("lease_until", models.DateTimeField(blank=True, null=True)),
            ],
        ),
    ]
