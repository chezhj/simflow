"""
Retention: what gets deleted, what is protected, and the once-a-day gate.

The rules under test are: keep the N most recent flight sessions per account,
drop ownerless sessions untouched for longer than the orphan window, take the
cascade and the session log file with them, and never run more than once per
interval however many Passenger workers ask at once.

The protections matter more than the deletions here. This command cascades into
FlightSessionAttribute, FlightItemState and RuleMissReport, and unlike the
content import there is no fixture to restore from.
"""

# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring

import pathlib
from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from checklist import maintenance
from checklist.maintenance import run_cleanup, run_cleanup_if_due
from checklist.models import (
    FlightItemState,
    FlightSession,
    FlightSessionAttribute,
    MaintenanceState,
)
from checklist.models import Attribute
from checklist.tests.ViewTestCase import ViewTestCase
from checklist.tests.testFactories import (
    AttributeFactory,
    CheckItemFactory,
    SOPFactory,
)
from checklist.views import profile_view

User = get_user_model()


def _session(profile=None, *, age_days=0, active=False, contact_days=None):
    """A flight session with created_at forced — auto_now_add ignores kwargs."""
    session = FlightSession.objects.create(user_profile=profile, is_active=active)
    created = timezone.now() - timedelta(days=age_days)
    contact = (
        timezone.now() - timedelta(days=contact_days)
        if contact_days is not None
        else None
    )
    FlightSession.objects.filter(pk=session.pk).update(
        created_at=created, last_plugin_contact=contact
    )
    session.refresh_from_db()
    return session


class _RetentionBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="pilot", password="pw")
        self.profile = self.user.profile
        self.other = User.objects.create_user(username="other", password="pw")
        self.other_profile = self.other.profile


class TestPerUserRetention(_RetentionBase):

    def test_keeps_the_most_recent_four_and_deletes_the_rest(self):
        kept = [_session(self.profile, age_days=d) for d in (0, 1, 2, 3)]
        doomed = [_session(self.profile, age_days=d) for d in (10, 20, 30)]

        run_cleanup(keep=4, orphan_days=30)

        alive = set(FlightSession.objects.values_list("pk", flat=True))
        self.assertSetEqual(alive, {s.pk for s in kept})
        for session in doomed:
            self.assertNotIn(session.pk, alive)

    def test_the_limit_is_per_account_not_global(self):
        """Two users with four sessions each keep all eight."""
        for profile in (self.profile, self.other_profile):
            for day in range(4):
                _session(profile, age_days=day)

        run_cleanup(keep=4, orphan_days=30)

        self.assertEqual(FlightSession.objects.count(), 8)

    def test_an_active_session_is_never_deleted(self):
        """
        Starting a flight deactivates a user's others, so the active one is
        always newest — but deleting a session a pilot is flying is not a risk
        worth taking on an invariant holding.
        """
        flying = _session(self.profile, age_days=99, active=True)
        for day in range(6):
            _session(self.profile, age_days=day)

        run_cleanup(keep=2, orphan_days=30)

        self.assertTrue(FlightSession.objects.filter(pk=flying.pk).exists())

    def test_keep_zero_deletes_every_inactive_session(self):
        for day in range(3):
            _session(self.profile, age_days=day)

        run_cleanup(keep=0, orphan_days=30)

        self.assertEqual(FlightSession.objects.count(), 0)


class TestOrphanRetention(_RetentionBase):

    def test_ownerless_sessions_go_after_the_orphan_window(self):
        old = _session(None, age_days=31)
        recent = _session(None, age_days=29)

        run_cleanup(keep=4, orphan_days=30)

        self.assertFalse(FlightSession.objects.filter(pk=old.pk).exists())
        self.assertTrue(FlightSession.objects.filter(pk=recent.pk).exists())

    def test_recent_plugin_contact_protects_an_old_session(self):
        """
        Liveness is last_plugin_contact where the plugin ever connected. A
        session opened 60 days ago but still being flown today is live.
        """
        long_lived = _session(None, age_days=60, contact_days=0)

        run_cleanup(keep=4, orphan_days=30)

        self.assertTrue(FlightSession.objects.filter(pk=long_lived.pk).exists())

    def test_the_active_flag_does_not_protect_an_orphan(self):
        """
        An anonymous session is only deactivated through its own browser
        session key. Lose the cookie and it stays is_active=True forever, so
        honouring the flag would make this sweep a no-op for exactly the rows
        it exists to collect.
        """
        stranded = _session(None, age_days=60, active=True)

        run_cleanup(keep=4, orphan_days=30)

        self.assertFalse(FlightSession.objects.filter(pk=stranded.pk).exists())

    def test_an_owned_session_is_never_touched_by_the_orphan_sweep(self):
        """Age alone must not delete a user's session — only the keep-N rule does."""
        ancient = _session(self.profile, age_days=400)

        run_cleanup(keep=4, orphan_days=30)

        self.assertTrue(FlightSession.objects.filter(pk=ancient.pk).exists())


