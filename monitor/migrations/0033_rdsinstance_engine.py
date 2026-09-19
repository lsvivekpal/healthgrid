from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("monitor", "0032_lock_control_enabled")]

    operations = [
        migrations.AddField(
            model_name="rdsinstance",
            name="engine",
            field=models.CharField(
                choices=[
                    ("postgresql", "PostgreSQL"),
                    ("mysql", "MySQL"),
                    ("mariadb", "MariaDB"),
                ],
                default="postgresql",
                max_length=16,
            ),
        ),
    ]
