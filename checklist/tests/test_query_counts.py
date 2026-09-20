"""
Query-count guards for the two endpoints that run on a timer.

The browser polls /api/poll/ every 1.5s and the plugin POSTs /api/plugin/state/
at 1Hz, per pilot, against SQLite. Neither may get more expensive as a
procedure grows.

This used to be violated badly: CheckItem.shouldshow() and should_warn() called
self.attributes.values_list(...), which builds a fresh queryset and therefore
ignores the prefetch_related("attributes") every caller already pays for. The
poll cost 15 + 3n queries — 135 for a 40-item procedure.
"""

# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring

import json
from datetime import datetime, timezone

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from checklist.models import (
    Attribute,
    FlightSession,
    FlightSessionAttribute,
    Procedure,
)
from checklist.tests.testFactories import CheckItemFactory, SOPFactory

RULE = {"dataref": "sim/test/x", "op": "eq", "value": 1}

# Headroom over the measured cost, so an accidental extra query is tolerated but
# a per-item one is not.
POLL_QUERY_CEILING = 15
STATE_QUERY_CEILING = 15


class _Base(TestCase):
    def _build(self, n, slug):
        """A procedure of n items, a third of them gated by each of 3 attributes."""
        sop = SOPFactory()
        proc = Procedure.objects.create(title="P", step=1, slug=slug, sop=sop)
        attrs = [Attribute.objects.create(title=f"{slug}-A{i}", order=i) for i in range(3)]
        session = FlightSession.objects.create(active_phase=slug)
        for a in attrs:
            FlightSessionAttribute.objects.create(
                flight_session=session, attribute=a, is_active=True
            )
        for i in range(n):
            item = CheckItemFactory(procedure=proc, step=i, auto_check_rule=RULE)
            item.attributes.add(attrs[i % 3])
        return session


@override_settings(DEBUG=False)
class TestPollQueryCount(_Base):

    def _count(self, n, slug):
        session = self._build(n, slug)
        s = self.client.session
        s["flight_session_key"] = session.session_key
        s.save()
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse("checklist:api_poll"), {"procedure": slug, "since": 0})
        return len(ctx.captured_queries)

    def test_cost_does_not_grow_with_procedure_size(self):
        small = self._count(5, "small")
        large = self._count(40, "large")
        self.assertEqual(
            small, large,
            f"poll cost scales with item count ({small} for 5 items, {large} for 40) — "
            "something is querying per item again",
        )

    def test_cost_stays_under_the_ceiling(self):
        self.assertLessEqual(self._count(40, "ceiling"), POLL_QUERY_CEILING)


@override_settings(DEBUG=False)
class TestPluginStateQueryCount(_Base):
    """The plugin posts this at 1Hz — the hottest endpoint in the app."""

    def setUp(self):
        user = get_user_model().objects.create_user(username="pilot", password="pw")
        self.profile = user.profile
        self.raw_key = self.profile.set_api_key()

    def _count(self, n, slug):
        session = self._build(n, slug)
        session.user_profile = self.profile
        session.save()
        body = {"session_id": session.pk, "datarefs": {"sim/test/x": 0}}
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.post(
                reverse("checklist:api_plugin_state"),
                data=json.dumps(body),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {self.raw_key}",
            )
        self.assertEqual(resp.status_code, 200)
        return len(ctx.captured_queries)

    def test_cost_does_not_grow_with_procedure_size(self):
        small = self._count(5, "psmall")
        large = self._count(40, "plarge")
        self.assertEqual(
            small, large,
            f"plugin state cost scales with item count ({small} for 5, {large} for 40)",
        )

    def test_cost_stays_under_the_ceiling(self):
        self.assertLessEqual(self._count(40, "pceiling"), STATE_QUERY_CEILING)