class TestCascadeAndLogs(_RetentionBase):

    def setUp(self):
        super().setUp()
        SOPFactory()
        self.item = CheckItemFactory()

    def test_children_go_with_the_session(self):
        doomed = _session(self.profile, age_days=99)
        FlightItemState.objects.create(
            flight_session=doomed, checklist_item=self.item, status="checked"
        )
        run_cleanup(keep=0, orphan_days=30)

        self.assertEqual(FlightItemState.objects.count(), 0)
        self.assertEqual(FlightSessionAttribute.objects.count(), 0)

    def test_the_session_log_file_goes_too(self):
        doomed = _session(self.profile, age_days=99)
        log_dir = maintenance._log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"session_{doomed.pk}.jsonl"
        path.write_text('{"event": "checked"}\n', encoding="utf-8")

        try:
            report = run_cleanup(keep=0, orphan_days=30)
            self.assertFalse(path.exists())
            self.assertEqual(report.log_files_deleted, 1)
        finally:
            path.unlink(missing_ok=True)

    def test_a_live_sessions_log_is_left_alone(self):
        live = _session(self.profile, age_days=0)
        log_dir = maintenance._log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"session_{live.pk}.jsonl"
        path.write_text('{"event": "checked"}\n', encoding="utf-8")

        try:
            run_cleanup(keep=4, orphan_days=30)
            self.assertTrue(path.exists())
        finally:
            path.unlink(missing_ok=True)

    def test_a_stray_file_for_a_vanished_session_needs_to_be_old(self):
        """
        Bounded by mtime as well as by existence, so a file written for a
        session created after the pk query ran is never caught by the race.
        """
        log_dir = maintenance._log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        fresh = log_dir / "session_99999901.jsonl"
        fresh.write_text("{}\n", encoding="utf-8")

        try:
            run_cleanup(keep=4, orphan_days=30)
            self.assertTrue(fresh.exists(), "a just-written stray file was deleted")
        finally:
            fresh.unlink(missing_ok=True)


class TestExpiredDjangoSessions(_RetentionBase):

    def test_expired_rows_are_cleared_and_live_ones_are_not(self):
        from django.contrib.sessions.backends.db import SessionStore

        expired = SessionStore()
        expired["attrib"] = [1, 2]
        expired.set_expiry(-1)
        expired.save()

        live = SessionStore()
        live["attrib"] = [3]
        live.set_expiry(3600)
        live.save()

        self.assertEqual(Session.objects.count(), 2)
        run_cleanup(keep=4, orphan_days=30)

        remaining = set(Session.objects.values_list("session_key", flat=True))
        self.assertSetEqual(remaining, {live.session_key})


class TestDryRun(_RetentionBase):

    def test_dry_run_reports_without_deleting(self):
        for day in (0, 10, 20, 30):
            _session(self.profile, age_days=day)

        report = run_cleanup(keep=1, orphan_days=30, dry_run=True)

        self.assertEqual(FlightSession.objects.count(), 4)
        self.assertEqual(report.flight_sessions_over_keep, 3)
        self.assertTrue(report.dry_run)
        self.assertFalse(report.expired_django_sessions)


