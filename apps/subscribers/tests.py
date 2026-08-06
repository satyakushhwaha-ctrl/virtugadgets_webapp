import uuid

from django.test import TestCase

from apps.subscribers.models import Subscriber


class SubscriberModelTests(TestCase):
    def test_subscriber_uses_uuid_primary_key_and_string_email(self) -> None:
        subscriber = Subscriber.objects.create(
            name="Test User",
            email="test@example.com",
            phone="9999999999",
        )

        self.assertIsInstance(subscriber.id, uuid.UUID)
        self.assertEqual(str(subscriber), "test@example.com")
        self.assertTrue(subscriber.is_active)
