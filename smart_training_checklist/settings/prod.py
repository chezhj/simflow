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
# With DEBUG=False and no LOGGING block, Django routes django.request errors to
# mail_admins only — and ADMINS was empty, so every 500 was discarded untraced.
#
# NOTE the interaction with ADMINS below: Django attaches mail_admins to the
# "django" logger, and the django.request entry here sets propagate=False, so it
# shadows that chain entirely. Setting ADMINS alone would therefore send
# nothing. mail_admins has to be named here explicitly, and that is easy to drop
# by accident — test_settings_prod.py fails if it goes missing.
_LOG_DIR = Path(BASE_DIR) / "logs"
_LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "require_debug_false": {"()": "django.utils.log.RequireDebugFalse"},
    },
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
        # Sends inline, in the request that failed, so a slow mail host delays
        # the error response by up to EMAIL_TIMEOUT. It is also unthrottled:
        # a persistent 500 sends one mail per request.
        "mail_admins": {
            "class": "django.utils.log.AdminEmailHandler",
            "level": "ERROR",
            "filters": ["require_debug_false"],
            "include_html": True,
        },
    },
    "loggers": {
        "django.request": {
            "handlers": ["file", "mail_admins"],
            "level": "ERROR",
            "propagate": False,
        },
        "checklist": {"handlers": ["file"], "level": "INFO", "propagate": False},
    },
}


# ── Outbound mail ─────────────────────────────────────────────────────────── #
#
# Host, port and TLS match weekmenu's prod.py, which is the working reference
# for this server: mail.vdwaal.net:587 offers AUTH only after STARTTLS, so
# credentials never cross the wire in the clear. They stay overridable from
# .env, but the defaults mean this app needs the same three variables its
# neighbour does — EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, DEFAULT_FROM_EMAIL.
#
# Do not "simplify" this to Django's defaults (localhost:25). Exim there
# advertises no AUTH even after STARTTLS, and Django calls login() whenever
# both credentials are set, so that combination raises SMTPNotSupportedError.
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = config("EMAIL_HOST", default="mail.vdwaal.net")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_USE_TLS = config("EMAIL_USE_TLS", default=True, cast=bool)
EMAIL_USE_SSL = config("EMAIL_USE_SSL", default=False, cast=bool)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")

# Django ships no timeout at all, so a host that accepts the connection and
# then never answers holds the worker open indefinitely. weekmenu has the same
# exposure; here it also bounds the error mail below, which sends inline.
EMAIL_TIMEOUT = config("EMAIL_TIMEOUT", default=10, cast=int)

# Django's own default is "webmaster@localhost", which no mail host accepts.
DEFAULT_FROM_EMAIL = config(
    "DEFAULT_FROM_EMAIL", default="SimFlow <noreply@simflow.vdwaal.net>"
)
# The From address on error mail specifically — Django uses SERVER_EMAIL for
# those, not DEFAULT_FROM_EMAIL.
SERVER_EMAIL = config("SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)


# ── Error reporting ───────────────────────────────────────────────────────── #
#
# Read from .env rather than hardcoded: this repository is public, and an
# address in it is an address that gets scraped. Unset means an empty ADMINS,
# which makes the mail_admins handler a no-op — no crash, just no mail.
_ADMIN_EMAIL = config("ADMIN_EMAIL", default="")
ADMINS = [("SimFlow admin", _ADMIN_EMAIL)] if _ADMIN_EMAIL else []
MANAGERS = ADMINS
