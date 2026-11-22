"""
Flight phase wired into the plugin tick, the poll and the content import.

The engine itself is covered in test_flight_phase.py. These tests check the seams:
that the machine runs on each plugin POST with state that survives between
requests, that it can never break the request, that the browser poll only reads
it, and that bad signal definitions are refused before they reach production.
"""

# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring

import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from checklist import flight_phase
from checklist.models import FlightSession, Procedure, SOP
from checklist.tests.testFactories import SOPFactory

User = get_user_model()
STATE_URL = reverse("checklist:api_plugin_state")
POLL_URL = reverse("checklist:api_poll")

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "checklist_content.json"
CONTENT = json.loads(FIXTURE.read_text(encoding="utf-8"))
B738_SIGNALS = next(
    o["fields"]["phase_signals"] for o in CONTENT if o["model"] == "checklist.sop"
)

T0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)


def _frozen(at):
    """A stand-in for plugin_views.datetime whose now() is `at`."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102
            return at

    return Frozen


def snapshot(**sig):
    """
    A dataref snapshot, written in the B738's own paths from readable values.
    Defaults describe an aircraft parked on the stand.
    """
    v = dict(
        gs_kt=0, ias=0, agl_m=1, radio=0, alt=0, vs=0, mcp=0, cruise=0,
        brake=1, n2=0, thrust=0, dist=250,
    )
    v.update(sig)
    return {
        "sim/flightmodel/position/groundspeed": v["gs_kt"] / flight_phase.MS_TO_KT,
        "sim/flightmodel/position/indicated_airspeed": v["ias"],
        "sim/flightmodel/position/y_agl": v["agl_m"],
        "sim/cockpit2/gauges/indicators/radio_altimeter_height_ft_pilot": v["radio"],
        "sim/cockpit2/gauges/indicators/altitude_ft_pilot": v["alt"],
        "sim/flightmodel/position/vh_ind_fpm": v["vs"],
        "laminar/B738/autopilot/mcp_alt_dial": v["mcp"],
        "laminar/B738/autopilot/fmc_cruise_alt": v["cruise"],
        "laminar/B738/parking_brake_pos": v["brake"],
        "laminar/B738/engine/indicators/N2_percent_1": v["n2"],
        "laminar/B738/engine/indicators/N2_percent_2": v["n2"],
        "laminar/B738/engine/thrust1_leveler": v["thrust"],
        "laminar/B738/engine/thrust2_leveler": v["thrust"],
        "laminar/B738/fms/dist_to_dest": v["dist"],
    }


class _Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="pilot", password="pw")
        self.profile = self.user.profile
        self.key = self.profile.set_api_key()
        # The app serves the first SOP (context_processors.sop_context), and a data
        # migration already creates one, so configure that row rather than adding a second.
        self.sop = SOP.objects.order_by("pk").first() or SOPFactory()
        SOP.objects.filter(pk=self.sop.pk).update(phase_signals=B738_SIGNALS)
        Procedure.objects.create(title="Before Start", step=1, slug="before-start", sop=self.sop)
        self.session = FlightSession.objects.create(
            user_profile=self.profile, active_phase="before-start", is_active=True
        )
        self._logs = TemporaryDirectory()
        self.addCleanup(self._logs.cleanup)
        patcher = patch("checklist.plugin_views._LOG_DIR", Path(self._logs.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tick(self, seconds, **sig):
        """POST one plugin tick `seconds` after T0."""
        at = datetime.fromtimestamp(T0.timestamp() + seconds, tz=timezone.utc)
        with patch("checklist.plugin_views.datetime", _frozen(at)):
            return self.client.post(
                STATE_URL,
                data=json.dumps({"session_id": self.session.pk, "datarefs": snapshot(**sig)}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {self.key}",
            )

    def state(self):
        self.session.refresh_from_db()
        return flight_phase.PhaseState.from_dict(self.session.flight_phase_state)

    def log_lines(self):
        path = Path(self._logs.name) / f"session_{self.session.pk}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def phase_log(self):
        return [e for e in self.log_lines() if e.get("event") == "flight_phase_change"]


# ── Plugin tick ───────────────────────────────────────────────────────────────


class TestPluginTickRunsTheMachine(_Base):
    def test_first_tick_on_the_stand_sets_the_phase(self):
        self.assertEqual(self.tick(0).status_code, 200)
        self.assertEqual(self.state().phase, flight_phase.Phase.STAND)

    def test_the_change_is_logged_with_time_and_reason(self):
        self.tick(0)
        (entry,) = self.phase_log()
        self.assertEqual(entry["to"], "stand")
        self.assertIsNone(entry["from"])
        self.assertEqual(entry["rule"], "parking_brake_set")
        self.assertEqual(entry["ts"], T0.isoformat())
        self.assertIn("parking brake", entry["why"])
        self.assertEqual(entry["signals"]["parking_brake"], 1)

    def test_state_and_debounce_carry_over_between_requests(self):
        self.tick(0)  # Stand
        moving = dict(brake=0, gs_kt=5)
        self.tick(1, **moving)
        self.tick(2, **moving)
        self.assertEqual(self.state().phase, flight_phase.Phase.STAND)
        self.assertIn("movement", self.state().pending)  # holding, across requests
        self.tick(3, **moving)
        self.assertEqual(self.state().phase, flight_phase.Phase.STAND)
        self.tick(4, **moving)  # 3 s held
        self.assertEqual(self.state().phase, flight_phase.Phase.PUSHBACK)
        self.assertEqual([e["to"] for e in self.phase_log()], ["stand", "pushback"])

    def test_a_tick_that_changes_nothing_logs_nothing(self):
        self.tick(0)
        self.tick(1)
        self.tick(2)
        self.assertEqual(len(self.phase_log()), 1)

    def test_phase_state_is_written_in_the_same_update_as_the_snapshot(self):
        self.tick(0)
        self.session.refresh_from_db()
        self.assertIn("sim/flightmodel/position/groundspeed", self.session.last_datarefs)
        self.assertEqual(self.session.flight_phase_state["phase"], "stand")

    def test_phase_datarefs_are_added_to_the_watch_list(self):
        watch = self.tick(0).json()["watch"]
        for path in (
            "sim/flightmodel/position/groundspeed",
            "laminar/B738/engine/indicators/N2_percent_2",
            "laminar/B738/engine/thrust1_leveler",
            "laminar/B738/fms/dist_to_dest",
            "sim/flightmodel/position/latitude",  # the corridor's implicit dependency
        ):
            self.assertIn(path, watch)

    def test_the_watch_list_has_no_duplicates(self):
        watch = self.tick(0).json()["watch"]
        self.assertEqual(len(watch), len(set(watch)))


class TestAircraftWithoutDefinitions(_Base):
    def setUp(self):
        super().setUp()
        SOP.objects.update(phase_signals={})

    def test_nothing_is_tracked_logged_or_watched(self):
        resp = self.tick(0)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.state(), flight_phase.PhaseState())
        self.assertEqual(self.phase_log(), [])
        self.assertNotIn("sim/flightmodel/position/groundspeed", resp.json()["watch"])


class TestMachineCannotBreakTheRequest(_Base):
    def test_an_exception_in_the_machine_is_swallowed(self):
        with patch("checklist.plugin_views.flight_phase.step", side_effect=RuntimeError("boom")):
            with self.assertLogs("checklist.plugin_views", level="ERROR"):
                resp = self.tick(0)
        self.assertEqual(resp.status_code, 200)
        self.session.refresh_from_db()
        self.assertEqual(self.session.flight_phase_state, {})
        self.assertIn("sim/flightmodel/position/groundspeed", self.session.last_datarefs)

    def test_an_unwritable_log_is_swallowed_too(self):
        with patch("checklist.plugin_views._session_log", side_effect=OSError("disk full")):
            with self.assertLogs("checklist.plugin_views", level="ERROR"):
                resp = self.tick(0)
        self.assertEqual(resp.status_code, 200)

    def test_damaged_stored_state_starts_afresh(self):
        FlightSession.objects.filter(pk=self.session.pk).update(flight_phase_state={"phase": "warp"})
        self.assertEqual(self.tick(0).status_code, 200)
        self.assertEqual(self.state().phase, flight_phase.Phase.STAND)

    def test_junk_datarefs_never_fail_the_request(self):
        at = datetime.fromtimestamp(T0.timestamp(), tz=timezone.utc)
        with patch("checklist.plugin_views.datetime", _frozen(at)):
            resp = self.client.post(
                STATE_URL,
                data=json.dumps({
                    "session_id": self.session.pk,
                    "datarefs": {"sim/flightmodel/position/groundspeed": "n/a",
                                 "laminar/B738/parking_brake_pos": None},
                }),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {self.key}",
            )
        self.assertEqual(resp.status_code, 200)


# ── Browser poll ──────────────────────────────────────────────────────────────


class TestPollReportsTheFlightPhase(TestCase):
    def setUp(self):
        self.sop = SOPFactory()
        self.session = FlightSession.objects.create()
        session = self.client.session
        session["flight_session_key"] = self.session.session_key
        session.save()

    def poll(self):
        return self.client.get(POLL_URL, {"since": 0}).json()

    def test_null_until_the_sim_has_been_seen(self):
        self.assertIsNone(self.poll()["flight_phase"])

    def test_reports_the_stored_phase(self):
        state = flight_phase.PhaseState(phase=flight_phase.Phase.CRUISE, since=1, last_tick=2)
        FlightSession.objects.filter(pk=self.session.pk).update(flight_phase_state=state.to_dict())
        fp = self.poll()["flight_phase"]
        self.assertEqual((fp["key"], fp["label"], fp["group"]), ("cruise", "Cruise", "airborne"))
        self.assertEqual((fp["index"], fp["total"]), (6, 11))
        self.assertEqual(fp["waiting_for"], "Descent · V/S below -300 fpm")

    def test_unclassified_session_reports_phase_unknown(self):
        state = flight_phase.PhaseState(last_tick=2)
        FlightSession.objects.filter(pk=self.session.pk).update(flight_phase_state=state.to_dict())
        self.assertEqual(self.poll()["flight_phase"]["label"], "Phase unknown")

    def test_does_not_collide_with_the_checklist_phase_state_key(self):
        data = self.poll()
        self.assertIn("phase_state", data)  # checklist completion, unchanged
        self.assertIn("phase_complete", data["phase_state"])
        self.assertNotIn("phase_complete", data["flight_phase"] or {})

    def test_the_poll_only_reads(self):
        state = flight_phase.PhaseState(phase=flight_phase.Phase.CRUISE, since=1, last_tick=2)
        FlightSession.objects.filter(pk=self.session.pk).update(flight_phase_state=state.to_dict())
        self.poll()
        self.poll()
        self.session.refresh_from_db()
        self.assertEqual(self.session.flight_phase_state, state.to_dict())


# ── SOP model and content import ──────────────────────────────────────────────


class TestSopValidation(TestCase):
    def test_default_is_off(self):
        self.assertEqual(SOPFactory().phase_signals, {})

    def test_the_shipped_definitions_pass_model_validation(self):
        SOPFactory(phase_signals=B738_SIGNALS).full_clean()

    def test_a_bad_definition_is_refused_by_model_validation(self):
        sop = SOPFactory.build(phase_signals={"warp_speed": {"dataref": "x"}})
        with self.assertRaises(ValidationError) as ctx:
            sop.full_clean()
        self.assertIn("phase_signals", ctx.exception.message_dict)


class TestContentImportValidatesSignals(TestCase):
    def _import(self, mutate=None):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        if mutate:
            mutate(next(o for o in data if o["model"] == "checklist.sop")["fields"])
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            call_command(
                "checklist_content", "import", f"--fixture={path}", "--noinput", "--dry-run"
            )

    def test_the_shipped_fixture_passes(self):
        self._import()

    def test_an_unknown_signal_name_is_refused(self):
        with self.assertRaisesMessage(CommandError, "phase_signals"):
            self._import(lambda f: f["phase_signals"].update(warp_speed={"dataref": "x"}))

    def test_a_malformed_definition_is_refused(self):
        with self.assertRaisesMessage(CommandError, "exactly one of"):
            self._import(lambda f: f["phase_signals"].update(ias_kt={}))

    def test_a_fixture_without_the_field_still_imports(self):
        """An older fixture (before this field existed) means 'detection off', not an error."""
        self._import(lambda f: f.pop("phase_signals"))

    def test_a_real_import_stores_the_definitions(self):
        call_command("checklist_content", "import", f"--fixture={FIXTURE}", "--noinput")
        self.assertEqual(SOP.objects.get(icao_code="B738").phase_signals, B738_SIGNALS)


# ── The shipped fixture ───────────────────────────────────────────────────────


class TestShippedSignalDefinitions(SimpleTestCase):
    def test_validate_clean(self):
        self.assertEqual(flight_phase.validate_definitions(B738_SIGNALS), [])

    def test_every_signal_the_machine_reads_is_defined(self):
        missing = set(flight_phase.SIGNAL_NAMES) - set(B738_SIGNALS)
        self.assertEqual(missing, set())

    def test_no_path_has_stray_whitespace(self):
        for path in flight_phase.watched_datarefs(B738_SIGNALS):
            self.assertEqual(path, path.strip())
            self.assertNotIn(" ", path)

    def test_units_are_converted_where_x_plane_uses_si(self):
        gs = B738_SIGNALS["ground_speed_kt"]
        agl = B738_SIGNALS["agl_ft"]
        self.assertEqual(gs["dataref"], "sim/flightmodel/position/groundspeed")
        self.assertAlmostEqual(gs["scale"], flight_phase.MS_TO_KT, places=5)  # m/s
        self.assertEqual(agl["dataref"], "sim/flightmodel/position/y_agl")
        self.assertAlmostEqual(agl["scale"], flight_phase.M_TO_FT, places=5)  # metres

    def test_the_runway_corridor_matches_the_before_takeoff_rule(self):
        before_takeoff = next(
            o["fields"]["show_rule"]
            for o in CONTENT
            if o["model"] == "checklist.procedure" and o["fields"]["slug"] == "before-takeoff-procedure"
        )
        corridor = next(r for r in before_takeoff["all"] if r.get("op") == "corridor")
        self.assertEqual(B738_SIGNALS["on_runway"]["rule"], corridor)
