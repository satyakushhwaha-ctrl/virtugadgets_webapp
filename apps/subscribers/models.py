import uuid

from django.db import models


class Subscriber(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name="ID",
    )
    name = models.CharField(max_length=120, blank=True, verbose_name="Name")
    email = models.EmailField(unique=True, verbose_name="Email address")
    phone = models.CharField(max_length=20, blank=True, verbose_name="Phone number")
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name="Active",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Created at",
    )

    class Meta:
        verbose_name = "Subscriber"
        verbose_name_plural = "Subscribers"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["is_active", "-created_at"],
                name="sub_active_created_idx",
            ),
            models.Index(fields=["created_at"], name="sub_created_at_idx"),
        ]

    def __str__(self) -> str:
        return self.email
