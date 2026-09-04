"""Single-process background classification for uploaded workbooks.

The job state lives in the database so progress works with both SQLite and
PostgreSQL. The in-memory thread registry only prevents duplicate workers in
the current (single-worker) web process; a later progress request can resume a
job after a process restart.
"""
from threading import Lock, Thread

from django.db import close_old_connections, transaction
from django.utils import timezone

from classifier.models import Classification, ClassificationJob, Product, TaxonomyCategory


_running_jobs_lock = Lock()
_running_job_id = None


def start_job(job_id):
    """Start one daemon worker, serializing jobs to protect the 512 MB worker."""
    global _running_job_id
    with _running_jobs_lock:
        if _running_job_id is not None:
            return False
        _running_job_id = job_id
    Thread(target=process_job, args=(job_id,), daemon=True, name=f"classification-job-{job_id}").start()
    return True


def _save_progress(job, *, classified=0, failed=0, needs_review=0):
    job.processed_products += 1
    job.classified_products += classified
    job.failed_products += failed
    job.needs_review_products += needs_review
    job.save(update_fields=[
        "processed_products", "classified_products", "failed_products", "needs_review_products",
    ])


def _classify_product(product):
    """Import the scientific classifier only inside the background worker."""
    from classifier.engine import classify_product, detect_attributes

    result = classify_product(product)
    category = None
    attrs = {}
    if result.best:
        category = TaxonomyCategory.objects.get(id=result.best.category_id)
        attrs = detect_attributes(product, category)

    Classification.objects.update_or_create(
        product=product,
        defaults={
            "category": category,
            "confidence": result.best.score if result.best else 0.0,
            "alternative_categories": [
                {"gid": item.gid, "full_name": item.full_name, "confidence": round(item.score, 4)}
                for item in result.alternatives
            ],
            "attributes": attrs,
            "needs_review": result.needs_review,
            "review_reasons": result.review_reasons,
        },
    )
    return result.needs_review


def _reset_progress_from_database(job):
    """Rebuild counters when resuming a job after an application restart."""
    done = job.products.filter(status=Product.STATUS_DONE).count()
    failed = job.products.filter(status=Product.STATUS_FAILED).count()
    job.total_products = job.products.count()
    job.processed_products = done + failed
    job.classified_products = done
    job.failed_products = failed
    job.needs_review_products = Classification.objects.filter(
        product__import_job=job, needs_review=True
    ).count()
    job.save(update_fields=[
        "total_products", "processed_products", "classified_products", "failed_products", "needs_review_products",
    ])


def process_job(job_id):
    """Classify a job incrementally, keeping only one product in memory."""
    close_old_connections()
    try:
        with transaction.atomic():
            job = ClassificationJob.objects.select_for_update().get(pk=job_id)
            if job.status in {ClassificationJob.STATUS_COMPLETED, ClassificationJob.STATUS_FAILED}:
                return
            job.status = ClassificationJob.STATUS_PROCESSING
            if not job.started_at:
                job.started_at = timezone.now()
            job.save(update_fields=["status", "started_at"])

        _reset_progress_from_database(job)
        while True:
            product = job.products.filter(
                status__in=[Product.STATUS_PENDING, Product.STATUS_PROCESSING]
            ).order_by("id").first()
            if product is None:
                break

            # The worker is intentionally single-process, but make the state
            # transition explicit so an interrupted job can be resumed.
            Product.objects.filter(pk=product.pk).update(status=Product.STATUS_PROCESSING)
            product.status = Product.STATUS_PROCESSING
            try:
                with transaction.atomic():
                    needs_review = _classify_product(product)
                    product.status = Product.STATUS_DONE
                    product.error_message = ""
                    product.save(update_fields=["status", "error_message"])
                _save_progress(job, classified=1, needs_review=int(needs_review))
            except Exception as exc:
                product.status = Product.STATUS_FAILED
                product.error_message = str(exc)[:2000]
                product.save(update_fields=["status", "error_message"])
                _save_progress(job, failed=1)

        job.status = ClassificationJob.STATUS_COMPLETED
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "completed_at"])
    except Exception as exc:
        ClassificationJob.objects.filter(pk=job_id).update(
            status=ClassificationJob.STATUS_FAILED,
            error_message=str(exc)[:2000],
            completed_at=timezone.now(),
        )
    finally:
        close_old_connections()
        global _running_job_id
        with _running_jobs_lock:
            _running_job_id = None

        # A second upload can wait in the database while this job runs. Start
        # it only after the current worker has released the classifier memory.
        next_job = ClassificationJob.objects.filter(
            status=ClassificationJob.STATUS_PENDING
        ).order_by("created_at").first()
        if next_job:
            start_job(next_job.id)
