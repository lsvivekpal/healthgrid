from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0004_alter_auditlog_action"),
    ]

    operations = [
        migrations.AddField(
            model_name="rdsinstance",
            name="owner_email",
            field=models.EmailField(blank=True, help_text="Receives lock alerts for this database", max_length=254),
        ),
        migrations.CreateModel(
            name="LockAlert",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("alert_key", models.CharField(max_length=255)),
                ("blocked_pid", models.IntegerField()),
                ("blocking_pid", models.IntegerField()),
                ("blocked_query", models.TextField(blank=True)),
                ("blocking_query", models.TextField(blank=True)),
                ("first_seen_at", models.DateTimeField()),
                ("last_seen_at", models.DateTimeField()),
                ("alerted_at", models.DateTimeField(blank=True, null=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("instance", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lock_alerts", to="monitor.rdsinstance")),
            ],
            options={
                "indexes": [
                    models.Index(fields=["instance", "resolved_at"], name="monitor_loc_instanc_2c14bc_idx"),
                    models.Index(fields=["alert_key"], name="monitor_loc_alert_k_9dd7e9_idx"),
                ],
            },
        ),
    ]
