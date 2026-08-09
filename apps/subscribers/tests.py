import uuid

from django.test import TestCase
from django.urls import reverse

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


class SubscribeViewTests(TestCase):
    def setUp(self) -> None:
        self.url = reverse("subscribers:subscribe")

    def test_subscribe_post_creates_subscriber(self) -> None:
        response = self.client.post(
            self.url,
            {
                "name": " Test User ",
                "email": " TEST@example.com ",
                "phone": " 98765 43210 ",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Subscriber.objects.count(), 1)

        subscriber = Subscriber.objects.get()
        self.assertEqual(subscriber.name, "Test User")
        self.assertEqual(subscriber.email, "test@example.com")
        self.assertEqual(subscriber.phone, "9876543210")
        self.assertTrue(response.json()["ok"])

    def test_subscribe_rejects_duplicate_email(self) -> None:
        Subscriber.objects.create(
            name="Existing User",
            email="test@example.com",
            phone="9876543210",
        )

        response = self.client.post(
            self.url,
            {
                "name": "New User",
                "email": "TEST@example.com",
                "phone": "9876543211",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["message"], "You're already subscribed.")
        self.assertEqual(Subscriber.objects.count(), 1)

    def test_subscribe_rejects_duplicate_phone(self) -> None:
        Subscriber.objects.create(
            name="Existing User",
            email="existing@example.com",
            phone="9876543210",
        )

        response = self.client.post(
            self.url,
            {
                "name": "New User",
                "email": "new@example.com",
                "phone": "98765-43210",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["message"], "You're already subscribed.")
        self.assertEqual(Subscriber.objects.count(), 1)

    def test_subscribe_rejects_empty_submission(self) -> None:
        response = self.client.post(
            self.url,
            {"name": " ", "email": " ", "phone": " "},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(Subscriber.objects.count(), 0)
