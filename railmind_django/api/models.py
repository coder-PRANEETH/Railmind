"""Durable execution records for the Digital Twin work-order engine.

The twin remains the authority for physical state and task verification, but
these models are the durable hand-off/audit record.  A Django restart can
therefore still serve an accepted order, its task progress, cancellation and
event history instead of treating a successful HTTP request as completion.
"""
from django.db import models


class WorkOrder(models.Model):
    """Persistent mirror of one twin work order, scoped to a twin session."""

    work_order_id = models.CharField(max_length=128, primary_key=True)
    session_id = models.CharField(max_length=128, default="default", db_index=True)
    incident_id = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    order_type = models.CharField(max_length=128, default="CRITICAL_INCIDENT_RESPONSE")
    target = models.CharField(max_length=128)
    status = models.CharField(max_length=32, default="UNRESOLVED", db_index=True)
    completion_percentage = models.PositiveSmallIntegerField(default=0)
    estimated_ticks_remaining = models.PositiveIntegerField(default=0)
    created_tick = models.PositiveIntegerField(default=0)
    cancelled = models.BooleanField(default=False)
    cancelled_tick = models.IntegerField(null=True, blank=True)
    cancel_reason = models.TextField(null=True, blank=True)
    auto_retry = models.BooleanField(default=False)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)
    # Keeps forward-compatible transport metadata without making it execution
    # authority.  Tasks and events are normalised in the related tables.
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [models.Index(fields=["session_id", "updated_at"])]

    def __str__(self):
        return self.work_order_id


class FieldTask(models.Model):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="field_tasks")
    task_id = models.CharField(max_length=128)
    action = models.CharField(max_length=64)
    target = models.CharField(max_length=128)
    status = models.CharField(max_length=32, default="PENDING")
    ticks_required = models.PositiveIntegerField()
    ticks_remaining = models.PositiveIntegerField()
    progress = models.FloatField(default=0.0)
    dependencies = models.JSONField(default=list, blank=True)
    blocking_reason = models.TextField(null=True, blank=True)
    params = models.JSONField(default=dict, blank=True)
    task_metadata = models.JSONField(default=dict, blank=True)
    crew_type = models.CharField(max_length=64, null=True, blank=True)
    detail = models.TextField(default="Not yet started.")
    started_tick = models.IntegerField(null=True, blank=True)
    completed_tick = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["work_order", "task_id"], name="unique_task_per_work_order")]


class WorkOrderEvent(models.Model):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="history")
    event_key = models.CharField(max_length=128)
    tick = models.IntegerField()
    kind = models.CharField(max_length=64)
    task_id = models.CharField(max_length=128, null=True, blank=True)
    detail = models.TextField()
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["work_order", "event_key"], name="unique_work_order_event")]
        ordering = ["tick", "id"]
