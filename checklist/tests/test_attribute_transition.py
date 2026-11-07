"""Tests for /api/attribute-transition/ (live_rule prompts)."""

# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring

from django.test import TestCase
from django.urls import reverse

from checklist.models import Attribute, FlightSession, FlightSessionAttribute
from checklist.tests.testFactories import AttributeFactory

TEMP = "sim/weather/temperature_sealevel_c"
AGL = "sim/flightmodel/position/y_agl"

BELOW_ZERO = {"all": [{"dataref": TEMP, "op": "lt", "value": 0}]}
ON_GROUND = {"all": [{"dataref": AGL, "op": "lt", "value": 152}]}


class TransitionTestBase(TestCase):
    def setUp(self):
        self.session = FlightSession.objects.create()
        client_session = self.client.session
        client_session["flight_session_key"] = self.session.session_key
        client_session.save()
        self.attr = AttributeFactory(
            title="Anti-Ice Normal",
            live_rule=BELOW_ZERO,
            live_rule_gate=ON_GROUND,
            live_rule_mode="prompt_on_change",
            prompt_message="OAT below 0",
        )

    def set_datarefs(self, datarefs):
        FlightSession.objects.filter(pk=self.session.pk).update(last_datarefs=datarefs)
        self.session.refresh_from_db()

    def set_active(self, active):
        FlightSessionAttribute.objects.update_or_create(
            flight_session=self.session,
            attribute=self.attr,
            defaults={"is_active": active},
        )

    def get(self):
        response = self.client.get(reverse("checklist:api_attribute_transition"))
        self.assertEqual(response.status_code, 200)
        return response.json()


class TestLiveRuleGate(TransitionTestBase):
    def test_on_ground_temperature_change_prompts_to_activate(self):
        self.set_datarefs({TEMP: -3, AGL: 0})
        prompts = self.get()["prompts"]
        self.assertEqual([p["attr_id"] for p in prompts], [self.attr.pk])
        self.assertTrue(prompts[0]["suggested_active"])

    def test_airborne_active_attribute_is_not_prompted_to_deactivate(self):
        # The #46 regression: y_agl used to sit inside live_rule, so in flight
        # the rule went false and an active attribute was offered "Deactivate".
        self.set_active(True)
        self.set_datarefs({TEMP: -3, AGL: 3500})
        self.assertEqual(self.get()["prompts"], [])

    def test_airborne_temperature_change_does_not_prompt(self):
        self.set_datarefs({TEMP: -3, AGL: 3500})
        self.assertEqual(self.get()["prompts"], [])

    def test_no_sim_data_does_not_prompt_active_attribute(self):
        self.set_active(True)
        self.set_datarefs({})
        self.assertEqual(self.get()["prompts"], [])

    def test_missing_gate_dataref_does_not_prompt(self):
        self.set_datarefs({TEMP: -3})
        self.assertEqual(self.get()["prompts"], [])

    def test_attribute_without_gate_still_prompts(self):
        Attribute.objects.filter(pk=self.attr.pk).update(live_rule_gate=None)
        self.set_datarefs({TEMP: -3})
        self.assertEqual(len(self.get()["prompts"]), 1)

    def test_on_ground_warm_active_attribute_prompts_to_deactivate(self):
        self.set_active(True)
        self.set_datarefs({TEMP: 5, AGL: 0})
        prompts = self.get()["prompts"]
        self.assertEqual(len(prompts), 1)
        self.assertFalse(prompts[0]["suggested_active"])

    def test_pilot_override_silences_prompt(self):
        FlightSession.objects.filter(pk=self.session.pk).update(
            pilot_overrides={str(self.attr.pk): False}
        )
        self.set_datarefs({TEMP: -3, AGL: 0})
        self.assertEqual(self.get()["prompts"], [])
