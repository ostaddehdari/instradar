import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TESTING = os.environ.get("INSTRADAR_TEST", "") == "1"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "test-only-secret" if TESTING else "")
if not SECRET_KEY:
    raise RuntimeError("DJANGO_SECRET_KEY is required")
DEBUG = False
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "srun.ir,127.0.0.1,localhost").split(",")
CSRF_TRUSTED_ORIGINS = ["https://srun.ir"]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = not TESTING
CSRF_COOKIE_SECURE = not TESTING
# This domain hosts other Django applications; never reuse their default cookie names.
SESSION_COOKIE_NAME = "instradar_session"
CSRF_COOKIE_NAME = "instradar_csrf"
SESSION_COOKIE_PATH = "/instradar/"
CSRF_COOKIE_PATH = "/instradar/"
SESSION_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles", "radar",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware", "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware", "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [BASE_DIR / "templates"],
              "APP_DIRS": True, "OPTIONS": {"builtins": ["radar.templatetags.persian"], "context_processors": [
                  "django.template.context_processors.debug", "django.template.context_processors.request",
                  "django.contrib.auth.context_processors.auth", "django.contrib.messages.context_processors.messages",
                  "radar.context_processors.app_version"]}}]
WSGI_APPLICATION = "config.wsgi.application"
if TESTING:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
else:
    required_pg = ("PGDATABASE", "PGUSER", "PGPASSWORD", "PGHOST", "PGPORT")
    missing_pg = [name for name in required_pg if not os.environ.get(name)]
    if missing_pg:
        raise RuntimeError(f"Missing PostgreSQL settings: {', '.join(missing_pg)}")
    DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ["PGDATABASE"], "USER": os.environ["PGUSER"],
        "PASSWORD": os.environ["PGPASSWORD"], "HOST": os.environ["PGHOST"],
        "PORT": os.environ["PGPORT"], "CONN_MAX_AGE": 60,
        "OPTIONS": {"connect_timeout": 5}}}
