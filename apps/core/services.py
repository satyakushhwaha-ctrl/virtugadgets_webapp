from typing import Any

from django.db import models
from django.db.models import Count, Prefetch, QuerySet

from apps.categories.models import Category
from apps.importer.models import AmazonProduct, FlipkartProduct, ProductMatch
from apps.products.models import Product, ProductPrice
from apps.products.services import build_product_card


def get_active_categories() -> QuerySet[Category]:
    return (
        Category.objects.filter(is_active=True)
        .annotate(product_count=Count("products", filter=models.Q(products__is_active=True)))
        .order_by("display_order", "name")
    )


def get_latest_product_cards(limit: int = 8) -> list[dict[str, Any]]:
    prices = ProductPrice.objects.order_by("platform")
    image_source_matches = ProductMatch.objects.filter(
        match_status="published",
    ).select_related("amazon_product")
    products = (
        Product.objects.public()
        .select_related("category")
        .prefetch_related(
            Prefetch("prices", queryset=prices, to_attr="home_prices"),
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
        .order_by("-created_at")[:limit]
    )

    return [
        build_product_card(product, price_attribute="home_prices")
        for product in products
    ]
