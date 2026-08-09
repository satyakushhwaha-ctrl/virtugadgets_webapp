from django.views.generic import TemplateView

from apps.core.services import get_active_categories, get_latest_product_cards


class HomeView(TemplateView):
    template_name = "home/index.html"

    def get_context_data(self, **kwargs: object) -> dict[str, object]:
        context = super().get_context_data(**kwargs)
        categories = list(get_active_categories())
        setattr(self.request, "nav_categories", categories)
        context["categories"] = categories
        context["products"] = get_latest_product_cards()
        return context
