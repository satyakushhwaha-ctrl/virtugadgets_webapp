"""Flipkart product-detail extraction and persistence."""

import logging
import asyncio
import inspect
import re
import time
from decimal import Decimal
from urllib.parse import urlparse

from django.db import transaction
from django.conf import settings
from django.utils import timezone

from ..models import FlipkartProduct, FlipkartSearchResult, ImportStatus
from .flipkart_scraper import parse_flipkart_product_html, scrape_flipkart
from .jobs import concise_error_message

logger = logging.getLogger(__name__)


def extract_flipkart_product_data(url: str) -> dict:
    """Fetch and parse one product, preferring Scrape.do when configured."""
    extract_flipkart_product_data.last_provider_metadata = {}
    if getattr(settings, "SCRAPEDO_API_TOKEN", "").strip():
        from .scrapedo import ScrapeDoWebProvider

        response = ScrapeDoWebProvider().fetch(url, render=True)
        extract_flipkart_product_data.last_provider_metadata = {
            "provider": response.provider,
            "status_code": response.status_code,
            "request_cost": response.request_cost,
            "provider_duration_ms": response.duration_ms,
        }
        pid = (extract_flipkart_pid(url) or "unknown").upper()
        html = response.html or ""
        logger.info(
            "[FLIPKART PRODUCT] pid=%s provider=%s status=%s html_length=%s request_cost=%s",
            pid, response.provider, response.status_code, len(html), response.request_cost or "unknown",
        )
        block_reason = _flipkart_product_block_reason(html)
        if block_reason:
            logger.warning(
                "[FLIPKART PRODUCT] pid=%s provider=%s page=blocked reason=%s",
                pid, response.provider, block_reason,
            )
            raise ValueError(f"Flipkart product page is blocked: {block_reason}.")
        parse_started = time.monotonic()
        parsed = parse_flipkart_product_html(html, url)
        extract_flipkart_product_data.last_provider_metadata["parse_duration_ms"] = max(0, round((time.monotonic() - parse_started) * 1000))
        logger.info("[FLIPKART PRODUCT] pid=%s parser=success", pid)
        return parsed
    logger.warning(
        "[FLIPKART PRODUCT] provider=scrapedo fallback=playwright reason=token_not_configured"
    )
    return _run_scraper(url)


def extract_flipkart_pid(url: str) -> str | None:
    match = re.search(r"(?:[?&]pid=)([^&#]+)", url or "", re.I)
    return match.group(1) if match else None


def _flipkart_product_block_reason(raw_html: str) -> str | None:
    content = (raw_html or "").lower()
    for marker, reason in (
        ("captcha", "captcha"),
        ("robot check", "robot-check"),
        ("access denied", "access-denied"),
        ("verify you are human", "human-verification"),
        ("login to continue", "login-page"),
        ("unusual traffic", "unusual-traffic"),
    ):
        if marker in content:
            return reason
    return None


def _nested(data, key, legacy_key=None):
    value = data.get(key)
    if isinstance(value, dict):
        return value
    value = data.get(legacy_key) if legacy_key else None
    return value if isinstance(value, dict) else {}


def _value(data, *keys):
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def _specification(specifications, *names):
    normalized = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in (specifications or {}).items()}
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


