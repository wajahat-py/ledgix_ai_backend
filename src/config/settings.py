from datetime import timedelta
from pathlib import Path

from decouple import config, Csv
from corsheaders.defaults import default_headers

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = config('DEBUG',cast=bool)

ALLOWED_HOSTS = config('ALLOWED_HOSTS', cast=Csv())

# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'corsheaders',
    'channels',
    'users',
    'invoices',
    'organizations',
    'gmail_integration',
    'billing',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': config('SQLITE_PATH', default=str(BASE_DIR / 'db.sqlite3')),
    }
}


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

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
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = config('STATIC_ROOT', default=str(BASE_DIR / 'staticfiles'))

# Media files (uploaded invoices)
MEDIA_URL = '/media/'
MEDIA_ROOT = config('MEDIA_ROOT', default=str(BASE_DIR / 'media'))

# Custom User Model
AUTH_USER_MODEL = 'users.User'

# CORS — credentials required for the HTTP-only refresh cookie to be sent cross-origin
CORS_ALLOWED_ORIGINS = config(
    "CORS_ALLOWED_ORIGINS",
    default="http://localhost:3000,http://127.0.0.1:3000",
    cast=Csv(),
)
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = list(default_headers) + [
    "x-organization-id",
]

# REST Framework settings
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    )
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=1),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=30),
}

# Channels — WebSocket support
ASGI_APPLICATION = 'config.asgi.application'

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [config('REDIS_URL', default='redis://localhost:6379')],
        },
    },
}

# Celery
REDIS_URL = config('REDIS_URL', default='redis://localhost:6379')
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TASK_TRACK_STARTED = True
# Windows: billiard's prefork pool uses Unix shared memory that Windows denies.
# The solo pool runs tasks in the worker's main thread — fine for development.
# On Linux in production, remove this line to use the default prefork pool.
CELERY_WORKER_POOL = config('CELERY_WORKER_POOL', default='solo')

# Mindee
MINDEE_API_KEY = config('MINDEE_API_KEY')
MINDEE_V2_API_KEY = config('MINDEE_V2_API_KEY', default=MINDEE_API_KEY)
MINDEE_MODEL_ID = config('MINDEE_MODEL_ID', default='fe31f6d8-7b31-43a6-9e9b-65ff6737c4e9')

# OpenAI — used for invoice embedding and duplicate detection (optional)
OPENAI_API_KEY = config('OPENAI_API_KEY', default=None)

# ── Gmail Integration ─────────────────────────────────────────────────────────
# Register http://localhost:8000/api/gmail/callback/ as an authorised redirect
# URI in your Google Cloud Console OAuth 2.0 client.
# OAuth credentials for Google Sign-In (NextAuth / id_token verification)
GOOGLE_CLIENT_ID_AUTH     = config('GOOGLE_CLIENT_ID_AUTH',     default='')
GOOGLE_CLIENT_SECRET_AUTH = config('GOOGLE_CLIENT_SECRET_AUTH', default='')

# OAuth credentials for Gmail inbox integration
GOOGLE_CLIENT_ID_EMAIL     = config('GOOGLE_CLIENT_ID_EMAIL',     default='')
GOOGLE_CLIENT_SECRET_EMAIL = config('GOOGLE_CLIENT_SECRET_EMAIL', default='')
GMAIL_OAUTH_REDIRECT_URI = config(
    'GMAIL_OAUTH_REDIRECT_URI',
    default='http://localhost:8000/api/gmail/callback/',
)
BACKEND_URL = config('BACKEND_URL', default='http://localhost:8000')
FRONTEND_URL = config('FRONTEND_URL', default='http://localhost:3000')

# ── Resend — transactional email ──────────────────────────────────────────────
RESEND_API_KEY    = config('RESEND_API_KEY',    default='')
RESEND_FROM_EMAIL = config('RESEND_FROM_EMAIL', default='Ledgix <onboarding@resend.dev>')

# ── Stripe ────────────────────────────────────────────────────────────────────
STRIPE_PUBLISHABLE_KEY  = config('DEV_STRIPE_PUBLISHABLE_KEY',  default='')
STRIPE_SECRET_KEY       = config('DEV_STRIPE_SECRET_KEY',       default='')
# NOTE: This should be a price_ ID (e.g. price_xxx), not a product ID.
# Find it in your Stripe Dashboard → Products → [your product] → Pricing.
STRIPE_PRO_PRICE_ID     = config('DEV_STRIPE_PRO_PRICE_ID',     default='')
# Set via `stripe listen --forward-to localhost:8000/api/billing/webhook/`
STRIPE_WEBHOOK_SECRET   = config('STRIPE_WEBHOOK_SECRET',       default='')

# Pub/Sub topic that Google should deliver Gmail push notifications to.
# Format: projects/<project-id>/topics/<topic-name>
GMAIL_PUBSUB_TOPIC = config('GMAIL_PUBSUB_TOPIC', default='')

# ── Celery Beat — periodic Gmail auto-sync ────────────────────────────────────
# Requires a separate beat worker:  celery -A config beat --loglevel=info
from celery.schedules import crontab  # noqa: E402

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{levelname}] {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "gmail_integration": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
    },
}

CELERY_BEAT_SCHEDULE = {
    # History-based incremental sync is cheap (only fetches new messages), so
    # running every 2 minutes gives near-real-time invoice detection.
    'gmail-auto-sync-every-2-min': {
        'task':     'gmail_integration.tasks.sync_all_active_integrations',
        'schedule': crontab(minute='*/2'),
    },
    # Gmail push watches expire after at most 7 days; renew any that are
    # within 24 hours of expiry so notifications stay active continuously.
    'renew-gmail-watches-daily': {
        'task':     'gmail_integration.tasks.renew_expiring_watches',
        'schedule': crontab(hour='*/12'),
    },
    # Purge unverified accounts that are older than 7 days so the email
    # can be re-used for a fresh sign-up.
    'delete-unverified-users-daily': {
        'task':     'users.tasks.delete_unverified_users',
        'schedule': crontab(hour=3, minute=0),  # 03:00 UTC daily
    },
}
