import uuid

from django.db import models


class Category(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name="ID",
    )
    name = models.CharField(max_length=120, unique=True, verbose_name="Name")
    slug = models.SlugField(
        max_length=140,
        unique=True,
        db_index=True,
        verbose_name="Slug",
    )
    icon = models.CharField(max_length=80, blank=True, verbose_name="Icon")
    description = models.TextField(blank=True, verbose_name="Description")
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

    class Meta:
        verbose_name = "Category"
        verbose_name_plural = "Categories"
        ordering = ["name"]
        indexes = [
            models.Index(fields=["is_active", "name"], name="cat_active_name_idx"),
            models.Index(fields=["created_at"], name="cat_created_at_idx"),
        ]

    def __str__(self) -> str:
        return self.name
