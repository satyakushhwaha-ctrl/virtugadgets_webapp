"""Deterministic matching of Amazon and Flipkart staging products."""

from dataclasses import dataclass
import re
import unicodedata
from urllib.parse import urlparse

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

TITLE_STOPWORDS = {
    "and", "for", "with", "the", "a", "an", "in", "on", "of", "by",
    "new", "latest", "home", "edition", "core", "inch", "inches", "cm",
    "laptop", "computer", "notebook", "gaming", "mobile", "phone",
}

SEARCH_MATCH_WEIGHTS = {
    "model": 50,
    "cpu": 15,
    "gpu": 15,
    "gpu_memory": 5,
    "ram": 7,
    "storage": 7,
    "brand": 3,
    "series": 2,
    "os": 1,
}


def normalize_text(value: str | None) -> str:
    """Normalize case, punctuation, whitespace, and Unicode variants."""
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def normalized_title_tokens(value: str | None) -> list[str]:
    """Return safe title tokens while retaining model and capacity identifiers."""
    normalized = normalize_text(value)
    normalized = re.sub(r"(?<=\d)\s+(?=(?:gb|tb)\b)", "", normalized)
    raw_tokens = re.findall(r"[a-z]+\d+[a-z0-9]*|\d+[a-z]+[a-z0-9]*|[a-z]+|\d+", normalized)
    tokens = []
    index = 0
    while index < len(raw_tokens):
        if (
            index + 1 < len(raw_tokens)
            and raw_tokens[index].isalpha()
            and raw_tokens[index + 1].isdigit()
            and raw_tokens[index] in {
                "ryzen", "core", "iphone", "windows", "android", "ios",
                "macos", "rtx", "gtx", "radeon", "arc",
            }
        ):
            tokens.append(raw_tokens[index] + raw_tokens[index + 1])
            index += 2
            continue
        tokens.append(raw_tokens[index])
        index += 1
    return tokens


def _title_signal_tokens(value: str | None) -> set[str]:
    return {
        token for token in normalized_title_tokens(value)
        if token not in TITLE_STOPWORDS and len(token) > 1
    }


def _search_match_text(product_or_title) -> str:
    if isinstance(product_or_title, str):
        return product_or_title
    fields = [
        getattr(product_or_title, "product_title", ""),
        getattr(product_or_title, "brand", ""),
        getattr(product_or_title, "processor", ""),
        getattr(product_or_title, "ram", ""),
        getattr(product_or_title, "storage", ""),
        getattr(product_or_title, "operating_system", ""),
        getattr(product_or_title, "display_size", ""),
        getattr(product_or_title, "resolution", ""),
    ]
    return " ".join(str(field or "") for field in fields)


