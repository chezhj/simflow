"""Tests for POST /api/plugin/state/ (plugin_views.plugin_state)."""

# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring

import json
from datetime import datetime, timezone

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from checklist.models import (
    Attribute,
    CheckItem,
    FlightItemState,
    FlightSession,
    FlightSessionAttribute,
    Procedure,
    generate_api_key,
)
from checklist.tests.testFactories import AttributeFactory, CheckItemFactory, SOPFactory

User = get_user_model()
URL = reverse("checklist:api_plugin_state")

RULE_PARKING_BRAKE_ON = {
    "dataref": "sim/cockpit/switches/parking_brake",
    "op": "eq",
    "value": 1,
}
RULE_PARKING_BRAKE_OFF = {
    "dataref": "sim/cockpit/switches/parking_brake",
    "op": "eq",
    "value": 0,
}


def _post(client, body, key=None):
    kwargs = {"content_type": "application/json"}
    if key is not None:
        kwargs["HTTP_AUTHORIZATION"] = f"Bearer {key}"
    return client.post(URL, data=json.dumps(body), **kwargs)


class _Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="pilot", password="pw")
        self.profile = self.user.profile
        raw, hashed, prefix = generate_api_key()
        self.raw_key = raw
        self.profile.api_key_hash = hashed
        self.profile.api_key_prefix = prefix
        self.profile.save()

        self.sop = SOPFactory()
        self.procedure = Procedure.objects.create(
            title="Before Start", step=1, slug="before-start", sop=self.sop
        )
        self.session = FlightSession.objects.create(
            user_profile=self.profile, active_phase="before-start", is_active=True
        )

    def _valid_body(self, datarefs=None):
        return {"session_id": self.session.pk, "datarefs": datarefs or {}}


class TestPluginStateAuth(_Base):

    def test_missing_auth_returns_401(self):
        resp = _post(self.client, self._valid_body())
        self.assertEqual(resp.status_code, 401)

    def test_wrong_key_returns_401(self):
        resp = _post(self.client, self._valid_body(), key="fvw_wrongkey")
        self.assertEqual(resp.status_code, 401)

    def test_get_returns_405(self):
        resp = self.client.get(URL, HTTP_AUTHORIZATION=f"Bearer {self.raw_key}")
        self.assertEqual(resp.status_code, 405)


class TestPluginStateBodyValidation(_Base):

    def test_invalid_json_returns_400(self):
        resp = self.client.post(
            URL,
            data="not json",
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw_key}",
        )
        self.assertEqual(resp.status_code, 400)

    def test_missing_session_id_returns_400(self):
        resp = _post(self.client, {"datarefs": {}}, key=self.raw_key)
        self.assertEqual(resp.status_code, 400)

    def test_non_integer_session_id_returns_400(self):
        resp = _post(self.client, {"session_id": "abc", "datarefs": {}}, key=self.raw_key)
        self.assertEqual(resp.status_code, 400)

    def test_missing_datarefs_returns_400(self):
        resp = _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        self.assertEqual(resp.status_code, 400)

    def test_non_dict_datarefs_returns_400(self):
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": [1, 2]}, key=self.raw_key)
        self.assertEqual(resp.status_code, 400)


class TestPluginStateSessionLookup(_Base):

    def test_unknown_session_id_returns_404(self):
        resp = _post(self.client, {"session_id": 99999, "datarefs": {}}, key=self.raw_key)
        self.assertEqual(resp.status_code, 404)

    def test_inactive_session_returns_404(self):
        self.session.is_active = False
        self.session.save()
        resp = _post(self.client, self._valid_body(), key=self.raw_key)
        self.assertEqual(resp.status_code, 404)

    def test_session_belonging_to_other_user_returns_404(self):
        other = User.objects.create_user(username="other", password="pw")
        other_session = FlightSession.objects.create(
            user_profile=other.profile, active_phase="before-start", is_active=True
        )
        resp = _post(
            self.client,
            {"session_id": other_session.pk, "datarefs": {}},
            key=self.raw_key,
        )
        self.assertEqual(resp.status_code, 404)


