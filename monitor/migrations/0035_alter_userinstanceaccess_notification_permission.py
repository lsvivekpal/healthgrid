from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("monitor", "0034_userinstanceaccess_can_manage_notifications")]

    operations = [
        migrations.AlterField(
            model_name="userinstanceaccess",
            name="can_manage_notifications",
            field=models.BooleanField(
                default=False,
                help_text="Allow this operator to disable global Teams notifications for the instance",
            ),
        ),
    ]
