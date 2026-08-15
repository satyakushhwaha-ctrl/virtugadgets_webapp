"""Amazon product-detail extraction and persistence.

The scraper is deliberately kept separate from Django.  This module bridges
the async scraper response into the existing staging/workflow model.
"""

import asyncio
import inspect
import re
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import AmazonProduct, AmazonSearchResult, ImportStatus
from .amazon_scraper import scrape_amazon
from .jobs import concise_error_message


def _run_scraper(url):
    result = scrape_amazon(url)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def extract_amazon_product_data(url: str) -> dict:
    """Compatibility name for callers; the canonical implementation is the standalone scraper."""
    return _run_scraper(url)


def _nested(data, key, legacy_key=None):
    value = data.get(key)
    if isinstance(value, dict):
        return value
    if legacy_key:
        value = data.get(legacy_key)
        if isinstance(value, dict):
            return value
    return {}


def _value(data, *keys):
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def _specification(specifications, *names):
    normalized = {
        re.sub(r"[^a-z0-9]", "", str(key).lower()): value
        for key, value in (specifications or {}).items()
    }
    for name in names:
        value = normalized.get(re.sub(r"[^a-z0-9]", "", name.lower()))
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _weight_kg(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    match = re.search(r"([\d.]+)\s*(kg|kilograms?|g|grams?)?", str(value), re.I)
    if not match:
        return None
    amount = Decimal(match.group(1))
    return amount / 1000 if (match.group(2) or "").lower().startswith("g") else amount


def extract_amazon_product(url: str) -> dict:
    """Return the scraper response mapped to AmazonProduct field names."""
    raw = extract_amazon_product_data(url)
    if not isinstance(raw, dict):
        raise ValueError("Amazon product scraper returned invalid data.")
    product = _nested(raw, "product")
    pricing = _nested(raw, "pricing")
    seller = _nested(raw, "seller", "seller_info")
    availability = _nested(raw, "availability")
    specifications = _nested(raw, "specifications")
    legacy_pricing = pricing or raw
    price_range = _nested(pricing, "selling_price_range_inr")

    asin = _value(product, "asin", "id", "sku") or raw.get("asin")
    return {
        "asin": asin,
        "product_title": _value(product, "title", "product_title") or raw.get("product_title", ""),
        "brand": _value(product, "brand") or raw.get("brand", ""),
        "url": raw.get("url") or url,
        "availability": _value(availability, "status") or raw.get("availability", ""),
        "images": raw.get("images") or [],
        "description": _value(product, "description") or raw.get("description", ""),
        "highlights": raw.get("highlights") or [],
        "specifications": specifications,
        "mrp_inr": _value(legacy_pricing, "mrp", "mrp_inr"),
        "current_selling_price_inr": _value(legacy_pricing, "selling_price", "current_selling_price_inr"),
        "selling_price_min_inr": _value(legacy_pricing, "min_price") or price_range.get("min"),
        "selling_price_max_inr": _value(legacy_pricing, "max_price") or price_range.get("max"),
        "discount_percentage": _value(legacy_pricing, "discount_percentage"),
        "primary_seller": _value(seller, "name", "primary_seller") or raw.get("primary_seller", ""),
        "seller_rating": _value(seller, "rating", "seller_rating"),
        "processor": _specification(specifications, "processor", "processor name"),
        "ram": _specification(specifications, "ram", "ram size", "memory"),
        "storage": _specification(specifications, "storage", "ssd", "hard drive", "storage capacity"),
        "operating_system": _specification(specifications, "operating system", "os"),
        "display_size": _specification(specifications, "display size", "screen size"),
        "resolution": _specification(specifications, "resolution"),
        "color": _specification(specifications, "color", "colour"),
        "weight_kg": _weight_kg(_specification(specifications, "weight", "item weight")),
        "software": _specification(specifications, "software"),
        "warranty": _specification(specifications, "warranty", "manufacturer warranty"),
    }


_EXTRACTED_FIELDS = (
    "product_title", "brand", "url", "availability", "images", "description",
    "highlights", "specifications", "mrp_inr",
    "current_selling_price_inr", "selling_price_min_inr", "selling_price_max_inr",
    "discount_percentage", "primary_seller", "seller_rating", "processor", "ram",
    "storage", "operating_system", "display_size", "resolution", "color", "weight_kg",
    "software", "warranty",
)


def process_amazon_search_result(search_result: AmazonSearchResult) -> bool:
    asin = (search_result.asin or "").strip().upper()
    if not asin:
        raise ValueError("Amazon search result is missing an ASIN.")
    product, _ = AmazonProduct.objects.get_or_create(
        asin=asin,
        defaults={"url": search_result.product_url, "status": ImportStatus.PENDING},
    )
    product.status = ImportStatus.RUNNING
    product.error_message = ""
    product.save(update_fields=["status", "error_message", "updated_at"])
    try:
        data = extract_amazon_product(search_result.product_url)
        extracted_asin = (data.get("asin") or asin).strip().upper()
        if extracted_asin != asin:
            raise ValueError(f"Scraper ASIN {extracted_asin!r} does not match search result ASIN {asin!r}.")
        with transaction.atomic():
            for field in _EXTRACTED_FIELDS:
                value = data.get(field)
                if field == "description":
                    value = value or ""
                elif field == "highlights":
                    value = value or []
                elif field == "specifications":
                    value = value or {}
                setattr(product, field, value)
            product.asin = asin
            product.status = ImportStatus.COMPLETED
            product.error_message = ""
            product.extracted_at = timezone.now()
            product.save()
            search_result.processed = True
            search_result.save(update_fields=["processed"])
    except Exception as exc:
        product.status = ImportStatus.FAILED
        product.error_message = concise_error_message(exc)
        product.save(update_fields=["status", "error_message", "updated_at"])
        raise
    return True
