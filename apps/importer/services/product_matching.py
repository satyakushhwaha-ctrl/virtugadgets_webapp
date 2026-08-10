"""Deterministic matching of Amazon and Flipkart staging products."""

from dataclasses import dataclass
import re
import unicodedata

from django.db import transaction

from ..models import (
    AmazonProduct,
    FlipkartProduct,
    ImportStatus,
    ImportBatch,
    MatchConfidence,
    MatchStatus,
    ProductMatch,
)


WEIGHTS = {
    "brand": 20,
    "model": 35,
    "storage": 15,
    "ram": 10,
    "color": 5,
    "processor": 5,
    "display": 5,
    "other_specs": 5,
}


@dataclass(frozen=True)
class KeywordMatchingSummary:
    amazon_products: int
    flipkart_products: int
    matches_created: int
    matches_updated: int
    high_confidence: int
    medium_confidence: int
    low_confidence: int
    no_candidate: int
    failed: int

CAPACITY_PATTERN = re.compile(
    r"(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>gb|tb)"
    r"(?:\s*(?:ram|rom|storage))?",
    re.IGNORECASE,
)


def normalize_text(value: str | None) -> str:
    """Normalize case, punctuation, whitespace, and Unicode variants."""
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def normalize_capacity(value: str | None) -> str:
    """Normalize values such as 256GB, 256 GB ROM, and 256 gb."""
    match = CAPACITY_PATTERN.search(str(value or ""))
    if not match:
        return normalize_text(value)
    amount = match.group("amount")
    if amount.endswith(".0"):
        amount = amount[:-2]
    return f"{amount} {match.group('unit').casefold()}"


def normalize_color(value: str | None) -> str:
    value = normalize_text(value)
    aliases = {
        "space black": "black",
        "jet black": "black",
        "light-gold": "light gold",
    }
    return aliases.get(value, value)


def extract_model_identity(product) -> str:
    """Extract a conservative model identity from the product title."""
    title = " ".join((product.product_title or "").split())
    title = re.sub(r"\([^)]*\)", "", title)
    title = re.split(r"\s*[|:;–—]\s*", title, maxsplit=1)[0]
    title = re.split(r"\s+with\s+", title, maxsplit=1, flags=re.IGNORECASE)[0]
    brand = " ".join((product.brand or "").split())
    if brand and title.casefold().startswith(brand.casefold()):
        title = title[len(brand):]

    title = CAPACITY_PATTERN.sub(" ", title)
    color = " ".join((product.color or "").split())
    if color:
        title = re.sub(rf"\b{re.escape(color)}\b", " ", title, flags=re.IGNORECASE)
    return normalize_text(title)


def _field_value(product, field: str) -> str:
    value = getattr(product, field, "")
    if field in {"storage", "ram"}:
        return normalize_capacity(value)
    if field == "color":
        return normalize_color(value)
    return normalize_text(value)


def _comparison(amazon_product, flipkart_product, key, field, hard=False):
    if key == "model":
        amazon_value = extract_model_identity(amazon_product)
        flipkart_value = extract_model_identity(flipkart_product)
    else:
        amazon_value = _field_value(amazon_product, field)
        flipkart_value = _field_value(flipkart_product, field)

    available = bool(amazon_value and flipkart_value)
    matched = available and amazon_value == flipkart_value
    mismatch = available and not matched
    return {
        "matched": matched if available else None,
        "available": available,
        "score": WEIGHTS[key] if matched else 0,
        "amazon": amazon_value,
        "flipkart": flipkart_value,
        "hard_mismatch": bool(hard and mismatch),
    }


def match_products(amazon_product, flipkart_product) -> dict:
    """Return a deterministic match decision without database writes."""
    comparisons = {
        "brand": _comparison(amazon_product, flipkart_product, "brand", "brand", True),
        "model": _comparison(amazon_product, flipkart_product, "model", "product_title", True),
        "storage": _comparison(amazon_product, flipkart_product, "storage", "storage", True),
        "ram": _comparison(amazon_product, flipkart_product, "ram", "ram", True),
        "color": _comparison(amazon_product, flipkart_product, "color", "color"),
        "processor": _comparison(amazon_product, flipkart_product, "processor", "processor"),
        "display": _comparison(
            amazon_product,
            flipkart_product,
            "display",
            "display_size",
        ),
        "other_specs": _comparison(
            amazon_product,
            flipkart_product,
            "other_specs",
            "operating_system",
        ),
    }

    # Include resolution as supporting evidence when operating system is absent.
    for product, key in ((amazon_product, "amazon"), (flipkart_product, "flipkart")):
        resolution = normalize_text(getattr(product, "resolution", ""))
        if resolution:
            comparisons["other_specs"][key] = resolution
            comparisons["other_specs"]["available"] = bool(
                comparisons["other_specs"]["amazon"]
                and comparisons["other_specs"]["flipkart"]
            )
            comparisons["other_specs"]["matched"] = (
                comparisons["other_specs"]["available"]
                and comparisons["other_specs"]["amazon"]
                == comparisons["other_specs"]["flipkart"]
            )
            comparisons["other_specs"]["score"] = (
                WEIGHTS["other_specs"]
                if comparisons["other_specs"]["matched"]
                else 0
            )

    applicable_weight = sum(
        WEIGHTS[key]
        for key, reason in comparisons.items()
        if reason["available"]
    )
    matched_weight = sum(reason["score"] for reason in comparisons.values())
    score = round(matched_weight * 100 / applicable_weight) if applicable_weight else 0

    soft_penalty = 0
    if comparisons["color"]["available"] and not comparisons["color"]["matched"]:
        soft_penalty += 15
    for key in ("processor", "display", "other_specs"):
        if comparisons[key]["available"] and not comparisons[key]["matched"]:
            soft_penalty += 5
    score = max(0, min(100, score - soft_penalty))

    hard_mismatch = any(
        reason["hard_mismatch"] for reason in comparisons.values()
    )
    if hard_mismatch:
        confidence = MatchConfidence.LOW
        match_status = MatchStatus.REJECTED
    elif score >= 85:
        confidence = MatchConfidence.HIGH
        match_status = MatchStatus.MATCHED
    elif score >= 65:
        confidence = MatchConfidence.MEDIUM
        match_status = MatchStatus.REVIEW
    else:
        confidence = MatchConfidence.LOW
        match_status = MatchStatus.REJECTED

    return {
        "score": score,
        "confidence": confidence,
        "match_status": match_status,
        "reasons": {
            **comparisons,
            "summary": {
                "applicable_weight": applicable_weight,
                "matched_weight": matched_weight,
                "hard_mismatch": hard_mismatch,
                "soft_penalty": soft_penalty,
            },
        },
    }


