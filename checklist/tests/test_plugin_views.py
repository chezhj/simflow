"""Tests for POST /api/plugin/check-next/ (plugin_views.py)."""

# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring

from datetime import datetime, timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.test import TestCase
from django.urls import reverse

from checklist.models import (
    api_key_digest,
    Attribute,
    FlightItemState,
    FlightSession,
    FlightSessionAttribute,
    Procedure,
    generate_api_key,
)
from checklist.tests.testFactories import AttributeFactory, CheckItemFactory, SOPFactory

User = get_user_model()
URL = reverse("checklist:api_plugin_check_next")


def _post(client, key=None):
    """POST to check-next, optionally with a Bearer token."""
    kwargs = {}
    if key is not None:
        kwargs["HTTP_AUTHORIZATION"] = f"Bearer {key}"
    return client.post(URL, **kwargs)


class TestPluginCheckNextAuth(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="pilot", password="pw")
        self.profile = self.user.profile
        self.raw_key = self.profile.set_api_key()

        sop = SOPFactory()
        procedure = Procedure.objects.create(title="Before Start", step=1, slug="before-start", sop=sop)
        CheckItemFactory(procedure=procedure, step=1)
        FlightSession.objects.create(
            user_profile=self.profile, active_phase="before-start", is_active=True
        )

    def test_missing_auth_header_returns_401(self):
        self.assertEqual(_post(self.client).status_code, 401)

    def test_wrong_key_returns_401(self):
        self.assertEqual(_post(self.client, key="fvw_wrongkey").status_code, 401)

    def test_get_returns_405(self):
        response = self.client.get(URL, HTTP_AUTHORIZATION=f"Bearer {self.raw_key}")
        self.assertEqual(response.status_code, 405)

    def test_a_current_key_is_resolved_without_hashing_at_all(self):
        """
        The whole point of the SHA-256 column. check_password runs a
        deliberately slow KDF — ~265 ms on the production host — and the plugin
        POSTs at 1-2 Hz per flying pilot, so paying it per request capped the
        server at a couple of concurrent pilots per core. A current key must
        cost zero slow hashes, no matter how many accounts exist.
        """
        for n in range(20):
            User.objects.create_user(username=f"other{n}", password="pw").profile.set_api_key()

        with patch("checklist.plugin_views.check_password") as spy:
            response = _post(self.client, key=self.raw_key)

        self.assertNotEqual(response.status_code, 401)
        spy.assert_not_called()

    def test_unknown_key_is_rejected_without_hashing(self):
        with patch("checklist.plugin_views.check_password") as spy:
            response = _post(self.client, key="fvw_zzzznot-a-real-key")

        self.assertEqual(response.status_code, 401)
        spy.assert_not_called()


