import uuid
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace

from django.core.management import call_command
from django.db import IntegrityError
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.categories.models import Category
from apps.importer.models import AmazonProduct
from apps.products.management.commands.seed_products import PRODUCTS
from apps.products.models import Product, ProductPrice
from apps.products.services import build_product_card


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

    def test_public_queryset_excludes_products_in_inactive_categories(self) -> None:
        self.category.is_active = False
        self.category.save(update_fields=["is_active"])
        product = Product.objects.create(
            category=self.category,
            title="Hidden product",
            slug="hidden-product",
        )

        self.assertFalse(Product.objects.public().filter(pk=product.pk).exists())


class ProductCardImageTests(TestCase):
    def setUp(self) -> None:
        self.category = Category.objects.create(name="Mobiles", slug="mobiles")
        self.product = Product.objects.create(
            category=self.category,
            title="iPhone 15",
            slug="iphone-15-image-test",
            brand="Apple",
            featured_image="products/featured/iphone-15.jpg",
        )

    def build_card(self, images):
        self.product.image_source_matches = [
            SimpleNamespace(amazon_product=AmazonProduct(images=images))
        ]
        return build_product_card(
            self.product,
            price_attribute="missing_prices",
        )

    def test_marketplace_image_is_preferred_over_uploaded_fallback(self) -> None:
        card = self.build_card(["", "not-a-url", "https://images.example/second.jpg"])

        self.assertEqual(card["image_url"], "https://images.example/second.jpg")
        self.assertEqual(card["fallback_image_url"], "/media/products/featured/iphone-15.jpg")

    def test_uploaded_image_is_used_when_amazon_images_are_empty(self) -> None:
        card = self.build_card([])

        self.assertEqual(card["image_url"], "/media/products/featured/iphone-15.jpg")

    def test_json_encoded_amazon_images_are_supported_safely(self) -> None:
        card = self.build_card('["", "https://images.example/iphone.jpg"]')

        self.assertEqual(card["image_url"], "https://images.example/iphone.jpg")
        self.assertEqual(
            self.build_card("not valid json")["image_url"],
            "/media/products/featured/iphone-15.jpg",
        )

    def test_product_card_renders_amazon_image_with_uploaded_fallback(self) -> None:
        card = self.build_card(["https://images.example/iphone.jpg"])

        html = render_to_string("components/product_card.html", {"product": card})

        self.assertIn('src="https://images.example/iphone.jpg"', html)
        self.assertIn("/media/products/featured/iphone-15.jpg", html)
        self.assertIn("object-contain", html)

    def test_product_card_is_safe_when_neither_image_exists(self) -> None:
        self.product.featured_image = ""
        card = self.build_card(None)

        self.assertEqual(card["image_url"], "")


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


