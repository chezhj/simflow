"""Tests for GET /api/plugin/session/ (plugin_views.plugin_session)."""

# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from checklist.models import FlightSession

User = get_user_model()
URL = reverse("checklist:api_plugin_session")


def _get(client, key=None):
    kwargs = {}
    if key is not None:
        kwargs["HTTP_AUTHORIZATION"] = f"Bearer {key}"
    return client.get(URL, **kwargs)


class _Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="pilot", password="pw")
        self.profile = self.user.profile
        self.raw_key = self.profile.set_api_key()


class TestPluginSessionAuth(_Base):

    def test_missing_auth_returns_401(self):
        self.assertEqual(_get(self.client).status_code, 401)

    def test_wrong_key_returns_401(self):
        self.assertEqual(_get(self.client, key="fvw_wrongkey").status_code, 401)

    def test_post_returns_405(self):
        resp = self.client.post(URL, HTTP_AUTHORIZATION=f"Bearer {self.raw_key}")
        self.assertEqual(resp.status_code, 405)


class TestPluginSessionNotFound(_Base):

    def test_no_session_returns_404(self):
        self.assertEqual(_get(self.client, self.raw_key).status_code, 404)

    def test_inactive_session_returns_404(self):
        FlightSession.objects.create(
            user_profile=self.profile, active_phase="before-start", is_active=False
        )
        self.assertEqual(_get(self.client, self.raw_key).status_code, 404)

    def test_other_users_session_not_returned(self):
        other = User.objects.create_user(username="other", password="pw")
        FlightSession.objects.create(
            user_profile=other.profile, active_phase="before-start", is_active=True
        )
        self.assertEqual(_get(self.client, self.raw_key).status_code, 404)


class TestPluginSessionHappyPath(_Base):

    def setUp(self):
        super().setUp()
        self.session = FlightSession.objects.create(
            user_profile=self.profile, active_phase="before-start", is_active=True
        )

    def test_returns_200_with_session_id_and_active_phase(self):
        resp = _get(self.client, self.raw_key)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["session_id"], self.session.pk)
        self.assertEqual(data["active_phase"], "before-start")

    def test_empty_active_phase_is_returned_as_is(self):
        self.session.active_phase = ""
        self.session.save()
        resp = _get(self.client, self.raw_key)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["active_phase"], "")

    def test_returns_an_active_session_when_multiple_exist(self):
        # A second active session exists — endpoint should still return 200
        # with one of the active sessions (ordering not asserted here).
        FlightSession.objects.create(
            user_profile=self.profile, active_phase="after-start", is_active=True
        )
        resp = _get(self.client, self.raw_key)
        self.assertEqual(resp.status_code, 200)
        active_ids = set(
            FlightSession.objects.filter(user_profile=self.profile, is_active=True)
            .values_list("pk", flat=True)
        )
        self.assertIn(resp.json()["session_id"], active_ids)
