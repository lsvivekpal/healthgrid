from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0030_user_instance_access"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="UserInstanceAccessScope",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("configured_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=models.CASCADE, related_name="instance_access_scope", to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
