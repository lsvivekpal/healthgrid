from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("monitor", "0031_user_instance_access_scope")]

    operations = [
        migrations.AddField(
            model_name="rdsinstance",
            name="lock_control_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Allow the dedicated lock-control credentials to be used; Administrator-only setting",
            ),
        ),
    ]
