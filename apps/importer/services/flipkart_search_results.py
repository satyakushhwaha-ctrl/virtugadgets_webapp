"""Persistence workflow for Flipkart candidate discovery."""

from dataclasses import dataclass
import logging
from urllib.parse import quote_plus

from django.db import transaction
from django.utils import timezone

from ..models import (
    AmazonProduct,
    BatchStatus,
    FlipkartSearchResult,
    ImportBatch,
    ImportStatus,
    SearchKeyword,
)
from .flipkart_search import build_flipkart_search_queries, search_flipkart


MAX_CANDIDATES_PER_PRODUCT = 10
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FlipkartSearchAttempt:
    query: str
    candidates_found: int


@dataclass(frozen=True)
class FlipkartSearchSummary:
    query: str
    candidates_found: int
    candidates_selected: int
    saved: int
    skipped_duplicates: int
    attempts: tuple[FlipkartSearchAttempt, ...] = ()
    candidate_pids: tuple[str, ...] = ()


@dataclass(frozen=True)
class KeywordFlipkartSearchSummary:
    amazon_products_total: int
    successful: int
    failed: int
    skipped: int
    candidates_found: int
    saved: int
    skipped_duplicates: int
    failed_products: tuple[str, ...] = ()


def search_and_save_flipkart_candidates(
    amazon_product: AmazonProduct,
    candidate_limit: int | None = MAX_CANDIDATES_PER_PRODUCT,
    require_completed: bool = True,
) -> FlipkartSearchSummary:
    """Search Flipkart and persist at most the safe candidate maximum."""
    if require_completed and amazon_product.status != ImportStatus.COMPLETED:
        raise ValueError(
            f"AmazonProduct {amazon_product.asin} is not completed."
        )
    if candidate_limit is not None and candidate_limit <= 0:
        raise ValueError("Candidate limit must be positive.")

    attempts = []
    candidates = []
    selected_query = ""
    logger.info(
        "Starting Flipkart search for Amazon product %s with keyword identity: %s",
        amazon_product.asin,
        amazon_product.product_title,
    )
    for query in build_flipkart_search_queries(amazon_product):
        logger.info("Opening Flipkart search URL for query: %s", query)
        candidates = search_flipkart(query)
        attempt = FlipkartSearchAttempt(query, len(candidates))
        attempts.append(attempt)
        logger.info(
            "Flipkart search attempt %d: %s; candidates: %d",
            len(attempts),
            query,
            len(candidates),
        )
        if candidates:
            selected_query = query
            break
    if not selected_query:
        selected_query = attempts[-1].query
    return _save_flipkart_candidates(
        amazon_product=amazon_product,
        candidates=candidates,
        selected_query=selected_query,
        candidate_limit=candidate_limit,
        attempts=tuple(attempts),
    )


def search_and_save_flipkart_candidates_for_amazon_product(
    amazon_product: AmazonProduct,
    keyword: str,
    candidate_limit: int = MAX_CANDIDATES_PER_PRODUCT,
) -> FlipkartSearchSummary:
    """Run an explicit admin keyword search and attach results to an Amazon record."""
    if amazon_product.status != ImportStatus.COMPLETED:
        raise ValueError(f"AmazonProduct {amazon_product.asin} is not completed.")
    query = (keyword or "").strip()
    if not query:
        raise ValueError("Flipkart search keyword cannot be empty.")
    candidates = search_flipkart(query)
    return _save_flipkart_candidates(
        amazon_product=amazon_product,
        candidates=candidates,
        selected_query=query,
        candidate_limit=candidate_limit,
        attempts=(FlipkartSearchAttempt(query, len(candidates)),),
    )


def _save_flipkart_candidates(
    *,
    amazon_product: AmazonProduct | None = None,
    search_keyword: SearchKeyword | None = None,
    candidates: list[dict],
    selected_query: str,
    candidate_limit: int | None = MAX_CANDIDATES_PER_PRODUCT,
    attempts: tuple[FlipkartSearchAttempt, ...] = (),
) -> FlipkartSearchSummary:
    if not amazon_product and not search_keyword:
        raise ValueError("Either amazon_product or search_keyword must be provided.")
    if amazon_product and search_keyword:
        raise ValueError("Provide either amazon_product or search_keyword, not both.")

    candidates_to_save = (
        candidates
        if candidate_limit is None
        else candidates[:min(candidate_limit, MAX_CANDIDATES_PER_PRODUCT)]
    )
    saved = 0
    skipped_duplicates = 0

    with transaction.atomic():
        for candidate in candidates_to_save:
            if amazon_product:
                _, created = FlipkartSearchResult.objects.get_or_create(
                    amazon_product=amazon_product,
                    pid=candidate["pid"],
                    defaults={
                        "title": candidate["title"],
                        "product_url": candidate["product_url"],
                        "position": candidate["position"],
                        "sponsored": candidate["sponsored"],
                    },
                )
            else:
                _, created = FlipkartSearchResult.objects.get_or_create(
                    search_keyword=search_keyword,
                    pid=candidate["pid"],
                    defaults={
                        "title": candidate["title"],
                        "product_url": candidate["product_url"],
                        "position": candidate["position"],
                        "sponsored": candidate["sponsored"],
                    },
                )
            if created:
                saved += 1
            else:
                skipped_duplicates += 1

    owner = amazon_product.asin if amazon_product else search_keyword.keyword
    logger.info(
        "Flipkart search completed for %s; candidates found: %d; saved: %d",
        owner,
        len(candidates),
        saved,
    )
    return FlipkartSearchSummary(
        query=selected_query,
        candidates_found=len(candidates),
        candidates_selected=len(candidates_to_save),
        saved=saved,
        skipped_duplicates=skipped_duplicates,
        attempts=attempts,
        candidate_pids=tuple(candidate["pid"] for candidate in candidates_to_save),
    )


