"""Amazon product-detail extraction and persistence.

The scraper is deliberately kept separate from Django.  This module bridges
the async scraper response into the existing staging/workflow model.
"""

import asyncio
import inspect
import json
import logging
import os
import re
import time
from decimal import Decimal
from html import unescape
import requests

from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone

from ..models import AmazonProduct, AmazonSearchResult, ImportStatus
from .amazon_scraper import scrape_amazon
from .jobs import concise_error_message


logger = logging.getLogger(__name__)

SCRAPINGBEE_URL = "https://app.scrapingbee.com/api/v1/"
_BLOCK_MARKERS = (
    "captcha", "robot check", "unusual traffic", "access denied",
    "download is starting", "sorry, we just need to make sure",
)


def _run_scraper(url, on_basic_data=None):
    result = scrape_amazon(url, on_basic_data=on_basic_data)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def extract_amazon_product_data(url: str, on_basic_data=None) -> dict:
    """Compatibility name for callers; the canonical implementation is the standalone scraper."""
    return _run_scraper(url, on_basic_data=on_basic_data)


def _html_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _json_ld_product(html):
    for body in re.findall(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        html or "", re.I | re.S,
    ):
        try:
            payload = json.loads(unescape(body).strip())
        except (TypeError, ValueError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        if isinstance(payload, dict) and isinstance(payload.get("@graph"), list):
            candidates.extend(payload["@graph"])
        for item in candidates:
            if isinstance(item, dict):
                item_type = item.get("@type")
                if item_type == "Product" or (isinstance(item_type, list) and "Product" in item_type):
                    return item
    return {}


def _static_html_product(url, html):
    """Parse the ScrapingBee document without waiting for client-side DOM."""
    product_ld = _json_ld_product(html)
    brand = product_ld.get("brand", {})
    if isinstance(brand, dict):
        brand = brand.get("name", "")
    title = product_ld.get("name") or re.search(
        r'<(?:meta[^>]+property=["\']og:title["\'][^>]+content|title)[^>]*>([^<]*)',
        html or "", re.I,
    )
    title = title if isinstance(title, str) else (title.group(1) if title else "")
    if not title:
        match = re.search(r'id=["\']productTitle["\'][^>]*>(.*?)</', html or "", re.I | re.S)
        title = _html_text(match.group(1)) if match else ""
    if not brand:
        match = re.search(r'id=["\']bylineInfo["\'][^>]*>(.*?)</', html or "", re.I | re.S)
        brand = _html_text(match.group(1)) if match else ""
    offers = product_ld.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    offers = offers if isinstance(offers, dict) else {}
    selling_price = offers.get("price")
    if selling_price is None:
        match = re.search(r'(?:₹|INR)\s*([\d,]+(?:\.\d+)?)', html or "", re.I)
        selling_price = match.group(1).replace(",", "") if match else None
    mrp_match = re.search(r'(?:M\.R\.P\.|MRP)[^₹\d]{0,30}(?:₹|INR)?\s*([\d,]+(?:\.\d+)?)', html or "", re.I)
    image = product_ld.get("image", [])
    if isinstance(image, str):
        image = [image]
    return {
        "url": url,
        "asin": product_ld.get("sku") or product_ld.get("mpn") or "",
        "product": {"asin": product_ld.get("sku", ""), "title": _html_text(title), "brand": _html_text(brand)},
        "pricing": {"selling_price": selling_price, "mrp": mrp_match.group(1).replace(",", "") if mrp_match else None},
        "images": image if isinstance(image, list) else [],
    }


def _extract_scrapingbee_product_data(url: str):
    api_key = os.getenv("SCRAPINGBEE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Amazon product fallback is unavailable: SCRAPINGBEE_API_KEY is not configured.")
    try:
        response = requests.get(
            SCRAPINGBEE_URL,
            params={"api_key": api_key, "url": url},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"ScrapingBee Amazon product request failed: {exc}") from exc
    html = response.text or ""
    if response.status_code != 200 or "html" not in response.headers.get("content-type", "").lower():
        raise RuntimeError(f"ScrapingBee Amazon product returned HTTP {response.status_code}.")
    if any(marker in html.lower() for marker in _BLOCK_MARKERS):
        raise RuntimeError("ScrapingBee returned an Amazon blocked product document.")
    return _static_html_product(url, html)


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


def _map_amazon_product_response(raw, url: str) -> dict:
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


def extract_amazon_product(url: str, on_basic_data=None) -> dict:
    """Extract through Playwright, validating before falling back to ScrapingBee."""
    async def forward_basic_data(raw_basic):
        if on_basic_data is None:
            return
        mapped_basic = _map_amazon_product_response(raw_basic, url)
        callback_result = on_basic_data(mapped_basic)
        if inspect.isawaitable(callback_result):
            await callback_result

    provider = os.getenv("AMAZON_PRODUCT_PROVIDER", "auto").strip().lower() or "auto"
    if provider not in {"auto", "playwright", "scrapingbee"}:
        raise ValueError("AMAZON_PRODUCT_PROVIDER must be auto, playwright, or scrapingbee.")

    attempts = [] if provider == "scrapingbee" else ["playwright"]
    if provider != "scrapingbee":
        attempts.append("scrapingbee")
    errors = []
    for current_provider in attempts:
        logger.info("[AMAZON PRODUCT] asin=%s provider=%s", _asin_from_url(url), current_provider)
        try:
            raw = (
                extract_amazon_product_data(url, on_basic_data=forward_basic_data if on_basic_data else None)
                if current_provider == "playwright"
                else _extract_scrapingbee_product_data(url)
            )
            mapped = _map_amazon_product_response(raw, url)
            missing = validate_amazon_product_data(mapped)
            if missing:
                logger.info("[AMAZON PRODUCT] asin=%s validation=failed missing=%s", _asin_from_url(url), ",".join(missing))
                raise ValueError(f"Amazon product data is incomplete: {', '.join(missing)}")
            logger.info("[AMAZON PRODUCT] asin=%s provider=%s validation=passed", _asin_from_url(url), current_provider)
            return mapped
        except Exception as exc:
            errors.append(exc)
            if current_provider == "playwright" and "scrapingbee" in attempts:
                logger.info("[AMAZON PRODUCT] asin=%s fallback=scrapingbee", _asin_from_url(url))
                continue
            raise RuntimeError("Amazon product extraction failed for all providers.") from exc
    raise RuntimeError("Amazon product extraction failed for all providers.") from errors[-1]


def _asin_from_url(url):
    match = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", url or "", re.I)
    return match.group(1).upper() if match else "unknown"


def validate_amazon_product_data(data):
    missing = []
    if not (data.get("product_title") or "").strip():
        missing.append("title")
    if not (data.get("brand") or "").strip():
        missing.append("brand")
    if data.get("current_selling_price_inr") is None and data.get("mrp_inr") is None:
        missing.append("price")
    return missing


_EXTRACTED_FIELDS = (
    "product_title", "brand", "url", "availability", "images", "description",
    "highlights", "specifications", "mrp_inr",
    "current_selling_price_inr", "selling_price_min_inr", "selling_price_max_inr",
    "discount_percentage", "primary_seller", "seller_rating", "processor", "ram",
    "storage", "operating_system", "display_size", "resolution", "color", "weight_kg",
    "software", "warranty",
)
_BASIC_FIELDS = (
    "product_title", "brand", "url", "availability", "mrp_inr",
    "current_selling_price_inr", "selling_price_min_inr", "selling_price_max_inr",
    "discount_percentage", "primary_seller", "seller_rating",
)


def _persist_extracted_fields(product, data, fields):
    changed_fields = []
    for field in fields:
        value = data.get(field)
        if value is None:
            continue
        if field == "description":
            value = value or ""
        elif field == "highlights":
            value = value or []
        elif field == "specifications":
            value = value or {}
        setattr(product, field, value)
        changed_fields.append(field)
    if changed_fields:
        product.save(update_fields=changed_fields + ["updated_at"])


def process_amazon_search_result(search_result: AmazonSearchResult, on_basic_data=None) -> bool:
    total_started = time.perf_counter()
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
        async def save_basic_data(data):
            await sync_to_async(_persist_extracted_fields, thread_sensitive=True)(
                product,
                data,
                _BASIC_FIELDS,
            )
            if on_basic_data is not None:
                callback_result = on_basic_data(data)
                if inspect.isawaitable(callback_result):
                    await callback_result

        data = extract_amazon_product(
            search_result.product_url,
            on_basic_data=save_basic_data,
        )
        missing = validate_amazon_product_data(data)
        if missing:
            logger.info(
                "[AMAZON PRODUCT] asin=%s validation=failed missing=%s",
                asin,
                ",".join(missing),
            )
            raise ValueError(f"Amazon product data is incomplete: {', '.join(missing)}")
        extracted_asin = (data.get("asin") or asin).strip().upper()
        if extracted_asin != asin:
            raise ValueError(f"Scraper ASIN {extracted_asin!r} does not match search result ASIN {asin!r}.")
        database_started = time.perf_counter()
        with transaction.atomic():
            _persist_extracted_fields(product, data, _EXTRACTED_FIELDS)
            product.asin = asin
            product.status = ImportStatus.COMPLETED
            product.error_message = ""
            product.extracted_at = timezone.now()
            product.save(update_fields=["asin", "status", "error_message", "extracted_at", "updated_at"])
            search_result.processed = True
            search_result.save(update_fields=["processed"])
        logger.info("[AMAZON PRODUCT] asin=%s saved=true status=completed", asin)
        timing_message = (
            f"database={time.perf_counter() - database_started:.3f}s "
            f"total={time.perf_counter() - total_started:.3f}s asin={asin}"
        )
        logger.info(
            "[AMAZON TIMING] database=%.3fs total=%.3fs asin=%s",
            time.perf_counter() - database_started,
            time.perf_counter() - total_started,
            asin,
        )
        print(f"[AMAZON TIMING] {timing_message}", flush=True)
    except Exception as exc:
        product.status = ImportStatus.FAILED
        product.error_message = concise_error_message(exc)
        product.save(update_fields=["status", "error_message", "updated_at"])
        raise
    return True
