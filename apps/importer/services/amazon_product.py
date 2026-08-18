"""Amazon product-detail extraction and persistence.

The scraper is deliberately kept separate from Django.  This module bridges
the async scraper response into the existing staging/workflow model.
"""

import json
import asyncio
import inspect
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation
from html import unescape
from urllib.parse import urlparse
import requests

from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone

from ..models import AmazonProduct, AmazonSearchResult, ImportStatus
from .amazon_scraper import clean_price, is_valid_amazon_image, normalize_amazon_brand, scrape_amazon, upgrade_amazon_image
from .jobs import concise_error_message


logger = logging.getLogger(__name__)

SCRAPINGBEE_URL = "https://app.scrapingbee.com/api/v1/"
_BLOCK_MARKERS = (
    "captcha", "robot check", "unusual traffic", "access denied",
    "download is starting", "sorry, we just need to make sure",
)


def extract_amazon_product_data(url: str, on_basic_data=None) -> dict:
    result = scrape_amazon(url, on_basic_data=on_basic_data)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


class AmazonProviderError(RuntimeError):
    """A safe, structured failure from one Amazon product provider."""

    def __init__(self, message, *, provider, stage, status=None, content_type="",
                 body_length=None, page_url="", page_title="", flags=None):
        super().__init__(message)
        self.provider = provider
        self.stage = stage
        self.status = status
        self.content_type = content_type
        self.body_length = body_length
        self.page_url = page_url
        self.page_title = page_title
        self.flags = flags or {}


class AmazonProductExtractionError(RuntimeError):
    """Raised after both product providers fail, with both causes retained."""


class _ProviderPayload(dict):
    """Dict-shaped provider data with non-sensitive response diagnostics."""

    def __init__(self, data, diagnostics=None):
        super().__init__(data)
        self.diagnostics = diagnostics or {}


def _html_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _asin_from_url(url):
    match = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", url or "", re.I)
    return match.group(1).upper() if match else "unknown"


def _page_flags(*values):
    text = " ".join(str(value or "") for value in values).lower()
    return {
        "captcha": "captcha" in text,
        "robot_check": "robot check" in text,
        "access_denied": "access denied" in text,
        "download_response": "download is starting" in text or "download" in text,
        "empty_page": not text.strip(),
    }


def _safe_error_message(exc):
    message = str(exc) or exc.__class__.__name__
    return re.sub(r"(?i)(api[_-]?key|key)=[^&\s]+", r"\1=<redacted>", message)


def _diagnostic_reason(flags):
    return ", ".join(name.replace("_", " ") for name, present in flags.items() if present) or "none detected"


def _quality_present(value):
    return value not in (None, "", [], {})


def _quality_price_present(value):
    if not _quality_present(value):
        return False
    try:
        return Decimal(str(value).replace(",", "")) > 0
    except (TypeError, ValueError, InvalidOperation):
        return False


def build_amazon_product_quality_report(data):
    """Return one quality report used by both providers and persistence."""
    fields = {
        "title": bool(str(data.get("product_title") or "").strip()),
        "brand": bool(str(data.get("brand") or "").strip()),
        "selling_price": _quality_price_present(data.get("current_selling_price_inr")),
        "mrp": _quality_price_present(data.get("mrp_inr")),
        "images": bool(data.get("images")),
        "seller": bool(str(data.get("primary_seller") or "").strip()),
        "availability": bool(str(data.get("availability") or "").strip()),
        "specifications": bool(data.get("specifications")),
        "highlights": bool(data.get("highlights")),
    }
    missing_required = [
        "title" if not fields["title"] else None,
        "brand" if not fields["brand"] else None,
        "price_or_mrp" if not (fields["selling_price"] or fields["mrp"]) else None,
    ]
    missing_important = [
        field for field in ("images", "seller", "availability", "specifications", "highlights")
        if not fields[field]
    ]
    return {
        "valid": not any(missing_required),
        "missing_required": [field for field in missing_required if field],
        "missing_important": missing_important,
        "fields": fields,
    }


def _log_quality(asin, provider, report):
    fields = report["fields"]
    logger.info(
        "[AMAZON PRODUCT] quality asin=%s provider=%s title=%s brand=%s price=%s mrp=%s "
        "images=%s seller=%s availability=%s specifications=%s highlights=%s",
        asin, provider,
        *("yes" if fields[field] else "no" for field in (
            "title", "brand", "selling_price", "mrp", "images", "seller",
            "availability", "specifications", "highlights",
        )),
    )


