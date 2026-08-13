"""Orchestration for complete importer batches.

This module coordinates existing search, extraction, matching, and publishing
services. It intentionally contains no Playwright or marketplace logic.
"""

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import (
    AmazonProduct,
    BatchStatus,
    FlipkartProduct,
    FlipkartSearchResult,
    ImportBatch,
    ImportStatus,
)
from .amazon_product import process_amazon_search_result
from .amazon_search_results import run_amazon_search_for_keyword
from .flipkart_product import process_flipkart_search_result
from .flipkart_search_results import search_and_save_flipkart_candidates
from .product_matching import run_product_matching_for_batch
from .product_publisher import publish_product_match


@dataclass(frozen=True)
class BatchPublishSummary:
    successful: int
    failed: int
    skipped: int


def _set_status(batch, status, *, error=None):
    batch.status = status
    if error is not None:
        batch.error_message = error
    batch.save(update_fields=["status", "error_message", "updated_at"])


def _refresh_counters(batch, failed_items=0):
    batch.amazon_results_count = batch.amazon_search_results.count()
    batch.amazon_products_count = batch.amazon_products.filter(
        status=ImportStatus.COMPLETED,
    ).count()
    batch.flipkart_results_count = batch.flipkart_search_results.count()
    batch.flipkart_products_count = batch.flipkart_products.filter(
        status=ImportStatus.COMPLETED,
    ).count()
    batch.matches_count = batch.product_matches.count()
    failed_products = (
        batch.amazon_products.filter(status=ImportStatus.FAILED).count()
        + batch.flipkart_products.filter(status=ImportStatus.FAILED).count()
    )
    # Item-level service failures are also persisted as FAILED staging rows;
    # use the larger value so one failure is not counted twice.
    batch.failed_count = max(failed_products, failed_items)
    batch.successful_count = (
        batch.amazon_products_count
        + batch.flipkart_products_count
        + batch.matches_count
    )
    batch.save(
        update_fields=[
            "amazon_results_count", "amazon_products_count",
            "flipkart_results_count", "flipkart_products_count",
            "matches_count", "successful_count", "failed_count",
            "updated_at",
        ]
    )


def _ensure_not_cancelled(batch):
    batch.refresh_from_db(fields=["status"])
    if batch.status == BatchStatus.CANCELLED:
        raise RuntimeError("Import batch was cancelled.")


def run_batch(batch: ImportBatch) -> ImportBatch:
    """Run or safely resume all automated staging stages for one batch."""
    batch.refresh_from_db()
    if batch.status == BatchStatus.CANCELLED:
        raise ValueError("Cancelled batches cannot be run.")
    if batch.status == BatchStatus.COMPLETED:
        return batch
    if batch.status == BatchStatus.READY_FOR_REVIEW and batch.failed_count == 0:
        return batch

    if not batch.started_at:
        batch.started_at = timezone.now()
        batch.save(update_fields=["started_at", "updated_at"])
    _set_status(batch, BatchStatus.RUNNING, error="")
    errors = []
    failed_items = 0

    try:
        _ensure_not_cancelled(batch)
        if not batch.amazon_search_results.exists():
            _set_status(batch, BatchStatus.AMAZON_SEARCH)
            run_amazon_search_for_keyword(batch.keyword)
        for result in batch.keyword.amazon_results.all():
            result.batches.add(batch)
        _refresh_counters(batch)

        _set_status(batch, BatchStatus.AMAZON_EXTRACTION)
        for result in batch.amazon_search_results.select_related("keyword"):
            try:
                process_amazon_search_result(result)
                product = AmazonProduct.objects.get(asin=result.asin)
                product.batches.add(batch)
            except Exception as exc:
                failed_items += 1
                errors.append(f"Amazon {result.asin}: {exc}")
        _refresh_counters(batch, failed_items)

        _ensure_not_cancelled(batch)
        _set_status(batch, BatchStatus.FLIPKART_SEARCH)
        # Handle keyword-level Flipkart search results (from SearchKeyword admin action)
        keyword_results = batch.flipkart_search_results.filter(
            search_keyword=batch.keyword,
            amazon_product__isnull=True,
        )
        if keyword_results.exists() and not keyword_results.filter(processed=True).exists():
            # Keyword-level search was already run via admin action; just associate
            for result in keyword_results:
                result.batches.add(batch)
        else:
            # Product-level Flipkart search for each completed Amazon product
            for product in batch.amazon_products.filter(status=ImportStatus.COMPLETED):
                if not batch.flipkart_search_results.filter(amazon_product=product).exists():
                    try:
                        search_and_save_flipkart_candidates(product)
                    except Exception as exc:
                        failed_items += 1
                        errors.append(f"Flipkart search for {product.asin}: {exc}")
                        continue
                for result in product.flipkart_results.all():
                    result.batches.add(batch)
        _refresh_counters(batch, failed_items)

        _ensure_not_cancelled(batch)
        _set_status(batch, BatchStatus.FLIPKART_EXTRACTION)
        for result in batch.flipkart_search_results.select_related("amazon_product", "search_keyword"):
            try:
                process_flipkart_search_result(result)
                product = result.flipkart_product
                product.batches.add(batch)
            except Exception as exc:
                failed_items += 1
                errors.append(f"Flipkart {result.pid}: {exc}")
        _refresh_counters(batch, failed_items)

        _ensure_not_cancelled(batch)
        _set_status(batch, BatchStatus.MATCHING)
        match_summary = run_product_matching_for_batch(batch)
        failed_items += match_summary.failed
        _refresh_counters(batch, failed_items)

        batch.completed_at = timezone.now()
        batch.status = BatchStatus.READY_FOR_REVIEW
        batch.error_message = "\n".join(errors)
        batch.save(update_fields=["status", "error_message", "completed_at", "updated_at"])
        return batch
    except Exception as exc:
        if batch.status != BatchStatus.CANCELLED:
            batch.status = BatchStatus.FAILED
            batch.error_message = "\n".join(errors + [str(exc)])
            batch.save(update_fields=["status", "error_message", "updated_at"])
        raise


def cancel_batch(batch: ImportBatch):
    if batch.status in {BatchStatus.READY_FOR_REVIEW, BatchStatus.COMPLETED, BatchStatus.CANCELLED}:
        raise ValueError("This batch cannot be cancelled in its current state.")
    batch.status = BatchStatus.CANCELLED
    batch.completed_at = timezone.now()
    batch.save(update_fields=["status", "completed_at", "updated_at"])
    return batch


def publish_approved_products(batch: ImportBatch, user=None) -> BatchPublishSummary:
    """Publish only explicitly approved ProductMatch records for this batch."""
    successful = failed = skipped = 0
    for product_match in batch.product_matches.select_related(
        "amazon_product", "flipkart_product", "publish_category", "published_product"
    ).prefetch_related("publish_categories"):
        if product_match.match_status != "approved":
            skipped += 1
            continue
        try:
            publish_product_match(product_match, user=user)
            successful += 1
        except Exception:
            failed += 1
    if failed:
        batch.error_message = f"{failed} approved product(s) failed to publish."
    batch.status = BatchStatus.COMPLETED if not failed else BatchStatus.READY_FOR_REVIEW
    batch.save(update_fields=["status", "error_message", "updated_at"])
    return BatchPublishSummary(successful, failed, skipped)
