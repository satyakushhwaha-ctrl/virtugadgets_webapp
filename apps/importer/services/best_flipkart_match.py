"""Extract only the strongest existing Flipkart search candidate."""

import logging

from django.db import transaction

from ..models import MatchConfidence, MatchStatus, ProductMatch, SearchKeyword
from .flipkart_product import process_flipkart_search_result
from .flipkart_search_results import search_and_save_flipkart_candidates
from .product_matching import rank_flipkart_search_results


logger = logging.getLogger(__name__)


def extract_best_matched_flipkart_product(amazon_product) -> dict:
    """Search, rank returned candidates, and extract at most one candidate."""
    original_keyword = (
        SearchKeyword.objects.filter(amazon_results__asin=amazon_product.asin)
        .values_list("keyword", flat=True)
        .first()
        or "NONE"
    )
    search_summary = search_and_save_flipkart_candidates(
        amazon_product,
        candidate_limit=None,
        require_completed=False,
    )
    attempts = getattr(search_summary, "attempts", ())
    fallback_query = (
        attempts[-1].query
        if len(attempts) > 1
        else "NONE"
    )
    logger.info(
        "Best-match Flipkart search: amazon=%s original_keyword=%s "
        "optimized_query=%s fallback_query=%s candidates_found=%s",
        amazon_product.product_title[:160],
        original_keyword,
        attempts[0].query if attempts else getattr(search_summary, "query", "NONE"),
        fallback_query,
        getattr(search_summary, "candidates_found", len(getattr(search_summary, "candidate_pids", ()))),
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
    matched = best["signals"]["matched"]
    logger.info(
        "Flipkart match decision: amazon=%s flipkart=%s score=%s confidence=%s "
        "model_match=%s cpu_match=%s gpu_match=%s ram_match=%s storage_match=%s "
        "conflicts=%s decision=%s",
        amazon_product.product_title[:160],
        best["candidate"].title[:160],
        best["score"],
        best["confidence"],
        bool(matched.get("model")),
        bool(matched.get("cpu")),
        bool(matched.get("gpu")),
        bool(matched.get("ram")),
        bool(matched.get("storage")),
        best["signals"]["conflicts"] or "NONE",
        best["match_status"],
    )
    exact_model = bool(best["signals"]["matched"].get("model"))
    hard_conflicts = bool(best["signals"]["conflicts"])
    next_score = ranked[1]["score"] if len(ranked) > 1 else None
    medium_identity_match = (
        best["confidence"] == MatchConfidence.MEDIUM
        and exact_model
        and not hard_conflicts
        and best["score"] >= 70
        and (next_score is None or best["score"] - next_score >= 5)
    )
    if best["confidence"] != MatchConfidence.HIGH and not medium_identity_match:
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
        # A previous matcher run may have left a non-workflow match for this
        # Amazon product. Keep the audit record, but ensure only the selected
        # candidate remains an active match. Approved/published decisions are
        # workflow-owned and must never be overwritten here.
        ProductMatch.objects.filter(
            amazon_product=amazon_product,
        ).exclude(
            flipkart_product=flipkart_product,
        ).exclude(
            match_status__in={MatchStatus.APPROVED, MatchStatus.PUBLISHED},
        ).update(match_status=MatchStatus.REJECTED)
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
