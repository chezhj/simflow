"""
Checks on the shipped checklist content (checklist/fixtures/checklist_content.json).

These read the fixture itself, so a content edit that breaks an intended
behaviour fails here instead of in the cockpit.
"""

# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring

import json
from pathlib import Path

from django.test import SimpleTestCase

from checklist.rules import evaluate_rule

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "checklist_content.json"
CONTENT = json.loads(FIXTURE.read_text(encoding="utf-8"))

MOISTURE = "sim/weather/aircraft/precipitation_on_aircraft_ratio"
ENG1 = "laminar/B738/ice/eng1_heat_pos"
ENG2 = "laminar/B738/ice/eng2_heat_pos"
WING = "laminar/B738/ice/wing_heat_pos"

ANTI_ICE_NORMAL = 7
ZERO_TO_TEN = 9
OPTIONAL = 4

PATH_KEYS = {"dataref", "ref", "fmc_line", "crs", "ref_lat", "ref_lon"}
RULE_FIELDS = ("auto_check_rule", "show_rule", "live_rule", "live_rule_gate")


def _item(pk):
    return next(
        o["fields"] for o in CONTENT
        if o["model"] == "checklist.checkitem" and o["pk"] == pk
    )


def _paths(node, found):
    """Collect every (key, value) in a rule whose key names a dataref path."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in PATH_KEYS and isinstance(value, str):
                found.append(value)
            _paths(value, found)
    elif isinstance(node, list):
        for value in node:
            _paths(value, found)


class TestDatarefPaths(SimpleTestCase):
    def test_no_dataref_path_has_stray_whitespace(self):
        # A trailing space made item 385's cold-and-wet branch unmatchable: the
        # path never appears in the plugin's snapshot, so the branch was dead.
        bad = []
        for obj in CONTENT:
            for field in RULE_FIELDS:
                rule = obj["fields"].get(field)
                if not rule:
                    continue
                found = []
                _paths(rule, found)
                bad += [
                    f"{obj['model']} pk{obj['pk']}.{field}: {p!r}"
                    for p in found if p != p.strip()
                ]
        self.assertEqual(bad, [])

    def test_engine_start_switches_cold_and_wet_branch_reads_the_real_dataref(self):
        # Item 385 (takeoff): CONT when below 10 C with visible moisture.
        rule = _item(385)["auto_check_rule"]
        state = {
            "laminar/B738/engine/starter1_pos": 2,
            "laminar/B738/engine/starter2_pos": 2,
            "sim/cockpit2/temperature/outside_air_temp_degc": 5,
            MOISTURE: 0.4,
        }
        self.assertTrue(evaluate_rule(rule, state))


class TestZeroToTenAntiIce(SimpleTestCase):
    """Before Taxi 274/275: anti-ice ON only with visible moisture."""

    def state(self, moisture=None, **switches):
        state = {ENG1: 0, ENG2: 0, WING: 0, **switches}
        if moisture is not None:
            state[MOISTURE] = moisture
        return state

    def test_setting_text_names_the_condition(self):
        for pk in (274, 275):
            self.assertEqual(_item(pk)["setting"], "ON with visible moisture")

    def test_still_zero_to_ten_items(self):
        for pk in (274, 275):
            self.assertEqual(_item(pk)["attributes"], [ZERO_TO_TEN])

    def test_dry_continues_without_the_switches(self):
        for pk in (274, 275):
            rule = _item(pk)["auto_check_rule"]
            self.assertTrue(evaluate_rule(rule, self.state(moisture=0.0)))
            self.assertTrue(evaluate_rule(rule, self.state(moisture=0.1)))

    def test_visible_moisture_waits_for_the_switches(self):
        for pk in (274, 275):
            rule = _item(pk)["auto_check_rule"]
            self.assertFalse(evaluate_rule(rule, self.state(moisture=0.4)))

    def test_visible_moisture_with_switches_on_continues(self):
        engine = _item(274)["auto_check_rule"]
        wing = _item(275)["auto_check_rule"]
        self.assertTrue(evaluate_rule(engine, self.state(0.4, **{ENG1: 1, ENG2: 1})))
        self.assertTrue(evaluate_rule(wing, self.state(0.4, **{WING: 1})))

    def test_one_engine_on_is_not_enough_in_moisture(self):
        rule = _item(274)["auto_check_rule"]
        self.assertFalse(evaluate_rule(rule, self.state(0.4, **{ENG1: 1})))

    def test_missing_moisture_data_does_not_auto_continue(self):
        for pk in (274, 275):
            rule = _item(pk)["auto_check_rule"]
            self.assertFalse(evaluate_rule(rule, self.state(moisture=None)))


class TestAntiIceNormalAntiIce(SimpleTestCase):
    """Before Taxi 255/256: sub-zero anti-ice, no moisture restriction."""

    def test_only_anti_ice_normal_gates_them(self):
        # With [7, 9] they needed both attributes, which the temperature rules
        # make mutually exclusive, so they effectively never showed.
        for pk in (255, 256):
            self.assertEqual(_item(pk)["attributes"], [ANTI_ICE_NORMAL])

    def test_no_moisture_condition(self):
        for pk in (255, 256):
            found = []
            _paths(_item(pk)["auto_check_rule"], found)
            self.assertNotIn(MOISTURE, found)

    def test_wing_and_engine_switches_are_the_condition(self):
        self.assertTrue(evaluate_rule(_item(255)["auto_check_rule"], {WING: 1}))
        self.assertFalse(evaluate_rule(_item(255)["auto_check_rule"], {WING: 0}))
        self.assertTrue(evaluate_rule(_item(256)["auto_check_rule"], {ENG1: 1, ENG2: 1}))
        self.assertFalse(evaluate_rule(_item(256)["auto_check_rule"], {ENG1: 1, ENG2: 0}))


class TestAfterLandingShutdown(SimpleTestCase):
    """After Landing 394-396: flight directors and trim air go off (issue #56)."""

    AFTER_LANDING = 21
    WEATHER_RADAR = 344
    STAB_TRIM = 352

    def _after_landing(self):
        return sorted(
            (o["fields"]["step"], o["pk"]) for o in CONTENT
            if o["model"] == "checklist.checkitem"
            and o["fields"]["procedure"] == self.AFTER_LANDING
        )

    def test_flight_directors_come_before_weather_radar(self):
        steps = dict((pk, step) for step, pk in self._after_landing())
        self.assertLess(steps[394], steps[395])
        self.assertLess(steps[395], steps[self.WEATHER_RADAR])
        self.assertGreater(steps[394], steps[343])

    def test_trim_air_is_last_before_the_power_transfer(self):
        self.assertEqual(self._after_landing()[-1][1], 396)
        self.assertGreater(_item(396)["step"], _item(self.STAB_TRIM)["step"])

    def test_flight_directors_show_for_every_profile(self):
        for pk in (394, 395):
            self.assertEqual(_item(pk)["attributes"], [])

    def test_trim_air_is_optional(self):
        # Same Optional attribute (4) as the Preflight Trim Air Switch (35).
        self.assertEqual(_item(396)["attributes"], [OPTIONAL])
        self.assertEqual(_item(35)["attributes"], [OPTIONAL])

    def test_each_item_checks_its_switch_is_off(self):
        expected = {
            394: ("PF", "laminar/B738/autopilot/flight_director_pos"),
            395: ("PM", "laminar/B738/autopilot/flight_director_fo_pos"),
            396: ("PM", "laminar/B738/air/trim_air_pos"),
        }
        for pk, (role, dataref) in expected.items():
            item = _item(pk)
            self.assertEqual(item["role"], role)
            self.assertEqual(item["setting"], "Off")
            self.assertTrue(evaluate_rule(item["auto_check_rule"], {dataref: 0}))
            self.assertFalse(evaluate_rule(item["auto_check_rule"], {dataref: 1}))


class TestSeatbeltBeforeStartFlightLeg(SimpleTestCase):
    """Flight Deck Preparation 397: the Zibo only starts the leg with the sign on (#57)."""

    SEATBELT_SIGN = "laminar/B738/toggle_switch/seatbelt_sign_pos"
    START_FLIGHT_LEG = 160

    def test_item_sits_directly_before_start_flight_leg(self):
        seatbelt, start = _item(397), _item(self.START_FLIGHT_LEG)
        self.assertEqual(seatbelt["procedure"], start["procedure"])
        self.assertEqual(seatbelt["step"] + 1, start["step"])

    def test_item_is_optional_like_the_ground_services_it_supports(self):
        self.assertEqual(_item(397)["attributes"], [OPTIONAL])
        self.assertEqual(_item(159)["attributes"], [OPTIONAL])
        self.assertEqual(_item(self.START_FLIGHT_LEG)["attributes"], [OPTIONAL])

    def test_auto_and_on_count_as_done_off_does_not(self):
        rule = _item(397)["auto_check_rule"]
        self.assertFalse(evaluate_rule(rule, {self.SEATBELT_SIGN: 0}))
        self.assertTrue(evaluate_rule(rule, {self.SEATBELT_SIGN: 1}))
        self.assertTrue(evaluate_rule(rule, {self.SEATBELT_SIGN: 2}))


class TestSecuringRules(SimpleTestCase):
    """Securing 372/382: recirc fans and battery read the Zibo switches (#55)."""

    L_RECIRC = "laminar/B738/air/l_recirc_fan_pos"
    R_RECIRC = "laminar/B738/air/r_recirc_fan_pos"
    BATTERY = "laminar/B738/electric/battery_pos"

    def test_recirc_fans_tick_only_when_both_are_off(self):
        rule = _item(372)["auto_check_rule"]
        self.assertTrue(evaluate_rule(rule, {self.L_RECIRC: 0, self.R_RECIRC: 0}))
        self.assertFalse(evaluate_rule(rule, {self.L_RECIRC: 1, self.R_RECIRC: 0}))
        self.assertFalse(evaluate_rule(rule, {self.L_RECIRC: 0, self.R_RECIRC: 1}))

    def test_recirc_rule_uses_the_datarefs_of_the_preflight_item(self):
        found, preflight = [], []
        _paths(_item(372)["auto_check_rule"], found)
        _paths(_item(39)["auto_check_rule"], preflight)
        self.assertEqual(sorted(found), sorted(preflight))

    def test_battery_off_uses_the_same_dataref_as_battery_on(self):
        on, off = _item(145)["auto_check_rule"], _item(382)["auto_check_rule"]
        self.assertEqual(off["dataref"], on["dataref"])
        self.assertEqual(off["dataref"], self.BATTERY)

    def test_battery_off_ticks_only_when_switch_is_off(self):
        rule = _item(382)["auto_check_rule"]
        self.assertTrue(evaluate_rule(rule, {self.BATTERY: 0}))
        self.assertFalse(evaluate_rule(rule, {self.BATTERY: 1}))


class TestLandingSpeedBrakesArmed(SimpleTestCase):
    """Landing Checklist 398: the speed brakes are checked armed before landing (#54)."""

    LANDING_CHECKLIST = 20
    ENGINE_START, LANDING_GEAR, APPROACH_SPEED_BRAKES = 336, 337, 330
    SPEEDBRAKE = "sim/cockpit2/controls/speedbrake_ratio"

    def test_item_sits_between_engine_start_and_landing_gear(self):
        item = _item(398)
        self.assertEqual(item["procedure"], self.LANDING_CHECKLIST)
        self.assertGreater(item["step"], _item(self.ENGINE_START)["step"])
        self.assertLess(item["step"], _item(self.LANDING_GEAR)["step"])

    def test_item_mirrors_the_approach_speed_brakes_item(self):
        item, model = _item(398), _item(self.APPROACH_SPEED_BRAKES)
        for field in ("item", "setting", "action_label", "role", "attributes",
                      "dataref_expression", "auto_check_rule"):
            self.assertEqual(item[field], model[field], field)

    def test_ticks_only_when_armed(self):
        rule = _item(398)["auto_check_rule"]
        self.assertTrue(evaluate_rule(rule, {self.SPEEDBRAKE: -0.5}))
        self.assertFalse(evaluate_rule(rule, {self.SPEEDBRAKE: 0}))


class TestProcedureCategories(SimpleTestCase):
    """The fixture carries category explicitly: loaddata resets a missing field to
    its default, which would undo the picker grouping (and the idle page's idea of
    which procedures belong to the flow) on every deploy."""

    OUT_OF_FLOW = {"go-around", "cruise-procedure-waypoint-reached"}
    RULE_TRIGGERED_FLOW = {
        "before-takeoff-procedure", "precruise-procedure", "cruise-entry",
        "descent-procedure", "descent-10k", "after-landing-procedure",
    }

    def _procedures(self):
        return {
            o["fields"]["slug"]: o["fields"]
            for o in CONTENT if o["model"] == "checklist.procedure"
        }

    def test_every_procedure_declares_a_category(self):
        for slug, fields in self._procedures().items():
            self.assertIn("category", fields, slug)

    def test_out_of_flow_procedures_are_situational(self):
        procs = self._procedures()
        for slug in self.OUT_OF_FLOW:
            self.assertEqual(procs[slug]["category"], "situational", slug)

    def test_rule_triggered_flow_procedures_are_normal(self):
        procs = self._procedures()
        for slug in self.RULE_TRIGGERED_FLOW:
            self.assertIsNotNone(procs[slug]["show_rule"], slug)
            self.assertEqual(procs[slug]["category"], "normal", slug)
