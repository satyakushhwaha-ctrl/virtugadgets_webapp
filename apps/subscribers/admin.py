from django.contrib import admin

from apps.subscribers.models import Subscriber


@admin.register(Subscriber)
class SubscriberAdmin(admin.ModelAdmin):
    list_display = ("email", "name", "phone", "is_active", "created_at")
    list_filter = ("is_active", "created_at")
    search_fields = ("email", "name", "phone")
    ordering = ("-created_at",)
    readonly_fields = ("id", "created_at")
