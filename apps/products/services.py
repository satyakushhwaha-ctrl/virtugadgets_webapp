import json
import re
from decimal import Decimal
from typing import Any

from django.db.models import Prefetch, Q, QuerySet
from django.urls import reverse
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe

from apps.importer.models import AmazonProduct, FlipkartProduct, ProductMatch

from .models import Product, ProductPrice


def first_valid_image_url(images) -> str:
    """Return the first HTTP(S) image URL from extracted image data."""
    if isinstance(images, str):
        try:
            decoded_images = json.loads(images)
        except (TypeError, ValueError):
            decoded_images = images
        images = [decoded_images] if isinstance(decoded_images, str) else decoded_images
    if not isinstance(images, (list, tuple)):
        return ""
    for image in images:
        if not isinstance(image, str):
            continue
        image = image.strip()
        if image.startswith(("http://", "https://")):
            return image
    return ""


def get_product_image_urls(product: Product) -> tuple[str, str]:
    """Return a remote marketplace image first, then the uploaded image."""
    marketplace_image_url = (product.marketplace_image_url or "").strip()
    uploaded_image_url = product.featured_image.url if product.featured_image else ""
    if marketplace_image_url:
        return marketplace_image_url, uploaded_image_url

    matches = getattr(product, "image_source_matches", None)
    if matches is None:
        matches = product.importer_product_matches.filter(
            match_status="published",
        ).select_related("amazon_product").all()
    amazon_image_url = next(
        (
            image_url
            for match in matches
            if (image_url := first_valid_image_url(match.amazon_product.images))
        ),
        "",
    )
    if not amazon_image_url:
        amazon_products = getattr(product, "published_amazon_products", None)
        if amazon_products is None:
            amazon_products = product.amazon_products.filter(published=True)
        amazon_image_url = next(
            (
                image_url
                for amazon_product in amazon_products
                if (image_url := first_valid_image_url(amazon_product.images))
            ),
            "",
        )
    flipkart_products = getattr(product, "published_flipkart_products", None)
    if not amazon_image_url and flipkart_products is None:
        flipkart_products = product.flipkart_products.filter(published=True)
    if flipkart_products is None:
        flipkart_products = []
    flipkart_image_url = next(
        (
            image_url
            for flipkart_product in flipkart_products or []
            if (image_url := first_valid_image_url(flipkart_product.images))
        ),
        "",
    )
    return amazon_image_url or flipkart_image_url or uploaded_image_url, uploaded_image_url


def get_product_detail_queryset() -> QuerySet[Product]:
    prices = ProductPrice.objects.order_by("platform")
    image_source_matches = ProductMatch.objects.filter(
        match_status="published",
    ).select_related("amazon_product")
    return (
        Product.objects.public()
        .select_related("category")
        .prefetch_related(
            Prefetch("prices", queryset=prices, to_attr="detail_prices"),
            Prefetch(
                "amazon_products",
                queryset=AmazonProduct.objects.filter(published=True),
                to_attr="published_amazon_products",
            ),
            Prefetch(
                "flipkart_products",
                queryset=FlipkartProduct.objects.filter(published=True),
                to_attr="published_flipkart_products",
            ),
            Prefetch(
                "importer_product_matches",
                queryset=image_source_matches,
                to_attr="image_source_matches",
            ),
        )
    )


def get_search_queryset(query: str) -> QuerySet[Product]:
    prices = ProductPrice.objects.order_by("platform")
    image_source_matches = ProductMatch.objects.filter(
        match_status="published",
    ).select_related("amazon_product")
    return (
        Product.objects.public()
        .filter(
            Q(title__icontains=query)
            | Q(brand__icontains=query)
            | Q(category__name__icontains=query)
        )
        .select_related("category")
        .prefetch_related(
            Prefetch("prices", queryset=prices, to_attr="search_prices"),
            Prefetch(
                "amazon_products",
                queryset=AmazonProduct.objects.filter(published=True),
                to_attr="published_amazon_products",
            ),
            Prefetch(
                "flipkart_products",
                queryset=FlipkartProduct.objects.filter(published=True),
                to_attr="published_flipkart_products",
            ),
            Prefetch(
                "importer_product_matches",
                queryset=image_source_matches,
                to_attr="image_source_matches",
            ),
        )
        .order_by("-created_at", "title")
        .distinct()
    )


