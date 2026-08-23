# Generated manually because this repository keeps migrations in source.
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="WorkOrder",
            fields=[
                ("work_order_id", models.CharField(max_length=128, primary_key=True, serialize=False)),
                ("session_id", models.CharField(db_index=True, default="default", max_length=128)),
                ("incident_id", models.CharField(blank=True, db_index=True, max_length=128, null=True)),
                ("order_type", models.CharField(default="CRITICAL_INCIDENT_RESPONSE", max_length=128)),
                ("target", models.CharField(max_length=128)),
                ("status", models.CharField(db_index=True, default="UNRESOLVED", max_length=32)),
                ("completion_percentage", models.PositiveSmallIntegerField(default=0)),
                ("estimated_ticks_remaining", models.PositiveIntegerField(default=0)),
                ("created_tick", models.PositiveIntegerField(default=0)),
                ("cancelled", models.BooleanField(default=False)),
                ("cancelled_tick", models.IntegerField(blank=True, null=True)),
                ("cancel_reason", models.TextField(blank=True, null=True)),
                ("auto_retry", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField()),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
            ],
        ),
        migrations.CreateModel(
            name="FieldTask",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("task_id", models.CharField(max_length=128)),
                ("action", models.CharField(max_length=64)),
                ("target", models.CharField(max_length=128)),
                ("status", models.CharField(default="PENDING", max_length=32)),
                ("ticks_required", models.PositiveIntegerField()),
                ("ticks_remaining", models.PositiveIntegerField()),
                ("progress", models.FloatField(default=0.0)),
                ("dependencies", models.JSONField(blank=True, default=list)),
                ("blocking_reason", models.TextField(blank=True, null=True)),
                ("params", models.JSONField(blank=True, default=dict)),
                ("task_metadata", models.JSONField(blank=True, default=dict)),
                ("crew_type", models.CharField(blank=True, max_length=64, null=True)),
                ("detail", models.TextField(default="Not yet started.")),
                ("started_tick", models.IntegerField(blank=True, null=True)),
                ("completed_tick", models.IntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("work_order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="field_tasks", to="api.workorder")),
            ],
        ),
        migrations.CreateModel(
            name="WorkOrderEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_key", models.CharField(max_length=128)),
                ("tick", models.IntegerField()),
                ("kind", models.CharField(max_length=64)),
                ("task_id", models.CharField(blank=True, max_length=128, null=True)),
                ("detail", models.TextField()),
                ("recorded_at", models.DateTimeField(auto_now_add=True)),
                ("work_order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="history", to="api.workorder")),
            ],
            options={"ordering": ["tick", "id"]},
        ),
        migrations.AddConstraint(
            model_name="fieldtask",
            constraint=models.UniqueConstraint(fields=("work_order", "task_id"), name="unique_task_per_work_order"),
        ),
        migrations.AddConstraint(
            model_name="workorderevent",
            constraint=models.UniqueConstraint(fields=("work_order", "event_key"), name="unique_work_order_event"),
        ),
        migrations.AddIndex(
            model_name="workorder",
            index=models.Index(fields=["session_id", "updated_at"], name="api_workord_session_3f9ec8_idx"),
        ),
    ]
