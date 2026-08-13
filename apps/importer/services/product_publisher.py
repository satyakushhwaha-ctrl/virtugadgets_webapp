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

from ..models import (
    AmazonProduct,
    ApprovalStatus,
    FlipkartProduct,
    ImportStatus,
    MatchStatus,
    ProductMatch,
)
from .product_matching import extract_model_identity, first_valid_image_url


@dataclass(frozen=True)
class PublishResult:
    product: Product
    amazon_price: Decimal | None
    flipkart_price: Decimal | None
    already_published: bool = False


class PublishValidationError(ValueError):
    """Raised when a staged match is not safe to publish."""


@transaction.atomic
def assign_staged_product_categories(model, product_ids, categories):
    """Add review categories to selected marketplace staging records.

    The operation is intentionally additive so an administrator cannot lose
    an existing category assignment through the bulk workflow.
    """
    categories = list(categories)
    if not categories:
        raise PublishValidationError("Please select at least one category.")

    products = list(
        model.objects.select_for_update().filter(pk__in=product_ids)
    )
    for product in products:
        product.categories.add(*categories)
    return products


def assign_amazon_product_categories(product_ids, categories):
    """Add active review categories to selected Amazon staging records."""
    return assign_staged_product_categories(AmazonProduct, product_ids, categories)


def approve_amazon_product(amazon_product: AmazonProduct, user=None) -> AmazonProduct:
    """Approve one extracted Amazon product without publishing it."""
    if amazon_product.status != ImportStatus.COMPLETED:
        raise PublishValidationError("AmazonProduct must be completed before approval.")
    amazon_product.approval_status = ApprovalStatus.APPROVED
    amazon_product.approved_at = timezone.now()
    amazon_product.approved_by = user if getattr(user, "is_authenticated", False) else None
    amazon_product.save(update_fields=["approval_status", "approved_at", "approved_by", "updated_at"])
    return amazon_product


def approve_flipkart_product(flipkart_product: FlipkartProduct, user=None) -> FlipkartProduct:
    """Approve one extracted Flipkart product without publishing it."""
    if flipkart_product.status != ImportStatus.COMPLETED:
        raise PublishValidationError("FlipkartProduct must be completed before approval.")
    flipkart_product.approval_status = ApprovalStatus.APPROVED
    flipkart_product.approved_at = timezone.now()
    flipkart_product.approved_by = user if getattr(user, "is_authenticated", False) else None
    flipkart_product.save(update_fields=["approval_status", "approved_at", "approved_by", "updated_at"])
    return flipkart_product


def _amazon_publish_category(amazon_product: AmazonProduct):
    categories = list(amazon_product.categories.order_by("display_order", "name"))
    if not categories:
        raise PublishValidationError("Please select at least one category before publishing.")
    if any(not category.is_active for category in categories):
        raise PublishValidationError("One or more selected categories are inactive.")
    return categories[0]


def _flipkart_publish_category(flipkart_product: FlipkartProduct):
    categories = list(flipkart_product.categories.order_by("display_order", "name"))
    if not categories:
        raise PublishValidationError("Please select at least one category before publishing.")
    if any(not category.is_active for category in categories):
        raise PublishValidationError("One or more selected categories are inactive.")
    return categories[0]


def _publish_staged_product(staged_product, *, platform, category, user=None) -> Product:
    title = (
        staged_product.product_title
        or staged_product.brand
        or getattr(staged_product, "asin", None)
        or getattr(staged_product, "pid", None)
    ).strip()[:255]
    slug = slugify(title)
    if not slug:
        raise PublishValidationError("Could not derive a safe live product slug.")

    with transaction.atomic():
        product = staged_product.published_product or Product.objects.filter(slug=slug).first()
        if product is None:
            product = Product.objects.create(
                category=category,
                title=title,
                slug=slug,
                brand=(staged_product.brand or "")[:120],
            )
        else:
            changed = []
            if product.category_id != category.pk:
                product.category = category
                changed.append("category")
            if not product.is_active:
                product.is_active = True
                changed.append("is_active")
            if changed:
                changed.append("updated_at")
                product.save(update_fields=changed)

        _upsert_price(product, platform, staged_product)
        staged_product.published_product = product
        staged_product.published = True
        staged_product.published_at = timezone.now()
        staged_product.save(update_fields=["published_product", "published", "published_at", "updated_at"])
        _refresh_marketplace_image(product.pk)
        product.refresh_from_db(fields=["marketplace_image_url"])
    return product


def publish_amazon_product(amazon_product: AmazonProduct, user=None) -> Product:
    """Publish one approved Amazon staging record into the live catalog."""
    amazon_product = AmazonProduct.objects.prefetch_related("categories").get(pk=amazon_product.pk)
    if amazon_product.approval_status != ApprovalStatus.APPROVED:
        raise PublishValidationError("AmazonProduct must be approved before publishing.")
    if amazon_product.status != ImportStatus.COMPLETED:
        raise PublishValidationError("AmazonProduct must be completed before publishing.")
    category = _amazon_publish_category(amazon_product)
    return _publish_staged_product(
        amazon_product,
        platform=ProductPrice.Platform.AMAZON,
        category=category,
        user=user,
    )