class SeedProductsCommandTests(TestCase):
    def test_seed_products_is_idempotent(self) -> None:
        out = StringIO()

        call_command("seed_products", stdout=out)
        call_command("seed_products", stdout=out)

        self.assertEqual(Product.objects.count(), len(PRODUCTS))
        self.assertEqual(ProductPrice.objects.count(), len(PRODUCTS) * 2)
        self.assertTrue(Product.objects.filter(brand="Apple").exists())
        self.assertTrue(Product.objects.filter(brand="Puma").exists())


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
class ProductDetailViewTests(TestCase):
    def setUp(self) -> None:
        self.category = Category.objects.create(
            name="Mobiles",
            slug="mobiles",
            description="Smartphones and mobile accessories.",
        )
        self.other_category = Category.objects.create(
            name="Laptops",
            slug="laptops",
        )
        self.inactive_category = Category.objects.create(
            name="Inactive Mobiles",
            slug="inactive-mobiles",
            is_active=False,
        )
        self.product = Product.objects.create(
            category=self.category,
            title="iPhone 14 128GB",
            slug="apple-iphone-14-128gb",
            brand="Apple",
            short_description="A compact smartphone with reliable performance.",
            description="Detailed product information.",
            rating=Decimal("4.50"),
            review_count=125,
        )
        ProductPrice.objects.create(
            product=self.product,
            platform=ProductPrice.Platform.AMAZON,
            price=Decimal("54999.00"),
            mrp=Decimal("69999.00"),
            affiliate_url="https://www.amazon.in/example",
        )
        ProductPrice.objects.create(
            product=self.product,
            platform=ProductPrice.Platform.FLIPKART,
            price=Decimal("55999.00"),
            mrp=Decimal("69999.00"),
            affiliate_url="https://www.flipkart.com/example",
        )
        for index in range(5):
            Product.objects.create(
                category=self.category,
                title=f"Related product {index}",
                slug=f"related-product-{index}",
                brand="Samsung",
            )
        Product.objects.create(
            category=self.other_category,
            title="Different category product",
            slug="different-category-product",
        )
        Product.objects.create(
            category=self.inactive_category,
            title="Inactive category product",
            slug="inactive-category-product",
        )

    def test_product_detail_page_renders_product_and_related_products(self) -> None:
        response = self.client.get(
            reverse("product-detail", kwargs={"slug": self.product.slug})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "iPhone 14 128GB")
        self.assertContains(response, "Best Price ₹54,999")
        self.assertContains(response, "Buy on Amazon")
        self.assertContains(response, "Buy on Flipkart")
        self.assertContains(response, 'rel="canonical"')
        self.assertContains(response, 'type="application/ld+json"')
        self.assertContains(response, "Related product 4")
        self.assertNotContains(response, "Different category product")
        self.assertEqual(len(response.context["related_products"]), 4)

    def test_inactive_product_returns_404(self) -> None:
        self.product.is_active = False
        self.product.save(update_fields=["is_active"])

        response = self.client.get(
            reverse("product-detail", kwargs={"slug": self.product.slug})
        )

        self.assertEqual(response.status_code, 404)

    def test_product_in_inactive_category_returns_404(self) -> None:
        response = self.client.get(
            reverse("product-detail", kwargs={"slug": "inactive-category-product"})
        )

        self.assertEqual(response.status_code, 404)


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
class SearchViewTests(TestCase):
    def setUp(self) -> None:
        self.mobile_category = Category.objects.create(
            name="Mobiles",
            slug="mobiles",
        )
        self.laptop_category = Category.objects.create(
            name="Laptops",
            slug="laptops",
        )
        self.inactive_category = Category.objects.create(
            name="Inactive Category",
            slug="inactive-category",
            is_active=False,
        )
        self.iphone = Product.objects.create(
            category=self.mobile_category,
            title="iPhone 15",
            slug="iphone-15-search-test",
            brand="Apple",
        )
        self.samsung = Product.objects.create(
            category=self.mobile_category,
            title="Galaxy S24",
            slug="galaxy-s24-search-test",
            brand="Samsung",
        )
        Product.objects.create(
            category=self.laptop_category,
            title="Work Laptop",
            slug="work-laptop-search-test",
            brand="Lenovo",
        )
        Product.objects.create(
            category=self.mobile_category,
            title="Inactive iPhone",
            slug="inactive-iphone-search-test",
            is_active=False,
        )
        Product.objects.create(
            category=self.inactive_category,
            title="Hidden iPhone",
            slug="hidden-iphone-search-test",
        )

    def test_search_matches_title_brand_and_category(self) -> None:
        title_response = self.client.get(reverse("search"), {"q": "iphone"})
        brand_response = self.client.get(reverse("search"), {"q": "samsung"})
        category_response = self.client.get(reverse("search"), {"q": "mobiles"})

        self.assertContains(title_response, "iPhone 15")
        self.assertNotContains(title_response, "Inactive iPhone")
        self.assertNotContains(title_response, "Hidden iPhone")
        self.assertContains(brand_response, "Galaxy S24")
        self.assertContains(category_response, "iPhone 15")
        self.assertContains(category_response, "Galaxy S24")

    def test_search_highlights_keyword_and_preserves_query_in_pagination(self) -> None:
        for index in range(13):
            Product.objects.create(
                category=self.mobile_category,
                title=f"iPhone accessory {index}",
                slug=f"iphone-accessory-{index}",
            )

        response = self.client.get(reverse("search"), {"q": "iphone"})

        self.assertContains(response, "<mark")
        self.assertContains(response, "q=iphone&amp;page=2")
        self.assertEqual(response.context["result_count"], 14)

    def test_empty_search_redirects_to_products(self) -> None:
        response = self.client.get(reverse("search"))

        self.assertRedirects(response, reverse("product-list"))

    def test_search_without_matches_shows_empty_state(self) -> None:
        response = self.client.get(reverse("search"), {"q": "nonexistent"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No products found for &quot;nonexistent&quot;.")
        self.assertContains(response, "Browse All Products")
