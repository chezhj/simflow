"""Production settings file"""

# pylint: disable=unused-wildcard-import,wildcard-import
import os
from pathlib import Path

from decouple import config
from .base import *

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config("SECRET_KEY")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = False

ALLOWED_HOSTS = [
    "vdwaal.net",
    "simflow.vdwaal.net",
]

WWW_DIR = os.path.join(Path(BASE_DIR).resolve(), "public_html")

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.1/howto/static-files/
STATIC_ROOT = os.path.join(WWW_DIR, "static")
STATIC_URL = "/static/"

SIMBRIEF_URL = "https://www.simbrief.com/api/xml.fetcher.php?userid="


# ── Security ──────────────────────────────────────────────────────────────── #
#
# The site is served over HTTPS only, so the cookies may as well be marked
# Secure: the flag is about what the browser will send back, and the browser's
# connection is already TLS regardless of what Passenger tells Django. These are
# therefore safe to switch on without knowing how the front end terminates TLS.

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

# The two below are NOT safe to enable blind, so they are opt-in from .env.
#
# SECURE_SSL_REDIRECT sends anything Django considers insecure back as a 301 to
# https. When Apache terminates TLS and hands Passenger a plain HTTP request
# without a proxy header Django can read, every request looks insecure and the
# redirect loops — the site goes down, and it goes down for everyone at once.
# Confirm the scheme first (log request.is_secure() on a live request, or set
# SECURE_PROXY_SSL_HEADER below to match whatever Apache actually forwards),
# then set SECURE_SSL_REDIRECT=True in the server's .env.
#
# SECURE_HSTS_SECONDS tells browsers to refuse plain HTTP for that long, and
# they honour it even if the certificate later lapses or the app moves. Ramp it:
# 3600, then a day, then a week, and only then a year. Do not add
# includeSubDomains until every host under vdwaal.net is HTTPS.
SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=False, cast=bool)
SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=0, cast=int)

# Uncomment once it is confirmed that Apache forwards this exact header.
# SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# ── Error visibility ──────────────────────────────────────────────────────── #
#
# With DEBUG=False and no LOGGING block, Django's default config routes
# django.request errors to mail_admins only — and ADMINS is empty, so every
# 500 the app serves is discarded without a trace. Write them to a file the
# server keeps instead, so a user reporting "it broke" can be matched to a
# stack trace. logs/ is created by the app at runtime and is gitignored.
_LOG_DIR = Path(BASE_DIR) / "logs"
_LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(_LOG_DIR / "django.log"),
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
            "formatter": "verbose",
        },
    },
    "loggers": {
        "django.request": {"handlers": ["file"], "level": "ERROR", "propagate": False},
        "checklist": {"handlers": ["file"], "level": "INFO", "propagate": False},
    },
}


# ── Outbound mail ─────────────────────────────────────────────────────────── #
#
# Django's default is the SMTP backend pointed at localhost:25. With no MTA
# there, PasswordResetForm.save() raises and the reset view returns a 500 — so
# the whole recovery flow was wired up but dead. Configure the real host here.
#
# NOTE: EMAIL_HOST must be set in the server's .env BEFORE the release carrying
# this change is activated. The default below reproduces Django's own default,
# so an unset value fails exactly the way it does today rather than breaking the
# boot — but it does not work either.
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = config("EMAIL_HOST", default="localhost")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = config("EMAIL_USE_TLS", default=True, cast=bool)
EMAIL_USE_SSL = config("EMAIL_USE_SSL", default=False, cast=bool)

# Django ships no default timeout, so a black-holed SMTP host holds the worker
# open indefinitely. Under Passenger with a handful of workers, a few reset
# requests against a dead mail server would take the site down. Bound it.
EMAIL_TIMEOUT = config("EMAIL_TIMEOUT", default=10, cast=int)

DEFAULT_FROM_EMAIL = config(
    "DEFAULT_FROM_EMAIL", default="SimFlow <noreply@simflow.vdwaal.net>"
)
SERVER_EMAIL = DEFAULT_FROM_EMAIL
