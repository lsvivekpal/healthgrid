import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('monitor', '0041_rdsinstance_query_runner_enabled_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='QueryRunnerExecution',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sql', models.TextField()),
                ('status', models.CharField(choices=[('pending', 'Pending commit'), ('committed', 'Committed'), ('discarded', 'Discarded'), ('failed', 'Failed')], default='pending', max_length=16)),
                ('rows_affected', models.IntegerField(blank=True, null=True)),
                ('error', models.TextField(blank=True)),
                ('previewed_at', models.DateTimeField(auto_now_add=True)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('instance', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='query_runner_executions', to='monitor.rdsinstance')),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='query_runner_executions', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-previewed_at'],
            },
        ),
    ]
