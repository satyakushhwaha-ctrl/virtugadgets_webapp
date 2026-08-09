import uuid
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.categories.management.commands.seed_categories import CATEGORIES
from apps.categories.models import Category


class CategoryModelTests(TestCase):
    def test_category_uses_uuid_primary_key_and_string_name(self) -> None:
        category = Category.objects.create(name="Mobiles", slug="mobiles")

        self.assertIsInstance(category.id, uuid.UUID)
        self.assertEqual(str(category), "Mobiles")
        self.assertTrue(category.is_active)

    def test_categories_order_by_display_order_then_name(self) -> None:
        Category.objects.create(name="Gaming", slug="gaming", display_order=20)
        Category.objects.create(name="Accessories", slug="accessories", display_order=10)
        Category.objects.create(name="Beauty", slug="beauty", display_order=10)

        self.assertEqual(
            list(Category.objects.values_list("name", flat=True)),
            ["Accessories", "Beauty", "Gaming"],
        )


class SeedCategoriesCommandTests(TestCase):
    def test_seed_categories_is_idempotent(self) -> None:
        out = StringIO()

        call_command("seed_categories", stdout=out)
        call_command("seed_categories", stdout=out)

        self.assertEqual(Category.objects.count(), len(CATEGORIES))
        self.assertTrue(Category.objects.filter(name="Mobiles").exists())
        self.assertTrue(Category.objects.filter(name="Home Appliances").exists())