def _merge_product_data(primary, fallback):
    """Merge provider fields without replacing valid values with empty ones."""
    merged = dict(primary or {})
    for field, fallback_value in (fallback or {}).items():
        primary_value = merged.get(field)
        if field == "images":
            values = []
            for value in (primary_value or []) + (fallback_value or []):
                if value and value not in values:
                    values.append(value)
            if values:
                merged[field] = values
        elif field == "specifications":
            values = dict(fallback_value or {})
            values.update({key: value for key, value in (primary_value or {}).items() if _quality_present(value)})
            if values:
                merged[field] = values
        elif not _quality_present(primary_value) and _quality_present(fallback_value):
            merged[field] = fallback_value
    return merged


def _log_playwright_failure(asin, url, exc, *, stage=None):
    flags = getattr(exc, "flags", None) or _page_flags(str(exc))
    logger.error(
        "[AMAZON PRODUCT] Playwright FAILED asin=%s url=%s stage=%s "
        "exception_type=%s reason=%s status=%s page_url=%s page_title=%s page_appears=%s",
        asin, url, stage or getattr(exc, "stage", "navigation"),
        exc.__class__.__name__, _safe_error_message(exc),
        getattr(exc, "status", None), getattr(exc, "page_url", "") or "<unavailable>",
        getattr(exc, "page_title", "") or "<unavailable>", _diagnostic_reason(flags),
    )


def _log_scrapingbee_failure(asin, url, exc):
    flags = getattr(exc, "flags", None) or _page_flags(str(exc))
    logger.error(
        "[AMAZON PRODUCT] ScrapingBee FAILED asin=%s url=%s status=%s "
        "exception_type=%s reason=%s content_type=%s body_length=%s response_appears=%s",
        asin, url, getattr(exc, "status", None), exc.__class__.__name__,
        _safe_error_message(exc), getattr(exc, "content_type", "") or "<unknown>",
        getattr(exc, "body_length", None), _diagnostic_reason(flags),
    )


def _provider_summary(outcome):
    if outcome["stage"] == "validation":
        return "validation failed; missing required fields: " + ", ".join(outcome["missing_required"])
    if outcome.get("status"):
        error = _safe_error_message(outcome.get("error", ""))
        prefix = f"HTTP {outcome['status']}"
        return error if error.startswith(prefix) else f"{prefix} {error}".strip()
    return f"{outcome['stage']} failed; {_safe_error_message(outcome.get('error', ''))}"


def _provider_label(provider):
    return "ScrapingBee" if provider == "scrapingbee" else "Playwright"


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


def _normalise_static_images(values):
    images = []
    rejected = ("placeholder", "transparent", "spacer", "pixel", "captcha", "robot-check")
    for value in values:
        if not isinstance(value, str):
            continue
        value = unescape(value).strip()
        if not value.startswith(("http://", "https://")):
            continue
        parsed = urlparse(value)
        lower = f"{parsed.path}?{parsed.query}".lower()
        if not is_valid_amazon_image(value) or any(marker in lower for marker in rejected):
            continue
        value = upgrade_amazon_image(value)
        if value and value not in images:
            images.append(value)
    return images


def _static_price_values(fragment):
    values = []
    for match in re.finditer(
        r'class=["\'][^"\']*a-price-whole[^"\']*["\'][^>]*>\s*([\d,]+)',
        fragment or "", re.I,
    ):
        value = clean_price(match.group(1))
        if value is not None:
            values.append(value)
    for match in re.finditer(r'(?:₹|INR)\s*([\d,]+(?:\.\d+)?)', fragment or "", re.I):
        value = clean_price(match.group(1))
        if value is not None:
            values.append(value)
    return values