class TestPluginStateHappyPath(_Base):

    def test_returns_ok_with_empty_checked_and_watch_when_no_rules(self):
        CheckItemFactory(procedure=self.procedure, step=1)
        resp = _post(self.client, self._valid_body(), key=self.raw_key)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["checked"], [])
        self.assertEqual(data["watch"], [])

    def test_updates_last_plugin_contact(self):
        before = datetime.now(tz=timezone.utc)
        _post(self.client, self._valid_body(), key=self.raw_key)
        self.session.refresh_from_db()
        self.assertIsNotNone(self.session.last_plugin_contact)
        self.assertGreaterEqual(self.session.last_plugin_contact, before)

    def test_updates_last_plugin_contact_even_with_empty_active_phase(self):
        self.session.active_phase = ""
        self.session.save()
        before = datetime.now(tz=timezone.utc)
        _post(self.client, self._valid_body(), key=self.raw_key)
        self.session.refresh_from_db()
        self.assertGreaterEqual(self.session.last_plugin_contact, before)

    def test_empty_active_phase_returns_empty_watch(self):
        self.session.active_phase = ""
        self.session.save()
        resp = _post(self.client, self._valid_body(), key=self.raw_key)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["watch"], [])

    def test_unknown_active_phase_slug_returns_empty_watch(self):
        self.session.active_phase = "no-such-phase"
        self.session.save()
        resp = _post(self.client, self._valid_body(), key=self.raw_key)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["watch"], [])


