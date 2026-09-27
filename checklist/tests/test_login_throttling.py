"""
Login throttling (django-axes).

Nothing throttled /login/, /register/ or /admin/ before this. What is worth
testing is not that axes works — it has its own suite — but that *this*
configuration does what was intended, because every value in settings
overrides an axes default that is wrong here:

    FAILURE_LIMIT      axes 3  ->  5   (3 is reachable by honest mistyping)
    COOLOFF_TIME       axes None -> 30m (None means the lockout NEVER expires)
    RESET_ON_SUCCESS   axes False -> True (failures otherwise accumulate forever)
    LOCKOUT_PARAMETERS axes ip_address -> [["username", "ip_address"]]

That last one is the important one and has two tests of its own: locking on IP
alone would take out everyone behind a shared address, and locking on username
alone is a trivial denial of service against any pilot whose username is known.
"""

# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring

from datetime import timedelta

from axes.models import AccessAttempt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from checklist.tests.testFactories import SOPFactory

User = get_user_model()

PASSWORD = "correct-horse-battery-staple"


class _ThrottleBase(TestCase):
    def setUp(self):
        SOPFactory()
        self.user = User.objects.create_user(
            username="pilot", email="pilot@example.test", password=PASSWORD
        )
        self.url = reverse("login")

    def tearDown(self):
        # axes keeps counters in the database, so TestCase rollback clears
        # them — but the cache-based handler would not, and a future switch
        # should not leak state between tests silently.
        AccessAttempt.objects.all().delete()

    def fail_login(self, username="pilot", **extra):
        return self.client.post(
            self.url, {"username": username, "password": "wrong"}, **extra
        )

    def succeed_login(self, username="pilot", **extra):
        return self.client.post(
            self.url, {"username": username, "password": PASSWORD}, **extra
        )


class TestTheConfiguredValues(TestCase):
    """
    Guards the settings themselves. Each of these reverts to an axes default
    that is actively wrong here if the line is ever dropped.
    """

    def test_the_cooloff_is_set_at_all(self):
        """
        axes defaults AXES_COOLOFF_TIME to None, which means the lockout never
        expires and has to be cleared by hand with `manage.py axes_reset`.
        """
        self.assertIsNotNone(settings.AXES_COOLOFF_TIME)
        self.assertIsInstance(settings.AXES_COOLOFF_TIME, timedelta)

    def test_failures_reset_on_a_successful_sign_in(self):
        """axes defaults this to False, so failures accumulate across logins."""
        self.assertTrue(settings.AXES_RESET_ON_SUCCESS)

    def test_the_lockout_is_on_the_pair_not_either_alone(self):
        """
        A nested list is AND in axes; a flat list is OR. Flattening this to
        ["username", "ip_address"] would silently turn a targeted lockout into
        a denial-of-service vector.
        """
        self.assertEqual(settings.AXES_LOCKOUT_PARAMETERS, [["username", "ip_address"]])

    def test_counters_live_in_the_database_not_the_cache(self):
        """
        The whole reason axes was chosen. No CACHES is configured, so a
        cache-backed handler would be LocMemCache — per Passenger worker —
        and an attacker would get failure_limit x n_workers attempts.
        """
        self.assertEqual(
            getattr(settings, "AXES_HANDLER", "axes.handlers.database.AxesDatabaseHandler"),
            "axes.handlers.database.AxesDatabaseHandler",
        )

    def test_the_client_address_is_not_taken_from_a_client_header(self):
        """
        Trusting X-Forwarded-For without a known proxy count lets a client
        spoof its address and bypass the lockout entirely.
        """
        order = getattr(settings, "AXES_IPWARE_META_PRECEDENCE_ORDER", ("REMOTE_ADDR",))
        self.assertEqual(tuple(order), ("REMOTE_ADDR",))


