from types import MethodType

from django.contrib import admin
from django.contrib.admin.sites import AdminSite
from django.template.response import TemplateResponse
from django.urls import reverse


def _dashboard_index(self: AdminSite, request, extra_context=None):
    """Render the operator dashboard using existing staging data."""
    if not self.has_permission(request):
        return self.login(request)

    from apps.importer.models import (
        AmazonProduct,
        FlipkartProduct,
        ImporterJob,
        ImporterJobStatus,
        MatchStatus,
        ProductMatch,
    )
    from apps.products.models import Product

    cards = (
        ("Products", "admin:products_product_changelist", Product.objects.count()),
        ("Amazon Products", "admin:importer_amazonproduct_changelist", AmazonProduct.objects.count()),
        ("Flipkart Products", "admin:importer_flipkartproduct_changelist", FlipkartProduct.objects.count()),
        ("Matched Products", "admin:importer_productmatch_changelist", ProductMatch.objects.filter(match_status__in=(MatchStatus.MATCHED, MatchStatus.APPROVED, MatchStatus.PUBLISHED)).count()),
        ("Pending Matches", "admin:importer_productmatch_changelist", ProductMatch.objects.filter(match_status__in=(MatchStatus.PENDING, MatchStatus.REVIEW)).count()),
        ("Running Jobs", "admin:importer_importerjob_changelist", ImporterJob.objects.filter(status=ImporterJobStatus.RUNNING).count()),
        ("Failed Jobs", "admin:importer_importerjob_changelist", ImporterJob.objects.filter(status=ImporterJobStatus.FAILED).count()),
        ("Completed Jobs", "admin:importer_importerjob_changelist", ImporterJob.objects.filter(status=ImporterJobStatus.COMPLETED).count()),
    )
    card_data = []
    for label, url_name, count in cards:
        url = reverse(url_name)
        if label == "Pending Matches":
            url += "?match_status=review"
        elif label in {"Running Jobs", "Failed Jobs", "Completed Jobs"}:
            url += f"?status={label.split()[0].lower()}"
        card_data.append({"label": label, "count": count, "url": url})

    failed_jobs = ImporterJob.objects.filter(status=ImporterJobStatus.FAILED).select_related(
        "amazon_product", "flipkart_product"
    )[:8]
    review_matches = ProductMatch.objects.filter(
        match_status=MatchStatus.REVIEW,
    ).select_related("amazon_product", "flipkart_product")[:8]

    context = {
        **self.each_context(request),
        "title": "Operations dashboard",
        "app_list": self.get_app_list(request),
        "dashboard_cards": card_data,
        "failed_jobs": failed_jobs,
        "review_matches": review_matches,
    }
    if extra_context:
        context.update(extra_context)
    return TemplateResponse(request, "admin/index.html", context)


# Keep Django's standard AdminSite and registrations; only replace its home
# view with the operator dashboard.
admin.site.index = MethodType(_dashboard_index, admin.site)


# A few existing admin-only tests use lightweight request-user doubles rather
# than Django User instances. Jazzmin's menu helper assumes the full User API;
# keep those compatibility requests renderable without affecting real users.
try:
    from jazzmin.templatetags import jazzmin as _jazzmin_tags

    _jazzmin_make_menu = _jazzmin_tags.make_menu

    def _safe_jazzmin_make_menu(user, *args, **kwargs):
        if user is not None and not hasattr(user, "get_all_permissions"):
            return []
        return _jazzmin_make_menu(user, *args, **kwargs)

    _jazzmin_tags.make_menu = _safe_jazzmin_make_menu
    _jazzmin_can_view_self = _jazzmin_tags.can_view_self

    def _safe_jazzmin_can_view_self(perms):
        try:
            return _jazzmin_can_view_self(perms)
        except AttributeError:
            return False

    _jazzmin_tags.can_view_self = _safe_jazzmin_can_view_self
    _jazzmin_tags.register.filters["can_view_self"] = _safe_jazzmin_can_view_self
except ImportError:
    pass

# Register your models here.
