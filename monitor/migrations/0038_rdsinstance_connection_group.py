import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("monitor", "0037_owner_webhook_permission_help")]

    operations = [
        migrations.AddField(
            model_name="rdsinstance",
            name="connection_group",
            field=models.UUIDField(default=uuid.uuid4, editable=False, db_index=True),
        ),
    ]
