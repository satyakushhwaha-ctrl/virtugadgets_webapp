from typing import Any

from django.db import models
from django.db.models import Count, Prefetch, QuerySet

from apps.categories.models import Category
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
    products = (
        Product.objects.public()
        .select_related("category")
        .prefetch_related(Prefetch("prices", queryset=prices, to_attr="home_prices"))
        .order_by("-created_at")[:limit]
    )

    return [
        build_product_card(product, price_attribute="home_prices")
        for product in products
    ]