class TestLegacyApiKeyUpgrade(TestCase):
    """
    Keys minted before api_key_sha256 existed carry only a PBKDF2 hash. They
    must keep working — a pilot whose plugin stops authenticating after a
    deploy has no way to diagnose it — and must move onto the fast path by
    themselves, because the raw key exists only for the instant of a request.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="pilot", password="pw")
        self.profile = self.user.profile

        # A profile exactly as the previous release would have left it.
        self.raw_key, _digest, prefix = generate_api_key()
        self.profile.api_key_sha256 = None
        self.profile.api_key_hash = make_password(self.raw_key)
        self.profile.api_key_prefix = prefix
        self.profile.save()

        sop = SOPFactory()
        procedure = Procedure.objects.create(
            title="Before Start", step=1, slug="before-start", sop=sop
        )
        CheckItemFactory(procedure=procedure, step=1)
        FlightSession.objects.create(
            user_profile=self.profile, active_phase="before-start", is_active=True
        )

    def test_a_legacy_key_still_authenticates(self):
        self.assertNotEqual(_post(self.client, key=self.raw_key).status_code, 401)

    def test_first_use_writes_the_digest(self):
        _post(self.client, key=self.raw_key)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.api_key_sha256, api_key_digest(self.raw_key))

    def test_second_use_no_longer_pays_the_kdf(self):
        """Upgraded in place, so the slow path is taken once and never again."""
        _post(self.client, key=self.raw_key)  # upgrades

        with patch("checklist.plugin_views.check_password") as spy:
            response = _post(self.client, key=self.raw_key)

        self.assertNotEqual(response.status_code, 401)
        spy.assert_not_called()

    def test_the_legacy_scan_is_narrowed_by_prefix(self):
        """
        Until every key has upgraded the old column is still scanned, so it
        stays bounded: decoys that share no prefix are never hashed. The caller
        is created last so an unnarrowed scan would hash all of them first.
        """
        for n in range(20):
            other = User.objects.create_user(username=f"other{n}", password="pw")
            raw, _d, prefix = generate_api_key()
            other.profile.api_key_sha256 = None
            other.profile.api_key_hash = make_password(raw)
            other.profile.api_key_prefix = prefix
            other.profile.save()

        with patch(
            "checklist.plugin_views.check_password", wraps=check_password
        ) as spy:
            response = _post(self.client, key=self.raw_key)

        self.assertNotEqual(response.status_code, 401)
        self.assertLessEqual(spy.call_count, 2)

    def test_an_upgraded_row_is_never_matched_by_the_legacy_path_again(self):
        """
        The load-bearing invariant. Once a row has a digest, the legacy query
        excludes it (api_key_sha256=None), so whatever is left in api_key_hash
        is unreachable. Drop that condition from the filter and a superseded
        key starts authenticating again — which is why this is pinned
        separately from the hygiene below.
        """
        self.profile.api_key_sha256 = api_key_digest("fvw_some-other-key")
        self.profile.save(update_fields=["api_key_sha256"])

        with patch(
            "checklist.plugin_views.check_password", wraps=check_password
        ) as spy:
            response = _post(self.client, key=self.raw_key)

        self.assertEqual(response.status_code, 401)
        spy.assert_not_called()

    def test_regenerating_clears_the_legacy_hash(self):
        """
        Hygiene, not the mechanism: the test above shows a superseded key is
        already unreachable. Clearing the column keeps a dead credential out of
        a database that is backed up before every migrate, lets the column
        drain so it can be dropped next release, and means relaxing that filter
        cannot resurrect anything.
        """
        self.profile.set_api_key()
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.api_key_hash)


class TestPluginCheckNextSession(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="pilot", password="pw")
        self.profile = self.user.profile
        self.raw_key = self.profile.set_api_key()

        self.sop = SOPFactory()
        self.procedure = Procedure.objects.create(
            title="Before Start", step=1, slug="before-start", sop=self.sop
        )
        CheckItemFactory(procedure=self.procedure, step=1)

    def test_no_active_session_returns_404(self):
        # No FlightSession at all
        self.assertEqual(_post(self.client, self.raw_key).status_code, 404)

    def test_inactive_session_returns_404(self):
        FlightSession.objects.create(
            user_profile=self.profile, active_phase="before-start", is_active=False
        )
        self.assertEqual(_post(self.client, self.raw_key).status_code, 404)

    def test_empty_active_phase_returns_404(self):
        FlightSession.objects.create(
            user_profile=self.profile, active_phase="", is_active=True
        )
        self.assertEqual(_post(self.client, self.raw_key).status_code, 404)

    def test_unknown_active_phase_slug_returns_404(self):
        FlightSession.objects.create(
            user_profile=self.profile, active_phase="no-such-phase", is_active=True
        )
        self.assertEqual(_post(self.client, self.raw_key).status_code, 404)


class TestPluginCheckNextHappyPath(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="pilot", password="pw")
        self.profile = self.user.profile
        self.raw_key = self.profile.set_api_key()

        self.sop = SOPFactory()
        self.procedure = Procedure.objects.create(
            title="Before Start", step=1, slug="before-start", sop=self.sop
        )
        self.item1 = CheckItemFactory(procedure=self.procedure, step=1)
        self.item2 = CheckItemFactory(procedure=self.procedure, step=2)
        self.session = FlightSession.objects.create(
            user_profile=self.profile, active_phase="before-start", is_active=True
        )

    def test_checks_first_unchecked_item(self):
        response = _post(self.client, self.raw_key)
        self.assertEqual(response.status_code, 200)
        state = FlightItemState.objects.get(
            flight_session=self.session, checklist_item=self.item1
        )
        self.assertEqual(state.status, "checked")
        self.assertEqual(state.source, "manual")
        self.assertIsNotNone(state.checked_at)

    def test_response_body_contains_action_label(self):
        response = _post(self.client, self.raw_key)
        self.assertEqual(response.status_code, 200)
        self.assertIn("checked", response.json())

    def test_updates_last_plugin_contact(self):
        before = datetime.now(tz=timezone.utc)
        _post(self.client, self.raw_key)
        self.session.refresh_from_db()
        self.assertIsNotNone(self.session.last_plugin_contact)
        self.assertGreaterEqual(self.session.last_plugin_contact, before)

    def test_last_plugin_contact_updated_even_on_phase_complete(self):
        # Pre-check all items so the response is 204 — contact still updated
        for item in [self.item1, self.item2]:
            FlightItemState.objects.create(
                flight_session=self.session,
                checklist_item=item,
                status="checked",
                source="manual",
                checked_at=datetime.now(tz=timezone.utc),
            )
        before = datetime.now(tz=timezone.utc)
        response = _post(self.client, self.raw_key)
        self.assertEqual(response.status_code, 204)
        self.session.refresh_from_db()
        self.assertGreaterEqual(self.session.last_plugin_contact, before)

    def test_skips_already_checked_item_and_checks_next(self):
        FlightItemState.objects.create(
            flight_session=self.session,
            checklist_item=self.item1,
            status="checked",
            source="manual",
            checked_at=datetime.now(tz=timezone.utc),
        )
        response = _post(self.client, self.raw_key)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            FlightItemState.objects.filter(
                flight_session=self.session,
                checklist_item=self.item2,
                status="checked",
            ).exists()
        )

    def test_phase_complete_returns_204(self):
        for item in [self.item1, self.item2]:
            FlightItemState.objects.create(
                flight_session=self.session,
                checklist_item=item,
                status="checked",
                source="manual",
                checked_at=datetime.now(tz=timezone.utc),
            )
        self.assertEqual(_post(self.client, self.raw_key).status_code, 204)

    def test_pressing_twice_does_not_duplicate_row(self):
        _post(self.client, self.raw_key)
        _post(self.client, self.raw_key)
        # item1 should be checked exactly once, item2 checked once
        self.assertEqual(
            FlightItemState.objects.filter(flight_session=self.session).count(), 2
        )

    def test_skipped_item1_is_bypassed_check_next_checks_item2(self):
        # item1 is already skipped — check_next should advance past it to item2
        FlightItemState.objects.create(
            flight_session=self.session,
            checklist_item=self.item1,
            status="skipped",
            source=None,
            checked_at=None,
        )
        response = _post(self.client, self.raw_key)
        self.assertEqual(response.status_code, 200)
        # item1 stays skipped, item2 gets checked
        self.assertEqual(
            FlightItemState.objects.get(
                flight_session=self.session, checklist_item=self.item1
            ).status,
            "skipped",
        )
        self.assertTrue(
            FlightItemState.objects.filter(
                flight_session=self.session, checklist_item=self.item2, status="checked"
            ).exists()
        )


class TestPluginCheckNextAttributeFiltering(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="pilot", password="pw")
        self.profile = self.user.profile
        self.raw_key = self.profile.set_api_key()

        self.sop = SOPFactory()
        self.procedure = Procedure.objects.create(
            title="Before Start", step=1, slug="before-start", sop=self.sop
        )
        self.session = FlightSession.objects.create(
            user_profile=self.profile, active_phase="before-start", is_active=True
        )

    def test_item_requiring_inactive_attribute_is_not_checked(self):
        attr = AttributeFactory()
        # step=1 so it comes first in order
        gated_item = CheckItemFactory(procedure=self.procedure, step=1, attributes=[attr])
        mandatory_item = CheckItemFactory(procedure=self.procedure, step=2)
        # Attribute is NOT active in this session (no FlightSessionAttribute row)
        response = _post(self.client, self.raw_key)
        self.assertEqual(response.status_code, 200)
        # Gated item must NOT be checked
        self.assertFalse(
            FlightItemState.objects.filter(
                flight_session=self.session, checklist_item=gated_item
            ).exists()
        )
        # Mandatory item (no attributes) IS checked
        self.assertTrue(
            FlightItemState.objects.filter(
                flight_session=self.session, checklist_item=mandatory_item,
                status="checked",
            ).exists()
        )

    def test_item_requiring_active_attribute_is_checked(self):
        attr = AttributeFactory()
        gated_item = CheckItemFactory(procedure=self.procedure, step=1, attributes=[attr])
        # Attribute IS active in session
        FlightSessionAttribute.objects.create(
            flight_session=self.session, attribute=attr, is_active=True
        )
        response = _post(self.client, self.raw_key)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            FlightItemState.objects.filter(
                flight_session=self.session, checklist_item=gated_item,
                status="checked",
            ).exists()
        )


class TestPluginCheckNextWarnItems(TestCase):
    """
    Regression: the button must be able to check a blocking warn item.

    Warn items (gated only by attr 3, Informational) are hidden by shouldshow()
    but still gate the sequence in plugin_state/poll_view. check_next matched on
    shouldshow() alone, so it skipped a blocking warn item and checked a later
    row instead — the gate never advanced and pressing the button looked like it
    did nothing. Most visible in RequireAllVisible mode, where the gate stops at
    every visible row.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="pilot", password="pw")
        self.profile = self.user.profile
        self.raw_key = self.profile.set_api_key()

        self.sop = SOPFactory()
        self.procedure = Procedure.objects.create(
            title="Engine Start", step=1, slug="engine-start", sop=self.sop
        )
        # attr 3 is _INFO_ATTR; leaving it inactive makes the item a warn row
        self.info_attr = Attribute.objects.create(title="NoActionNeed", order=1, pk=3)
        self.session = FlightSession.objects.create(
            user_profile=self.profile,
            active_phase="engine-start",
            is_active=True,
            require_all_visible=True,
        )

    def _warn_item(self, step):
        item = CheckItemFactory(
            procedure=self.procedure,
            step=step,
            auto_check_rule={"dataref": "sim/test/starter", "op": "eq", "value": 1},
        )
        item.attributes.add(self.info_attr)
        return item

    def _checked_ids(self):
        return set(
            FlightItemState.objects.filter(
                flight_session=self.session, status="checked"
            ).values_list("checklist_item_id", flat=True)
        )

    def test_blocking_warn_item_is_checked_first(self):
        warn_item = self._warn_item(step=1)
        later_item = CheckItemFactory(procedure=self.procedure, step=2)

        self.assertEqual(_post(self.client, self.raw_key).status_code, 200)

        self.assertIn(warn_item.pk, self._checked_ids())
        self.assertNotIn(later_item.pk, self._checked_ids())

    def test_presses_walk_the_list_in_order_without_skipping(self):
        warn_item = self._warn_item(step=1)
        later_item = CheckItemFactory(procedure=self.procedure, step=2)

        _post(self.client, self.raw_key)
        _post(self.client, self.raw_key)

        self.assertEqual(self._checked_ids(), {warn_item.pk, later_item.pk})
        # Nothing left → phase complete
        self.assertEqual(_post(self.client, self.raw_key).status_code, 204)

    def test_item_gated_by_an_inactive_attribute_is_still_skipped(self):
        """Only attr 3 makes an item a warn row; other gates still hide it."""
        other = AttributeFactory()
        hidden = CheckItemFactory(procedure=self.procedure, step=1, attributes=[other])
        visible = CheckItemFactory(procedure=self.procedure, step=2)

        _post(self.client, self.raw_key)

        self.assertNotIn(hidden.pk, self._checked_ids())
        self.assertIn(visible.pk, self._checked_ids())
