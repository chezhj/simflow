"""
Periodic cleanup of data that would otherwise grow without limit.

Three things accumulate, at very different rates:

- ``FlightSession`` and its cascade. One flight is ~170 rows: the session
  itself, 19 eagerly-created ``FlightSessionAttribute`` rows (one per
  ``Attribute``), and up to 363 lazy ``FlightItemState`` rows — plus a
  ``last_datarefs`` JSON snapshot. This is the fast grower, and it lives in
  ``db.sqlite3``, so it inflates every pre-migrate backup.
- ``django_session``. One row per visitor who touches the profile page, ~400
  bytes, expiring logically but never deleted.
- ``logs/session_<id>.jsonl``. One file per flight, written when an item is
  auto-checked or auto-skipped. Persistent since ``logs`` joined
  ``SHARED_PATHS``, which is what makes retention necessary rather than
  academic.

``run_cleanup`` does all three and is called two ways: from the
``checklist_prune`` management command, and from the start of a flight behind
``MaintenanceState.claim``. The trigger is what makes the clock *usage* rather
than release cadence — these tables only grow when the app is used, so that is
the right thing to tie cleanup to. A deploy hook alone would fire as often as
releases happen, which a year from now may be twice.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import FlightSession, MaintenanceState

logger = logging.getLogger(__name__)

_SESSION_LOG_RE = re.compile(r"^session_(\d+)\.jsonl$")


@dataclass
class CleanupReport:
    """What a run did, or would do under --dry-run."""

    flight_sessions_over_keep: int = 0
    flight_sessions_orphaned: int = 0
    rows_deleted: dict[str, int] = field(default_factory=dict)
    log_files_deleted: int = 0
    expired_django_sessions: bool = False
    dry_run: bool = False

    @property
    def flight_sessions(self) -> int:
        return self.flight_sessions_over_keep + self.flight_sessions_orphaned

    def lines(self) -> list[str]:
        prefix = "would delete" if self.dry_run else "deleted"
        out = [
            f"{prefix} {self.flight_sessions_over_keep} flight session(s) beyond "
            f"the per-user limit",
            f"{prefix} {self.flight_sessions_orphaned} orphaned flight session(s)",
        ]
        for model, count in sorted(self.rows_deleted.items()):
            out.append(f"  {prefix} {count} {model}")
        out.append(f"{prefix} {self.log_files_deleted} session log file(s)")
        out.append(
            "skipped expired django_session rows (dry run)"
            if self.dry_run
            else "cleared expired django_session rows"
        )
        return out


def _log_dir() -> Path:
    return Path(settings.BASE_DIR) / "logs"


def _sessions_over_keep(keep: int) -> list[int]:
    """
    Primary keys of each user's flight sessions beyond their most recent `keep`.

    Done in Python rather than as a window function because SQLite's support
    varies by the version bundled with the host's Python, and the row counts
    here are small — one query, ordered, then a counter per user.

    Active sessions are never returned. In practice the active one is always
    the newest (starting a flight deactivates the user's others, views.py), so
    this only matters if that invariant is ever broken — but deleting a session
    a pilot is flying is not a failure worth risking to save a query.
    """
    rows = (
        FlightSession.objects.filter(user_profile__isnull=False, is_active=False)
        .order_by("user_profile_id", "-created_at", "-pk")
        .values_list("pk", "user_profile_id")
    )
    doomed: list[int] = []
    seen: dict[int, int] = {}
    for pk, profile_id in rows:
        seen[profile_id] = seen.get(profile_id, 0) + 1
        if seen[profile_id] > keep:
            doomed.append(pk)
    return doomed


def _orphaned_sessions(orphan_days: int) -> list[int]:
    """
    Primary keys of sessions with no owner that have been untouched too long.

    `is_active` is deliberately NOT honoured here. A logged-in user's old
    sessions are deactivated when they start a new one, but an anonymous
    session is only deactivated through its own browser session key — lose the
    cookie and the row stays is_active=True forever. Honouring the flag would
    make this sweep a no-op for exactly the rows it exists to collect.

    Liveness is `last_plugin_contact` where the plugin ever connected, falling
    back to `created_at`. The plugin stamps it at 2 Hz while flying, so a
    genuinely long-running session is never collected on age alone.
    """
    cutoff = timezone.now() - timedelta(days=orphan_days)
    return list(
        FlightSession.objects.filter(user_profile__isnull=True)
        .annotate(last_touched=Coalesce("last_plugin_contact", "created_at"))
        .filter(last_touched__lt=cutoff)
        .values_list("pk", flat=True)
    )


def _count_session_logs(session_ids: set[int]) -> int:
    """How many log files a delete of these sessions would take. Reads nothing."""
    log_dir = _log_dir()
    if not log_dir.is_dir():
        return 0
    count = 0
    for path in log_dir.glob("session_*.jsonl"):
        match = _SESSION_LOG_RE.match(path.name)
        if match is not None and int(match.group(1)) in session_ids:
            count += 1
    return count


def _delete_session_logs(session_ids: set[int], orphan_days: int) -> int:
    """
    Remove `logs/session_<id>.jsonl` for the given sessions, and for sessions
    that no longer exist at all.

    The second sweep catches files left by sessions deleted before this command
    existed. It is bounded by mtime as well as by existence, so a file written
    for a session created moments ago — after the pk query ran — is never
    caught by the race.
    """
    log_dir = _log_dir()
    if not log_dir.is_dir():
        return 0

    stale_before = (timezone.now() - timedelta(days=orphan_days)).timestamp()
    deleted = 0
    known: set[int] | None = None

    for path in log_dir.glob("session_*.jsonl"):
        match = _SESSION_LOG_RE.match(path.name)
        if match is None:
            continue
        file_id = int(match.group(1))

        if file_id not in session_ids:
            # Only now is the extra query worth making, and only once.
            if known is None:
                known = set(FlightSession.objects.values_list("pk", flat=True))
            if file_id in known:
                continue
            try:
                if path.stat().st_mtime >= stale_before:
                    continue
            except OSError:
                continue

        try:
            path.unlink()
            deleted += 1
        except OSError:
            logger.warning("could not delete session log %s", path, exc_info=True)

    return deleted


def run_cleanup(
    *, keep: int | None = None, orphan_days: int | None = None, dry_run: bool = False
) -> CleanupReport:
    """
    Delete what retention says is expired and report what went.

    Safe to run concurrently: every step is a delete, so a second runner doing
    the same work finds nothing left rather than doing damage.
    """
    if keep is None:
        keep = getattr(settings, "CLEANUP_KEEP_SESSIONS_PER_USER", 4)
    if orphan_days is None:
        orphan_days = getattr(settings, "CLEANUP_ORPHAN_DAYS", 30)

    over_keep = _sessions_over_keep(keep)
    orphaned = _orphaned_sessions(orphan_days)
    doomed = set(over_keep) | set(orphaned)

    report = CleanupReport(
        flight_sessions_over_keep=len(over_keep),
        flight_sessions_orphaned=len(orphaned),
        dry_run=dry_run,
    )

    if dry_run:
        # Report the cascade without performing it. Counting the children
        # directly is the honest answer: Django reports them only as part of
        # an actual delete.
        from .models import (
            FlightItemState,
            FlightSessionAttribute,
            RuleMissReport,
        )

        for model in (FlightSessionAttribute, FlightItemState, RuleMissReport):
            count = model.objects.filter(flight_session_id__in=doomed).count()
            if count:
                report.rows_deleted[model.__name__] = count
        report.rows_deleted["FlightSession"] = len(doomed)
        report.log_files_deleted = _count_session_logs(doomed)
        return report

    if doomed:
        _, per_model = FlightSession.objects.filter(pk__in=doomed).delete()
        report.rows_deleted = {
            label.split(".")[-1]: count for label, count in per_model.items()
        }

    # Files after rows: if the delete fails the logs are still reachable from
    # the sessions they belong to.
    report.log_files_deleted = _delete_session_logs(doomed, orphan_days)

    call_command("clearsessions")
    report.expired_django_sessions = True

    return report


def run_cleanup_if_due() -> CleanupReport | None:
    """
    Run cleanup at most once per CLEANUP_MIN_INTERVAL_HOURS, across all workers.

    Called from the start of a flight. It must never take that request down
    with it — a pilot who cannot start a checklist because housekeeping failed
    is a far worse outcome than a table that grows for another day — so every
    failure is logged and swallowed.
    """
    hours = getattr(settings, "CLEANUP_MIN_INTERVAL_HOURS", 24)
    try:
        if not MaintenanceState.claim(timedelta(hours=hours)):
            return None
        report = run_cleanup()
    except Exception:  # noqa: BLE001 — see the docstring
        logger.exception("periodic cleanup failed")
        return None

    logger.info(
        "periodic cleanup: %s flight session(s), %s log file(s)",
        report.flight_sessions,
        report.log_files_deleted,
    )
    return report