class TestTheOncePerIntervalGate(_RetentionBase):

    def test_the_first_claim_on_a_fresh_install_succeeds(self):
        self.assertFalse(MaintenanceState.objects.exists())
        self.assertTrue(MaintenanceState.claim(timedelta(hours=24)))

    def test_a_second_claim_inside_the_interval_is_refused(self):
        MaintenanceState.claim(timedelta(hours=24))
        self.assertFalse(MaintenanceState.claim(timedelta(hours=24)))

    def test_a_claim_after_the_interval_succeeds(self):
        MaintenanceState.claim(timedelta(hours=24))
        later = timezone.now() + timedelta(hours=25)
        self.assertTrue(MaintenanceState.claim(timedelta(hours=24), now=later))

    def test_exactly_one_of_many_simultaneous_claims_wins(self):
        """
        The worker-safety guarantee. Every caller passes the same `now`, which
        is the worst case: all of them see the same stale timestamp. A
        read-then-write would let them all through; the conditional UPDATE
        lets exactly one.
        """
        MaintenanceState.claim(timedelta(hours=24))
        now = timezone.now() + timedelta(hours=25)

        wins = sum(
            MaintenanceState.claim(timedelta(hours=24), now=now) for _ in range(10)
        )

        self.assertEqual(wins, 1)

    def test_the_lock_is_taken_before_the_work_not_after(self):
        """A run that crashes must not hold the gate open for the next caller."""
        MaintenanceState.claim(timedelta(hours=24))
        MaintenanceState.objects.update(
            last_cleanup=timezone.now() - timedelta(days=2)
        )

        with patch.object(maintenance, "run_cleanup", side_effect=RuntimeError("boom")):
            self.assertIsNone(run_cleanup_if_due())

        # The claim was taken despite the failure, so the next caller waits.
        self.assertIsNone(run_cleanup_if_due())


class TestTheTriggerCannotBreakTheRequest(_RetentionBase):

    def test_a_failing_cleanup_is_swallowed_and_logged(self):
        """
        A pilot who cannot start a checklist because housekeeping failed is a
        far worse outcome than a table that grows for another day.
        """
        with patch.object(maintenance, "run_cleanup", side_effect=RuntimeError("boom")):
            with self.assertLogs("checklist.maintenance", level="ERROR") as logs:
                self.assertIsNone(run_cleanup_if_due())
        self.assertIn("periodic cleanup failed", "\n".join(logs.output))

    def test_a_failing_claim_is_swallowed_too(self):
        with patch.object(
            MaintenanceState, "claim", side_effect=RuntimeError("db gone")
        ):
            with self.assertLogs("checklist.maintenance", level="ERROR"):
                self.assertIsNone(run_cleanup_if_due())

    @override_settings(CLEANUP_MIN_INTERVAL_HOURS=24)
    def test_it_does_nothing_when_not_due(self):
        MaintenanceState.claim(timedelta(hours=24))
        with patch.object(maintenance, "run_cleanup") as cleanup:
            self.assertIsNone(run_cleanup_if_due())
        cleanup.assert_not_called()


class TestTheCommand(_RetentionBase):

    def test_noinput_prunes_without_prompting(self):
        for day in (0, 10, 20):
            _session(self.profile, age_days=day)

        call_command("checklist_prune", "--keep", "1", "--noinput", verbosity=0)

        self.assertEqual(FlightSession.objects.count(), 1)

    def test_dry_run_needs_no_confirmation_and_writes_nothing(self):
        for day in (0, 10):
            _session(self.profile, age_days=day)

        call_command("checklist_prune", "--keep", "0", "--dry-run", verbosity=0)

        self.assertEqual(FlightSession.objects.count(), 2)

    def test_a_negative_keep_is_refused(self):
        with self.assertRaises(CommandError):
            call_command("checklist_prune", "--keep", "-1", "--noinput", verbosity=0)

    def test_a_negative_orphan_window_is_refused(self):
        with self.assertRaises(CommandError):
            call_command(
                "checklist_prune", "--orphan-days", "-1", "--noinput", verbosity=0
            )

    def test_declining_the_prompt_deletes_nothing(self):
        _session(self.profile, age_days=99)
        with patch("builtins.input", return_value="n"):
            call_command("checklist_prune", "--keep", "0", verbosity=0)
        self.assertEqual(FlightSession.objects.count(), 1)