class TestPluginStateRuleEvaluation(_Base):

    def test_matching_rule_creates_auto_checked_state(self):
        item = CheckItemFactory(
            procedure=self.procedure, step=1, auto_check_rule=RULE_PARKING_BRAKE_ON
        )
        datarefs = {"sim/cockpit/switches/parking_brake": 1}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(item.pk, resp.json()["checked"])
        state = FlightItemState.objects.get(flight_session=self.session, checklist_item=item)
        self.assertEqual(state.status, "checked")
        self.assertEqual(state.source, "auto")
        self.assertIsNotNone(state.checked_at)

    def test_non_matching_rule_does_not_check_item(self):
        item = CheckItemFactory(
            procedure=self.procedure, step=1, auto_check_rule=RULE_PARKING_BRAKE_ON
        )
        datarefs = {"sim/cockpit/switches/parking_brake": 0}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(item.pk, resp.json()["checked"])
        self.assertFalse(
            FlightItemState.objects.filter(flight_session=self.session, checklist_item=item).exists()
        )

    def test_already_checked_item_not_in_newly_checked(self):
        item = CheckItemFactory(
            procedure=self.procedure, step=1, auto_check_rule=RULE_PARKING_BRAKE_ON
        )
        FlightItemState.objects.create(
            flight_session=self.session,
            checklist_item=item,
            status="checked",
            source="manual",
            checked_at=datetime.now(tz=timezone.utc),
        )
        datarefs = {"sim/cockpit/switches/parking_brake": 1}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(item.pk, resp.json()["checked"])

    def test_already_checked_item_still_appears_in_watch(self):
        item = CheckItemFactory(
            procedure=self.procedure, step=1, auto_check_rule=RULE_PARKING_BRAKE_ON
        )
        FlightItemState.objects.create(
            flight_session=self.session,
            checklist_item=item,
            status="checked",
            source="manual",
            checked_at=datetime.now(tz=timezone.utc),
        )
        resp = _post(self.client, self._valid_body(), key=self.raw_key)
        self.assertIn("sim/cockpit/switches/parking_brake", resp.json()["watch"])

    def test_missing_dataref_in_state_does_not_check_item(self):
        item = CheckItemFactory(
            procedure=self.procedure, step=1, auto_check_rule=RULE_PARKING_BRAKE_ON
        )
        resp = _post(self.client, self._valid_body(), key=self.raw_key)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(item.pk, resp.json()["checked"])

    def test_item_without_rule_not_in_watch(self):
        CheckItemFactory(procedure=self.procedure, step=1)
        resp = _post(self.client, self._valid_body(), key=self.raw_key)
        self.assertEqual(resp.json()["watch"], [])

    def test_watch_list_deduplicates_shared_datarefs(self):
        rule = RULE_PARKING_BRAKE_ON
        CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=rule)
        CheckItemFactory(procedure=self.procedure, step=2, auto_check_rule=rule)
        resp = _post(self.client, self._valid_body(), key=self.raw_key)
        watch = resp.json()["watch"]
        self.assertEqual(watch.count("sim/cockpit/switches/parking_brake"), 1)

    def test_compound_all_rule_fires_when_all_conditions_met(self):
        rule = {
            "all": [
                {"dataref": "sim/parking_brake", "op": "eq", "value": 1},
                {"dataref": "sim/throttle", "op": "lte", "value": 0.05},
            ]
        }
        item = CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=rule)
        datarefs = {"sim/parking_brake": 1, "sim/throttle": 0.0}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertIn(item.pk, resp.json()["checked"])

    def test_compound_all_rule_does_not_fire_when_one_condition_fails(self):
        rule = {
            "all": [
                {"dataref": "sim/parking_brake", "op": "eq", "value": 1},
                {"dataref": "sim/throttle", "op": "lte", "value": 0.05},
            ]
        }
        item = CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=rule)
        datarefs = {"sim/parking_brake": 1, "sim/throttle": 0.5}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertNotIn(item.pk, resp.json()["checked"])

    def test_compound_any_rule_fires_when_one_condition_met(self):
        rule = {
            "any": [
                {"dataref": "sim/parking_brake", "op": "eq", "value": 1},
                {"dataref": "sim/throttle", "op": "lte", "value": 0.05},
            ]
        }
        item = CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=rule)
        datarefs = {"sim/parking_brake": 0, "sim/throttle": 0.0}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertIn(item.pk, resp.json()["checked"])

    def test_fmc_line_contains_fires_when_substring_present(self):
        rule = {"fmc_line": "laminar/B738/fmc1/Line02_L", "contains": "EHAM"}
        item = CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=rule)
        datarefs = {"laminar/B738/fmc1/Line02_L": "EHAM"}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertIn(item.pk, resp.json()["checked"])

    def test_fmc_line_contains_does_not_fire_when_substring_absent(self):
        rule = {"fmc_line": "laminar/B738/fmc1/Line02_L", "contains": "EHAM"}
        item = CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=rule)
        datarefs = {"laminar/B738/fmc1/Line02_L": "----"}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertNotIn(item.pk, resp.json()["checked"])

    def test_fmc_line_not_contains_fires_when_substring_absent(self):
        rule = {"fmc_line": "laminar/B738/fmc1/Line02_L", "not_contains": "----"}
        item = CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=rule)
        datarefs = {"laminar/B738/fmc1/Line02_L": "EHAM"}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertIn(item.pk, resp.json()["checked"])

    def test_fmc_line_not_contains_does_not_fire_when_substring_present(self):
        rule = {"fmc_line": "laminar/B738/fmc1/Line02_L", "not_contains": "----"}
        item = CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=rule)
        datarefs = {"laminar/B738/fmc1/Line02_L": "----"}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertNotIn(item.pk, resp.json()["checked"])

    def test_fmc_line_dataref_appears_in_watch(self):
        rule = {"fmc_line": "laminar/B738/fmc1/Line02_L", "not_contains": "----"}
        CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=rule)
        resp = _post(self.client, self._valid_body(), key=self.raw_key)
        self.assertIn("laminar/B738/fmc1/Line02_L", resp.json()["watch"])

    def test_fmc_line_missing_from_state_does_not_check_item(self):
        rule = {"fmc_line": "laminar/B738/fmc1/Line02_L", "not_contains": "----"}
        item = CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=rule)
        resp = _post(self.client, self._valid_body(), key=self.raw_key)
        self.assertNotIn(item.pk, resp.json()["checked"])

    def test_fmc_line_tail_not_contains_fires_when_suffix_clean(self):
        """tail: 8 + not_contains: '----' fires when last 8 chars have no dashes."""
        path = "laminar/B738/fmc1/Line02_L"
        rule = {"fmc_line": path, "tail": 8, "not_contains": "----"}
        item = CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=rule)
        # Full line has dashes at the start but clean suffix
        datarefs = {path: "----EHAM    "}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertIn(item.pk, resp.json()["checked"])

    def test_fmc_line_tail_not_contains_does_not_fire_when_suffix_dashed(self):
        """tail: 8 checks only the last 8 chars — dashes in suffix block firing."""
        path = "laminar/B738/fmc1/Line02_L"
        rule = {"fmc_line": path, "tail": 8, "not_contains": "----"}
        item = CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=rule)
        datarefs = {path: "EHAM----"}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertNotIn(item.pk, resp.json()["checked"])

    def test_fmc_line_head_not_contains_fires_when_prefix_clean(self):
        """head: 8 + not_contains: '----' fires when first 8 chars have no dashes."""
        path = "laminar/B738/fmc1/Line02_L"
        rule = {"fmc_line": path, "head": 8, "not_contains": "----"}
        item = CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=rule)
        datarefs = {path: "EHAM    ----"}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertIn(item.pk, resp.json()["checked"])

    def test_fmc_line_head_not_contains_does_not_fire_when_prefix_dashed(self):
        """head: 8 checks only the first 8 chars — dashes in prefix block firing."""
        path = "laminar/B738/fmc1/Line02_L"
        rule = {"fmc_line": path, "head": 8, "not_contains": "----"}
        item = CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=rule)
        datarefs = {path: "----EHAM"}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertNotIn(item.pk, resp.json()["checked"])

    def test_fmc_line_count_gte_fires_when_substring_appears_enough_times(self):
        """count_gte: 2 fires when '<SEL>' appears at least twice."""
        path = "laminar/B738/fmc1/Line01_L"
        rule = {"fmc_line": path, "contains": "<SEL>", "count_gte": 2}
        item = CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=rule)
        datarefs = {path: "<SEL>  ILS28R  <SEL>"}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertIn(item.pk, resp.json()["checked"])

    def test_fmc_line_count_gte_does_not_fire_when_too_few_occurrences(self):
        """count_gte: 2 does not fire when '<SEL>' appears only once."""
        path = "laminar/B738/fmc1/Line01_L"
        rule = {"fmc_line": path, "contains": "<SEL>", "count_gte": 2}
        item = CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=rule)
        datarefs = {path: "<SEL>  ILS28R"}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertNotIn(item.pk, resp.json()["checked"])


