from django.core.management.base import BaseCommand
from django.utils.text import slugify

from apps.categories.models import Category


CATEGORIES = [
    {
        "name": "Mobiles",
        "icon": "smartphone",
        "description": "Smartphones and mobile accessories.",
        "display_order": 10,
    },
    {
        "name": "Laptops",
        "icon": "laptop",
        "description": "Work, gaming, and everyday laptops.",
        "display_order": 20,
    },
    {
        "name": "Accessories",
        "icon": "headphones",
        "description": "Useful tech accessories and wearables.",
        "display_order": 30,
    },
    {
        "name": "Fashion",
        "icon": "shirt",
        "description": "Clothing, footwear, and style essentials.",
        "display_order": 40,
    },
    {
        "name": "Gaming",
        "icon": "gamepad",
        "description": "Gaming devices and performance gear.",
        "display_order": 50,
    },
    {
        "name": "Beauty",
        "icon": "sparkles",
        "description": "Beauty, grooming, and personal care products.",
        "display_order": 60,
    },
    {
        "name": "Home Appliances",
        "icon": "home",
        "description": "Appliances for modern Indian homes.",
        "display_order": 70,
    },
]


class Command(BaseCommand):
    help = "Seed default VirtuGadgets product categories."

    def handle(self, *args: object, **options: object) -> None:
        created_count = 0

        for category_data in CATEGORIES:
            _, created = Category.objects.get_or_create(
                slug=slugify(category_data["name"]),
                defaults={
                    "name": category_data["name"],
                    "icon": category_data["icon"],
                    "description": category_data["description"],
                    "display_order": category_data["display_order"],
                    "is_active": True,
                },
            )
            created_count += int(created)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded categories. Created {created_count}, "
                f"existing {len(CATEGORIES) - created_count}."
            )
        )
