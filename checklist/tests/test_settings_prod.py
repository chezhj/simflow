"""
Structural checks on the production settings module.

These are not style assertions. Each one guards a configuration that fails
*silently* in production — no exception, no log line, just mail that never
arrives or credentials sent to a port that does not accept them. None of it is
exercised by the rest of the suite, which runs under settings.dev.
"""

# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring

import importlib
import os
from unittest import mock

from django.test import SimpleTestCase


def _load_prod(**env):
    """Import settings.prod fresh with the given environment."""
    base = {"SECRET_KEY": "test-key-not-used-for-anything"}
    base.update(env)
    with mock.patch.dict(os.environ, base, clear=False):
        module = importlib.import_module("smart_training_checklist.settings.prod")
        return importlib.reload(module)


class TestProductionMailTransport(SimpleTestCase):
    """
    Defaults must match weekmenu's prod.py, the working reference on this
    server. Django's own defaults (localhost:25) are actively wrong here: Exim
    advertises no AUTH on that port even after STARTTLS, and Django calls
    login() whenever both credentials are set, so the combination raises
    SMTPNotSupportedError on every send.
    """

    def test_defaults_point_at_the_submission_port(self):
        s = _load_prod()
        self.assertEqual(s.EMAIL_HOST, "mail.vdwaal.net")
        self.assertEqual(s.EMAIL_PORT, 587)
        self.assertTrue(s.EMAIL_USE_TLS)
        self.assertFalse(s.EMAIL_USE_SSL)

    def test_transport_stays_overridable(self):
        s = _load_prod(EMAIL_HOST="smtp.example.com", EMAIL_PORT="465",
                       EMAIL_USE_TLS="False", EMAIL_USE_SSL="True")
        self.assertEqual(s.EMAIL_HOST, "smtp.example.com")
        self.assertEqual(s.EMAIL_PORT, 465)
        self.assertFalse(s.EMAIL_USE_TLS)
        self.assertTrue(s.EMAIL_USE_SSL)

    def test_a_send_timeout_is_always_set(self):
        """Django ships none, so a dead host would hold a worker open forever."""
        self.assertIsNotNone(_load_prod().EMAIL_TIMEOUT)


class TestProductionErrorMail(SimpleTestCase):

    def test_django_request_still_reaches_mail_admins(self):
        """
        The trap this file exists for. Django attaches mail_admins to the
        "django" logger; our django.request entry sets propagate=False and so
        shadows that chain. Drop "mail_admins" from its handler list and error
        mail stops dead, with nothing anywhere to say so.
        """
        logging = _load_prod(ADMIN_EMAIL="ops@example.com").LOGGING
        self.assertIn("mail_admins", logging["handlers"])
        self.assertIn("mail_admins", logging["loggers"]["django.request"]["handlers"])
        self.assertIn("file", logging["loggers"]["django.request"]["handlers"])

    def test_admins_populated_from_the_environment(self):
        s = _load_prod(ADMIN_EMAIL="ops@example.com")
        self.assertEqual([addr for _, addr in s.ADMINS], ["ops@example.com"])
        self.assertEqual(s.MANAGERS, s.ADMINS)

    def test_no_admin_email_is_a_no_op_not_a_crash(self):
        """Unset must degrade to "no mail", never to a broken settings import."""
        s = _load_prod(ADMIN_EMAIL="")
        self.assertEqual(s.ADMINS, [])

    def test_error_mail_sender_is_configured(self):
        """Django uses SERVER_EMAIL for error mail, not DEFAULT_FROM_EMAIL."""
        s = _load_prod()
        self.assertTrue(s.SERVER_EMAIL)
        self.assertNotEqual(s.SERVER_EMAIL, "root@localhost")

    def test_mail_admins_is_suppressed_when_debug_is_on(self):
        handler = _load_prod().LOGGING["handlers"]["mail_admins"]
        self.assertIn("require_debug_false", handler["filters"])
