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
    requested_pages: int = 1
    available_pages: int = 1
    scraped_pages: int = 1
    sorting: str = "Featured / Relevance"
    sorting_value: str = "relevanceblender"
    reason: str = ""


def _sorting_label(search_keyword: SearchKeyword) -> str:
    choices = dict(SearchKeyword.AMAZON_SORTING_CHOICES)
    return choices.get(search_keyword.amazon_sorting, search_keyword.amazon_sorting_label)


def _persist_results(search_keyword, results):
    saved = 0
    skipped_duplicates = 0
    seen_asins = set()
    with transaction.atomic():
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
    return saved, skipped_duplicates


def run_amazon_search_for_keyword(search_keyword: SearchKeyword) -> SearchImportSummary:
    """Search and persist up to the configured number of Amazon pages.

    The legacy one-page provider call remains intentionally supported for the
    default configuration.  Paginated/sorted searches use the metadata-aware
    provider API so the existing scraper and fallback parser stay canonical.
    """
    requested_pages = max(1, int(search_keyword.amazon_pages or 1))
    sorting_value = search_keyword.amazon_sorting or amazon_search.DEFAULT_AMAZON_SORTING
    sorting_label = _sorting_label(search_keyword)
    search_keyword.status = ImportStatus.RUNNING
    search_keyword.amazon_sorting_label = sorting_label
    search_keyword.save(update_fields=["status", "amazon_sorting_label", "updated_at"])

    results_found = 0
    saved = 0
    skipped_duplicates = 0
    scraped_pages = 0
    available_pages = requested_pages
    reason = ""

    try:
        for page_number in range(1, requested_pages + 1):
            # Keep existing callers and integrations on the proven legacy
            # one-page method unless pagination/sorting was explicitly used.
            if requested_pages == 1 and sorting_value == amazon_search.DEFAULT_AMAZON_SORTING:
                page_payload = {
                    "results": amazon_search.search_amazon(search_keyword.keyword),
                    "has_next": False,
                }
            else:
                page_payload = amazon_search.search_amazon_page(
                    search_keyword.keyword, sorting_value, page_number
                )

            page_results = page_payload.get("results", [])
            if not page_results:
                available_pages = max(0, page_number - 1)
                if available_pages < requested_pages:
                    reason = (
                        f"Requested {requested_pages} pages, but Amazon had only "
                        f"{available_pages} available pages for this search."
                    )
                break

            page_saved, page_duplicates = _persist_results(search_keyword, page_results)
            results_found += len(page_results)
            saved += page_saved
            skipped_duplicates += page_duplicates
            scraped_pages += 1

            has_next = bool(page_payload.get("has_next"))
            if page_number < requested_pages and not has_next:
                available_pages = page_number
                reason = (
                    f"Requested {requested_pages} pages, but Amazon had only "
                    f"{available_pages} available pages for this search."
                )
                break
            available_pages = page_number if not has_next else requested_pages

        total_results = search_keyword.amazon_results.count()
        search_keyword.status = ImportStatus.COMPLETED
        search_keyword.total_results = total_results
        search_keyword.save(update_fields=["status", "total_results", "updated_at"])
    except Exception:
        search_keyword.status = ImportStatus.FAILED
        search_keyword.save(update_fields=["status", "updated_at"])
        raise

    return SearchImportSummary(
        results_found=results_found,
        saved=saved,
        skipped_duplicates=skipped_duplicates,
        total_results=total_results,
        requested_pages=requested_pages,
        available_pages=available_pages,
        scraped_pages=scraped_pages,
        sorting=sorting_label,
        sorting_value=sorting_value,
        reason=reason,
    )
