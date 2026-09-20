from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("monitor", "0033_rdsinstance_engine")]

    operations = [
        migrations.AddField(
            model_name="userinstanceaccess",
            name="can_manage_notifications",
            field=models.BooleanField(
                default=False,
                help_text="Allow this operator to change the instance Teams notification settings",
            ),
        ),
    ]
