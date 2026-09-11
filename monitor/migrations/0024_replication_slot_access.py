from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0023_rename_long_query_indexes"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ReplicationSlotAccess",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("can_drop", models.BooleanField(default=False)),
                ("can_terminate", models.BooleanField(default=False)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="replication_slot_access", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AlterField(
            model_name="auditlog",
            name="action",
            field=models.CharField(choices=[
                ("kill_session", "Kill session"),
                ("kill_chain", "Kill blocking chain"),
                ("bulk_kill", "Bulk kill selected"),
                ("kill_replication_slot", "Kill replication slot backend"),
                ("drop_replication_slot", "Drop replication slot"),
                ("add_instance", "Add instance"),
                ("remove_instance", "Remove instance"),
                ("rename_instance", "Rename instance"),
                ("update_slot_permissions", "Update replication-slot permissions"),
            ], max_length=32),
        ),
    ]
