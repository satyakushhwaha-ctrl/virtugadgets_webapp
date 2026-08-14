"""Extract only the strongest existing Flipkart search candidate."""

import logging

from django.db import transaction

from ..models import MatchConfidence, MatchStatus, ProductMatch
from .flipkart_product import process_flipkart_search_result
from .flipkart_search_results import search_and_save_flipkart_candidates
from .product_matching import rank_flipkart_search_results


logger = logging.getLogger(__name__)


def extract_best_matched_flipkart_product(amazon_product) -> dict:
    """Search, rank returned candidates, and extract at most one candidate."""
    search_summary = search_and_save_flipkart_candidates(
        amazon_product,
        candidate_limit=None,
        require_completed=False,
    )
    candidates = list(
        amazon_product.flipkart_results.filter(
            pid__in=search_summary.candidate_pids,
        )
    )
    if not candidates:
        return {"status": "skipped", "reason": "no Flipkart search candidates available."}

    ranked = rank_flipkart_search_results(amazon_product, candidates)
    for result in ranked:
        signals = result["signals"]
        logger.info(
            "Flipkart candidate match: asin=%s pid=%s title=%s "
            "normalized_amazon=%s normalized_flipkart=%s attributes=%s "
            "matched=%s conflicts=%s component_scores=%s score=%s confidence=%s reasons=%s",
            amazon_product.asin,
            result["candidate"].pid,
            result["candidate"].title,
            signals["normalized_amazon_title"],
            signals["normalized_flipkart_title"],
            {
                "amazon": signals["amazon_attributes"],
                "flipkart": signals["flipkart_attributes"],
            },
            signals["matched"],
            signals["conflicts"],
            signals["component_scores"],
            result["score"],
            result["confidence"],
            "; ".join(result["reasons"]),
        )
    best = ranked[0]
    logger.info(
        "Best Flipkart candidate: asin=%s pid=%s score=%s confidence=%s",
        amazon_product.asin,
        best["candidate"].pid,
        best["score"],
        best["confidence"],
    )
    if best["confidence"] != MatchConfidence.HIGH:
        return {
            "status": "skipped",
            "reason": "no sufficiently matched Flipkart candidate.",
            "match": best,
        }

    search_result = best["candidate"]
    # This is the only call to the detail extractor in this workflow.
    process_flipkart_search_result(search_result)
    flipkart_product = search_result.flipkart_product
    match_defaults = {
        "score": best["score"],
        "confidence": best["confidence"],
        "reasons": {
            "summary": best["reasons"],
            **best["signals"],
        },
        "match_status": best["match_status"],
    }
    with transaction.atomic():
        existing = ProductMatch.objects.filter(
            amazon_product=amazon_product,
            flipkart_product=flipkart_product,
        ).first()
        if existing and existing.match_status in {
            MatchStatus.APPROVED,
            MatchStatus.PUBLISHED,
        }:
            match_defaults["match_status"] = existing.match_status
        ProductMatch.objects.update_or_create(
            amazon_product=amazon_product,
            flipkart_product=flipkart_product,
            defaults=match_defaults,
        )

    return {
        "status": "extracted",
        "match": best,
        "flipkart_product": flipkart_product,
    }
