from django.db import IntegrityError, transaction

from apps.subscribers.forms import SubscriberForm
from apps.subscribers.models import Subscriber


def subscribe_user(form: SubscriberForm) -> Subscriber:
    try:
        with transaction.atomic():
            return form.save()
    except IntegrityError as error:
        raise ValueError("You're already subscribed.") from error
