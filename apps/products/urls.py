from django.urls import path

from .views import ProductDetailView, ProductListView, SearchView

urlpatterns = [
    path("products/", ProductListView.as_view(), name="product-list"),
    path("product/<slug:slug>/", ProductDetailView.as_view(), name="product-detail"),
    path("search/", SearchView.as_view(), name="search"),
]
