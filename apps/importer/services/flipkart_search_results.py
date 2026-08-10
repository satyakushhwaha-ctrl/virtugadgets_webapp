"""Persistence workflow for Flipkart candidate discovery."""

from dataclasses import dataclass
import logging

from django.db import transaction

from ..models import AmazonProduct, FlipkartSearchResult, ImportStatus
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


def search_and_save_flipkart_candidates(
    amazon_product: AmazonProduct,
    candidate_limit: int = MAX_CANDIDATES_PER_PRODUCT,
) -> FlipkartSearchSummary:
    """Search Flipkart and persist at most the safe candidate maximum."""
    if amazon_product.status != ImportStatus.COMPLETED:
        raise ValueError(
            f"AmazonProduct {amazon_product.asin} is not completed."
        )
    if candidate_limit <= 0:
        raise ValueError("Candidate limit must be positive.")

    attempts = []
    candidates = []
    selected_query = ""
    for query in build_flipkart_search_queries(amazon_product):
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
    candidates_to_save = candidates[:min(candidate_limit, MAX_CANDIDATES_PER_PRODUCT)]
    saved = 0
    skipped_duplicates = 0

    with transaction.atomic():
        for candidate in candidates_to_save:
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
            if created:
                saved += 1
            else:
                skipped_duplicates += 1

    return FlipkartSearchSummary(
        query=selected_query,
        candidates_found=len(candidates),
        candidates_selected=len(candidates_to_save),
        saved=saved,
        skipped_duplicates=skipped_duplicates,
        attempts=tuple(attempts),
    )
