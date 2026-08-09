from decimal import Decimal

from django.test import TestCase, override_settings

from apps.categories.models import Category
from apps.products.models import Product, ProductPrice


@override_settings(
    STORAGES={
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
)
class HomeViewTests(TestCase):
    def setUp(self) -> None:
        self.category = Category.objects.create(
            name="Mobiles",
            slug="mobiles",
            description="Smartphones and mobile accessories.",
            display_order=20,
        )
        self.second_category = Category.objects.create(
            name="Accessories",
            slug="accessories",
            description="Useful tech accessories and wearables.",
            display_order=10,
        )
        self.inactive_category = Category.objects.create(
            name="Hidden",
            slug="hidden",
            is_active=False,
        )

        for index in range(9):
            product = Product.objects.create(
                category=self.category,
                title=f"Product {index}",
                slug=f"product-{index}",
                brand="Samsung",
                is_active=True,
            )
            ProductPrice.objects.create(
                product=product,
                platform=ProductPrice.Platform.AMAZON,
                price=Decimal("1000.00") + index,
                mrp=Decimal("1200.00"),
                discount_percent=17,
                affiliate_url="https://www.amazon.in/",
            )
            ProductPrice.objects.create(
                product=product,
                platform=ProductPrice.Platform.FLIPKART,
                price=Decimal("900.00") + index,
                mrp=Decimal("1200.00"),
                discount_percent=25,
                affiliate_url="https://www.flipkart.com/",
            )

        Product.objects.create(
            category=self.category,
            title="Inactive Product",
            slug="inactive-product",
            brand="Apple",
            is_active=False,
        )
        Product.objects.create(
            category=self.inactive_category,
            title="Inactive Category Product",
            slug="inactive-category-product",
            brand="Apple",
        )

    def test_homepage_passes_active_categories_and_latest_products(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Compare Prices Before You Buy.")
        self.assertContains(response, "Mobiles")
        self.assertContains(response, "/products/?category=mobiles")
        self.assertContains(response, "/products/?category=accessories")
        self.assertNotContains(response, "Hidden")
        self.assertNotContains(response, "/products/?category=hidden")
        self.assertNotContains(response, "Inactive Category Product")
        self.assertContains(response, "View Details", count=8)
        self.assertEqual(len(response.context["products"]), 8)

    def test_homepage_avoids_n_plus_one_queries(self) -> None:
        with self.assertNumQueries(3):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
