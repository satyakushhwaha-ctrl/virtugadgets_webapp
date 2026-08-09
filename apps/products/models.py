import uuid
from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from apps.categories.models import Category


class ProductQuerySet(models.QuerySet):
    def public(self) -> "ProductQuerySet":
        return self.filter(is_active=True, category__is_active=True)


class Product(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name="ID",
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="products",
        verbose_name="Category",
    )
    title = models.CharField(max_length=255, verbose_name="Title")
    slug = models.SlugField(
        max_length=280,
        unique=True,
        db_index=True,
        verbose_name="Slug",
    )
    brand = models.CharField(max_length=120, blank=True, verbose_name="Brand")
    featured_image = models.ImageField(
        upload_to="products/featured/",
        blank=True,
        verbose_name="Featured image",
    )
    short_description = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Short description",
    )
    description = models.TextField(blank=True, verbose_name="Description")
    rating = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[
            MinValueValidator(Decimal("0.00")),
            MaxValueValidator(Decimal("5.00")),
        ],
        verbose_name="Rating",
    )
    review_count = models.PositiveIntegerField(
        default=0,
        verbose_name="Review count",
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name="Active",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Created at",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Updated at")

    objects = ProductQuerySet.as_manager()

    class Meta:
        verbose_name = "Product"
        verbose_name_plural = "Products"
        ordering = ["title"]
        indexes = [
            models.Index(fields=["category", "is_active"], name="prod_cat_active_idx"),
            models.Index(fields=["brand"], name="prod_brand_idx"),
            models.Index(fields=["created_at"], name="prod_created_at_idx"),
        ]

    def __str__(self) -> str:
        return self.title


class ProductPrice(models.Model):
    class Platform(models.TextChoices):
        AMAZON = "AMAZON", "Amazon"
        FLIPKART = "FLIPKART", "Flipkart"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name="ID",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="prices",
        verbose_name="Product",
    )
    platform = models.CharField(
        max_length=20,
        choices=Platform.choices,
        verbose_name="Platform",
    )
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name="Price",
    )
    mrp = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name="MRP",
    )
    discount_percent = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(100)],
        verbose_name="Discount percent",
    )
    affiliate_url = models.URLField(
        max_length=2048,
        verbose_name="Affiliate URL",
    )
    last_updated = models.DateTimeField(auto_now=True, verbose_name="Last updated")

    class Meta:
        verbose_name = "Product price"
        verbose_name_plural = "Product prices"
        ordering = ["product", "platform"]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "platform"],
                name="unique_product_platform_price",
            ),
            models.CheckConstraint(
                condition=Q(price__gte=0),
                name="product_price_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(mrp__gte=0) | Q(mrp__isnull=True),
                name="product_price_mrp_non_negative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(discount_percent__gte=0)
                    & Q(discount_percent__lte=100)
                ),
                name="product_price_discount_range",
            ),
        ]
        indexes = [
            models.Index(fields=["platform"], name="price_platform_idx"),
            models.Index(fields=["last_updated"], name="price_last_updated_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.product} - {self.get_platform_display()}"