class TestPluginStateAttributeFiltering(_Base):

    def test_item_with_inactive_attribute_not_auto_checked(self):
        attr = AttributeFactory()
        item = CheckItemFactory(
            procedure=self.procedure, step=1,
            attributes=[attr], auto_check_rule=RULE_PARKING_BRAKE_ON
        )
        # No FlightSessionAttribute row → attribute not active
        datarefs = {"sim/cockpit/switches/parking_brake": 1}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertNotIn(item.pk, resp.json()["checked"])

    def test_item_with_active_attribute_is_auto_checked(self):
        attr = AttributeFactory()
        item = CheckItemFactory(
            procedure=self.procedure, step=1,
            attributes=[attr], auto_check_rule=RULE_PARKING_BRAKE_ON
        )
        FlightSessionAttribute.objects.create(
            flight_session=self.session, attribute=attr, is_active=True
        )
        datarefs = {"sim/cockpit/switches/parking_brake": 1}
        resp = _post(self.client, {"session_id": self.session.pk, "datarefs": datarefs}, key=self.raw_key)
        self.assertIn(item.pk, resp.json()["checked"])

    def test_item_with_inactive_attribute_not_in_watch(self):
        attr = AttributeFactory()
        CheckItemFactory(
            procedure=self.procedure, step=1,
            attributes=[attr], auto_check_rule=RULE_PARKING_BRAKE_ON
        )
        resp = _post(self.client, self._valid_body(), key=self.raw_key)
        self.assertEqual(resp.json()["watch"], [])


