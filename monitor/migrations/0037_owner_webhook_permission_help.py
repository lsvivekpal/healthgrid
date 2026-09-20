from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("monitor", "0036_global_notification_permission")]

    operations = [
        migrations.AlterField(
            model_name="userinstanceaccess",
            name="can_manage_notifications",
            field=models.BooleanField(
                default=False,
                help_text="Allow this operator to manage the instance owner Teams webhook",
            ),
        ),
    ]
