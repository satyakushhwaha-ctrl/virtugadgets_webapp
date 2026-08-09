# VirtuGadgets

## Project

VirtuGadgets is a modern price comparison platform for Indian eCommerce websites.

Users can compare prices from Amazon and Flipkart before making a purchase.

The project is SEO-first and mobile-first.

---

## Tech Stack

Backend

- Python 3.12+
- Django 5.x

Database

- PostgreSQL

Frontend

- Django Templates
- Tailwind CSS v4
- Alpine.js (only when required)

Deployment

- Docker
- Gunicorn
- Nginx

Storage

- Local during development
- Cloudflare R2 in production

---

## MVP

- Categories
- Products
- Product Detail
- Product Prices
- Subscriber Form

---

## Future Features

- Search
- Filters
- Wishlist
- Price Alerts
- Price History
- Coupons
- AI Recommendations

---

## Coding Rules

Always

- Follow Django Best Practices.
- Use Class Based Views.
- Use UUID primary keys.
- Use Slugs for URLs.
- Keep views thin.
- Move business logic into services.py.
- Use select_related and prefetch_related.
- Avoid duplicated HTML.
- Build reusable template components.
- Use Tailwind CSS only.
- Mobile First.
- Accessible HTML.
- Semantic markup.

Never

- Inline CSS
- Inline JavaScript
- Business logic inside templates
- Raw SQL unless necessary

Before changing architecture, review all documentation.
