"""Tests for /api/attribute-transition/ (live_rule prompts)."""

# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring

import json

from django.test import TestCase
from django.urls import reverse

from checklist.models import Attribute, FlightSession, FlightSessionAttribute
from checklist.tests.testFactories import AttributeFactory, ProcedureFactory

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


class TestPromptMessage(TransitionTestBase):
    def prompt(self):
        return self.get()["prompts"][0]

    def test_observed_value_is_filled_into_the_message(self):
        Attribute.objects.filter(pk=self.attr.pk).update(
            prompt_message="Temperature is now {temperature_sealevel_c:.0f} °C"
        )
        self.set_datarefs({TEMP: -2.4, AGL: 0})
        self.assertEqual(self.prompt()["prompt_message"], "Temperature is now -2 °C")

    def test_plain_message_is_returned_unchanged(self):
        self.set_datarefs({TEMP: -3, AGL: 0})
        self.assertEqual(self.prompt()["prompt_message"], "OAT below 0")

    def test_malformed_template_falls_back_to_the_raw_message(self):
        Attribute.objects.filter(pk=self.attr.pk).update(
            prompt_message="Temperature {temperature_sealevel_c:.0f"
        )
        self.set_datarefs({TEMP: -3, AGL: 0})
        self.assertEqual(self.prompt()["prompt_message"], "Temperature {temperature_sealevel_c:.0f")

    def test_unknown_placeholder_stays_readable(self):
        Attribute.objects.filter(pk=self.attr.pk).update(prompt_message="Wind {wind}")
        self.set_datarefs({TEMP: -3, AGL: 0})
        self.assertEqual(self.prompt()["prompt_message"], "Wind {wind}")


class TestDecisions(TransitionTestBase):
    def post(self, url, accepted):
        return self.client.post(
            url,
            data=json.dumps({"decisions": [{"attr_id": self.attr.pk, "accepted": accepted}]}),
            content_type="application/json",
        )

    def setUp(self):
        super().setUp()
        self.set_datarefs({TEMP: -3, AGL: 0})
        self.urls = {
            "procedure": reverse("checklist:detail", args=[ProcedureFactory(slug="pre").slug]),
            "idle": reverse("checklist:idle"),
        }

    def test_match_activates_the_attribute_on_a_procedure_page(self):
        self.assertEqual(self.post(self.urls["procedure"], True).status_code, 200)
        fsa = FlightSessionAttribute.objects.get(flight_session=self.session, attribute=self.attr)
        self.assertTrue(fsa.is_active)
        self.assertEqual(fsa.source, "live_rule")

    def test_match_on_the_idle_page_is_recorded_too(self):
        # Idle used to ignore POST, so the decision was lost and the dialog
        # came back on every later navigation.
        self.assertEqual(self.post(self.urls["idle"], True).status_code, 200)
        self.assertTrue(
            FlightSessionAttribute.objects.get(
                flight_session=self.session, attribute=self.attr
            ).is_active
        )

    def test_keep_current_leaves_the_attribute_and_silences_the_prompt(self):
        self.assertEqual(self.post(self.urls["idle"], False).status_code, 200)
        self.assertFalse(
            FlightSessionAttribute.objects.filter(
                flight_session=self.session, attribute=self.attr, is_active=True
            ).exists()
        )
        self.assertEqual(self.get()["prompts"], [])

    def test_match_while_airborne_changes_nothing(self):
        self.set_datarefs({TEMP: -3, AGL: 3500})
        self.post(self.urls["procedure"], True)
        self.assertFalse(
            FlightSessionAttribute.objects.filter(
                flight_session=self.session, attribute=self.attr
            ).exists()
        )
