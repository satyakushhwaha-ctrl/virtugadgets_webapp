from typing import Any

from django.contrib import messages
from django.http import HttpRequest, JsonResponse
from django.views import View

from apps.subscribers.forms import SubscriberForm
from apps.subscribers.services import subscribe_user


class SubscribeView(View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        form = SubscriberForm(request.POST)

        if not form.is_valid():
            message = _get_first_error(form)
            messages.error(request, message)
            return JsonResponse(
                {
                    "ok": False,
                    "message": message,
                    "errors": form.errors.get_json_data(),
                },
                status=400,
            )

        try:
            subscribe_user(form)
        except ValueError as error:
            message = str(error)
            messages.error(request, message)
            return JsonResponse({"ok": False, "message": message}, status=400)

        message = "Thank you for subscribing!\n\nWe'll notify you when prices drop."
        messages.success(request, message)
        return JsonResponse({"ok": True, "message": message})


def _get_first_error(form: SubscriberForm) -> str:
    for errors in form.errors.values():
        if errors:
            return str(errors[0])
    return "Please check your details and try again."
