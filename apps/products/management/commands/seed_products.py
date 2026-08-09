from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils.text import slugify

from apps.categories.models import Category
from apps.products.models import Product, ProductPrice


PRODUCTS = [
    {
        "category": "Mobiles",
        "title": "Apple iPhone 15 128GB",
        "brand": "Apple",
        "rating": Decimal("4.60"),
        "review_count": 18421,
        "amazon_price": Decimal("69900.00"),
        "flipkart_price": Decimal("68999.00"),
        "mrp": Decimal("79900.00"),
    },
    {
        "category": "Mobiles",
        "title": "Apple iPhone 14 128GB",
        "brand": "Apple",
        "rating": Decimal("4.50"),
        "review_count": 28433,
        "amazon_price": Decimal("57999.00"),
        "flipkart_price": Decimal("56999.00"),
        "mrp": Decimal("69900.00"),
    },
    {
        "category": "Mobiles",
        "title": "Samsung Galaxy S24 5G",
        "brand": "Samsung",
        "rating": Decimal("4.40"),
        "review_count": 9820,
        "amazon_price": Decimal("64999.00"),
        "flipkart_price": Decimal("63999.00"),
        "mrp": Decimal("79999.00"),
    },
    {
        "category": "Mobiles",
        "title": "Samsung Galaxy M55 5G",
        "brand": "Samsung",
        "rating": Decimal("4.20"),
        "review_count": 6431,
        "amazon_price": Decimal("26999.00"),
        "flipkart_price": Decimal("27499.00"),
        "mrp": Decimal("32999.00"),
    },
    {
        "category": "Mobiles",
        "title": "OnePlus Nord CE4 5G",
        "brand": "OnePlus",
        "rating": Decimal("4.30"),
        "review_count": 11782,
        "amazon_price": Decimal("24999.00"),
        "flipkart_price": Decimal("25999.00"),
        "mrp": Decimal("29999.00"),
    },
    {
        "category": "Mobiles",
        "title": "OnePlus 12R 5G",
        "brand": "OnePlus",
        "rating": Decimal("4.50"),
        "review_count": 8624,
        "amazon_price": Decimal("39999.00"),
        "flipkart_price": Decimal("40999.00"),
        "mrp": Decimal("45999.00"),
    },
    {
        "category": "Laptops",
        "title": "HP Pavilion 15 Ryzen 5 Laptop",
        "brand": "HP",
        "rating": Decimal("4.20"),
        "review_count": 3188,
        "amazon_price": Decimal("56990.00"),
        "flipkart_price": Decimal("55990.00"),
        "mrp": Decimal("68999.00"),
    },
    {
        "category": "Laptops",
        "title": "HP Victus Gaming Ryzen 5 Laptop",
        "brand": "HP",
        "rating": Decimal("4.30"),
        "review_count": 4210,
        "amazon_price": Decimal("62990.00"),
        "flipkart_price": Decimal("61990.00"),
        "mrp": Decimal("78999.00"),
    },
    {
        "category": "Laptops",
        "title": "Dell Inspiron 14 Intel Core i5 Laptop",
        "brand": "Dell",
        "rating": Decimal("4.10"),
        "review_count": 2765,
        "amazon_price": Decimal("52990.00"),
        "flipkart_price": Decimal("53990.00"),
        "mrp": Decimal("65999.00"),
    },
    {
        "category": "Laptops",
        "title": "Dell G15 Gaming Laptop",
        "brand": "Dell",
        "rating": Decimal("4.40"),
        "review_count": 1942,
        "amazon_price": Decimal("74990.00"),
        "flipkart_price": Decimal("73990.00"),
        "mrp": Decimal("92999.00"),
    },
    {
        "category": "Accessories",
        "title": "boAt Airdopes 141 Bluetooth Earbuds",
        "brand": "boAt",
        "rating": Decimal("4.00"),
        "review_count": 153230,
        "amazon_price": Decimal("1299.00"),
        "flipkart_price": Decimal("1399.00"),
        "mrp": Decimal("4490.00"),
    },
    {
        "category": "Accessories",
        "title": "boAt Rockerz 255 Pro Plus Neckband",
        "brand": "boAt",
        "rating": Decimal("4.10"),
        "review_count": 88420,
        "amazon_price": Decimal("1199.00"),
        "flipkart_price": Decimal("1099.00"),
        "mrp": Decimal("3990.00"),
    },
    {
        "category": "Accessories",
        "title": "Noise ColorFit Pro 5 Smart Watch",
        "brand": "Noise",
        "rating": Decimal("4.10"),
        "review_count": 22118,
        "amazon_price": Decimal("3999.00"),
        "flipkart_price": Decimal("3799.00"),
        "mrp": Decimal("7999.00"),
    },
    {
        "category": "Accessories",
        "title": "Noise Buds VS104 Truly Wireless Earbuds",
        "brand": "Noise",
        "rating": Decimal("4.00"),
        "review_count": 35671,
        "amazon_price": Decimal("999.00"),
        "flipkart_price": Decimal("1099.00"),
        "mrp": Decimal("3499.00"),
    },
    {
        "category": "Fashion",
        "title": "Puma Flyer Runner Men's Shoes",
        "brand": "Puma",
        "rating": Decimal("4.20"),
        "review_count": 7450,
        "amazon_price": Decimal("2299.00"),
        "flipkart_price": Decimal("2199.00"),
        "mrp": Decimal("4999.00"),
    },
    {
        "category": "Fashion",
        "title": "Puma Essentials Logo Hoodie",
        "brand": "Puma",
        "rating": Decimal("4.10"),
        "review_count": 1840,
        "amazon_price": Decimal("1899.00"),
        "flipkart_price": Decimal("1999.00"),
        "mrp": Decimal("3999.00"),
    },
    {
        "category": "Gaming",
        "title": "Samsung Odyssey G3 24 Inch Gaming Monitor",
        "brand": "Samsung",
        "rating": Decimal("4.30"),
        "review_count": 3841,
        "amazon_price": Decimal("12999.00"),
        "flipkart_price": Decimal("12499.00"),
        "mrp": Decimal("19999.00"),
    },
    {
        "category": "Gaming",
        "title": "HP HyperX Cloud Stinger 2 Headset",
        "brand": "HP",
        "rating": Decimal("4.20"),
        "review_count": 5292,
        "amazon_price": Decimal("3490.00"),
        "flipkart_price": Decimal("3299.00"),
        "mrp": Decimal("5990.00"),
    },
    {
        "category": "Beauty",
        "title": "OnePlus Electric Shaver Kit",
        "brand": "OnePlus",
        "rating": Decimal("4.00"),
        "review_count": 925,
        "amazon_price": Decimal("2499.00"),
        "flipkart_price": Decimal("2399.00"),
        "mrp": Decimal("3999.00"),
    },
    {
        "category": "Home Appliances",
        "title": "Samsung 253L Frost Free Refrigerator",
        "brand": "Samsung",
        "rating": Decimal("4.30"),
        "review_count": 6720,
        "amazon_price": Decimal("25990.00"),
        "flipkart_price": Decimal("25490.00"),
        "mrp": Decimal("34990.00"),
    },
]


