import os
from pathlib import Path

import environ


# Paths
BASE_DIR = Path(__file__).resolve().parent.parent


# Environment
env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
    CSRF_TRUSTED_ORIGINS=(list, []),
    PUBLIC_DOMAINS=(list, ["virtugadgets.in", "www.virtugadgets.in"]),
    DB_PORT=(int, 5432),
)
env.read_env(BASE_DIR / ".env")


# Background task broker/backend. Railway should provide REDIS_URL; the
# fallback keeps local development convenient without embedding credentials.
REDIS_URL = env("REDIS_URL", default="redis://127.0.0.1:6379/0")

# Existing marketplace provider settings remain unchanged in Phase 1.
AMAZON_SEARCH_PROVIDER = env("AMAZON_SEARCH_PROVIDER", default="auto").lower()
SCRAPINGBEE_API_KEY = env("SCRAPINGBEE_API_KEY", default="")
SCRAPEDO_API_TOKEN = env("SCRAPEDO_API_TOKEN", default="")
SCRAPEDO_TIMEOUT = env.int("SCRAPEDO_TIMEOUT", default=45)
SCRAPEDO_TASK_SOFT_TIME_LIMIT = env.int("SCRAPEDO_TASK_SOFT_TIME_LIMIT", default=300)
SCRAPEDO_TASK_TIME_LIMIT = env.int("SCRAPEDO_TASK_TIME_LIMIT", default=360)


# Security
SECRET_KEY = env("SECRET_KEY")
DEBUG = os.getenv("DEBUG", "False").lower() in ("true", "1", "yes")

if not DEBUG and not SCRAPEDO_API_TOKEN.strip():
    raise RuntimeError("SCRAPEDO_API_TOKEN must be configured when DEBUG=False.")


def _unique(values):
    return list(dict.fromkeys(value for value in values if value))


def _hostname(value):
    value = value.strip().rstrip("/")
    for scheme in ("https://", "http://"):
        if value.startswith(scheme):
            value = value[len(scheme):]
            break
    return value.split("/", 1)[0]


def _https_origin(value):
    hostname = _hostname(value)
    return f"https://{hostname}" if hostname else ""


configured_allowed_hosts = [_hostname(value) for value in env.list("ALLOWED_HOSTS")]
configured_csrf_origins = env.list("CSRF_TRUSTED_ORIGINS", default=[])
public_domains = [_hostname(domain) for domain in env.list("PUBLIC_DOMAINS")]

if DEBUG:
    ALLOWED_HOSTS = _unique(configured_allowed_hosts)
    CSRF_TRUSTED_ORIGINS = _unique(configured_csrf_origins)
else:
    ALLOWED_HOSTS = _unique(configured_allowed_hosts + public_domains)
    CSRF_TRUSTED_ORIGINS = _unique(
        [_https_origin(origin) for origin in configured_csrf_origins]
        + [_https_origin(domain) for domain in public_domains]
    )

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool(
        "SECURE_HSTS_INCLUDE_SUBDOMAINS",
        default=True,
    )
    SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=True)


# Applications
INSTALLED_APPS = [
    'unfold',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    'apps.core',
    'apps.categories',
    'apps.products',
    'apps.subscribers',
    'apps.importer',
]


# Unfold themes the existing Django admin; all model registrations and
# workflows continue to come from the project's current ModelAdmin classes.
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

