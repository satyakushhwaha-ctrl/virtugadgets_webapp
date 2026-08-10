import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create the configured Django admin user without changing an existing user."

    environment_names = ("ADMIN_USERNAME", "ADMIN_EMAIL", "ADMIN_PASSWORD")

    def handle(self, *args, **options):
        values = {
            "ADMIN_USERNAME": os.environ.get("ADMIN_USERNAME", "").strip(),
            "ADMIN_EMAIL": os.environ.get("ADMIN_EMAIL", "").strip(),
            "ADMIN_PASSWORD": os.environ.get("ADMIN_PASSWORD", ""),
        }
        missing = [
            name for name, value in values.items()
            if not value or (name != "ADMIN_PASSWORD" and not value.strip())
        ]

        if missing:
            self.stdout.write(
                self.style.WARNING(
                    "Admin provisioning skipped; missing environment variable(s): "
                    + ", ".join(missing)
                )
            )
            return

        user_model = get_user_model()
        username_field = user_model.USERNAME_FIELD
        username = values["ADMIN_USERNAME"]

        if user_model._default_manager.filter(**{username_field: username}).exists():
            self.stdout.write("Admin user already exists; no changes made.")
            return

        user_model._default_manager.create_superuser(
            **{
                username_field: username,
                "email": values["ADMIN_EMAIL"],
                "password": values["ADMIN_PASSWORD"],
            }
        )
        self.stdout.write(self.style.SUCCESS("Admin user created successfully."))