class Command(BaseCommand):
    help = "Seed realistic VirtuGadgets products with marketplace prices."

    def handle(self, *args: object, **options: object) -> None:
        created_products = 0
        created_prices = 0

        for product_data in PRODUCTS:
            category, _ = Category.objects.get_or_create(
                slug=slugify(product_data["category"]),
                defaults={
                    "name": product_data["category"],
                    "description": f"{product_data['category']} products.",
                    "is_active": True,
                },
            )
            product, created = Product.objects.get_or_create(
                slug=slugify(product_data["title"]),
                defaults={
                    "category": category,
                    "title": product_data["title"],
                    "brand": product_data["brand"],
                    "short_description": (
                        f"Compare {product_data['title']} prices on "
                        "Amazon and Flipkart."
                    ),
                    "description": (
                        f"{product_data['title']} from "
                        f"{product_data['brand']} with live marketplace "
                        "comparison support."
                    ),
                    "rating": product_data["rating"],
                    "review_count": product_data["review_count"],
                    "is_active": True,
                },
            )
            created_products += int(created)
            created_prices += self.create_prices(product, product_data)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded products. Created {created_products} products and "
                f"{created_prices} prices."
            )
        )

    def create_prices(self, product: Product, product_data: dict[str, object]) -> int:
        created_count = 0
        price_rows = [
            (
                ProductPrice.Platform.AMAZON,
                product_data["amazon_price"],
                "https://www.amazon.in/",
            ),
            (
                ProductPrice.Platform.FLIPKART,
                product_data["flipkart_price"],
                "https://www.flipkart.com/",
            ),
        ]

        for platform, price, affiliate_url in price_rows:
            _, created = ProductPrice.objects.get_or_create(
                product=product,
                platform=platform,
                defaults={
                    "price": price,
                    "mrp": product_data["mrp"],
                    "discount_percent": self.calculate_discount(
                        price=price,
                        mrp=product_data["mrp"],
                    ),
                    "affiliate_url": affiliate_url,
                },
            )
            created_count += int(created)

        return created_count

    def calculate_discount(self, price: Decimal, mrp: Decimal) -> int:
        if mrp <= Decimal("0.00") or price >= mrp:
            return 0

        discount = ((mrp - price) / mrp) * Decimal("100")
        return int(discount.quantize(Decimal("1")))
