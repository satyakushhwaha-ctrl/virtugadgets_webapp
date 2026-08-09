from apps.categories.models import Category


def navigation(request) -> dict[str, object]:
    nav_categories = getattr(request, "nav_categories", None)
    if nav_categories is not None:
        categories = nav_categories
    else:
        categories = Category.objects.filter(is_active=True).only(
            "name", "slug", "description", "display_order"
        ).order_by("display_order", "name")

    current_category = None
    category_slug = request.GET.get("category")
    if category_slug:
        current_category = Category.objects.filter(slug=category_slug, is_active=True).first()

    return {
        "nav_categories": categories,
        "current_category": current_category,
    }
