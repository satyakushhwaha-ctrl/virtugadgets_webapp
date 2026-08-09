from django.urls import path

from apps.subscribers.views import SubscribeView

app_name = "subscribers"

urlpatterns = [
    path("subscribe/", SubscribeView.as_view(), name="subscribe"),
]