def _search_match_signals(value: str | None) -> dict[str, set[str]]:
    """Extract comparable technical signals from a title or product fields."""
    raw_text = str(value or "")
    truncated = bool(re.search(r"(?:\.\.\.|…)[\s]*$", raw_text))
    text = normalize_text(raw_text)
    tokens = _title_signal_tokens(text)
    capacities = {
        token for token in tokens if re.fullmatch(r"\d+(?:gb|tb)", token)
    }
    cpu = set()
    for pattern in (
        r"ryzen\s*\d+",
        r"core\s*i[3579](?:\s*[- ]?\s*\d+)?",
        r"\bi[3579]\s*[- ]?\s*\d+",
        r"\b\d{4,6}[a-z]{1,4}\b",
    ):
        cpu.update(re.sub(r"\s+", "", match) for match in re.findall(pattern, text))
    gpu = {
        re.sub(r"\s+", "", match)
        for match in re.findall(r"\b(?:rtx|gtx|rx|arc)\s*\d{3,4}\b", text)
    }
    gpu_memory = set()
    for match in re.finditer(
        r"(?P<memory>\d+)\s*(?P<unit>gb|tb)\s*graphics\b",
        text,
    ):
        gpu_memory.add(f"{match.group('memory')}{match.group('unit')}")
    for match in re.finditer(
        r"(?P<memory>\d+)\s*(?P<unit>gb|tb)\s*(?:graphics\s*)?(?:nvidia\s+)?(?:geforce\s+)?(?:rtx|gtx|rx|arc)\s*\d{3,4}",
        text,
    ):
        gpu_memory.add(f"{match.group('memory')}{match.group('unit')}")
    for match in re.finditer(
        r"(?:rtx|gtx|rx|arc)\s*\d{3,4}[^,;()]{0,30}?(?P<memory>\d+)\s*(?P<unit>gb|tb)\s*(?:graphics|vram)",
        text,
    ):
        gpu_memory.add(f"{match.group('memory')}{match.group('unit')}")
    os = {
        re.sub(r"\s+", "", match)
        for match in re.findall(r"\b(?:windows|android|ios|macos)\s*\d*\b", text)
    }
    display = {
        re.sub(r"\s+", "", match)
        for match in re.findall(
            r"\b\d+(?:\.\d+)?\s*(?:inch|inches|cm)\b",
            raw_text.casefold(),
        )
    }

    technical = capacities | cpu | gpu | os
    def is_model_identifier(token: str) -> bool:
        if token in technical or token in {"ddr", "ssd"}:
            return False
        if token.startswith(("ips", "nits")) or token.endswith(("hz", "kg", "cm")):
            return False
        if not any(character.isalpha() for character in token):
            return False
        if not any(character.isdigit() for character in token):
            return False
        numeric_runs = re.findall(r"\d+", token)
        return max((len(run) for run in numeric_runs), default=0) >= 3 or token.startswith("iphone")

    model = {token for token in tokens if is_model_identifier(token)}
    brand = tokens & _title_signal_tokens(getattr(value, "brand", "")) if not isinstance(value, str) else set()
    generic = tokens - technical - model - brand
    return {
        "tokens": tokens,
        "model": model,
        "cpu": cpu,
        "gpu": gpu,
        "gpu_memory": gpu_memory,
        "capacities": capacities,
        "os": os,
        "display": display,
        "brand": brand,
        "generic": generic,
        "truncated": truncated,
    }


