import logging

from celery import shared_task


logger = logging.getLogger(__name__)
_RETRYABLE_EXCEPTIONS = (TimeoutError, ConnectionError, OSError)


def _job_for_task(job_id, *, title, job_type, marketplace="", total_items=1,
                  amazon_product=None, flipkart_product=None):
    from apps.importer.services.jobs import get_or_create_task_job

    return get_or_create_task_job(
        job_id=job_id,
        title=title,
        job_type=job_type,
        marketplace=marketplace,
        total_items=total_items,
        amazon_product=amazon_product,
        flipkart_product=flipkart_product,
    )


def _start_job(job):
    from apps.importer.services.jobs import mark_job_running

    return mark_job_running(job)


def _handle_failure(task, job, exc, operation):
    from apps.importer.services.jobs import increment_job_retry, mark_job_failed, mark_job_queued

    if isinstance(exc, _RETRYABLE_EXCEPTIONS) and task.request.retries < 2:
        increment_job_retry(job)
        mark_job_queued(job, task.request.id or job.celery_task_id, force=True)
        logger.warning("%s failed transiently; retrying.", operation, exc_info=True)
        raise task.retry(exc=exc, countdown=2 ** task.request.retries)
    mark_job_failed(job, exc)
    logger.exception("%s failed permanently.", operation)
    raise exc


def _missing_job(job_id, *, title, job_type, marketplace, reason):
    from apps.importer.models import ImporterJob
    from apps.importer.services.jobs import create_job, mark_job_failed, mark_job_running

    job = ImporterJob.objects.filter(pk=job_id).first() if job_id else None
    job = job or create_job(title=title, job_type=job_type, marketplace=marketplace)
    mark_job_running(job)
    mark_job_failed(job, reason)
    return {"status": "failed", "reason": reason, "job_id": str(job.pk)}


@shared_task
def celery_health_check():
    """Return a deterministic value for verifying Celery task execution."""
    return "ok"


@shared_task(bind=True, name="apps.core.tasks.amazon_search_task")
def amazon_search_task(self, search_keyword_id, job_id=None):
    from apps.importer.models import ImporterJobMarketplace, ImporterJobType, SearchKeyword
    from apps.importer.services.jobs import mark_job_completed, mark_job_failed, update_job_progress
    from apps.importer.services.amazon_search_results import run_amazon_search_for_keyword

    try:
        keyword = SearchKeyword.objects.get(pk=search_keyword_id)
    except SearchKeyword.DoesNotExist:
        return _missing_job(job_id, title=f"Amazon Search — {search_keyword_id}", job_type=ImporterJobType.AMAZON_SEARCH, marketplace=ImporterJobMarketplace.AMAZON, reason="SearchKeyword not found.")
    job = _job_for_task(job_id, title=f"Amazon Search — {keyword.keyword}", job_type=ImporterJobType.AMAZON_SEARCH, marketplace=ImporterJobMarketplace.AMAZON)
    _start_job(job)
    try:
        summary = run_amazon_search_for_keyword(keyword)
        update_job_progress(job, processed_items=1, success_count=1, result_message=f"Search returned {summary.results_found} results; saved {summary.saved}.")
        mark_job_completed(job, job.result_message)
        return {"status": "completed", "id": str(keyword.pk), "job_id": str(job.pk), "results_found": summary.results_found, "saved": summary.saved}
    except Exception as exc:
        _handle_failure(self, job, exc, f"Amazon search for {keyword.keyword}")


@shared_task(bind=True, name="apps.core.tasks.amazon_product_extraction_task")
def amazon_product_extraction_task(self, search_result_id, job_id=None):
    from apps.importer.models import AmazonSearchResult, ImporterJobMarketplace, ImporterJobType
    from apps.importer.services.jobs import mark_job_completed, mark_job_skipped, update_job_progress
    from apps.importer.services.amazon_product import process_amazon_search_result

    try:
        result = AmazonSearchResult.objects.select_related("keyword").get(pk=search_result_id)
    except AmazonSearchResult.DoesNotExist:
        return _missing_job(job_id, title=f"Amazon Product Extraction — {search_result_id}", job_type=ImporterJobType.AMAZON_PRODUCT_EXTRACTION, marketplace=ImporterJobMarketplace.AMAZON, reason="AmazonSearchResult not found.")
    job = _job_for_task(job_id, title=f"Amazon Product Extraction — {result.asin}", job_type=ImporterJobType.AMAZON_PRODUCT_EXTRACTION, marketplace=ImporterJobMarketplace.AMAZON)
    _start_job(job)
    try:
        processed = process_amazon_search_result(result)
        update_job_progress(job, processed_items=1, success_count=int(bool(processed)), skipped_count=int(not processed))
        if processed:
            mark_job_completed(job, f"Amazon product {result.asin} extracted successfully.")
        else:
            mark_job_skipped(job, f"Amazon product {result.asin} was skipped.")
        return {"status": "completed" if processed else "skipped", "id": str(result.pk), "job_id": str(job.pk)}
    except Exception as exc:
        _handle_failure(self, job, exc, f"Amazon product extraction for {result.asin}")


