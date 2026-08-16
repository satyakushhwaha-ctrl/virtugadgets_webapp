from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.categories.models import Category


@admin.register(Category)
class CategoryAdmin(ModelAdmin):
    list_display = (
        "name",
        "slug",
        "display_order",
        "is_active",
        "created_at",
        "updated_at",
    )
    list_editable = ("display_order", "is_active")
    list_filter = ("is_active", "created_at", "updated_at")
    search_fields = ("name", "slug", "description")
    ordering = ("display_order", "name")
    readonly_fields = ("id", "created_at", "updated_at")
    prepopulated_fields = {"slug": ("name",)}