def publish_flipkart_product(flipkart_product: FlipkartProduct, user=None) -> Product:
    """Publish one approved Flipkart staging record independently."""
    flipkart_product = FlipkartProduct.objects.prefetch_related("categories").get(pk=flipkart_product.pk)
    if flipkart_product.approval_status != ApprovalStatus.APPROVED:
        raise PublishValidationError("FlipkartProduct must be approved before publishing.")
    if flipkart_product.status != ImportStatus.COMPLETED:
        raise PublishValidationError("FlipkartProduct must be completed before publishing.")
    category = _flipkart_publish_category(flipkart_product)
    return _publish_staged_product(
        flipkart_product,
        platform=ProductPrice.Platform.FLIPKART,
        category=category,
        user=user,
    )


def associate_flipkart_product(
    flipkart_product: FlipkartProduct,
    product: Product,
    user=None,
) -> Product:
    """Attach a reviewed Flipkart offer to an existing canonical Product."""
    if flipkart_product.status != ImportStatus.COMPLETED:
        raise PublishValidationError("FlipkartProduct must be completed before association.")
    with transaction.atomic():
        _upsert_price(product, ProductPrice.Platform.FLIPKART, flipkart_product)
        flipkart_product.published_product = product
        flipkart_product.published = True
        flipkart_product.approval_status = ApprovalStatus.APPROVED
        flipkart_product.published_at = timezone.now()
        flipkart_product.save(
            update_fields=[
                "published_product", "published", "approval_status",
                "published_at", "updated_at",
            ]
        )
        _refresh_marketplace_image(product.pk)
        product.refresh_from_db(fields=["marketplace_image_url"])
    return product


def _deactivate_catalog_product_if_unlinked(product_id):
    if not product_id:
        return
    if AmazonProduct.objects.filter(published_product_id=product_id, published=True).exists():
        return
    if FlipkartProduct.objects.filter(published_product_id=product_id, published=True).exists():
        return
    Product.objects.filter(pk=product_id).update(is_active=False, updated_at=timezone.now())


def _refresh_marketplace_image(product_id):
    if not product_id:
        return
    product = Product.objects.get(pk=product_id)
    image_url = ""
    amazon = AmazonProduct.objects.filter(
        published_product_id=product_id,
        published=True,
    ).order_by("updated_at").first()
    if amazon:
        image_url = first_valid_image_url(amazon)
    if not image_url:
        flipkart = FlipkartProduct.objects.filter(
            published_product_id=product_id,
            published=True,
        ).order_by("updated_at").first()
        if flipkart:
            image_url = first_valid_image_url(flipkart)
    if product.marketplace_image_url != image_url:
        product.marketplace_image_url = image_url
        Product.objects.filter(pk=product_id).update(
            marketplace_image_url=image_url,
            updated_at=timezone.now(),
        )


def unpublish_amazon_product(amazon_product: AmazonProduct) -> AmazonProduct:
    """Remove an Amazon-backed catalog product from public queries."""
    amazon_product = AmazonProduct.objects.get(pk=amazon_product.pk)
    with transaction.atomic():
        amazon_product.published = False
        amazon_product.published_at = None
        product_id = amazon_product.published_product_id
        amazon_product.save(update_fields=["published", "published_at", "updated_at"])
        ProductPrice.objects.filter(
            product_id=product_id,
            platform=ProductPrice.Platform.AMAZON,
        ).delete()
        _refresh_marketplace_image(product_id)
        _deactivate_catalog_product_if_unlinked(product_id)
    return amazon_product


def unpublish_flipkart_product(flipkart_product: FlipkartProduct) -> FlipkartProduct:
    """Unlink the current Flipkart offer without deleting the catalog product."""
    flipkart_product = FlipkartProduct.objects.get(pk=flipkart_product.pk)
    with transaction.atomic():
        product_id = flipkart_product.published_product_id
        flipkart_product.published = False
        flipkart_product.published_at = None
        flipkart_product.save(update_fields=["published", "published_at", "updated_at"])
        ProductPrice.objects.filter(
            product_id=product_id,
            platform=ProductPrice.Platform.FLIPKART,
        ).delete()
        _refresh_marketplace_image(product_id)
        _deactivate_catalog_product_if_unlinked(product_id)
    return flipkart_product


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

            product = (
                product_match.published_product
                or amazon.published_product
                or flipkart.published_product
                or Product.objects.filter(slug=slug).first()
            )
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
            now = timezone.now()
            amazon.published_product = product
            amazon.published = True
            amazon.approval_status = ApprovalStatus.APPROVED
            amazon.published_at = now
            amazon.save(
                update_fields=[
                    "published_product", "published", "approval_status",
                    "published_at", "updated_at",
                ]
            )
            flipkart.published_product = product
            flipkart.published = True
            flipkart.approval_status = ApprovalStatus.APPROVED
            flipkart.published_at = now
            flipkart.save(
                update_fields=[
                    "published_product", "published", "approval_status",
                    "published_at", "updated_at",
                ]
            )
            _refresh_marketplace_image(product.pk)
            product.refresh_from_db(fields=["marketplace_image_url"])
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
