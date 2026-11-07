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