def _keyword_amazon_products(search_keyword: SearchKeyword) -> list[AmazonProduct]:
    return list(
        AmazonProduct.objects.filter(
            asin__in=search_keyword.amazon_results.values("asin"),
            status=ImportStatus.COMPLETED,
        ).order_by("updated_at", "asin")
    )


def _keyword_flipkart_results_count(search_keyword: SearchKeyword) -> int:
    return FlipkartSearchResult.objects.filter(
        search_keyword=search_keyword,
    ).distinct().count()


def search_and_save_flipkart_candidates_for_keyword(
    search_keyword: SearchKeyword,
    candidate_limit: int = MAX_CANDIDATES_PER_PRODUCT,
) -> FlipkartSearchSummary:
    if not search_keyword.keyword or not search_keyword.keyword.strip():
        raise ValueError("SearchKeyword keyword cannot be empty for Flipkart search.")
    if candidate_limit <= 0:
        raise ValueError("Candidate limit must be positive.")

    query = search_keyword.keyword.strip()
    logger.info(
        "Starting Flipkart keyword search for %s using query: %s",
        search_keyword.keyword,
        query,
    )
    logger.info("Opening Flipkart search URL for keyword query: %s", query)
    candidates = search_flipkart(query)
    return _save_flipkart_candidates(
        search_keyword=search_keyword,
        candidates=candidates,
        selected_query=query,
        candidate_limit=candidate_limit,
        attempts=(FlipkartSearchAttempt(query, len(candidates)),),
    )


def _create_flipkart_search_batch(
    search_keyword: SearchKeyword,
    amazon_products_total: int,
) -> ImportBatch:
    return ImportBatch.objects.create(
        keyword=search_keyword,
        status=BatchStatus.FLIPKART_SEARCH,
        amazon_products_count=amazon_products_total,
        successful_count=0,
        failed_count=0,
        flipkart_results_count=_keyword_flipkart_results_count(search_keyword),
        started_at=timezone.now(),
        error_message="",
    )


def _update_flipkart_search_batch(
    batch: ImportBatch,
    search_keyword: SearchKeyword,
    *,
    successful: int,
    failed: int,
    error_message: str = "",
) -> None:
    batch.successful_count = successful
    batch.failed_count = failed
    batch.flipkart_results_count = _keyword_flipkart_results_count(search_keyword)
    batch.error_message = error_message
    batch.save(
        update_fields=[
            "successful_count",
            "failed_count",
            "flipkart_results_count",
            "error_message",
            "updated_at",
        ]
    )


def run_flipkart_search_for_keyword(
    search_keyword: SearchKeyword,
) -> KeywordFlipkartSearchSummary:
    """Run candidate discovery for one keyword using the existing Flipkart scraper."""
    logger.info("Starting Flipkart search for keyword: %s", search_keyword.keyword)
    batch = _create_flipkart_search_batch(search_keyword, 1)

    try:
        summary = search_and_save_flipkart_candidates_for_keyword(search_keyword)
        for result in FlipkartSearchResult.objects.filter(
            search_keyword=search_keyword,
        ):
            result.batches.add(batch)
        _update_flipkart_search_batch(
            batch,
            search_keyword,
            successful=1,
            failed=0,
        )
        batch.status = BatchStatus.COMPLETED
        batch.completed_at = timezone.now()
        batch.error_message = ""
        batch.save(
            update_fields=["status", "completed_at", "error_message", "updated_at"]
        )
        logger.info(
            "Flipkart search completed for keyword %s; keyword queries searched: 1/1; candidates found: %d",
            search_keyword.keyword,
            _keyword_flipkart_results_count(search_keyword),
        )
        return KeywordFlipkartSearchSummary(
            amazon_products_total=1,
            successful=1,
            failed=0,
            skipped=0,
            candidates_found=summary.candidates_found,
            saved=summary.saved,
            skipped_duplicates=summary.skipped_duplicates,
            failed_products=(),
        )
    except Exception as exc:
        batch.status = BatchStatus.FAILED
        batch.completed_at = timezone.now()
        batch.error_message = f"Flipkart search failed: {exc}"
        batch.successful_count = 0
        batch.failed_count = 1
        batch.save(
            update_fields=[
                "status",
                "completed_at",
                "error_message",
                "successful_count",
                "failed_count",
                "updated_at",
            ]
        )
        logger.exception(
            "Flipkart search aborted unexpectedly for keyword %s",
            search_keyword.keyword,
        )
        return KeywordFlipkartSearchSummary(
            amazon_products_total=1,
            successful=0,
            failed=1,
            skipped=0,
            candidates_found=0,
            saved=0,
            skipped_duplicates=0,
            failed_products=(str(exc),),
        )
