import os

import environ

from celery.schedules import crontab


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

env = environ.Env()
environ.Env.read_env(os.path.join(BASE_DIR, ".env"))

DEBUG = env.bool("DEBUG")
STAGE = env.bool("STAGE")
SECRET_KEY = env("SECRET_KEY")

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env.list("TRUSTED_ORIGINS")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB"),
        "USER": env("POSTGRES_USER"),
        "PASSWORD": env("POSTGRES_PASSWORD"),
        "HOST": env("PGBOUNCER_CONTAINER"),
        "PORT": env("PGBOUNCER_PORT"),
        "CONN_MAX_AGE": 0,
        "DISABLE_SERVER_SIDE_CURSORS": True,
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "core",
    "health",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [os.path.join(BASE_DIR, "templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "core.wsgi.application"

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")
STATICFILES_DIRS = [os.path.join(BASE_DIR, "static")]
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

REDIS_URL = env("REDIS_URL")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND")
CELERY_RESULT_BACKEND_THREAD_SAFE = True
CELERY_BROKER_POOL_LIMIT = 20
CELERY_REDIS_MAX_CONNECTIONS = 20

CELERY_BEAT_SCHEDULE = {
    # Обновления данных о репо каждые 12 ч (00:00, 12:00)
    'task_update_all_public_repos': {
        'task': 'health.tasks.task_update_all_public_repos',
        'schedule': crontab(minute=0, hour='*/12'),
    },
    # Сканирование метрик каждые 6 ч (00:30, 6:30, 12:30, 18:30)
    #'scan_all_public_repositories': {
    #    'task': 'health.tasks.task_scan_all_public_repositories',
    #    'schedule': crontab(minute=30, hour='*/6'),
    #},
    # Снимает зависшие сканы каждые 20 м
    'reap_stale_scans': {
        'task': 'health.tasks.task_reap_stale_scans',
        'schedule': crontab(minute='*/20'),
    },
}

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
}

SOURCECRAFT_API_BASE_URL = env("SOURCECRAFT_API_BASE_URL")
SOURCECRAFT_API_FILE_BASE_URL = env("SOURCECRAFT_API_FILE_BASE_URL")
SOURCECRAFT_API_APPSEC_BASE_URL = env("SOURCECRAFT_API_APPSEC_BASE_URL")
SOURCECRAFT_API_TOKEN = env("SOURCECRAFT_API_TOKEN")

FIELD_ENCRYPTION_KEYS = [
    env("FIELD_ENCRYPTION_KEY"),
]

SCAN_STALE_TIMEOUT_MINUTES = env.int("SCAN_STALE_TIMEOUT_MINUTES")

LOGIN_URL = "health:yandex-login"
LOGIN_REDIRECT_URL = "health:my-repos"
LOGOUT_REDIRECT_URL = "health:repo-list"

YANDEX_CLIENT_ID = env("YANDEX_CLIENT_ID", default="")
YANDEX_CLIENT_SECRET = env("YANDEX_CLIENT_SECRET", default="")
YANDEX_REDIRECT_URI = env(
    "YANDEX_REDIRECT_URI",
    default="http://127.0.0.1:8002/auth/yandex/callback/",
)
YANDEX_OAUTH_BASE = env("YANDEX_OAUTH_BASE", default="https://oauth.yandex.ru")
YANDEX_USERINFO_URL = env(
    "YANDEX_USERINFO_URL",
    default="https://login.yandex.ru/info",
)
YANDEX_OAUTH_SCOPE = env("YANDEX_OAUTH_SCOPE", default="login:info login:email")
