import json
import re
from decimal import Decimal
from typing import Any

from django.db.models import Prefetch, Q, QuerySet
from django.urls import reverse
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe

from .models import Product, ProductPrice


def get_product_detail_queryset() -> QuerySet[Product]:
    prices = ProductPrice.objects.order_by("platform")
    return (
        Product.objects.public()
        .select_related("category")
        .prefetch_related(Prefetch("prices", queryset=prices, to_attr="detail_prices"))
    )


def get_search_queryset(query: str) -> QuerySet[Product]:
    prices = ProductPrice.objects.order_by("platform")
    return (
        Product.objects.public()
        .filter(
            Q(title__icontains=query)
            | Q(brand__icontains=query)
            | Q(category__name__icontains=query)
        )
        .select_related("category")
        .prefetch_related(Prefetch("prices", queryset=prices, to_attr="search_prices"))
        .order_by("-created_at", "title")
        .distinct()
    )


def get_related_product_cards(product: Product, limit: int = 4) -> list[dict[str, Any]]:
    prices = ProductPrice.objects.order_by("platform")
    products = (
        Product.objects.public().filter(
            category_id=product.category_id,
        )
        .exclude(pk=product.pk)
        .select_related("category")
        .prefetch_related(Prefetch("prices", queryset=prices, to_attr="card_prices"))
        .order_by("-created_at", "title")[:limit]
    )
    return [
        build_product_card(related, price_attribute="card_prices")
        for related in products
    ]


def build_product_card(
    product: Product,
    *,
    price_attribute: str,
    highlight_query: str = "",
) -> dict[str, Any]:
    prices_by_platform = {
        price.platform: price
        for price in getattr(product, price_attribute, [])
    }
    amazon = prices_by_platform.get(ProductPrice.Platform.AMAZON)
    flipkart = prices_by_platform.get(ProductPrice.Platform.FLIPKART)
    lowest_platform = get_lowest_platform(amazon=amazon, flipkart=flipkart)

    card = {
        "product": product,
        "title": product.title,
        "brand": product.brand or "Featured",
        "image_url": product.featured_image.url if product.featured_image else "",
        "amazon_price": format_price(amazon.price if amazon else None),
        "flipkart_price": format_price(flipkart.price if flipkart else None),
        "is_amazon_lowest": lowest_platform == ProductPrice.Platform.AMAZON,
        "is_flipkart_lowest": lowest_platform == ProductPrice.Platform.FLIPKART,
        "details_url": reverse("product-detail", kwargs={"slug": product.slug}),
    }
    if highlight_query:
        card["highlighted_title"] = highlight_text(product.title, highlight_query)
    return card


def highlight_text(text: str, query: str) -> str:
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    parts: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        parts.append(conditional_escape(text[cursor:match.start()]))
        parts.append(
            '<mark class="rounded bg-brand-100 px-0.5 text-brand-800">'
            f"{conditional_escape(match.group(0))}</mark>"
        )
        cursor = match.end()
    parts.append(conditional_escape(text[cursor:]))
    return mark_safe("".join(parts))


def build_product_detail_context(
    product: Product,
    *,
    canonical_url: str,
    image_url: str,
) -> dict[str, Any]:
    prices_by_platform = {
        price.platform: price for price in product.detail_prices
    }
    amazon = prices_by_platform.get(ProductPrice.Platform.AMAZON)
    flipkart = prices_by_platform.get(ProductPrice.Platform.FLIPKART)
    offers = [
        build_offer(amazon, ProductPrice.Platform.AMAZON),
        build_offer(flipkart, ProductPrice.Platform.FLIPKART),
    ]
    available_offers = [offer for offer in offers if offer["price_value"] is not None]
    best_offer = min(
        available_offers,
        key=lambda offer: offer["price_value"],
        default=None,
    )
    if best_offer:
        best_offer["is_best"] = True
    description = product.short_description or product.description
    seo_description = (description or f"Compare prices for {product.title}.")[:160]

    return {
        "offers": offers,
        "best_price_display": best_offer["price_display"] if best_offer else "",
        "canonical_url": canonical_url,
        "image_url": image_url,
        "seo_description": seo_description,
        "product_schema_json": build_product_schema(
            product,
            offers=available_offers,
            image_url=image_url,
            canonical_url=canonical_url,
        ),
    }


def build_offer(
    price: ProductPrice | None,
    platform: str,
) -> dict[str, Any]:
    if price is None:
        return {
            "platform": platform,
            "platform_label": dict(ProductPrice.Platform.choices).get(
                platform,
                platform.title(),
            ),
            "is_amazon": platform == ProductPrice.Platform.AMAZON,
            "price_display": "Not listed",
            "price_value": None,
            "mrp_display": "",
            "savings_display": "",
            "affiliate_url": "",
            "is_best": False,
        }

    savings = max(price.mrp - price.price, Decimal("0")) if price.mrp else None
    return {
        "platform": platform,
        "platform_label": price.get_platform_display(),
        "is_amazon": platform == ProductPrice.Platform.AMAZON,
        "price_display": format_currency(price.price),
        "price_value": price.price,
        "mrp_display": format_currency(price.mrp) if price.mrp else "",
        "savings_display": format_currency(savings) if savings else "",
        "affiliate_url": price.affiliate_url or "",
        "is_best": False,
    }


def build_product_schema(
    product: Product,
    *,
    offers: list[dict[str, Any]],
    image_url: str,
    canonical_url: str,
) -> str:
    schema: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": product.title,
        "description": product.short_description or product.description,
        "url": canonical_url,
        "brand": {
            "@type": "Brand",
            "name": product.brand or "VirtuGadgets",
        },
        "offers": [
            {
                "@type": "Offer",
                "url": offer["affiliate_url"] or canonical_url,
                "priceCurrency": "INR",
                "price": str(offer["price_value"]),
                "availability": "https://schema.org/InStock",
            }
            for offer in offers
        ],
    }
    if image_url:
        schema["image"] = [image_url]
    if product.review_count and product.rating:
        schema["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": str(product.rating),
            "reviewCount": product.review_count,
        }
    return json.dumps(schema, ensure_ascii=True).replace("<", "\\u003c")


def get_lowest_platform(
    *,
    amazon: ProductPrice | None,
    flipkart: ProductPrice | None,
) -> str:
    if amazon and flipkart:
        return (
            ProductPrice.Platform.AMAZON
            if amazon.price <= flipkart.price
            else ProductPrice.Platform.FLIPKART
        )
    if amazon:
        return ProductPrice.Platform.AMAZON
    if flipkart:
        return ProductPrice.Platform.FLIPKART
    return ""


def format_price(price: Decimal | None) -> str:
    if price is None:
        return "Not listed"
    return f"Rs. {price:,.0f}"


def format_currency(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"₹{value:,.0f}"
