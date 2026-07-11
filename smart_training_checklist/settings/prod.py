"""Production settings file"""

# pylint: disable=unused-wildcard-import,wildcard-import
import os
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
