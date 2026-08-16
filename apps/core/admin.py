"""Unfold dashboard data for the existing Django admin site."""

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone


def _admin_url(name, query="", *, args=None, kwargs=None):
    """Build an admin URL, including arguments for object-specific routes."""
    return f"{reverse(name, args=args, kwargs=kwargs)}{query}"


def _percentage(value, total):
    if not total:
        return None
    return min(100, round(value * 100 / total))


def _duration_label(job):
    if not job.started_at:
        return "—"
    end = job.completed_at or timezone.now()
    seconds = max(0, int((end - job.started_at).total_seconds()))
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}m {remainder}s" if minutes else f"{remainder}s"


def dashboard_callback(request, context):
    """Build the operator dashboard from existing catalog and job data."""
    from apps.importer.models import (
        AmazonProduct,
        FlipkartProduct,
        ImporterJob,
        ImporterJobMarketplace,
        ImporterJobStatus,
        ImporterJobType,
        MatchStatus,
        ProductMatch,
    )
    from apps.products.models import Product

    total_products = Product.objects.count()
    amazon_products = AmazonProduct.objects.count()
    flipkart_products = FlipkartProduct.objects.count()
    matched_products = ProductMatch.objects.filter(
        match_status__in=(MatchStatus.MATCHED, MatchStatus.APPROVED, MatchStatus.PUBLISHED),
    ).count()
    pending_matches = ProductMatch.objects.filter(
        match_status__in=(MatchStatus.PENDING, MatchStatus.REVIEW),
    ).count()
    running_jobs = ImporterJob.objects.filter(status=ImporterJobStatus.RUNNING).count()
    failed_jobs_count = ImporterJob.objects.filter(status=ImporterJobStatus.FAILED).count()
    completed_jobs = ImporterJob.objects.filter(status=ImporterJobStatus.COMPLETED).count()

    def _icon_style(tone):
        styles = {
            "blue": {"bg": "#eff6ff", "color": "#2563eb"},
            "orange": {"bg": "#fff7ed", "color": "#ea580c"},
            "indigo": {"bg": "#eef2ff", "color": "#4f46e5"},
            "green": {"bg": "#ecfdf5", "color": "#059669"},
            "amber": {"bg": "#fffbeb", "color": "#d97706"},
            "red": {"bg": "#fef2f2", "color": "#dc2626"},
            "gray": {"bg": "#f3f4f6", "color": "#6b7280"},
        }
        return styles.get(tone, styles["blue"])

    kpis = [
        {"label": "Total Products", "value": total_products, "detail": "Total catalog products", "icon": "inventory_2", **_icon_style("blue"), "url": _admin_url("admin:products_product_changelist")},
        {"label": "Amazon Products", "value": amazon_products, "detail": "Amazon staging catalog", "icon": "shopping_cart", **_icon_style("orange"), "url": _admin_url("admin:importer_amazonproduct_changelist")},
        {"label": "Flipkart Products", "value": flipkart_products, "detail": "Flipkart staging catalog", "icon": "storefront", **_icon_style("indigo"), "url": _admin_url("admin:importer_flipkartproduct_changelist")},
        {"label": "Matched Products", "value": matched_products, "detail": "Approved or matched", "icon": "link", **_icon_style("green"), "url": _admin_url("admin:importer_productmatch_changelist", "?match_status=matched")},
        {"label": "Pending Matches", "value": pending_matches, "detail": "Require a decision", "icon": "fact_check", **_icon_style("amber"), "url": _admin_url("admin:importer_productmatch_changelist", "?match_status=review")},
        {"label": "Running Jobs", "value": running_jobs, "detail": "Active operations", "icon": "autorenew", **_icon_style("blue"), "url": _admin_url("admin:importer_importerjob_changelist", "?status=running")},
        {"label": "Failed Jobs", "value": failed_jobs_count, "detail": "Require attention", "icon": "error", **_icon_style("red"), "url": _admin_url("admin:importer_importerjob_changelist", "?status=failed")},
        {"label": "Completed Jobs", "value": completed_jobs, "detail": "Successfully finished", "icon": "task_alt", **_icon_style("green"), "url": _admin_url("admin:importer_importerjob_changelist", "?status=completed")},
    ]

    matched_amazon_ids = ProductMatch.objects.filter(
        flipkart_product__isnull=False,
    ).values("amazon_product_id").distinct()
    unmatched_products = AmazonProduct.objects.exclude(pk__in=matched_amazon_ids).count()
    # JSONField defaults to an empty list/dict; these exclusions remain
    # database-side and avoid loading the product catalog into Python.
    products_missing_images = AmazonProduct.objects.filter(Q(images=[]) | Q(images__isnull=True)).count()
    products_missing_prices = AmazonProduct.objects.filter(current_selling_price_inr__isnull=True).count()
    extraction_failures = ImporterJob.objects.filter(
        job_type__in=(ImporterJobType.AMAZON_PRODUCT_EXTRACTION, ImporterJobType.FLIPKART_PRODUCT_EXTRACTION),
        status=ImporterJobStatus.FAILED,
    ).count()
    action_items = [
        {"icon": "error", "title": "Failed jobs", "description": f"{failed_jobs_count} job(s) require attention", "count": failed_jobs_count, **_icon_style("red"), "url": _admin_url("admin:importer_importerjob_changelist", "?status=failed"), "action": "Review failed jobs"},
        {"icon": "fact_check", "title": "Review required", "description": f"{pending_matches} product match(es) need a decision", "count": pending_matches, **_icon_style("amber"), "url": _admin_url("admin:importer_productmatch_changelist", "?match_status=review"), "action": "Review matches"},
        {"icon": "link_off", "title": "Unmatched products", "description": f"{unmatched_products} Amazon product(s) have no Flipkart match", "count": unmatched_products, **_icon_style("blue"), "url": _admin_url("admin:importer_amazonproduct_changelist"), "action": "Find matches"},
        {"icon": "image_not_supported", "title": "Missing images", "description": f"{products_missing_images} product(s) have no image", "count": products_missing_images, **_icon_style("gray"), "url": _admin_url("admin:importer_amazonproduct_changelist"), "action": "View products"},
        {"icon": "sell", "title": "Missing prices", "description": f"{products_missing_prices} product(s) have no current price", "count": products_missing_prices, **_icon_style("gray"), "url": _admin_url("admin:importer_amazonproduct_changelist"), "action": "View products"},
        {"icon": "sync_problem", "title": "Extraction failures", "description": f"{extraction_failures} product extraction job(s) failed", "count": extraction_failures, **_icon_style("red"), "url": _admin_url("admin:importer_importerjob_changelist", "?status=failed"), "action": "View failures"},
    ]

    recent_jobs = []
    for job in ImporterJob.objects.select_related("amazon_product", "flipkart_product").order_by("-created_at")[:8]:
        recent_jobs.append({
            "job": job,
            "title": job.title,
            "marketplace": job.get_marketplace_display() or "Internal",
            "status": job.get_status_display(),
            "status_key": job.status,
            "progress": job.progress_percent,
            "products": job.success_count or job.total_items or 0,
            "started": job.started_at,
            "duration": _duration_label(job),
            "url": _admin_url(
                "admin:importer_importerjob_change",
                kwargs={"object_id": str(job.pk)},
            ),
        })

    active_jobs = []
    for job in ImporterJob.objects.filter(
        status__in=(ImporterJobStatus.QUEUED, ImporterJobStatus.RUNNING),
    ).order_by("-created_at")[:5]:
        active_jobs.append({
            "title": job.title,
            "progress": job.progress_percent,
            "progress_text": job.progress_display,
            "status": job.get_status_display(),
            "url": _admin_url(
                "admin:importer_importerjob_change",
                kwargs={"object_id": str(job.pk)},
            ),
        })

    total_staged = AmazonProduct.objects.count()
    catalog_health = [
        {"label": "Images", "value": _percentage(total_staged - products_missing_images, total_staged), "icon": "image"},
        {"label": "Prices", "value": _percentage(total_staged - products_missing_prices, total_staged), "icon": "sell"},
        {"label": "Flipkart Match", "value": _percentage(total_staged - unmatched_products, total_staged), "icon": "compare_arrows"},
        {"label": "Specifications", "value": _percentage(AmazonProduct.objects.exclude(specifications={}).count(), total_staged), "icon": "list_alt"},
    ]

    def marketplace_card(label, product_count, marketplace, search_type, extraction_type):
        icon = "shopping_cart" if marketplace == ImporterJobMarketplace.AMAZON else "storefront"
        tone = "orange" if marketplace == ImporterJobMarketplace.AMAZON else "indigo"
        style = _icon_style(tone)
        return {
            "label": label,
            "products": product_count,
            "search_jobs": ImporterJob.objects.filter(job_type=search_type).count(),
            "extraction_jobs": ImporterJob.objects.filter(job_type=extraction_type).count(),
            "failed_jobs": ImporterJob.objects.filter(marketplace=marketplace, status=ImporterJobStatus.FAILED).count(),
            "icon": icon,
            "icon_bg": style["bg"],
            "icon_color": style["color"],
            "url": _admin_url("admin:importer_amazonproduct_changelist" if marketplace == ImporterJobMarketplace.AMAZON else "admin:importer_flipkartproduct_changelist"),
        }

    marketplace_overview = [
        marketplace_card("Amazon", amazon_products, ImporterJobMarketplace.AMAZON, ImporterJobType.AMAZON_SEARCH, ImporterJobType.AMAZON_PRODUCT_EXTRACTION),
        marketplace_card("Flipkart", flipkart_products, ImporterJobMarketplace.FLIPKART, ImporterJobType.FLIPKART_SEARCH, ImporterJobType.FLIPKART_PRODUCT_EXTRACTION),
    ]

    context.update({
        "title": "Operations Dashboard",
        "dashboard_kpis": kpis,
        "action_items": action_items,
        "recent_jobs": recent_jobs,
        "active_jobs": active_jobs,
        "catalog_health": catalog_health,
        "marketplace_overview": marketplace_overview,
        "failed_jobs_count": failed_jobs_count,
        "review_matches": pending_matches,
        "unmatched_products": unmatched_products,
        "quick_actions": [
            {"label": "Search Amazon", "icon": "search", "url": _admin_url("admin:importer_searchkeyword_add")},
            {"label": "Search Flipkart", "icon": "storefront", "url": _admin_url("admin:importer_searchkeyword_changelist")},
            {"label": "Find product match", "icon": "compare_arrows", "url": _admin_url("admin:importer_productmatch_changelist")},
            {"label": "View failed jobs", "icon": "error", "url": _admin_url("admin:importer_importerjob_changelist", "?status=failed")},
            {"label": "View products", "icon": "inventory_2", "url": _admin_url("admin:products_product_changelist")},
            {"label": "View running jobs", "icon": "autorenew", "url": _admin_url("admin:importer_importerjob_changelist", "?status=running")},
        ],
        "dashboard_updated_at": timezone.localtime(),
    })
    return context


# Unfold's AdminSite invokes this callback for the existing admin index. The
# callback keeps all dashboard queries in Python while the template remains a
# presentation layer.
