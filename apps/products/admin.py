from django.contrib import admin
from django.utils.html import format_html
from unfold.admin import ModelAdmin

from apps.products.models import Product, ProductPrice


class ProductPriceInline(admin.TabularInline):
    model = ProductPrice
    extra = 0
    fields = (
        "platform",
        "price",
        "mrp",
        "discount_percent",
        "affiliate_url",
        "last_updated",
    )
    readonly_fields = ("last_updated",)


@admin.register(Product)
class ProductAdmin(ModelAdmin):
    list_display = (
        "image_preview",
        "title",
        "category",
        "brand",
        "marketplace_image_preview",
        "amazon_offer_status",
        "flipkart_offer_status",
        "rating",
        "review_count",
        "is_active",
        "created_at",
    )
    list_filter = ("is_active", "category", "brand", "created_at", "updated_at")
    search_fields = ("title", "slug", "brand", "short_description", "description")
    ordering = ("title",)
    readonly_fields = ("id", "image_preview", "marketplace_image_preview", "created_at", "updated_at")
    prepopulated_fields = {"slug": ("title",)}
    autocomplete_fields = ("category",)
    list_select_related = ("category",)
    inlines = (ProductPriceInline,)

    @admin.display(description="Image")
    def image_preview(self, obj: Product) -> str:
        image_url = obj.marketplace_image_url or (obj.featured_image.url if obj.featured_image else "")
        if not image_url:
            return "-"

        return format_html(
            '<img src="{}" alt="{}" width="48" height="48" />',
            image_url,
            obj.title,
        )

    @admin.display(description="Marketplace image")
    def marketplace_image_preview(self, obj: Product) -> str:
        if not obj.marketplace_image_url:
            return "-"
        return format_html(
            '<img src="{}" alt="{}" width="96" height="96" style="object-fit:contain" />',
            obj.marketplace_image_url,
            obj.title,
        )

    @admin.display(description="Amazon")
    def amazon_offer_status(self, obj: Product) -> str:
        return "✓" if obj.amazon_products.filter(published=True).exists() else "—"

    @admin.display(description="Flipkart")
    def flipkart_offer_status(self, obj: Product) -> str:
        return "✓" if obj.flipkart_products.filter(published=True).exists() else "—"


@admin.register(ProductPrice)
class ProductPriceAdmin(ModelAdmin):
    list_display = (
        "product",
        "platform",
        "price",
        "mrp",
        "discount_percent",
        "last_updated",
    )
    list_filter = ("platform", "last_updated")
    search_fields = ("product__title", "product__slug", "affiliate_url")
    ordering = ("product__title", "platform")
    readonly_fields = ("id", "last_updated")
    autocomplete_fields = ("product",)
    list_select_related = ("product",)
