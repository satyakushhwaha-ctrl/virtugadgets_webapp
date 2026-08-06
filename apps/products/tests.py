import uuid
from decimal import Decimal

from django.db import IntegrityError
from django.test import TestCase

from apps.categories.models import Category
from apps.products.models import Product, ProductPrice


class ProductModelTests(TestCase):
    def setUp(self) -> None:
        self.category = Category.objects.create(name="Mobiles", slug="mobiles")

    def test_product_uses_uuid_primary_key_and_string_title(self) -> None:
        product = Product.objects.create(
            category=self.category,
            title="iPhone 15",
            slug="iphone-15",
            brand="Apple",
        )

        self.assertIsInstance(product.id, uuid.UUID)
        self.assertEqual(str(product), "iPhone 15")
        self.assertTrue(product.is_active)


class ProductPriceModelTests(TestCase):
    def setUp(self) -> None:
        self.category = Category.objects.create(name="Mobiles", slug="mobiles")
        self.product = Product.objects.create(
            category=self.category,
            title="iPhone 15",
            slug="iphone-15",
            brand="Apple",
        )

    def test_product_price_platform_choices_and_string_value(self) -> None:
        price = ProductPrice.objects.create(
            product=self.product,
            platform=ProductPrice.Platform.AMAZON,
            price=Decimal("69999.00"),
            mrp=Decimal("79999.00"),
            discount_percent=13,
            affiliate_url="https://www.amazon.in/example",
        )

        self.assertIsInstance(price.id, uuid.UUID)
        self.assertEqual(price.platform, ProductPrice.Platform.AMAZON)
        self.assertEqual(str(price), "iPhone 15 - Amazon")

    def test_product_price_is_unique_per_product_and_platform(self) -> None:
        ProductPrice.objects.create(
            product=self.product,
            platform=ProductPrice.Platform.FLIPKART,
            price=Decimal("68999.00"),
            affiliate_url="https://www.flipkart.com/example",
        )

        with self.assertRaises(IntegrityError):
            ProductPrice.objects.create(
                product=self.product,
                platform=ProductPrice.Platform.FLIPKART,
                price=Decimal("67999.00"),
                affiliate_url="https://www.flipkart.com/duplicate",
            )
