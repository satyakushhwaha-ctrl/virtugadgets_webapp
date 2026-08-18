"""Queue-safe manual product refresh orchestration."""

from django.db import transaction
from django.db.models import Q

from ..models import (
    AmazonProduct,
    AmazonSearchResult,
    FlipkartProduct,
    ImporterJob,
    ImporterJobMarketplace,
    ImporterJobStatus,
    ImporterJobType,
)
from .jobs import create_job, enqueue_job


ACTIVE_STATUSES = (
    ImporterJobStatus.PENDING,
    ImporterJobStatus.QUEUED,
    ImporterJobStatus.RUNNING,
)


def queue_amazon_product_refresh(*, product: AmazonProduct, task, user=None):
    result = AmazonSearchResult.objects.filter(asin=product.asin).order_by("-created_at").first()
    if not result:
        return None, "Unable to queue refresh: no Amazon search result exists for this ASIN."
    with transaction.atomic():
        AmazonProduct.objects.select_for_update().get(pk=product.pk)
        active = ImporterJob.objects.filter(
            job_type=ImporterJobType.AMAZON_PRODUCT_EXTRACTION,
            status__in=ACTIVE_STATUSES,
        ).filter(
            Q(amazon_product=product)
            | Q(metadata__search_result_id=str(result.pk))
        ).first()
        if active:
            return active, "Amazon product extraction is already running."
        job = create_job(
            title=f"Amazon Product Refresh — {product.asin}",
            job_type=ImporterJobType.AMAZON_PRODUCT_EXTRACTION,
            marketplace=ImporterJobMarketplace.AMAZON,
            created_by=user,
            amazon_product=product,
        )
        return enqueue_job(task=task, job=job, args=(str(result.pk),)), None


def queue_flipkart_product_refresh(*, product: FlipkartProduct, task, user=None):
    if not product.search_result_id:
        return None, "Unable to queue refresh: no Flipkart search result exists for this product."
    with transaction.atomic():
        FlipkartProduct.objects.select_for_update().get(pk=product.pk)
        active = ImporterJob.objects.filter(
            job_type=ImporterJobType.FLIPKART_PRODUCT_EXTRACTION,
            status__in=ACTIVE_STATUSES,
        ).filter(
            Q(flipkart_product=product)
            | Q(metadata__search_result_id=str(product.search_result_id))
        ).first()
        if active:
            return active, "Flipkart product extraction is already running."
        job = create_job(
            title=f"Flipkart Product Refresh — {product.pid}",
            job_type=ImporterJobType.FLIPKART_PRODUCT_EXTRACTION,
            marketplace=ImporterJobMarketplace.FLIPKART,
            created_by=user,
            flipkart_product=product,
        )
        return enqueue_job(
            task=task,
            job=job,
            args=(str(product.search_result_id),),
        ), None
