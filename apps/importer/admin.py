from django.contrib import admin

from .models import SearchKeyword, AmazonSearchResult


@admin.register(SearchKeyword)
class SearchKeywordAdmin(admin.ModelAdmin):
    list_display = (
        "keyword",
        "status",
        "total_results",
        "created_at",
    )

    search_fields = ("keyword",)

    list_filter = ("status",)


@admin.register(AmazonSearchResult)
class AmazonSearchResultAdmin(admin.ModelAdmin):
    list_display = (
        "asin",
        "position",
        "sponsored",
        "processed",
    )

    search_fields = (
        "asin",
        "title",
    )

    list_filter = (
        "processed",
        "sponsored",
    )