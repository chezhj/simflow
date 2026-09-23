"""
Plugin version window, and how it reaches the pilot.

The plugin learns its own status from an API response and writes it to
X-Plane's Log.txt. Nobody reads that, so the browser banner is the only place
an outdated plugin is actually visible — these cover the path from the
X-Plugin-Version header to the poll response that drives it.
"""

# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring

import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from checklist.models import FlightSession, Procedure
from checklist.plugin_views import plugin_status_for_version
from checklist.tests.testFactories import CheckItemFactory, SOPFactory

User = get_user_model()


@override_settings(PLUGIN_MIN_VERSION=(1, 0, 2), PLUGIN_WARN_BELOW=(1, 1, 0))
class TestVersionWindow(TestCase):

    def test_the_oldest_released_version_is_warned_not_blocked(self):
        """
        Nothing in 1.1.0 changed the wire protocol, so a 1.0.2 plugin still
        works — just slower. Blocking it would strand a pilot mid-flight over
        a performance change.
        """
        self.assertEqual(plugin_status_for_version("1.0.2"), "warn")

    def test_the_current_version_is_ok(self):
        self.assertEqual(plugin_status_for_version("1.1.0"), "ok")
        self.assertEqual(plugin_status_for_version("1.2.0"), "ok")

    def test_below_the_minimum_is_blocked(self):
        self.assertEqual(plugin_status_for_version("1.0.1"), "blocked")
        self.assertEqual(plugin_status_for_version("0.9.9"), "blocked")

    def test_an_unreadable_version_warns_rather_than_blocking(self):
        """
        Blocking withholds session data and stops the checklist following the
        sim — too much to do to a client that merely failed to identify
        itself. Only an explicitly too-old version earns that. It still does
        not pass as current.
        """
        for raw in ("", "   ", "not-a-version", None):
            with self.subTest(raw=raw):
                self.assertEqual(plugin_status_for_version(raw or ""), "warn")

    def test_the_warn_band_is_not_empty(self):
        """
        Guards the configuration itself. Setting MIN to the current version
        would make "blocked" catch everything below it and leave no version
        that warns — so the warning nobody asked to be a block never fires.
        """
        from django.conf import settings
        self.assertLess(settings.PLUGIN_MIN_VERSION, settings.PLUGIN_WARN_BELOW)


@override_settings(PLUGIN_MIN_VERSION=(1, 0, 2), PLUGIN_WARN_BELOW=(1, 1, 0))
class TestVersionReachesTheBrowser(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="pilot", password="pw")
        self.profile = self.user.profile
        self.raw_key = self.profile.set_api_key()

        sop = SOPFactory()
        self.procedure = Procedure.objects.create(
            title="Before Start", step=1, slug="before-start", sop=sop
        )
        CheckItemFactory(procedure=self.procedure, step=1)
        self.session = FlightSession.objects.create(
            user_profile=self.profile, active_phase="before-start", is_active=True
        )
        s = self.client.session
        s["flight_session_key"] = self.session.session_key
        s.save()

    def _post_state(self, version):
        return self.client.post(
            reverse("checklist:api_plugin_state"),
            data=json.dumps({"session_id": self.session.pk, "datarefs": {}}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw_key}",
            HTTP_X_PLUGIN_VERSION=version,
        )

    def _poll(self):
        return self.client.get(
            reverse("checklist:api_poll"), {"procedure": "before-start", "since": 0}
        ).json()

    def test_state_post_records_the_reported_version(self):
        self._post_state("1.0.2")
        self.session.refresh_from_db()
        self.assertEqual(self.session.plugin_version, "1.0.2")

    def test_an_outdated_plugin_produces_a_warning_with_a_download_link(self):
        self._post_state("1.0.2")
        data = self._poll()
        self.assertEqual(data["plugin_status"], "warn")
        self.assertEqual(data["plugin_version"], "1.0.2")
        self.assertTrue(data["plugin_update_url"])

    def test_a_current_plugin_produces_nothing_to_show(self):
        self._post_state("1.1.0")
        data = self._poll()
        self.assertEqual(data["plugin_status"], "ok")
        self.assertEqual(data["plugin_update_url"], "")

    def test_no_banner_once_the_sim_has_disconnected(self):
        """
        A version recorded hours ago would otherwise produce a banner about a
        plugin that is not running — advice the pilot can do nothing with.
        """
        self._post_state("1.0.2")
        FlightSession.objects.filter(pk=self.session.pk).update(
            last_plugin_contact=timezone.now() - timezone.timedelta(minutes=30)
        )
        data = self._poll()
        self.assertFalse(data["sim_connected"])
        self.assertEqual(data["plugin_status"], "ok")
        self.assertEqual(data["plugin_version"], "")


class TestPluginDownloadUrl(TestCase):
    """
    Where an outdated plugin sends the pilot. The failure mode is silent — a
    404 nobody notices until someone tries to update — so the shape is pinned.
    """

    def test_it_does_not_use_the_latest_release_shortcut(self):
        """
        GitHub's /releases/latest is the newest release across ALL tags, and
        app releases (v*) share this repository with plugin releases
        (plugin-v*). A plugin release is normally followed by an app release,
        which makes "latest" an app release carrying no xflow-plugin.zip.

        Observed: with v2.8.0 as the latest release, the old URL redirected to
        /releases/download/v2.8.0/xflow-plugin.zip and returned 404.
        """
        from django.conf import settings

        self.assertNotIn("/releases/latest/", settings.PLUGIN_DOWNLOAD_URL)

    def test_it_points_at_plugin_releases(self):
        from django.conf import settings

        self.assertIn("/releases", settings.PLUGIN_DOWNLOAD_URL)
        self.assertIn("plugin", settings.PLUGIN_DOWNLOAD_URL)

    def test_the_rendered_link_survives_template_escaping(self):
        """
        The URL carries a query string, so autoescaping turns & into &amp; in
        the href. That is correct HTML and browsers resolve it back — this
        pins that the round trip is lossless.
        """
        import html

        from django.conf import settings
        from django.template import Context, Template

        rendered = Template(
            "{% load environment_tags %}"
            "<a href=\"{{ 'PLUGIN_DOWNLOAD_URL'|setting }}\">x</a>"
        ).render(Context({}))
        href = rendered.split('"')[1]
        self.assertEqual(html.unescape(href), settings.PLUGIN_DOWNLOAD_URL)