def _static_price_from_context(html, markers):
    lower = (html or "").lower()
    for marker in markers:
        start = lower.find(marker.lower())
        if start < 0:
            continue
        values = _static_price_values(html[max(0, start - 500):start + 5000])
        if values:
            return values[0]
    return None


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
        selling_price = _static_price_from_context(
            html,
            ("priceToPay", "priceblock_dealprice", "priceblock_ourprice", "corePriceDisplay", "buybox"),
        )
    else:
        selling_price = clean_price(selling_price)
    mrp = _static_price_from_context(
        html,
        ("basisPrice", "a-text-price", "listPrice", "priceblock_listprice", "M.R.P."),
    )
    image = product_ld.get("image", [])
    if isinstance(image, str):
        image = [image]
    image_values = list(image) if isinstance(image, list) else []
    for match in re.finditer(r'data-old-hires=["\']([^"\']+)', html or "", re.I):
        image_values.append(match.group(1))
    for match in re.finditer(r'(?:src|data-src)=["\']([^"\']+)', html or "", re.I):
        if "amazon" in match.group(1).lower() and ("images" in match.group(1).lower() or "media" in match.group(1).lower()):
            image_values.append(match.group(1))
    return {
        "url": url,
        "asin": product_ld.get("sku") or product_ld.get("mpn") or "",
        "product": {
            "asin": product_ld.get("sku", ""),
            "title": _html_text(title),
            "brand": normalize_amazon_brand(_html_text(brand)),
        },
        "pricing": {"selling_price": selling_price, "mrp": mrp},
        "images": _normalise_static_images(image_values),
    }