@override_settings(AXES_FAILURE_LIMIT=3)
class TestLockout(_ThrottleBase):

    def test_attempts_below_the_limit_are_not_locked_out(self):
        for _ in range(2):
            response = self.fail_login()
        self.assertNotEqual(response.status_code, 429)

    def test_the_limit_locks_the_account_out(self):
        for _ in range(3):
            response = self.fail_login()
        self.assertEqual(response.status_code, 429)

    def test_the_lockout_page_is_the_custom_one(self):
        for _ in range(3):
            response = self.fail_login()
        body = response.content.decode()
        self.assertIn("Hold short", body)
        self.assertIn("30 minutes", body)

    def test_the_lockout_page_does_not_echo_the_attempted_username(self):
        """
        It would reflect attacker-chosen text, and confirm to whoever is
        guessing that they reached the right form.
        """
        for _ in range(3):
            response = self.fail_login(username="CANARY-NAME")
        self.assertNotIn("CANARY-NAME", response.content.decode())

    def test_the_correct_password_is_refused_while_locked_out(self):
        """The point of a lockout: the attacker's next guess cannot be right."""
        for _ in range(3):
            self.fail_login()
        response = self.succeed_login()
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_successful_sign_in_clears_the_count(self):
        """AXES_RESET_ON_SUCCESS — otherwise failures bank up forever."""
        self.fail_login()
        self.fail_login()
        self.succeed_login()
        self.assertIn("_auth_user_id", self.client.session)

        self.client.logout()
        # Two more failures would have tripped the limit of three without the
        # reset; with it, the count started again.
        response = self.fail_login()
        response = self.fail_login()
        self.assertNotEqual(response.status_code, 429)


@override_settings(AXES_FAILURE_LIMIT=3)
class TestTheLockoutIsOnTheCombination(_ThrottleBase):
    """
    The two failure modes the combination exists to avoid. These are the tests
    that would catch someone "simplifying" LOCKOUT_PARAMETERS to a flat list.
    """

    def test_another_pilot_from_the_same_address_can_still_sign_in(self):
        """
        Locking on ip_address alone would take out everyone behind a shared
        address — a club, a household, a mis-detected proxy.
        """
        other = User.objects.create_user(username="copilot", password=PASSWORD)
        for _ in range(3):
            self.fail_login(username="pilot")

        response = self.client.post(
            self.url, {"username": other.username, "password": PASSWORD}
        )

        self.assertNotEqual(response.status_code, 429)
        self.assertIn("_auth_user_id", self.client.session)

    def test_the_same_pilot_from_a_different_address_can_still_sign_in(self):
        """
        Locking on username alone would be a denial of service: anyone who
        knows a username could lock that pilot out of their own checklist.
        """
        for _ in range(3):
            self.fail_login(REMOTE_ADDR="198.51.100.10")

        response = self.succeed_login(REMOTE_ADDR="203.0.113.20")

        self.assertNotEqual(response.status_code, 429)
        self.assertIn("_auth_user_id", self.client.session)


@override_settings(AXES_FAILURE_LIMIT=3)
class TestTheAttemptIsRecorded(_ThrottleBase):

    def test_a_failure_is_written_to_the_database(self):
        self.fail_login()
        self.assertEqual(AccessAttempt.objects.count(), 1)

    def test_the_recorded_address_is_the_request_address(self):
        """
        If this ever comes back as the proxy's address rather than the
        client's, IP-based locking is meaningless and the combination is
        silently doing username-only matching.
        """
        self.fail_login(REMOTE_ADDR="198.51.100.10")
        attempt = AccessAttempt.objects.get()
        self.assertEqual(attempt.ip_address, "198.51.100.10")

    def test_a_forwarded_header_cannot_move_the_recorded_address(self):
        self.fail_login(
            REMOTE_ADDR="198.51.100.10", HTTP_X_FORWARDED_FOR="203.0.113.99"
        )
        attempt = AccessAttempt.objects.get()
        self.assertEqual(attempt.ip_address, "198.51.100.10")


class TestTheSecondBackendDidNotBreakRegistration(TestCase):
    """
    Adding AxesStandaloneBackend gave the project two authentication backends,
    and `login(request, user)` can only infer the backend when there is
    exactly one. Registration calls login() directly on a freshly created user
    — never through authenticate() — so it began raising

        AttributeError: 'User' object has no attribute 'backend'

    and registration was broken outright until the backend was named. Nothing
    about throttling touches this view, which is exactly why it is easy to
    miss; the test lives here, with the change that caused it.
    """

    def setUp(self):
        SOPFactory()

    def test_a_new_pilot_is_signed_in_after_registering(self):
        response = self.client.post(
            reverse("register"),
            {
                "username": "newpilot",
                "email": "newpilot@example.test",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)
        self.assertTrue(User.objects.filter(username="newpilot").exists())
