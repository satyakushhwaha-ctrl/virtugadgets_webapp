# Database

## Category

Purpose

Stores product categories.

Fields

- id (UUID)
- name
- slug
- icon
- description
- display_order
- is_active
- created_at
- updated_at

---

## Product

Purpose

Stores product information.

Fields

- id (UUID)
- category
- title
- slug
- brand
- featured_image
- short_description
- description
- rating
- review_count
- is_active
- created_at
- updated_at

---

## ProductPrice

Purpose

Stores marketplace prices.

Fields

- id (UUID)
- product
- platform
- price
- mrp
- discount_percent
- affiliate_url
- last_updated

Platforms

- AMAZON
- FLIPKART

Unique

product + platform

---

## Subscriber

Purpose

Stores subscribers.

Fields

- id (UUID)
- name
- email
- phone
- is_active
- created_at
