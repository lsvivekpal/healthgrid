from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0029_lock_storm_alerts"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="UserInstanceAccess",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[("readonly", "Read-only"), ("operator", "Operator")], max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("instance", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="user_access_grants", to="monitor.rdsinstance")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="instance_access_grants", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["instance__name"]},
        ),
        migrations.AddConstraint(
            model_name="userinstanceaccess",
            constraint=models.UniqueConstraint(fields=("user", "instance"), name="unique_user_instance_access"),
        ),
    ]
