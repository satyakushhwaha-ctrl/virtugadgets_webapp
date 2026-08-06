import uuid

from django.test import TestCase

from apps.categories.models import Category


class CategoryModelTests(TestCase):
    def test_category_uses_uuid_primary_key_and_string_name(self) -> None:
        category = Category.objects.create(name="Mobiles", slug="mobiles")

        self.assertIsInstance(category.id, uuid.UUID)
        self.assertEqual(str(category), "Mobiles")
        self.assertTrue(category.is_active)