@shared_task(bind=True, name="apps.core.tasks.flipkart_search_task")
def flipkart_search_task(self, search_keyword_id, job_id=None):
    from apps.importer.models import ImporterJobMarketplace, ImporterJobType, SearchKeyword
    from apps.importer.services.jobs import mark_job_completed, mark_job_partial, update_job_progress
    from apps.importer.services.flipkart_search_results import run_flipkart_search_for_keyword

    try:
        keyword = SearchKeyword.objects.get(pk=search_keyword_id)
    except SearchKeyword.DoesNotExist:
        return _missing_job(job_id, title=f"Flipkart Search — {search_keyword_id}", job_type=ImporterJobType.FLIPKART_SEARCH, marketplace=ImporterJobMarketplace.FLIPKART, reason="SearchKeyword not found.")
    job = _job_for_task(job_id, title=f"Flipkart Search — {keyword.keyword}", job_type=ImporterJobType.FLIPKART_SEARCH, marketplace=ImporterJobMarketplace.FLIPKART)
    _start_job(job)
    try:
        summary = run_flipkart_search_for_keyword(keyword)
        failed = int(bool(summary.failed))
        update_job_progress(job, processed_items=1, success_count=int(not failed), failed_count=failed, result_message=f"Flipkart search returned {summary.candidates_found} candidates; saved {summary.saved}.")
        if failed:
            mark_job_partial(job, job.result_message)
        else:
            mark_job_completed(job, job.result_message)
        return {"status": "partial" if failed else "completed", "id": str(keyword.pk), "job_id": str(job.pk), "candidates_found": summary.candidates_found, "saved": summary.saved}
    except Exception as exc:
        _handle_failure(self, job, exc, f"Flipkart search for {keyword.keyword}")


@shared_task(bind=True, name="apps.core.tasks.flipkart_product_extraction_task")
def flipkart_product_extraction_task(self, search_result_id, job_id=None):
    from apps.importer.models import FlipkartSearchResult, ImporterJobMarketplace, ImporterJobType
    from apps.importer.services.jobs import mark_job_completed, mark_job_skipped, update_job_progress
    from apps.importer.services.flipkart_product import process_flipkart_search_result

    try:
        result = FlipkartSearchResult.objects.select_related("amazon_product").get(pk=search_result_id)
    except FlipkartSearchResult.DoesNotExist:
        return _missing_job(job_id, title=f"Flipkart Product Extraction — {search_result_id}", job_type=ImporterJobType.FLIPKART_PRODUCT_EXTRACTION, marketplace=ImporterJobMarketplace.FLIPKART, reason="FlipkartSearchResult not found.")
    job = _job_for_task(job_id, title=f"Flipkart Product Extraction — PID {result.pid}", job_type=ImporterJobType.FLIPKART_PRODUCT_EXTRACTION, marketplace=ImporterJobMarketplace.FLIPKART)
    _start_job(job)
    try:
        processed = process_flipkart_search_result(result)
        update_job_progress(job, processed_items=1, success_count=int(bool(processed)), skipped_count=int(not processed))
        if processed:
            mark_job_completed(job, f"Flipkart PID {result.pid} extracted successfully.")
        else:
            mark_job_skipped(job, f"Flipkart PID {result.pid} was skipped.")
        return {"status": "completed" if processed else "skipped", "id": str(result.pk), "pid": result.pid, "job_id": str(job.pk)}
    except Exception as exc:
        _handle_failure(self, job, exc, f"Flipkart product extraction for PID {result.pid}")


