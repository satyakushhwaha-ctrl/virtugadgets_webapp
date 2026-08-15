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

# Amazon search remains Playwright-first; ScrapingBee is only used as the
# configured fallback when direct Amazon access is blocked or unavailable.
AMAZON_SEARCH_PROVIDER = env("AMAZON_SEARCH_PROVIDER", default="auto").lower()
SCRAPINGBEE_API_KEY = env("SCRAPINGBEE_API_KEY", default="")


# Security
SECRET_KEY = env("SECRET_KEY")
DEBUG = os.getenv("DEBUG", "False").lower() in ("true", "1", "yes")


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
    'jazzmin',
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


# Jazzmin themes the existing Django admin; all model registrations and
# workflows continue to come from the project's current ModelAdmin classes.
JAZZMIN_SETTINGS = {
    "site_title": "VirtuGadgets Operations",
    "site_header": "VirtuGadgets",
    "site_brand": "VirtuGadgets",
    "welcome_sign": "Marketplace operations console",
    "copyright": "VirtuGadgets",
    "show_sidebar": True,
    "navigation_expanded": False,
    "hide_models": ["auth.Group"],
    "search_model": [
        "importer.AmazonProduct",
        "importer.FlipkartProduct",
        "importer.ProductMatch",
        "importer.ImporterJob",
    ],
    "order_with_respect_to": [
        "products",
        "importer",
        "categories",
        "subscribers",
        "auth",
    ],
    "icons": {
        "products.Product": "fas fa-box-open",
        "importer.AmazonProduct": "fab fa-amazon",
        "importer.FlipkartProduct": "fas fa-store",
        "importer.ProductMatch": "fas fa-code-compare",
        "importer.ImporterJob": "fas fa-list-check",
        "importer.ImportBatch": "fas fa-layer-group",
        "importer.SearchKeyword": "fas fa-magnifying-glass",
        "importer.AmazonSearchResult": "fab fa-amazon",
        "importer.FlipkartSearchResult": "fas fa-store",
        "products.ProductPrice": "fas fa-tags",
        "categories.Category": "fas fa-folder-tree",
        "auth.User": "fas fa-user",
        "auth.Group": "fas fa-users",
    },
    "topmenu_links": [
        {"name": "Operations dashboard", "url": "admin:index", "permissions": ["auth.view_user"]},
    ],
    "related_modal_active": True,
    "changeform_format": "horizontal_tabs",
}

JAZZMIN_UI_TWEAKS = {
    # Keep Jazzmin's bundled AdminLTE styling as the baseline. This also
    # avoids requiring a collected Bootswatch manifest for admin previews.
    "theme": "default",
    "dark_mode_theme": None,
    "navbar_small_text": False,
    "body_small_text": False,
    "footer_small_text": False,
    "sidebar_nav_small_text": False,
    "sidebar_disable_expand": False,
    "sidebar_nav_child_indent": True,
    "navbar_fixed": True,
    "sidebar_fixed": True,
    "button_classes": {
        "primary": "btn-primary",
        "secondary": "btn-outline-secondary",
        "info": "btn-info",
        "warning": "btn-warning",
        "danger": "btn-danger",
        "success": "btn-success",
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
