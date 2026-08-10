"""Publish approved importer data into the live catalog.

This module is deliberately the only importer entry point that writes to the
live Product and ProductPrice tables.  It consumes staged data only; it never
opens a marketplace page or invokes an external scraper.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.products.models import Product, ProductPrice

from ..models import ImportStatus, MatchStatus, ProductMatch
from .product_matching import extract_model_identity


@dataclass(frozen=True)
class PublishResult:
    product: Product
    amazon_price: Decimal | None
    flipkart_price: Decimal | None
    already_published: bool = False


class PublishValidationError(ValueError):
    """Raised when a staged match is not safe to publish."""


def _canonical_title(amazon_product, flipkart_product) -> str:
    brand = (amazon_product.brand or flipkart_product.brand or "").strip()
    model = extract_model_identity(amazon_product) or extract_model_identity(
        flipkart_product
    )
    title = " ".join(part for part in (brand, model) if part).strip()
    if not title:
        title = (amazon_product.product_title or flipkart_product.product_title or "").strip()
    return title[:255]


def _price_value(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PublishValidationError(f"Invalid marketplace price: {value!r}") from exc


def _price_defaults(staged_product, source_url: str, current_price: Decimal):
    if not source_url:
        raise PublishValidationError("Marketplace product URL is required for pricing.")
    defaults = {
        "price": current_price,
        "affiliate_url": source_url,
    }
    if staged_product.mrp_inr is not None:
        defaults["mrp"] = Decimal(staged_product.mrp_inr)
    if staged_product.discount_percentage is not None:
        defaults["discount_percent"] = min(staged_product.discount_percentage, 100)
    return defaults


def _upsert_price(product, platform, staged_product):
    current_price = _price_value(staged_product.current_selling_price_inr)
    if current_price is None:
        return None
    defaults = _price_defaults(staged_product, staged_product.url, current_price)
    ProductPrice.objects.update_or_create(
        product=product,
        platform=platform,
        defaults=defaults,
    )
    return current_price


def _publish_category(product_match: ProductMatch):
    selected_categories = list(product_match.publish_categories.all())
    if selected_categories:
        if any(not category.is_active for category in selected_categories):
            raise PublishValidationError(
                "One or more selected categories are inactive. Please select active categories."
            )
        return selected_categories[0]
    if not product_match.publish_category_id:
        raise PublishValidationError(
            "Please select at least one active category before publishing."
        )
    if not product_match.publish_category.is_active:
        raise PublishValidationError(
            "One or more selected categories are inactive. Please select active categories."
        )
    return product_match.publish_category


def _validate_match(product_match: ProductMatch):
    if product_match.match_status not in {
        MatchStatus.APPROVED,
        MatchStatus.PUBLISHED,
        MatchStatus.PUBLISH_FAILED,
    }:
        raise PublishValidationError(
            "ProductMatch must be explicitly approved before publishing."
        )
    if product_match.match_status == MatchStatus.REJECTED:
        raise PublishValidationError("Rejected ProductMatch records cannot be published.")
    publish_category = _publish_category(product_match)

    amazon = product_match.amazon_product
    flipkart = product_match.flipkart_product
    if amazon.status != ImportStatus.COMPLETED:
        raise PublishValidationError("AmazonProduct is not completed.")
    if flipkart.status != ImportStatus.COMPLETED:
        raise PublishValidationError("FlipkartProduct is not completed.")
    if not amazon.current_selling_price_inr and not flipkart.current_selling_price_inr:
        raise PublishValidationError("At least one marketplace price is required.")
    return publish_category


def publish_product_match(product_match: ProductMatch, user=None) -> PublishResult:
    """Atomically publish one explicitly approved match.

    The function is idempotent: it reuses the ProductMatch's published product
    and the live ProductPrice uniqueness constraint prevents duplicate platform
    rows.  It raises on validation or persistence failure so callers can show a
    useful admin error.
    """
    product_match = (
        ProductMatch.objects.select_related(
            "amazon_product",
            "flipkart_product",
            "publish_category",
            "published_product",
        )
        .get(pk=product_match.pk)
    )
    publish_category = _validate_match(product_match)
    already_published = product_match.match_status == MatchStatus.PUBLISHED

    try:
        with transaction.atomic():
            amazon = product_match.amazon_product
            flipkart = product_match.flipkart_product
            title = _canonical_title(amazon, flipkart)
            slug = slugify(title)
            if not slug:
                raise PublishValidationError("Could not derive a safe live product slug.")

            product = product_match.published_product
            if product is None:
                product = Product.objects.filter(slug=slug).first()
            if product is None:
                product = Product.objects.create(
                    category=publish_category,
                    title=title,
                    slug=slug,
                    brand=(amazon.brand or flipkart.brand or "")[:120],
                )
            else:
                # Preserve curated fields such as descriptions, rating, and
                # images while applying the explicitly selected category.
                changed = []
                if product.category_id != publish_category.pk:
                    product.category = publish_category
                    changed.append("category")
                if title and product.title != title:
                    product.title = title
                    changed.append("title")
                brand = (amazon.brand or flipkart.brand or "")[:120]
                if brand and product.brand != brand:
                    product.brand = brand
                    changed.append("brand")
                if changed:
                    changed.append("updated_at")
                    product.save(update_fields=changed)

            amazon_price = _upsert_price(
                product, ProductPrice.Platform.AMAZON, amazon
            )
            flipkart_price = _upsert_price(
                product, ProductPrice.Platform.FLIPKART, flipkart
            )
            product_match.published_product = product
            if user is not None:
                product_match.published_by = user
            product_match.match_status = MatchStatus.PUBLISHED
            product_match.publish_error = ""
            product_match.published_at = timezone.now()
            product_match.save(
                update_fields=[
                    "published_product",
                    "published_by",
                    "match_status",
                    "publish_error",
                    "published_at",
                    "updated_at",
                ]
            )
    except Exception as exc:
        ProductMatch.objects.filter(pk=product_match.pk).update(
            match_status=MatchStatus.PUBLISH_FAILED,
            publish_error=str(exc) or exc.__class__.__name__,
            updated_at=timezone.now(),
        )
        raise

    return PublishResult(product, amazon_price, flipkart_price, already_published)