def get_related_product_cards(product: Product, limit: int = 4) -> list[dict[str, Any]]:
    prices = ProductPrice.objects.order_by("platform")
    image_source_matches = ProductMatch.objects.filter(
        match_status="published",
    ).select_related("amazon_product")
    products = (
        Product.objects.public().filter(
            category_id=product.category_id,
        )
        .exclude(pk=product.pk)
        .select_related("category")
        .prefetch_related(
            Prefetch("prices", queryset=prices, to_attr="card_prices"),
            Prefetch(
                "amazon_products",
                queryset=AmazonProduct.objects.filter(published=True),
                to_attr="published_amazon_products",
            ),
            Prefetch(
                "flipkart_products",
                queryset=FlipkartProduct.objects.filter(published=True),
                to_attr="published_flipkart_products",
            ),
            Prefetch(
                "importer_product_matches",
                queryset=image_source_matches,
                to_attr="image_source_matches",
            ),
        )
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
    available_prices = [price for price in (amazon, flipkart) if price is not None]
    image_url, fallback_image_url = get_product_image_urls(product)

    card = {
        "product": product,
        "title": product.title,
        "brand": product.brand or "Featured",
        "image_url": image_url,
        "fallback_image_url": fallback_image_url,
        "amazon_price": format_price(amazon.price if amazon else None),
        "flipkart_price": format_price(flipkart.price if flipkart else None),
        "has_amazon": amazon is not None,
        "has_flipkart": flipkart is not None,
        "lowest_price": (
            format_price(min(available_prices, key=lambda price: price.price).price)
            if available_prices
            else ""
        ),
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
        build_offer(amazon, ProductPrice.Platform.AMAZON)
        for amazon in [amazon]
        if amazon is not None
    ] + [
        build_offer(flipkart, ProductPrice.Platform.FLIPKART)
        for flipkart in [flipkart]
        if flipkart is not None
    ]
    available_offers = [offer for offer in offers if offer["price_value"] is not None]
    best_offer = min(
        available_offers,
        key=lambda offer: offer["price_value"],
        default=None,
    )
    if best_offer:
        best_offer["is_best"] = True
    amazon_source = next(iter(getattr(product, "published_amazon_products", [])), None)
    amazon_description = getattr(amazon_source, "description", "") if amazon_source else ""
    highlights = _clean_detail_values(
        getattr(amazon_source, "highlights", []),
        list_value=True,
    ) if amazon_source else []
    amazon_specifications = _build_amazon_specifications(amazon_source) if amazon_source else []
    description = amazon_description or product.short_description or product.description
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
        "amazon_details": {
            "source": amazon_source,
            "description": description,
            "highlights": highlights,
            "specifications": amazon_specifications,
        },
    }


def _clean_detail_values(value, *, list_value=False):
    """Return display-safe extracted values, omitting null/empty structures."""
    if list_value:
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item).strip() for item in value if str(item).strip() and str(item).strip() not in {"None", "null", "{}", "[]"}]
    if value is None or value == "":
        return ""
    if isinstance(value, (dict, list, tuple)):
        if not value:
            return ""
        value = ", ".join(f"{key}: {item}" for key, item in value.items()) if isinstance(value, dict) else ", ".join(map(str, value))
    result = str(value).strip()
    return "" if result.casefold() in {"none", "null", "{}", "[]"} else result


def _build_amazon_specifications(amazon_product) -> list[dict[str, str]]:
    if amazon_product is None:
        return []
    fields = (
        ("Brand", "brand"),
        ("Model", "product_title"),
        ("RAM", "ram"),
        ("Storage", "storage"),
        ("Processor", "processor"),
        ("Operating system", "operating_system"),
        ("Display size", "display_size"),
        ("Resolution", "resolution"),
        ("Color", "color"),
        ("Weight", "weight_kg"),
        ("Software", "software"),
        ("Warranty", "warranty"),
        ("Seller", "primary_seller"),
        ("Availability", "availability"),
        ("MRP", "mrp_inr"),
        ("Selling price", "current_selling_price_inr"),
        ("Minimum price", "selling_price_min_inr"),
        ("Maximum price", "selling_price_max_inr"),
    )
    specifications = []
    seen = set()
    for label, field in fields:
        value = _clean_detail_values(getattr(amazon_product, field, ""))
        if field == "weight_kg" and value:
            value = f"{value} kg"
        elif field in {"mrp_inr", "current_selling_price_inr", "selling_price_min_inr", "selling_price_max_inr"} and value:
            value = f"₹{value}"
        if value:
            specifications.append({"label": label, "value": value})
            seen.add(label.casefold())
    for key, value in (getattr(amazon_product, "specifications", {}) or {}).items():
        label = _clean_detail_values(key)
        display_value = _clean_detail_values(value)
        if label and display_value and label.casefold() not in seen:
            specifications.append({"label": label, "value": display_value})
    return specifications


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
    return f"₹{price:,.0f}"


def format_currency(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"₹{value:,.0f}"