def rank_flipkart_search_result(amazon_product, search_result) -> dict:
    """Score one unextracted Flipkart search result against an Amazon title.

    This is deliberately title-only: search candidates have no structured
    Flipkart attributes yet. The weights mirror the existing matcher’s intent,
    with identifier/spec tokens receiving most of the score.
    """
    amazon_signals = _search_match_signals(_search_match_text(amazon_product))
    candidate_signals = _search_match_signals(search_result.title)
    amazon_brand = _title_signal_tokens(amazon_product.brand)
    amazon_signals["brand"] = amazon_brand
    candidate_signals["brand"] = candidate_signals["tokens"] & amazon_brand

    matched = {}
    component_scores = {}
    conflicts = {}
    applicable_weight = 0
    matched_weight = 0
    for key in ("model", "cpu", "gpu", "gpu_memory", "brand", "os"):
        source = amazon_signals[key]
        if not source:
            continue
        matched[key] = source & candidate_signals[key]
        # A missing model/SKU is not a contradiction. It cannot earn the
        # model bonus, but it must not drown out matching hardware signals.
        if (
            not candidate_signals[key]
            and (key == "model" or candidate_signals["truncated"])
        ):
            component_scores[key] = 0
            continue
        applicable_weight += SEARCH_MATCH_WEIGHTS[key]
        if matched[key]:
            component_scores[key] = round(
                SEARCH_MATCH_WEIGHTS[key] * len(matched[key]) / len(source), 2
            )
            matched_weight += component_scores[key]
        else:
            component_scores[key] = 0
            if candidate_signals[key]:
                conflicts[key] = sorted(candidate_signals[key])

    for key in ("ram", "storage"):
        field = getattr(amazon_product, key, "")
        source = {
            capacity for capacity in _search_match_signals(field)["capacities"]
        }
        if not source:
            continue
        applicable_weight += SEARCH_MATCH_WEIGHTS[key]
        matched[key] = source & candidate_signals["capacities"]
        if matched[key]:
            component_scores[key] = SEARCH_MATCH_WEIGHTS[key]
            matched_weight += component_scores[key]
        else:
            component_scores[key] = 0
            if candidate_signals["capacities"]:
                conflicts[key] = sorted(candidate_signals["capacities"])

    amazon_series = amazon_signals["generic"] - TITLE_STOPWORDS
    candidate_series = candidate_signals["generic"] - TITLE_STOPWORDS
    if amazon_series:
        applicable_weight += SEARCH_MATCH_WEIGHTS["series"]
        matched["series"] = amazon_series & candidate_series
        if matched["series"]:
            component_scores["series"] = round(
                SEARCH_MATCH_WEIGHTS["series"]
                * len(matched["series"])
                / len(amazon_series),
                2,
            )
            matched_weight += component_scores["series"]
        else:
            component_scores["series"] = 0

    if amazon_signals["tokens"]:
        applicable_weight += SEARCH_MATCH_WEIGHTS["os"]
        title_overlap = amazon_signals["tokens"] & candidate_signals["tokens"]
        matched["title"] = title_overlap
        if title_overlap:
            component_scores["title"] = round(
                SEARCH_MATCH_WEIGHTS["os"]
                * len(title_overlap)
                / len(amazon_signals["tokens"]),
                2,
            )
            matched_weight += component_scores["title"]
        else:
            component_scores["title"] = 0

    score = round(100 * matched_weight / applicable_weight) if applicable_weight else 0

    if score >= 90:
        confidence = MatchConfidence.HIGH
        match_status = MatchStatus.MATCHED
    elif score >= 80:
        confidence = MatchConfidence.HIGH
        match_status = MatchStatus.MATCHED
    elif score >= 70:
        confidence = MatchConfidence.MEDIUM
        match_status = MatchStatus.REVIEW
    else:
        confidence = MatchConfidence.LOW
        match_status = MatchStatus.REJECTED

    reasons = []
    labels = {
        "model": "model identifiers",
        "cpu": "CPU models",
        "gpu": "GPU models",
        "ram": "RAM",
        "storage": "storage",
        "brand": "brand",
        "series": "product series",
        "os": "operating system",
    }
    for key, signals in matched.items():
        if signals:
            reasons.append(f"{labels.get(key, 'title')} matched: {', '.join(sorted(signals))}")
    if not reasons:
        reasons.append("no meaningful product identity signals matched")

    return {
        "candidate": search_result,
        "score": score,
        "confidence": confidence,
        "match_status": match_status,
        "reasons": reasons,
        "signals": {
            "matched": {key: sorted(value) for key, value in matched.items()},
            "component_scores": component_scores,
            "conflicts": conflicts,
            "normalized_amazon_title": normalize_text(amazon_product.product_title),
            "normalized_flipkart_title": normalize_text(search_result.title),
            "amazon_attributes": {
                key: sorted(value) for key, value in amazon_signals.items()
                if key not in {"tokens", "truncated"}
            },
            "flipkart_attributes": {
                key: sorted(value) for key, value in candidate_signals.items()
                if key not in {"tokens", "truncated"}
            },
            "flipkart_title_truncated": candidate_signals["truncated"],
            "applicable_weight": applicable_weight,
            "matched_weight": round(matched_weight, 2),
        },
    }


def rank_flipkart_search_results(amazon_product, search_results) -> list[dict]:
    """Rank all existing candidates, highest score first, deterministically."""
    ranked = [rank_flipkart_search_result(amazon_product, result) for result in search_results]
    return sorted(ranked, key=lambda item: (-item["score"], item["candidate"].position, item["candidate"].pk))


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


def first_valid_image_url(product) -> str:
    """Return the first usable extracted image URL without fetching it."""
    images = getattr(product, "images", None)
    if not isinstance(images, (list, tuple)):
        return ""

    for image in images:
        if not isinstance(image, str):
            continue
        image = image.strip()
        parsed = urlparse(image)
        if image and parsed.scheme in {"http", "https"} and parsed.netloc:
            return image
    return ""


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
