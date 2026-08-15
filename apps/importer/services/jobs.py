"""Shared lifecycle helpers for importer Celery jobs."""

from __future__ import annotations

from django.utils import timezone

from ..models import ImporterJob, ImporterJobStatus


def concise_error_message(exc_or_message, *, limit=500):
    """Return a useful one-line reason while leaving full traceback logging to Celery."""
    if isinstance(exc_or_message, Exception):
        message = str(exc_or_message) or exc_or_message.__class__.__name__
    else:
        message = str(exc_or_message)
    message = message.split("Call log:", 1)[0].strip()
    return message[:limit] or "Unknown extraction failure."


def create_job(*, title, job_type, marketplace="", total_items=0, created_by=None,
               amazon_product=None, flipkart_product=None, import_batch=None,
               product_match=None, metadata=None):
    return ImporterJob.objects.create(
        title=title,
        job_type=job_type,
        marketplace=marketplace,
        total_items=total_items,
        created_by=created_by,
        amazon_product=amazon_product,
        flipkart_product=flipkart_product,
        import_batch=import_batch,
        product_match=product_match,
        metadata=metadata or {},
    )


def get_or_create_task_job(*, job_id, title, job_type, marketplace="", total_items=0,
                           amazon_product=None, flipkart_product=None, metadata=None):
    if job_id:
        return ImporterJob.objects.get(pk=job_id)
    return create_job(
        title=title,
        job_type=job_type,
        marketplace=marketplace,
        total_items=total_items,
        amazon_product=amazon_product,
        flipkart_product=flipkart_product,
        metadata=metadata,
    )


def mark_job_queued(job, celery_task_id="", *, force=False):
    now = timezone.now()
    fields = {"celery_task_id": celery_task_id, "queued_at": now}
    if force or job.status == ImporterJobStatus.PENDING:
        fields["status"] = ImporterJobStatus.QUEUED
    ImporterJob.objects.filter(pk=job.pk, status=job.status).update(**fields, updated_at=now)
    job.refresh_from_db()
    return job


def mark_job_running(job):
    now = timezone.now()
    job.status = ImporterJobStatus.RUNNING
    job.started_at = job.started_at or now
    job.save(update_fields=["status", "started_at", "updated_at"])
    return job


def update_job_progress(job, *, total_items=None, processed_items=None, success_count=None,
                        failed_count=None, skipped_count=None, result_message=None):
    for field, value in {
        "processed_items": processed_items,
        "total_items": total_items,
        "success_count": success_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "result_message": result_message,
    }.items():
        if value is not None:
            setattr(job, field, value)
    job.save(update_fields=[
        field for field, value in {
            "processed_items": processed_items,
            "total_items": total_items,
            "success_count": success_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "result_message": result_message,
        }.items() if value is not None
    ] + ["updated_at"])
    return job


def _finish(job, status, *, result_message="", error_message=""):
    job.status = status
    job.result_message = result_message
    job.error_message = error_message
    job.completed_at = timezone.now()
    job.save(update_fields=["status", "result_message", "error_message", "completed_at", "updated_at"])
    return job


def mark_job_completed(job, result_message=""):
    return _finish(job, ImporterJobStatus.COMPLETED, result_message=result_message)


def mark_job_partial(job, result_message=""):
    return _finish(job, ImporterJobStatus.PARTIAL, result_message=result_message)


def mark_job_skipped(job, result_message=""):
    return _finish(job, ImporterJobStatus.SKIPPED, result_message=result_message)


def mark_job_failed(job, exc_or_message):
    message = concise_error_message(exc_or_message)
    return _finish(job, ImporterJobStatus.FAILED, error_message=message)


def increment_job_retry(job):
    job.retry_count += 1
    job.save(update_fields=["retry_count", "updated_at"])
    return job


def enqueue_job(*, task, job, args=(), kwargs=None):
    """Dispatch a task and close the PENDING -> QUEUED transition safely."""
    task_kwargs = dict(kwargs or {})
    task_kwargs["job_id"] = str(job.pk)
    try:
        async_result = task.delay(*args, **task_kwargs)
    except Exception as exc:
        mark_job_failed(job, exc)
        raise
    job.celery_task_id = str(async_result.id)
    job.save(update_fields=["celery_task_id", "updated_at"])
    return mark_job_queued(job, str(async_result.id))
