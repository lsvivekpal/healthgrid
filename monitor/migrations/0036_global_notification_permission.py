from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("monitor", "0035_alter_userinstanceaccess_notification_permission")]

    operations = [
        migrations.AddField(
            model_name="userinstanceaccessscope",
            name="can_manage_global_notifications",
            field=models.BooleanField(
                default=False,
                help_text="Allow this operator to manage global notification settings",
            ),
        ),
    ]
