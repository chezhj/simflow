"""
Flight phase on the pages: markup the browser JS fills in, and the script wiring.

The drawing itself is covered in js-tests/flight-phase.test.js. Here: that the
elements it looks for exist, hidden until a phase arrives, on the right pages.
"""

# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring

import re

from django.test import TestCase
from django.urls import reverse

from checklist import flight_phase
from checklist.models import FlightSession
from checklist.tests.testFactories import CheckItemFactory, ProcedureFactory


class _PageCase(TestCase):
    def setUp(self):
        self.session = FlightSession.objects.create()
        session = self.client.session
        session["flight_session_key"] = self.session.session_key
        session.save()

    def idle(self):
        resp = self.client.get(reverse("checklist:idle"))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def detail(self):
        procedure = ProcedureFactory()
        CheckItemFactory(procedure=procedure)
        resp = self.client.get(reverse("checklist:detail", args=[procedure.slug]))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()


class TestConnectionBarIndicators(_PageCase):
    """Both indicators live in base.html, so every page with the bar has them."""

    def test_idle_has_the_row_and_the_pill_hidden_until_a_phase_arrives(self):
        html = self.idle()
        indicators = re.findall(r'<div class="([^"]*)" data-phase-indicator', html)
        self.assertEqual(len(indicators), 2)
        self.assertTrue(all("js-hidden" in c for c in indicators))
        self.assertEqual(sorted(c.split()[0] for c in indicators), ["conn-phase", "phase-row"])

    def test_detail_has_them_too(self):
        self.assertEqual(self.detail().count("data-phase-indicator"), 2)

    def test_each_indicator_has_the_name_and_tag_slots_the_script_fills(self):
        html = self.idle()
        self.assertEqual(html.count("data-phase-name"), 2 + 1)  # row, pill, hub card
        self.assertEqual(html.count("data-phase-tag"), 2)

    def test_the_pill_sits_inside_the_bar_and_the_row_below_it(self):
        html = self.idle()
        bar_start = html.index('class="conn-bar"')
        bar_end = html.index('<div class="phase-row')
        self.assertTrue(bar_start < html.index('class="conn-phase') < bar_end)
        self.assertTrue(html.index('class="phase-row') < html.index('id="plugin-update-bar"'))


class TestHubCard(_PageCase):
    def test_card_is_hidden_until_the_first_phase(self):
        html = self.idle()
        self.assertRegex(html, r'<section class="phase-card js-hidden" id="phase-card"')

    def test_stepper_has_the_eleven_phases_in_order(self):
        html = self.idle()
        keys = re.findall(r'data-step data-phase-key="([a-z_]+)"', html)
        self.assertEqual(keys, [p.value for p in flight_phase.PHASE_ORDER])
        self.assertEqual(len(keys), 11)

    def test_stepper_shows_the_short_names(self):
        html = self.idle()
        labels = re.findall(r'<span class="phase-step-label">([^<]+)</span>', html)
        self.assertEqual(labels[:3], ["Stand", "Pushback", "Taxi out"])

    def test_group_captions_span_the_stepper(self):
        html = self.idle()
        spans = re.findall(r'phase-group--span-(\d)">([^<]+)<', html)
        self.assertEqual(
            spans, [("3", "Ground"), ("1", "Rwy"), ("4", "Airborne"), ("1", "Rwy"), ("2", "Ground")]
        )

    def test_foot_lines_start_hidden(self):
        html = self.idle()
        self.assertIn('class="phase-card-foot js-hidden" data-phase-foot', html)
        self.assertIn('class="js-hidden" data-phase-previous', html)
        self.assertIn('class="js-hidden" data-phase-next', html)

    def test_the_fixed_in_flight_labels_remain_as_the_fallback(self):
        """Until a phase is known they must still say something, as before."""
        html = self.idle()
        self.assertIn('class="proc-phase" data-phase-label>In Flight<', html)
        self.assertIn('class="nav-label" data-phase-label>In Flight<', html)

    def test_checklist_page_has_no_card(self):
        self.assertNotIn('id="phase-card"', self.detail())


class TestScriptWiring(_PageCase):
    def test_both_pages_load_the_script_and_call_it_from_their_poll(self):
        for html in (self.idle(), self.detail()):
            self.assertIn("checklist/flight-phase.js", html)
            self.assertIn("SimFlowPhase.apply(data);", html)

    def test_the_script_loads_before_the_inline_poll_loop(self):
        html = self.idle()
        self.assertLess(html.index("flight-phase.js"), html.index("SimFlowPhase.apply"))

    def test_the_call_comes_after_the_stale_response_guard(self):
        """A late reply must not paint an older phase over a newer one."""
        for html in (self.idle(), self.detail()):
            self.assertLess(html.index("seq <= appliedSeq"), html.index("SimFlowPhase.apply(data)"))

    def test_the_script_is_served(self):
        from django.contrib.staticfiles import finders  # pylint: disable=import-outside-toplevel

        self.assertIsNotNone(finders.find("checklist/flight-phase.js"))


class TestStylesheets(TestCase):
    """The classes the markup uses are defined, in the files the project keeps them in."""

    def _css(self, name):
        from django.contrib.staticfiles import finders  # pylint: disable=import-outside-toplevel

        with open(finders.find(f"checklist/css/{name}"), encoding="utf-8") as f:
            return f.read()

    def test_structure_is_in_components_and_states_are_in_states(self):
        components, states = self._css("components.css"), self._css("states.css")
        for selector in (".phase-row", ".conn-phase", ".phase-card", ".phase-steps", ".phase-step"):
            self.assertIn(selector, components)
        for selector in (".phase-step.is-current", ".phase-step.is-done", ".phase-card.is-stale"):
            self.assertIn(selector, states)

    def test_pill_and_row_swap_at_the_desktop_breakpoint(self):
        components = self._css("components.css")
        self.assertRegex(
            components,
            r"@media \(min-width: 900px\) \{\s*\.phase-row \{ display: none; \}\s*\.conn-phase \{ display: inline-flex; \}",
        )

    def test_there_are_no_inline_styles_in_the_new_markup(self):
        # CLAUDE.md: no inline styles except data-driven badge colours.
        from pathlib import Path  # pylint: disable=import-outside-toplevel

        templates = Path(__file__).resolve().parent.parent / "templates" / "checklist"
        for name in ("base.html", "idle.html"):
            text = (templates / name).read_text(encoding="utf-8")
            for match in re.finditer(r"<[^>]*data-phase[^>]*>", text):
                self.assertNotIn("style=", match.group(0))
