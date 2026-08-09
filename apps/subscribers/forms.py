import re

from django import forms

from apps.subscribers.models import Subscriber


class SubscriberForm(forms.ModelForm):
    class Meta:
        model = Subscriber
        fields = ("name", "email", "phone")
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "autocomplete": "name",
                    "class": "mt-2 min-h-12 w-full rounded-xl border border-blue-100 bg-white px-4 text-base text-slate-950 shadow-sm outline-none transition placeholder:text-slate-400 focus:border-blue-600 focus:ring-2 focus:ring-blue-100",
                    "placeholder": "Your name",
                }
            ),
            "email": forms.EmailInput(
                attrs={
                    "autocomplete": "email",
                    "class": "mt-2 min-h-12 w-full rounded-xl border border-blue-100 bg-white px-4 text-base text-slate-950 shadow-sm outline-none transition placeholder:text-slate-400 focus:border-blue-600 focus:ring-2 focus:ring-blue-100",
                    "placeholder": "you@example.com",
                }
            ),
            "phone": forms.TextInput(
                attrs={
                    "autocomplete": "tel",
                    "class": "mt-2 min-h-12 w-full rounded-xl border border-blue-100 bg-white px-4 text-base text-slate-950 shadow-sm outline-none transition placeholder:text-slate-400 focus:border-blue-600 focus:ring-2 focus:ring-blue-100",
                    "inputmode": "tel",
                    "pattern": r"^\+?[0-9\s-]{10,18}$",
                    "placeholder": "9876543210",
                }
            ),
        }

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.required = True
            field.widget.attrs["required"] = "required"

    def clean_name(self) -> str:
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("Enter your name.")
        return name

    def clean_email(self) -> str:
        email = self.cleaned_data["email"].strip().lower()
        if Subscriber.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("You're already subscribed.")
        return email

    def clean_phone(self) -> str:
        raw_phone = self.cleaned_data["phone"].strip()
        phone = re.sub(r"[\s-]+", "", raw_phone)
        if not phone:
            raise forms.ValidationError("Enter your phone number.")
        if Subscriber.objects.filter(phone=phone).exists():
            raise forms.ValidationError("You're already subscribed.")
        return phone
