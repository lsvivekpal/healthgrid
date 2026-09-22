import uuid

from django.db import migrations


def group_legacy_instances(apps, schema_editor):
    RDSInstance = apps.get_model("monitor", "RDSInstance")
    groups = {}
    fields = ("name", "engine", "db_identifier", "region", "host", "port", "username")

    for instance in RDSInstance.objects.order_by("id"):
        identity = tuple(getattr(instance, field) for field in fields)
        group_id = groups.setdefault(identity, instance.connection_group or uuid.uuid4())
        if instance.connection_group != group_id:
            instance.connection_group = group_id
            instance.save(update_fields=["connection_group"])


class Migration(migrations.Migration):
    dependencies = [("monitor", "0038_rdsinstance_connection_group")]

    operations = [migrations.RunPython(group_legacy_instances, migrations.RunPython.noop)]