AUTH_PASSWORD_VALIDATORS = [{"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
                            {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"}]
LANGUAGE_CODE = "fa"
TIME_ZONE = "Asia/Tehran"
USE_TZ = True
STATIC_URL = "/instradar/static/"
STATIC_ROOT = Path(os.environ.get("STATIC_ROOT", BASE_DIR / "staticfiles"))
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/instradar/login/"
LOGIN_REDIRECT_URL = "/instradar/"
LOGOUT_REDIRECT_URL = LOGIN_URL
CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/6")
CELERY_RESULT_BACKEND = CELERY_BROKER_URL
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 3600}
CELERY_TASK_ROUTES = {
    "radar.publish_tasks.*": {"queue": "telegram"},
}
CELERY_BEAT_SCHEDULE = {
    "recover-jobs": {"task": "radar.tasks.recover_jobs", "schedule": 60.0},
    "heartbeat": {"task": "radar.tasks.worker_heartbeat", "schedule": 30.0},
    "recover-media-sync": {"task": "radar.media_tasks.recover_media_sync", "schedule": 60.0},
    "recover-comment-sync": {"task": "radar.comment_tasks.recover_comments", "schedule": 60.0},
    "recover-relationship-sync": {"task": "radar.relationship_tasks.recover_relationships", "schedule": 60.0},
    "recover-telegram-publish": {"task": "radar.publish_tasks.recover_publish_jobs", "schedule": 60.0},
    "telegram-worker-heartbeat": {"task": "radar.publish_tasks.telegram_worker_heartbeat", "schedule": 15.0},
    "orchestrate-observations": {"task": "radar.observation_tasks.orchestrate_due_observations", "schedule": 15.0},
    "daily-profile-enrichment": {"task": "radar.observation_tasks.enrichment_tick", "schedule": 60.0},
    "system-metrics": {"task": "radar.monitor_tasks.collect_system_metric", "schedule": 15.0},
    "cleanup-monitoring": {"task": "radar.monitor_tasks.cleanup_monitoring", "schedule": 86400.0},
}
INSTRADAR_BROWSER_SOCKET = os.environ.get("INSTRADAR_BROWSER_SOCKET", "/opt/instradar-py/shared/browser.sock")
INSTRADAR_SHARED = Path(os.environ.get("INSTRADAR_SHARED", "/opt/instradar-py/shared"))
INSTRADAR_JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT_SECONDS", "180"))
INSTRADAR_MIN_DELAY_MS = int(os.environ.get("MIN_DELAY_MS", "1500"))
INSTRADAR_MAX_DELAY_MS = int(os.environ.get("MAX_DELAY_MS", "4000"))
INSTRADAR_MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
INSTRADAR_BACKOFF_BASE = int(os.environ.get("BACKOFF_BASE_SECONDS", "60"))
INSTRADAR_BACKOFF_MAX = int(os.environ.get("BACKOFF_MAX_SECONDS", "600"))
INSTRADAR_MEDIA_TIMEOUT = int(os.environ.get("MEDIA_PAGE_TIMEOUT_SECONDS", "240"))
INSTRADAR_MEDIA_PAGE_SIZE = min(30, max(1, int(os.environ.get("MEDIA_PAGE_SIZE", "8"))))
INSTRADAR_COMMENT_TIMEOUT = int(os.environ.get("COMMENT_JOB_TIMEOUT_SECONDS", "240"))
INSTRADAR_COMMENT_REQUEST_TIMEOUT = int(os.environ.get("COMMENT_REQUEST_TIMEOUT_SECONDS", "90"))
INSTRADAR_COMMENT_PAGE_SIZE = min(50, max(1, int(os.environ.get("COMMENT_PAGE_SIZE", "30"))))
INSTRADAR_POOL_MAX_CONCURRENCY = min(10, max(1, int(os.environ.get(
    "POOL_MAX_CONCURRENCY", "4"))))
INSTRADAR_ACCOUNT_HARD_HOURLY_LIMIT = min(1000, max(1, int(os.environ.get(
    "ACCOUNT_HARD_HOURLY_LIMIT", "120"))))
INSTRADAR_ACCOUNT_HARD_DAILY_LIMIT = min(10000, max(1, int(os.environ.get(
    "ACCOUNT_HARD_DAILY_LIMIT", "600"))))
INSTRADAR_ACCOUNT_MIN_INTERVAL_FLOOR = max(3, int(os.environ.get(
    "ACCOUNT_MIN_INTERVAL_FLOOR", "5")))
INSTRADAR_POOL_HARD_HOURLY_LIMIT = min(10000, max(1, int(os.environ.get(
    "POOL_HARD_HOURLY_LIMIT", "1000"))))
INSTRADAR_POOL_HARD_DAILY_LIMIT = min(100000, max(1, int(os.environ.get(
    "POOL_HARD_DAILY_LIMIT", "10000"))))
INSTRADAR_RATE_COOLDOWN_SECONDS = max(900, int(os.environ.get(
    "RATE_COOLDOWN_SECONDS", "21600")))
INSTRADAR_FEEDBACK_COOLDOWN_SECONDS = max(3600, int(os.environ.get(
    "FEEDBACK_COOLDOWN_SECONDS", "43200")))
INSTRADAR_FAILURE_COOLDOWN_SECONDS = max(60, int(os.environ.get(
    "FAILURE_COOLDOWN_SECONDS", "900")))
INSTRADAR_FAILURES_BEFORE_COOLDOWN = min(10, max(1, int(os.environ.get(
    "FAILURES_BEFORE_COOLDOWN", "2"))))
INSTRADAR_CIRCUIT_BREAKER_EVENTS = min(20, max(2, int(os.environ.get(
    "CIRCUIT_BREAKER_EVENTS", "3"))))
INSTRADAR_CIRCUIT_BREAKER_WINDOW_SECONDS = max(60, int(os.environ.get(
    "CIRCUIT_BREAKER_WINDOW_SECONDS", "900")))
INSTRADAR_COMMENT_DISPATCH_STALE_SECONDS = max(60, int(os.environ.get(
    "COMMENT_DISPATCH_STALE_SECONDS", "300")))
INSTRADAR_COMMENT_INCREMENTAL_REFRESH_HOURS = max(1, int(os.environ.get(
    "COMMENT_INCREMENTAL_REFRESH_HOURS", "168")))
INSTRADAR_ENRICHMENT_BATCH_SIZE = min(100, max(1, int(os.environ.get(
    "ENRICHMENT_BATCH_SIZE", "20"))))
INSTRADAR_ENRICHMENT_SPACING_SECONDS = max(15, int(os.environ.get(
    "ENRICHMENT_SPACING_SECONDS", "30")))
INSTRADAR_RELATIONSHIP_TIMEOUT = int(os.environ.get("RELATIONSHIP_JOB_TIMEOUT_SECONDS", "300"))
INSTRADAR_RELATIONSHIP_REQUEST_TIMEOUT = int(os.environ.get("RELATIONSHIP_REQUEST_TIMEOUT_SECONDS", "120"))
INSTRADAR_RELATIONSHIP_PAGE_SIZE = min(200, max(1, int(os.environ.get("RELATIONSHIP_PAGE_SIZE", "100"))))
INSTRADAR_RELATIONSHIP_PAGES_PER_TASK = min(10, max(1, int(os.environ.get("RELATIONSHIP_PAGES_PER_TASK", "2"))))
INSTRADAR_STORAGE_ROOT = Path(os.environ.get(
    "INSTRADAR_STORAGE_ROOT", str(INSTRADAR_SHARED / "storage")))
INSTRADAR_DOWNLOAD_RUNTIME = Path(os.environ.get(
    "INSTRADAR_DOWNLOAD_RUNTIME", str(INSTRADAR_SHARED / "runtime")))
INSTRADAR_DOWNLOAD_TIMEOUT = min(3600, max(60, int(os.environ.get(
    "DOWNLOAD_TIMEOUT_SECONDS", "900"))))
INSTRADAR_DOWNLOAD_MAX_RETRIES = min(10, max(1, int(os.environ.get(
    "DOWNLOAD_MAX_RETRIES", "3"))))
INSTRADAR_DOWNLOAD_BACKOFF_BASE = min(3600, max(10, int(os.environ.get(
    "DOWNLOAD_BACKOFF_BASE_SECONDS", "60"))))
INSTRADAR_DOWNLOAD_MIN_FREE_BYTES = max(104857600, int(os.environ.get(
    "DOWNLOAD_MIN_FREE_BYTES", "1073741824")))
INSTRADAR_DOWNLOAD_RATE_LIMIT = os.environ.get("DOWNLOAD_RATE_LIMIT", "").strip()[:32]
INSTRADAR_TELEGRAM_TIMEOUT = min(3600, max(60, int(os.environ.get(
    "TELEGRAM_ITEM_TIMEOUT_SECONDS", "900"))))
INSTRADAR_TELEGRAM_MAX_RETRIES = min(10, max(1, int(os.environ.get(
    "TELEGRAM_MAX_RETRIES", "3"))))
INSTRADAR_TELEGRAM_BACKOFF_BASE = min(3600, max(10, int(os.environ.get(
    "TELEGRAM_BACKOFF_BASE_SECONDS", "60"))))
INSTRADAR_LOG_WINDOW_DEFAULT = min(300, max(10, int(os.environ.get("LOG_WINDOW_SECONDS", "60"))))
INSTRADAR_LOG_WINDOW_MAX = min(600, max(INSTRADAR_LOG_WINDOW_DEFAULT,
                                        int(os.environ.get("LOG_WINDOW_MAX_SECONDS", "300"))))
INSTRADAR_LOG_MAX_LINES = min(20000, max(100, int(os.environ.get("LOG_MAX_LINES", "5000"))))
INSTRADAR_LOG_MAX_BYTES = min(5_000_000, max(100_000, int(os.environ.get("LOG_MAX_BYTES", "2000000"))))
INSTRADAR_MONITOR_RETENTION_DAYS = min(365, max(1, int(os.environ.get("MONITOR_RETENTION_DAYS", "30"))))
if INSTRADAR_MIN_DELAY_MS < 0 or INSTRADAR_MAX_DELAY_MS < INSTRADAR_MIN_DELAY_MS:
    raise RuntimeError("Invalid MIN_DELAY_MS / MAX_DELAY_MS")
