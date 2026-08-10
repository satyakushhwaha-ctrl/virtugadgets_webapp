"""Persistence workflow for Amazon search imports."""

from dataclasses import dataclass

from django.db import transaction

from ..models import AmazonSearchResult, ImportStatus, SearchKeyword
from . import amazon_search


@dataclass(frozen=True)
class SearchImportSummary:
    results_found: int
    saved: int
    skipped_duplicates: int
    total_results: int


def run_amazon_search_for_keyword(search_keyword: SearchKeyword) -> SearchImportSummary:
    """Run the scraper and persist results for one SearchKeyword."""
    search_keyword.status = ImportStatus.RUNNING
    search_keyword.save(update_fields=["status", "updated_at"])

    try:
        results = amazon_search.search_amazon(search_keyword.keyword)
        with transaction.atomic():
            saved = 0
            skipped_duplicates = 0
            seen_asins = set()
            for result in results:
                asin = result["asin"].strip().upper()
                if asin in seen_asins:
                    skipped_duplicates += 1
                    continue
                seen_asins.add(asin)
                _, created = AmazonSearchResult.objects.get_or_create(
                    keyword=search_keyword,
                    asin=asin,
                    defaults={
                        "title": result["title"],
                        "product_url": result["product_url"],
                        "position": result["position"],
                        "sponsored": result["sponsored"],
                    },
                )
                if created:
                    saved += 1
                else:
                    skipped_duplicates += 1

            total_results = search_keyword.amazon_results.count()
            search_keyword.status = ImportStatus.COMPLETED
            search_keyword.total_results = total_results
            search_keyword.save(update_fields=["status", "total_results", "updated_at"])
    except Exception:
        search_keyword.status = ImportStatus.FAILED
        search_keyword.save(update_fields=["status", "updated_at"])
        raise

    return SearchImportSummary(
        results_found=len(results),
        saved=saved,
        skipped_duplicates=skipped_duplicates,
        total_results=total_results,
    )
