"""passenger file to connect django app to server environment"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# Passenger imports this module and uses the resulting `application`. The default
# must be set BEFORE importing wsgi.py, because wsgi.py only defines the `app`
# symbol when DJANGO_SETTINGS_MODULE does not end in "dev". setdefault still lets
# a server-set env var win.
os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE", "smart_training_checklist.settings.prod"
)

from smart_training_checklist.wsgi import app  # noqa: E402

application = app