UNFOLD = {
    "SITE_TITLE": "VirtuGadgets Admin",
    "SITE_HEADER": "VirtuGadgets",
    "SITE_SUBHEADER": "Marketplace Operations",
    "SITE_URL": "/",
    "DASHBOARD_CALLBACK": "apps.core.admin.dashboard_callback",
    "SHOW_LANGUAGES": False,
    "COMMAND": {
        "search_models": True,
        "show_history": True,
    },
    "COLORS": {
        "primary": {
            "50": "#eff6ff",
            "100": "#dbeafe",
            "200": "#bfdbfe",
            "300": "#93c5fd",
            "400": "#60a5fa",
            "500": "#2563eb",
            "600": "#1d4ed8",
            "700": "#1e40af",
            "800": "#1e3a8a",
            "900": "#172554",
            "950": "#0f172a",
        },
    },
    "SIDEBAR": {
        "show_search": True,
        # Keep the curated operational navigation above, while also exposing
        # every permission-checked registered app/model through Unfold's
        # built-in All applications drawer.
        "show_all_applications": True,
        "navigation": [
            {
                "title": _("Dashboard"),
                "separator": True,
                "items": [
                    {"title": _("Operations dashboard"), "icon": "dashboard", "link": reverse_lazy("admin:index")},
                ],
            },
            {
                "title": _("Catalog"),
                "separator": True,
                "items": [
                    {"title": _("Products"), "icon": "inventory_2", "link": reverse_lazy("admin:products_product_changelist")},
                    {"title": _("Amazon Products"), "icon": "shopping_cart", "link": reverse_lazy("admin:importer_amazonproduct_changelist")},
                    {"title": _("Flipkart Products"), "icon": "storefront", "link": reverse_lazy("admin:importer_flipkartproduct_changelist")},
                    {"title": _("Product Matches"), "icon": "compare_arrows", "link": reverse_lazy("admin:importer_productmatch_changelist")},
                    {"title": _("Categories"), "icon": "category", "link": reverse_lazy("admin:categories_category_changelist")},
                ],
            },
            {
                "title": _("Discovery"),
                "separator": True,
                "items": [
                    {"title": _("Search Keywords"), "icon": "search", "link": reverse_lazy("admin:importer_searchkeyword_changelist")},
                    {"title": _("Amazon Search Results"), "icon": "travel_explore", "link": reverse_lazy("admin:importer_amazonsearchresult_changelist")},
                    {"title": _("Flipkart Search Results"), "icon": "manage_search", "link": reverse_lazy("admin:importer_flipkartsearchresult_changelist")},
                ],
            },
            {
                "title": _("Operations"),
                "separator": True,
                "items": [
                    {"title": _("Importer Jobs"), "icon": "pending_actions", "link": reverse_lazy("admin:importer_importerjob_changelist")},
                    {"title": _("Import Batches"), "icon": "layers", "link": reverse_lazy("admin:importer_importbatch_changelist")},
                ],
            },
            {
                "title": _("Pricing"),
                "separator": True,
                "items": [
                    {"title": _("Product Prices"), "icon": "sell", "link": reverse_lazy("admin:products_productprice_changelist")},
                ],
            },
            {
                "title": _("System"),
                "separator": True,
                "items": [
                    {"title": _("Users"), "icon": "group", "link": reverse_lazy("admin:auth_user_changelist")},
                    {"title": _("Groups"), "icon": "groups", "link": reverse_lazy("admin:auth_group_changelist")},
                ],
            },
        ],
    },
}


# Middleware
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'


# Templates
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'apps.core.context_processors.navigation',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
database_url = env("DATABASE_URL", default="").strip()
if database_url:
    database = env.db_url("DATABASE_URL")
    database["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
else:
    database = {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': env('DB_NAME'),
        'USER': env('DB_USER'),
        'PASSWORD': env('DB_PASSWORD'),
        'HOST': env('DB_HOST'),
        'PORT': env.int('DB_PORT'),
        'CONN_MAX_AGE': env.int('DB_CONN_MAX_AGE', default=60),
    }
DATABASES = {'default': database}


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
LANGUAGE_CODE = 'en-us'

TIME_ZONE = env('TIME_ZONE', default='UTC')

USE_I18N = True

USE_TZ = True


# Static and media files
STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': (
            'django.contrib.staticfiles.storage.StaticFilesStorage'
            if DEBUG
            else 'whitenoise.storage.CompressedManifestStaticFilesStorage'
        ),
    },
}

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'


# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Celery
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
