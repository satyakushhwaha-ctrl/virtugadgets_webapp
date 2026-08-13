from typing import Any
from urllib.parse import urlencode

from django.db.models import Prefetch, QuerySet
from django.http import Http404
from django.urls import reverse
from django.shortcuts import redirect
from django.views.generic import DetailView, ListView

from apps.categories.models import Category
from apps.importer.models import AmazonProduct, FlipkartProduct, ProductMatch
from apps.products.models import Product, ProductPrice
from apps.products.services import (
    build_product_card,
    build_product_detail_context,
    get_product_image_urls,
    get_product_detail_queryset,
    get_related_product_cards,
    get_search_queryset,
)


class ProductListView(ListView):
    model = Product
    template_name = "products/list.html"
    context_object_name = "products"
    paginate_by = 12

    def get_queryset(self) -> QuerySet[Product]:
        category_slug = self.request.GET.get("category")
        prices = ProductPrice.objects.order_by("platform")
        image_source_matches = ProductMatch.objects.filter(
            match_status="published",
        ).select_related("amazon_product")

        queryset = (
            Product.objects.public()
            .select_related("category")
            .prefetch_related(
                Prefetch("prices", queryset=prices, to_attr="list_prices"),
                Prefetch(
                    "importer_product_matches",
                    queryset=image_source_matches,
                    to_attr="image_source_matches",
                ),
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
            )
            .order_by("-created_at")
        )

        if category_slug:
            try:
                category = Category.objects.get(slug=category_slug, is_active=True)
                self.category = category
                queryset = queryset.filter(category=category)
            except Category.DoesNotExist:
                raise Http404("Category not found")
        else:
            self.category = None

        return queryset

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["current_category"] = getattr(self, "category", None)
        context["product_cards"] = [
            self._build_product_card(product) for product in context["products"]
        ]
        return context

    def _build_product_card(self, product: Product) -> dict[str, Any]:
        return build_product_card(product, price_attribute="list_prices")


class SearchView(ListView):
    template_name = "products/search.html"
    context_object_name = "products"
    paginate_by = 12

    def dispatch(self, request, *args: Any, **kwargs: Any):
        self.search_query = request.GET.get("q", "").strip()
        if not self.search_query:
            return redirect("product-list")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self) -> QuerySet[Product]:
        return get_search_queryset(self.search_query)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["search_query"] = self.search_query
        context["result_count"] = context["paginator"].count
        context["canonical_url"] = self.request.build_absolute_uri(
            f"{reverse('search')}?{urlencode({'q': self.search_query})}"
        )
        context["product_cards"] = [
            build_product_card(
                product,
                price_attribute="search_prices",
                highlight_query=self.search_query,
            )
            for product in context["products"]
        ]
        return context


class ProductDetailView(DetailView):
    template_name = "products/detail.html"
    context_object_name = "product"
    slug_field = "slug"
    slug_url_kwarg = "slug"

    def get_queryset(self) -> QuerySet[Product]:
        return get_product_detail_queryset()

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        product = self.object
        canonical_url = self.request.build_absolute_uri(
            reverse("product-detail", kwargs={"slug": product.slug})
        )
        image_url, _ = get_product_image_urls(product)
        if image_url.startswith("/"):
            image_url = self.request.build_absolute_uri(image_url)
        context.update(
            build_product_detail_context(
                product,
                canonical_url=canonical_url,
                image_url=image_url,
            )
        )
        context["related_products"] = get_related_product_cards(product)
        return context