class TestTheFlightStartTrigger(ViewTestCase):
    """
    The wiring, not the retention rules: starting a flight is what drives
    cleanup, so if this call is ever dropped from the view the whole scheme
    silently stops and nothing else notices.
    """

    def _start_checklist_request(self):
        attr = Attribute.objects.create(title="Optional", order=1)
        request = self.create_request_with_session(
            "/",
            request_data={"action": "start_checklist", "attributes": str(attr.id)},
        )
        request.user = Mock(is_authenticated=False)
        return request

    def test_starting_a_flight_asks_whether_cleanup_is_due(self):
        request = self._start_checklist_request()

        with patch("checklist.views.run_cleanup_if_due") as due:
            response = profile_view(request)

        self.assertEqual(response.status_code, 302)
        due.assert_called_once()

    def test_a_failing_cleanup_still_lets_the_flight_start(self):
        """End to end: the swallow in run_cleanup_if_due protects the view."""
        request = self._start_checklist_request()

        with patch.object(maintenance, "run_cleanup", side_effect=RuntimeError("boom")):
            with self.assertLogs("checklist.maintenance", level="ERROR"):
                response = profile_view(request)

        self.assertEqual(response.status_code, 302)
        self.assertIn("flight_session_key", request.session)


class TestTheLogDirectoryIsOverridable(_RetentionBase):
    """
    CLEANUP_LOG_DIR exists for scripts/prune_dry_run.py. Without it, a dry run
    against a copy of the production database reports the file count of
    whatever machine it runs on — production data, local logs, one number
    presented as if both came from the same place.
    """

    def test_the_override_is_used_when_set(self):
        import tempfile
        from pathlib import Path

        from django.test import override_settings

        doomed = _session(self.profile, age_days=99)
        with tempfile.TemporaryDirectory() as tmp:
            elsewhere = Path(tmp)
            (elsewhere / f"session_{doomed.pk}.jsonl").write_text("{}\n")

            with override_settings(CLEANUP_LOG_DIR=str(elsewhere)):
                report = run_cleanup(keep=0, orphan_days=30, dry_run=True)

            self.assertEqual(report.log_files_deleted, 1)

    def test_it_falls_back_to_base_dir_when_unset(self):
        from django.conf import settings

        self.assertEqual(
            maintenance._log_dir(), pathlib.Path(settings.BASE_DIR) / "logs"
        )


class TestTheDryRunMatchesTheRealThing(_RetentionBase):
    """
    The guarantee that makes a dry run worth running.

    It did not hold. The dry run enumerated child models by hand —
    FlightSessionAttribute, FlightItemState, RuleMissReport — and omitted
    FlightInfo, a OneToOneField that also cascades. The first real production
    run deleted 68 FlightInfo rows the dry run had never mentioned. Nothing
    caught it because every test asserted on the models the list already knew
    about.

    This compares the two reports instead, so any model the cascade reaches is
    covered whether or not anyone thought of it.
    """

    def setUp(self):
        super().setUp()
        SOPFactory()
        self.item = CheckItemFactory()

    def _populate(self):
        from checklist.models import FlightInfo, RuleMissReport

        for day in (0, 10, 20, 30, 40):
            session = _session(self.profile, age_days=day)
            FlightItemState.objects.create(
                flight_session=session, checklist_item=self.item, status="checked"
            )
            FlightSessionAttribute.objects.create(
                flight_session=session, attribute=AttributeFactory(), is_active=True
            )
            FlightInfo.objects.create(flight_session=session)
            RuleMissReport.objects.create(
                flight_session=session,
                reported_at=timezone.now(),
                reported_item_label="x",
                active_phase="before-start",
                leaf_evaluations=[],
            )

    def test_the_two_reports_agree_exactly(self):
        self._populate()

        planned = run_cleanup(keep=1, orphan_days=30, dry_run=True).rows_deleted
        actual = run_cleanup(keep=1, orphan_days=30).rows_deleted

        self.assertEqual(
            planned,
            actual,
            "the dry run and the real run disagree — a dry run that under-reports "
            "is worse than none, because it is trusted",
        )

    def test_the_cascade_includes_every_child_model(self):
        """
        Named explicitly as well, so a regression says which table went
        missing rather than only that two dicts differ.
        """
        self._populate()

        planned = run_cleanup(keep=1, orphan_days=30, dry_run=True).rows_deleted

        for model_name in (
            "FlightSession",
            "FlightSessionAttribute",
            "FlightItemState",
            "RuleMissReport",
            "FlightInfo",
        ):
            self.assertIn(model_name, planned, f"{model_name} missing from the plan")