def extract_flipkart_product(url: str) -> dict:
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {"flipkart.com", "www.flipkart.com"}:
        raise ValueError("Invalid Flipkart product URL.")
    raw = extract_flipkart_product_data(url)
    if not isinstance(raw, dict):
        raise ValueError("Flipkart product scraper returned invalid data.")
    product = _nested(raw, "product")
    pricing = _nested(raw, "pricing")
    seller = _nested(raw, "seller", "seller_info")
    availability = _nested(raw, "availability")
    specifications = _nested(raw, "specifications")
    legacy_pricing = pricing or raw
    price_range = _nested(pricing, "selling_price_range_inr")
    normalized = {
        "pid": _value(product, "pid", "sku", "id") or raw.get("pid", ""),
        "product_title": _value(product, "title", "product_title") or raw.get("product_title", ""),
        "brand": _value(product, "brand") or raw.get("brand", ""),
        "url": raw.get("url") or url,
        "availability": _value(availability, "status") or raw.get("availability", ""),
        "images": raw.get("images") or [],
        "mrp_inr": _value(legacy_pricing, "mrp", "mrp_inr"),
        "current_selling_price_inr": _value(legacy_pricing, "selling_price", "current_selling_price_inr"),
        "selling_price_min_inr": _value(legacy_pricing, "min_price") or price_range.get("min"),
        "selling_price_max_inr": _value(legacy_pricing, "max_price") or price_range.get("max"),
        "discount_percentage": _value(legacy_pricing, "discount_percentage"),
        "primary_seller": _value(seller, "name", "primary_seller") or raw.get("primary_seller", ""),
        "seller_rating": _value(seller, "rating", "seller_rating"),
        "processor": _specification(specifications, "processor", "processor name"),
        "ram": _specification(specifications, "ram", "ram size", "memory"),
        "storage": _specification(specifications, "storage", "ssd", "hard drive"),
        "operating_system": _specification(specifications, "operating system", "os"),
        "display_size": _specification(specifications, "display size", "screen size"),
        "resolution": _specification(specifications, "resolution"),
        "color": _specification(specifications, "color", "colour"),
        "weight_kg": _weight_kg(_specification(specifications, "weight", "item weight")),
        "software": _specification(specifications, "software"),
        "warranty": _specification(specifications, "warranty", "manufacturer warranty"),
    }
    if not normalized["product_title"]:
        raise ValueError("Flipkart product validation failed: title is missing.")
    if not normalized["pid"]:
        raise ValueError("Flipkart product validation failed: PID is missing.")
    if not normalized["url"]:
        raise ValueError("Flipkart product validation failed: product URL is missing.")
    return normalized


_EXTRACTED_FIELDS = (
    "product_title", "brand", "url", "availability", "images", "mrp_inr",
    "current_selling_price_inr", "selling_price_min_inr", "selling_price_max_inr",
    "discount_percentage", "primary_seller", "seller_rating", "processor", "ram",
    "storage", "operating_system", "display_size", "resolution", "color", "weight_kg",
    "software", "warranty",
)


def process_flipkart_search_result(search_result: FlipkartSearchResult) -> bool:
    pid = (search_result.pid or "").strip()
    if not pid:
        raise ValueError("Flipkart search result is missing a PID.")
    product = FlipkartProduct.objects.filter(pid=pid).first()
    if product is None:
        product = FlipkartProduct.objects.create(
            search_result=search_result,
            pid=pid,
            url=search_result.product_url,
            status=ImportStatus.PENDING,
        )
    product.status = ImportStatus.RUNNING
    product.error_message = ""
    product.save(update_fields=["status", "error_message", "updated_at"])
    try:
        data = extract_flipkart_product(search_result.product_url)
        extracted_pid = (data.get("pid") or pid).strip()
        if extracted_pid != pid:
            raise ValueError(f"Scraper PID {extracted_pid!r} does not match search result PID {pid!r}.")
        with transaction.atomic():
            for field in _EXTRACTED_FIELDS:
                setattr(product, field, data.get(field))
            product.pid = pid
            product.status = ImportStatus.COMPLETED
            product.error_message = ""
            product.extracted_at = timezone.now()
            product.save()
            search_result.processed = True
            search_result.save(update_fields=["processed"])
        logger.info(
            "[FLIPKART PRODUCT] pid=%s validation=passed title=%s identity=%s",
            pid, bool(data.get("product_title")), bool(data.get("url")),
        )
        logger.info("[FLIPKART PRODUCT] pid=%s saved=true status=completed", pid)
    except Exception as exc:
        logger.warning(
            "[FLIPKART PRODUCT] pid=%s validation=failed reason=%s",
            pid, concise_error_message(exc),
        )
        product.status = ImportStatus.FAILED
        product.error_message = concise_error_message(exc)
        product.save(update_fields=["status", "error_message", "updated_at"])
        raise
    return True
def _run_scraper(url):
    result = scrape_flipkart(url)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result