class TestPluginStateIdleAndShowRule(_Base):
    """Watch list behaviour for idle page datarefs and show_rule procedures."""

    def setUp(self):
        super().setUp()
        from checklist.models import IdleDataref
        self.idle_dr = IdleDataref.objects.create(
            label="TestAlt", dataref_path="sim/test/altitude_ft", unit="ft", order=99,
        )
        self.cond_proc = Procedure.objects.create(
            title="Go Around Test", step=999, slug="go-around-watch-test",
            show_rule={"dataref": "sim/test/go_around_watch", "op": "eq", "value": 1},
            sop=self.sop,
        )

    def tearDown(self):
        self.idle_dr.delete()
        self.cond_proc.delete()

    def test_idle_dataref_always_in_watch(self):
        resp = _post(self.client, self._valid_body(), key=self.raw_key)
        self.assertIn("sim/test/altitude_ft", resp.json()["watch"])

    def test_show_rule_dataref_in_watch_when_gate_cleared(self):
        """show_rule datarefs appear when the active procedure has no blocking gate."""
        # No items in the procedure → gate_item is None → idle → show_rule watched
        resp = _post(self.client, self._valid_body(), key=self.raw_key)
        self.assertIn("sim/test/go_around_watch", resp.json()["watch"])

    def test_show_rule_dataref_in_watch_when_gate_active(self):
        """show_rule datarefs are always watched regardless of gate state."""
        item = CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=RULE_PARKING_BRAKE_ON)
        resp = _post(self.client, self._valid_body(), key=self.raw_key)
        watch = resp.json()["watch"]
        self.assertIn(RULE_PARKING_BRAKE_ON["dataref"], watch)
        self.assertIn("sim/test/go_around_watch", watch)
        item.delete()


class TestPluginStateWarnItems(_Base):
    """
    Warn items are gated by attr 3 (Informational) which is absent from the session.
    They are hidden by shouldshow() but included via should_warn(), flowing through
    the same gate loop as regular mandatory items.
    """

    def setUp(self):
        super().setUp()
        # attr 3 exists in DB but is NOT added to the session's FlightSessionAttribute
        # → active_attr_ids is empty → warn items have should_warn() = True
        self.info_attr = Attribute.objects.create(
            pk=CheckItem._INFO_ATTR, title="Informational Items", order=99
        )

    def _make_warn_item(self, step, rule):
        item = CheckItemFactory(procedure=self.procedure, step=step, auto_check_rule=rule)
        item.attributes.add(self.info_attr)
        return item

    def test_warn_item_passing_rule_is_auto_checked(self):
        """A warn item whose rule passes is auto-checked like any mandatory item."""
        item = self._make_warn_item(step=1, rule=RULE_PARKING_BRAKE_ON)
        datarefs = {RULE_PARKING_BRAKE_ON["dataref"]: 1}  # rule passes
        resp = _post(self.client, self._valid_body(datarefs=datarefs), key=self.raw_key)
        self.assertIn(item.pk, resp.json()["checked"])

    def test_warn_item_failing_rule_not_in_checked(self):
        """A warn item whose rule fails is not auto-checked."""
        item = self._make_warn_item(step=1, rule=RULE_PARKING_BRAKE_ON)
        datarefs = {RULE_PARKING_BRAKE_ON["dataref"]: 0}  # rule fails (expects 1)
        resp = _post(self.client, self._valid_body(datarefs=datarefs), key=self.raw_key)
        self.assertNotIn(item.pk, resp.json()["checked"])

    def test_warn_item_failing_rule_blocks_subsequent_item(self):
        """A failing warn item acts as a gate: items after it are not auto-checked."""
        warn_item = self._make_warn_item(step=1, rule=RULE_PARKING_BRAKE_ON)
        # Mandatory item after warn item (no attributes → always shown, not a warn item)
        subsequent = CheckItemFactory(
            procedure=self.procedure, step=2, auto_check_rule=RULE_PARKING_BRAKE_OFF
        )
        # parking_brake = 0: warn_item fails (expects 1); subsequent would pass (expects 0)
        datarefs = {RULE_PARKING_BRAKE_ON["dataref"]: 0}
        resp = _post(self.client, self._valid_body(datarefs=datarefs), key=self.raw_key)
        checked = resp.json()["checked"]
        self.assertNotIn(warn_item.pk, checked)
        self.assertNotIn(subsequent.pk, checked)