def run_product_matching_for_keyword(search_keyword) -> KeywordMatchingSummary:
    """Match only completed staging products belonging to one keyword.

    Flipkart products are reached through the Amazon products associated with
    the selected keyword. This preserves the importer provenance chain and
    deliberately avoids all search/extraction services.
    """
    amazon_products = list(
        AmazonProduct.objects.filter(
            status=ImportStatus.COMPLETED,
            asin__in=search_keyword.amazon_results.values("asin"),
        ).distinct()
    )
    amazon_ids = [product.pk for product in amazon_products]
    flipkart_products = list(
        FlipkartProduct.objects.filter(
            status=ImportStatus.COMPLETED,
            search_result__amazon_product_id__in=amazon_ids,
        ).select_related("search_result")
    )

    flipkart_by_amazon = {}
    for flipkart_product in flipkart_products:
        flipkart_by_amazon.setdefault(
            flipkart_product.search_result.amazon_product_id, []
        ).append(flipkart_product)

    created = updated = high = medium = low = failed = 0
    no_candidate = 0
    for amazon_product in amazon_products:
        candidates = flipkart_by_amazon.get(amazon_product.pk, [])
        if not candidates:
            no_candidate += 1
            continue
        for flipkart_product in candidates:
            try:
                result = match_products(amazon_product, flipkart_product)
                with transaction.atomic():
                    _, was_created = ProductMatch.objects.update_or_create(
                        amazon_product=amazon_product,
                        flipkart_product=flipkart_product,
                        defaults=result,
                    )
                if was_created:
                    created += 1
                else:
                    updated += 1
                if result["confidence"] == MatchConfidence.HIGH:
                    high += 1
                elif result["confidence"] == MatchConfidence.MEDIUM:
                    medium += 1
                else:
                    low += 1
            except Exception:
                failed += 1

    return KeywordMatchingSummary(
        amazon_products=len(amazon_products),
        flipkart_products=len(flipkart_products),
        matches_created=created,
        matches_updated=updated,
        high_confidence=high,
        medium_confidence=medium,
        low_confidence=low,
        no_candidate=no_candidate,
        failed=failed,
    )


def run_product_matching_for_batch(batch: ImportBatch) -> KeywordMatchingSummary:
    """Match only staged products associated with this ImportBatch."""
    amazon_products = list(
        batch.amazon_products.filter(status=ImportStatus.COMPLETED).distinct()
    )
    amazon_ids = [product.pk for product in amazon_products]
    flipkart_products = list(
        batch.flipkart_products.filter(
            status=ImportStatus.COMPLETED,
            search_result__amazon_product_id__in=amazon_ids,
        ).select_related("search_result")
    )
    by_amazon = {}
    for product in flipkart_products:
        by_amazon.setdefault(product.search_result.amazon_product_id, []).append(product)

    created = updated = high = medium = low = failed = no_candidate = 0
    for amazon_product in amazon_products:
        candidates = by_amazon.get(amazon_product.pk, [])
        if not candidates:
            no_candidate += 1
            continue
        for flipkart_product in candidates:
            try:
                result = match_products(amazon_product, flipkart_product)
                with transaction.atomic():
                    existing = ProductMatch.objects.filter(
                        amazon_product=amazon_product,
                        flipkart_product=flipkart_product,
                    ).first()
                    defaults = dict(result)
                    if existing and existing.match_status in {
                        MatchStatus.APPROVED,
                        MatchStatus.PUBLISHED,
                    }:
                        defaults["match_status"] = existing.match_status
                    match, was_created = ProductMatch.objects.update_or_create(
                        amazon_product=amazon_product,
                        flipkart_product=flipkart_product,
                        defaults=defaults,
                    )
                    match.batches.add(batch)
                if was_created:
                    created += 1
                else:
                    updated += 1
                if result["confidence"] == MatchConfidence.HIGH:
                    high += 1
                elif result["confidence"] == MatchConfidence.MEDIUM:
                    medium += 1
                else:
                    low += 1
            except Exception:
                failed += 1

    return KeywordMatchingSummary(
        amazon_products=len(amazon_products),
        flipkart_products=len(flipkart_products),
        matches_created=created,
        matches_updated=updated,
        high_confidence=high,
        medium_confidence=medium,
        low_confidence=low,
        no_candidate=no_candidate,
        failed=failed,
    )
