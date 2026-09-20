"""Tests for POST /api/plugin/report-miss/ (plugin_views.plugin_report_miss)."""

# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

import checklist.plugin_views as plugin_views
from checklist.models import (
    FlightItemState,
    FlightSession,
    FlightSessionAttribute,
    Procedure,
    RuleMissReport,
)
from checklist.tests.testFactories import AttributeFactory, CheckItemFactory, SOPFactory

User = get_user_model()
URL = reverse("checklist:api_plugin_report_miss")

RULE_PARKING_BRAKE_ON = {
    "dataref": "sim/cockpit/switches/parking_brake",
    "op": "eq",
    "value": 1,
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
        self.raw_key = self.profile.set_api_key()

        self.sop = SOPFactory()
        self.procedure = Procedure.objects.create(
            title="Before Start", step=1, slug="before-start", sop=self.sop
        )
        self.session = FlightSession.objects.create(
            user_profile=self.profile, active_phase="before-start", is_active=True
        )

    def _seed_datarefs(self, datarefs=None):
        FlightSession.objects.filter(pk=self.session.pk).update(last_datarefs=datarefs or {})

    def tearDown(self):
        pass


class TestReportMissAuth(_Base):

    def setUp(self):
        super().setUp()
        CheckItemFactory(procedure=self.procedure, step=1)
        self._seed_datarefs()

    def test_missing_auth_returns_401(self):
        resp = _post(self.client, {"session_id": self.session.pk})
        self.assertEqual(resp.status_code, 401)

    def test_wrong_key_returns_401(self):
        resp = _post(self.client, {"session_id": self.session.pk}, key="fvw_wrongkey")
        self.assertEqual(resp.status_code, 401)

    def test_get_returns_405(self):
        resp = self.client.get(URL, HTTP_AUTHORIZATION=f"Bearer {self.raw_key}")
        self.assertEqual(resp.status_code, 405)


class TestReportMissValidation(_Base):

    def test_missing_session_id_returns_400(self):
        self._seed_datarefs()
        resp = _post(self.client, {}, key=self.raw_key)
        self.assertEqual(resp.status_code, 400)

    def test_non_integer_session_id_returns_400(self):
        self._seed_datarefs()
        resp = _post(self.client, {"session_id": "abc"}, key=self.raw_key)
        self.assertEqual(resp.status_code, 400)

    def test_unknown_session_id_returns_404(self):
        self._seed_datarefs()
        resp = _post(self.client, {"session_id": 99999}, key=self.raw_key)
        self.assertEqual(resp.status_code, 404)

    def test_inactive_session_returns_404(self):
        self.session.is_active = False
        self.session.save()
        self._seed_datarefs()
        resp = _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        self.assertEqual(resp.status_code, 404)

    def test_no_active_phase_returns_404(self):
        self.session.active_phase = ""
        self.session.save()
        self._seed_datarefs()
        resp = _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        self.assertEqual(resp.status_code, 404)

    def test_no_cached_datarefs_returns_422(self):
        CheckItemFactory(procedure=self.procedure, step=1)
        # Do NOT seed last_datarefs — stays None, meaning "plugin never reported"
        resp = _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("detail", resp.json())

    def test_all_items_done_returns_204(self):
        item = CheckItemFactory(procedure=self.procedure, step=1)
        FlightItemState.objects.create(
            flight_session=self.session,
            checklist_item=item,
            status="checked",
            source="manual",
        )
        self._seed_datarefs()
        resp = _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(RuleMissReport.objects.count(), 0)


class TestReportMissRequiredItem(_Base):
    """First unchecked item is a required item (no attributes) with a rule."""

    def setUp(self):
        super().setUp()
        self.item = CheckItemFactory(
            procedure=self.procedure,
            step=1,
            auto_check_rule=RULE_PARKING_BRAKE_ON,
        )
        self._seed_datarefs({"sim/cockpit/switches/parking_brake": 0})

    def test_returns_200_with_report_id(self):
        resp = _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("report_id", data)
        self.assertEqual(data["status"], "ok")

    def test_creates_rule_miss_report(self):
        _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        self.assertEqual(RuleMissReport.objects.count(), 1)

    def test_report_fields_are_correct(self):
        _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        report = RuleMissReport.objects.get()

        self.assertEqual(report.flight_session, self.session)
        self.assertEqual(report.reported_item, self.item)
        self.assertEqual(report.reported_item_label, self.item.item)
        self.assertEqual(report.active_phase, "before-start")
        self.assertEqual(report.rule, RULE_PARKING_BRAKE_ON)
        self.assertEqual(report.conditions_total, 1)
        self.assertEqual(report.conditions_failing, 1)  # brake is 0, rule needs 1

    def test_leaf_evaluations_contain_condition_result(self):
        _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        report = RuleMissReport.objects.get()
        self.assertEqual(len(report.leaf_evaluations), 1)
        leaf = report.leaf_evaluations[0]
        self.assertFalse(leaf["pass"])
        self.assertEqual(leaf["actual"], 0)
        self.assertEqual(leaf["required"], 1)

    def test_second_press_creates_second_report(self):
        _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        self.assertEqual(RuleMissReport.objects.count(), 2)


class TestReportMissItemWithAttributes(_Base):
    """First unchecked visible item has attributes (optional-style) — still reported."""

    def setUp(self):
        super().setUp()
        self.attr = AttributeFactory()
        self.item = CheckItemFactory(
            procedure=self.procedure,
            step=1,
            auto_check_rule=RULE_PARKING_BRAKE_ON,
            attributes=[self.attr],
        )
        FlightSessionAttribute.objects.create(
            flight_session=self.session,
            attribute=self.attr,
            is_active=True,
        )
        self._seed_datarefs({"sim/cockpit/switches/parking_brake": 0})

    def test_item_with_active_attribute_is_reported(self):
        resp = _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        self.assertEqual(resp.status_code, 200)
        report = RuleMissReport.objects.get()
        self.assertEqual(report.reported_item, self.item)

    def test_item_with_inactive_attribute_is_not_visible_returns_204(self):
        FlightSessionAttribute.objects.filter(
            flight_session=self.session, attribute=self.attr
        ).update(is_active=False)
        resp = _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        # Item doesn't pass shouldshow() → no visible unchecked items → 204
        self.assertEqual(resp.status_code, 204)


class TestReportMissNullRule(_Base):
    """Item with null auto_check_rule is still reported (leaf_evaluations=[])."""

    def setUp(self):
        super().setUp()
        self.item = CheckItemFactory(
            procedure=self.procedure, step=1, auto_check_rule=None
        )
        self._seed_datarefs()

    def test_returns_200(self):
        resp = _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        self.assertEqual(resp.status_code, 200)

    def test_report_created_with_empty_leaf_evaluations(self):
        _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        report = RuleMissReport.objects.get()
        self.assertEqual(report.leaf_evaluations, [])
        self.assertIsNone(report.rule)
        self.assertEqual(report.conditions_total, 0)
        self.assertEqual(report.conditions_failing, 0)


class TestReportMissFirstUnchecked(_Base):
    """Reports the first unchecked item, skipping done ones."""

    def setUp(self):
        super().setUp()
        self.item1 = CheckItemFactory(
            procedure=self.procedure, step=1, auto_check_rule=RULE_PARKING_BRAKE_ON
        )
        self.item2 = CheckItemFactory(
            procedure=self.procedure, step=2, auto_check_rule=None
        )
        self._seed_datarefs()

    def test_reports_item1_when_nothing_done(self):
        _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        report = RuleMissReport.objects.get()
        self.assertEqual(report.reported_item, self.item1)

    def test_reports_item2_when_item1_is_done(self):
        FlightItemState.objects.create(
            flight_session=self.session,
            checklist_item=self.item1,
            status="checked",
            source="manual",
        )
        _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        report = RuleMissReport.objects.get()
        self.assertEqual(report.reported_item, self.item2)

    def test_204_when_both_done(self):
        for item in [self.item1, self.item2]:
            FlightItemState.objects.create(
                flight_session=self.session,
                checklist_item=item,
                status="checked",
                source="manual",
            )
        resp = _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        self.assertEqual(resp.status_code, 204)


class TestReportMissPluginVersion(_Base):
    """plugin_version is captured from the X-Plugin-Version header."""

    def setUp(self):
        super().setUp()
        CheckItemFactory(procedure=self.procedure, step=1, auto_check_rule=None)
        self._seed_datarefs()

    def test_plugin_version_stored_when_header_present(self):
        self.client.post(
            URL,
            data=json.dumps({"session_id": self.session.pk}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw_key}",
            HTTP_X_PLUGIN_VERSION="0.6.0",
        )
        report = RuleMissReport.objects.get()
        self.assertEqual(report.plugin_version, "0.6.0")

    def test_plugin_version_blank_when_header_absent(self):
        _post(self.client, {"session_id": self.session.pk}, key=self.raw_key)
        report = RuleMissReport.objects.get()
        self.assertEqual(report.plugin_version, "")