@shared_task(bind=True, name="apps.core.tasks.flipkart_product_search_task")
def flipkart_product_search_task(self, amazon_product_id, keyword=None, job_id=None):
    from apps.importer.models import AmazonProduct, ImporterJobMarketplace, ImporterJobType
    from apps.importer.services.jobs import mark_job_completed, update_job_progress
    from apps.importer.services.flipkart_search_results import search_and_save_flipkart_candidates, search_and_save_flipkart_candidates_for_amazon_product

    try:
        product = AmazonProduct.objects.get(pk=amazon_product_id)
    except AmazonProduct.DoesNotExist:
        return _missing_job(job_id, title=f"Flipkart Search — {amazon_product_id}", job_type=ImporterJobType.FLIPKART_SEARCH, marketplace=ImporterJobMarketplace.FLIPKART, reason="AmazonProduct not found.")
    job = _job_for_task(job_id, title=f"Flipkart Search — {product.product_title or product.asin}", job_type=ImporterJobType.FLIPKART_SEARCH, marketplace=ImporterJobMarketplace.FLIPKART, amazon_product=product)
    _start_job(job)
    try:
        summary = search_and_save_flipkart_candidates_for_amazon_product(product, keyword) if keyword else search_and_save_flipkart_candidates(product)
        update_job_progress(job, processed_items=1, success_count=1, result_message=f"Flipkart search returned {summary.candidates_found} candidates; saved {summary.saved}.")
        mark_job_completed(job, job.result_message)
        return {"status": "completed", "id": str(product.pk), "job_id": str(job.pk), "candidates_found": summary.candidates_found, "saved": summary.saved}
    except Exception as exc:
        _handle_failure(self, job, exc, f"Flipkart search for {product.asin}")


@shared_task(bind=True, name="apps.core.tasks.extract_best_matched_flipkart_product")
def extract_best_matched_flipkart_product(self, amazon_product_id, job_id=None):
    from apps.importer.models import AmazonProduct, ImporterJobMarketplace, ImporterJobType, ImportStatus
    from apps.importer.services.jobs import mark_job_completed, mark_job_failed, mark_job_skipped, update_job_progress
    from apps.importer.services.best_flipkart_match import extract_best_matched_flipkart_product as run_best_match

    try:
        amazon_product = AmazonProduct.objects.get(pk=amazon_product_id)
    except AmazonProduct.DoesNotExist:
        return _missing_job(job_id, title=f"Best Match Flipkart — {amazon_product_id}", job_type=ImporterJobType.BEST_MATCH_FLIPKART, marketplace=ImporterJobMarketplace.FLIPKART, reason="AmazonProduct not found.")
    job = _job_for_task(job_id, title=f"Best Match Flipkart — {amazon_product.asin}", job_type=ImporterJobType.BEST_MATCH_FLIPKART, marketplace=ImporterJobMarketplace.FLIPKART, amazon_product=amazon_product)
    _start_job(job)
    previous_status = amazon_product.status
    amazon_product.status = ImportStatus.RUNNING
    amazon_product.error_message = ""
    amazon_product.save(update_fields=["status", "error_message", "updated_at"])
    try:
        result = run_best_match(amazon_product)
        match = result.get("match") or {}
        candidate_count = amazon_product.flipkart_results.count()
        evaluated_message = f"Flipkart search returned {candidate_count} candidates. {candidate_count} candidates evaluated."
        if result.get("status") == "skipped":
            best_score = match.get("score") if isinstance(match, dict) else None
            reason = result.get("reason", "No sufficiently matched Flipkart candidate.")
            if best_score is not None:
                reason = f"{reason} Best score: {best_score}."
            update_job_progress(job, total_items=candidate_count, processed_items=candidate_count, skipped_count=1, result_message=f"{evaluated_message} {reason}")
            mark_job_skipped(job, job.result_message)
        else:
            candidate = match.get("candidate") if isinstance(match, dict) else None
            pid = candidate.pid if candidate else ""
            score = match.get("score", "") if isinstance(match, dict) else ""
            flipkart_product = result.get("flipkart_product")
            if flipkart_product:
                job.flipkart_product = flipkart_product
                job.product_match = flipkart_product.product_matches.filter(amazon_product=amazon_product).first()
                job.save(update_fields=["flipkart_product", "product_match", "updated_at"])
            update_job_progress(job, total_items=max(candidate_count, 1), processed_items=max(candidate_count, 1), success_count=1, result_message=f"Completed. Flipkart PID {pid} extracted successfully with score {score}.")
            mark_job_completed(job, job.result_message)
        if amazon_product.status != previous_status:
            amazon_product.status = previous_status
            amazon_product.error_message = ""
            amazon_product.save(update_fields=["status", "error_message", "updated_at"])
        return {"status": result.get("status"), "reason": result.get("reason", ""), "amazon_product_id": str(amazon_product.pk), "asin": amazon_product.asin, "job_id": str(job.pk), "pid": result.get("flipkart_product").pid if result.get("flipkart_product") else ""}
    except Exception as exc:
        amazon_product.status = ImportStatus.FAILED
        amazon_product.error_message = str(exc) or exc.__class__.__name__
        amazon_product.save(update_fields=["status", "error_message", "updated_at"])
        _handle_failure(self, job, exc, f"Best-match extraction for {amazon_product.asin}")