def _extract_scrapingbee_product_data(url: str):
    asin = _asin_from_url(url)
    api_key = os.getenv("SCRAPINGBEE_API_KEY", "").strip()
    if not api_key:
        raise AmazonProviderError(
            "SCRAPINGBEE_API_KEY is not configured",
            provider="scrapingbee", stage="configuration",
        )
    try:
        response = requests.get(
            SCRAPINGBEE_URL,
            params={"api_key": api_key, "url": url},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise AmazonProviderError(
            _safe_error_message(exc), provider="scrapingbee", stage="request",
        ) from exc
    html = response.text or ""
    content_type = response.headers.get("content-type", "")
    flags = _page_flags(html)
    if response.status_code != 200:
        raise AmazonProviderError(
            f"HTTP {response.status_code} {response.reason or ''}".strip(),
            provider="scrapingbee", stage="response", status=response.status_code,
            content_type=content_type, body_length=len(html), flags=flags,
        )
    if not html:
        raise AmazonProviderError(
            "empty response body", provider="scrapingbee", stage="response",
            status=response.status_code, content_type=content_type,
            body_length=0, flags=flags,
        )
    if "html" not in content_type.lower():
        raise AmazonProviderError(
            f"non-HTML response ({content_type or '<unknown>'})",
            provider="scrapingbee", stage="response", status=response.status_code,
            content_type=content_type, body_length=len(html), flags=flags,
        )
    if any(marker in html.lower() for marker in _BLOCK_MARKERS):
        raise AmazonProviderError(
            "Amazon block page returned", provider="scrapingbee", stage="response",
            status=response.status_code, content_type=content_type,
            body_length=len(html), flags=flags,
        )
    return _ProviderPayload(
        _static_html_product(url, html),
        {"status": response.status_code, "content_type": content_type, "body_length": len(html)},
    )


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
        "brand": normalize_amazon_brand(_value(product, "brand") or raw.get("brand", "")),
        "url": raw.get("url") or url,
        "availability": _value(availability, "status") or raw.get("availability", ""),
        "images": _normalise_static_images(raw.get("images") or []),
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
    outcomes = []
    provider_errors = []
    partial_data = None
    for current_provider in attempts:
        asin = _asin_from_url(url)
        logger.info("[AMAZON PRODUCT] asin=%s provider=%s", asin, current_provider)
        received = False
        try:
            raw = (
                extract_amazon_product_data(url, on_basic_data=forward_basic_data if on_basic_data else None)
                if current_provider == "playwright"
                else _extract_scrapingbee_product_data(url)
            )
            received = True
            diagnostics = getattr(raw, "diagnostics", {})
            if diagnostics:
                logger.info(
                    "[AMAZON PRODUCT] ScrapingBee page received asin=%s status=%s html_length=%s content_type=%s",
                    asin, diagnostics.get("status"), diagnostics.get("body_length"),
                    diagnostics.get("content_type") or "<unknown>",
                )
            mapped = _map_amazon_product_response(raw, url)
            mapped = _merge_product_data(partial_data, mapped) if partial_data else mapped
            partial_data = mapped
            report = build_amazon_product_quality_report(mapped)
            _log_quality(asin, current_provider, report)
            if not report["valid"]:
                missing = report["missing_required"]
                outcome = {
                    "provider": current_provider, "success": False, "stage": "validation",
                    "missing_required": missing,
                }
                outcomes.append(outcome)
                provider_errors.append(ValueError(_provider_summary(outcome)))
                logger.error(
                    "[AMAZON PRODUCT] validation=failed asin=%s missing_required=%s missing_important=%s",
                    asin, ",".join(missing), ",".join(report["missing_important"]) or "none",
                )
                if current_provider == "playwright" and "scrapingbee" in attempts:
                    logger.info("[AMAZON PRODUCT] fallback → ScrapingBee asin=%s", asin)
                    continue
                break
            logger.info("[AMAZON PRODUCT] asin=%s provider=%s validation=passed", asin, current_provider)
            return mapped
        except Exception as exc:
            if isinstance(exc, AmazonProductExtractionError):
                raise
            stage = getattr(exc, "stage", None) or ("parse" if received else ("navigation" if current_provider == "playwright" else "request"))
            outcome = {
                "provider": current_provider, "success": False, "stage": stage,
                "error_type": exc.__class__.__name__, "error": _safe_error_message(exc),
                "status": getattr(exc, "status", None),
            }
            outcomes.append(outcome)
            provider_errors.append(exc)
            if current_provider == "playwright":
                if received or stage == "parse":
                    logger.error(
                        "[AMAZON PRODUCT] Playwright parse FAILED asin=%s missing=%s reason=%s",
                        asin, "title,brand,price", _safe_error_message(exc),
                    )
                else:
                    _log_playwright_failure(asin, url, exc, stage=stage)
            else:
                if received:
                    logger.error(
                        "[AMAZON PRODUCT] ScrapingBee parse FAILED asin=%s reason=%s",
                        asin, _safe_error_message(exc),
                    )
                else:
                    _log_scrapingbee_failure(asin, url, exc)
            if current_provider == "playwright" and "scrapingbee" in attempts:
                logger.info("[AMAZON PRODUCT] fallback → ScrapingBee asin=%s", asin)
                continue
            break
    summary = "; ".join(
        f"{_provider_label(outcome['provider'])}: {_provider_summary(outcome)}."
        for outcome in outcomes
    )
    final_error = AmazonProductExtractionError(
        "Amazon product extraction failed for all providers. " + summary
    )
    if provider_errors:
        raise final_error from provider_errors[-1]
    raise final_error


def validate_amazon_product_data(data):
    """Compatibility helper returning required failures from the central report."""
    return build_amazon_product_quality_report(data)["missing_required"]


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
        report = build_amazon_product_quality_report(data)
        _log_quality(asin, "persistence", report)
        if not report["valid"]:
            missing = report["missing_required"]
            logger.info(
                "[AMAZON PRODUCT] validation=failed asin=%s missing_required=%s missing_important=%s",
                asin,
                ",".join(missing),
                ",".join(report["missing_important"]) or "none",
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


# Keep the pre-Phase-3 implementation available only as an explicit local
# rollback when Scrape.do has not been configured. Once a token is present,
# the PDP path below is the only provider attempted.
_legacy_extract_amazon_product = extract_amazon_product


def normalize_scrapedo_amazon_product(raw: dict, url: str) -> dict:
    """Normalize Scrape.do's flat PDP response to VirtuGadgets' structure."""
    if not isinstance(raw, dict):
        raise ValueError("Scrape.do Amazon PDP returned invalid JSON.")

    product = raw.get("product") if isinstance(raw.get("product"), dict) else raw
    pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else raw
    asin = _value(product, "asin", "id", "sku") or raw.get("asin")
    title = _value(product, "title", "name", "product_title") or raw.get("title", "")
    brand = normalize_amazon_brand(_value(product, "brand") or raw.get("brand", ""))

    selling_price = _value(pricing, "selling_price", "sellingPrice", "price")
    mrp = _value(pricing, "mrp", "list_price", "listPrice")
    selling_price = clean_price(selling_price) if selling_price is not None else None
    mrp = clean_price(mrp) if mrp is not None else None

    image_values = raw.get("images") or product.get("images") or []
    if isinstance(image_values, (str, dict)):
        image_values = [image_values]
    image_urls = [
        item.get("url") if isinstance(item, dict) else item
        for item in image_values
    ]
    thumbnail = raw.get("thumbnail") or product.get("thumbnail")
    if thumbnail:
        image_urls.append(thumbnail)

    specifications = (
        raw.get("technical_details")
        or raw.get("technicalDetails")
        or raw.get("specifications")
        or {}
    )
    if not isinstance(specifications, dict):
        specifications = {}

    seller = raw.get("seller") or raw.get("seller_info") or {}
    if isinstance(seller, dict):
        seller_name = seller.get("name") or seller.get("seller_name") or ""
        seller_rating = seller.get("rating") or seller.get("seller_rating")
    else:
        seller_name = seller if isinstance(seller, str) else ""
        seller_rating = None

    availability = raw.get("availability") or product.get("availability") or ""
    if isinstance(availability, dict):
        availability = availability.get("status") or availability.get("value") or ""
    shipping_info = raw.get("shipping_info") or raw.get("shippingInfo") or []
    highlights = (
        raw.get("highlights")
        or raw.get("feature_bullets")
        or raw.get("featureBullets")
        or raw.get("bullet_points")
        or []
    )
    if isinstance(highlights, str):
        highlights = [highlights]
    highlights = list(dict.fromkeys(item for item in highlights if item)) if isinstance(highlights, list) else []

    return {
        "asin": str(asin or "").strip().upper(),
        "product_title": str(title or "").strip(),
        "brand": brand,
        "url": raw.get("url") or product.get("url") or url,
        "availability": str(availability or "").strip(),
        "images": _normalise_static_images(image_urls),
        "description": _value(product, "description") or raw.get("description", ""),
        "highlights": highlights,
        "specifications": specifications,
        "mrp_inr": mrp,
        "current_selling_price_inr": selling_price,
        "selling_price_min_inr": selling_price,
        "selling_price_max_inr": selling_price,
        "primary_seller": seller_name,
        "seller_rating": seller_rating,
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
        # These are retained in the normalized provider structure when the
        # current database has no corresponding columns.
        "rating": raw.get("rating") or product.get("rating"),
        "review_count": raw.get("total_ratings") or raw.get("reviewCount") or raw.get("review_count"),
        "delivery": shipping_info,
        "category": raw.get("category") or raw.get("category_name"),
        "model": _specification(specifications, "model", "model name") or raw.get("model"),
    }


def extract_amazon_product(url: str, on_basic_data=None) -> dict:
    """Extract and validate an Amazon PDP through Scrape.do."""
    from django.conf import settings
    from .scrapedo import ScrapeDoAmazonProvider

    if not getattr(settings, "SCRAPEDO_API_TOKEN", "").strip():
        extract_amazon_product.last_provider_metadata = {}
        return _legacy_extract_amazon_product(url, on_basic_data=on_basic_data)
    asin = _asin_from_url(url)
    if asin == "unknown":
        raise ValueError("Invalid Amazon product URL.")

    logger.info("[AMAZON PRODUCT] asin=%s provider=scrapedo_amazon", asin)
    response = ScrapeDoAmazonProvider().product(asin)
    metadata = {
        "provider": response.provider,
        "status_code": response.status_code,
        "request_cost": response.request_cost,
        "provider_duration_ms": response.duration_ms,
    }
    extract_amazon_product.last_provider_metadata = metadata
    logger.info(
        "[AMAZON PRODUCT] asin=%s provider=%s status=%s request_cost=%s",
        asin, response.provider, response.status_code, response.request_cost or "unknown",
    )
    parse_started = time.monotonic()
    data = normalize_scrapedo_amazon_product(response.data or {}, url)
    report = build_amazon_product_quality_report(data)
    metadata["parse_duration_ms"] = max(0, round((time.monotonic() - parse_started) * 1000))
    extract_amazon_product.last_provider_metadata = metadata
    _log_quality(asin, response.provider, report)
    if not report["valid"]:
        logger.error(
            "[AMAZON PRODUCT] asin=%s validation=failed missing_required=%s",
            asin, ",".join(report["missing_required"]),
        )
        raise ValueError(f"Amazon product data is incomplete: {', '.join(report['missing_required'])}")
    logger.info("[AMAZON PRODUCT] asin=%s validation=passed", asin)
    return data
