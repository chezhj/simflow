"""
Delete flight sessions, session logs and expired Django sessions past retention.

Run by hand, from POST_MIGRATE_COMMANDS on deploy, and — behind a once-a-day
gate — at the start of a flight. See checklist/maintenance.py for why the
last of those is the one that matters.

    manage.py checklist_prune --dry-run      # report, write nothing
    manage.py checklist_prune --noinput      # no confirmation prompt
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from checklist.maintenance import run_cleanup


class Command(BaseCommand):
    help = "Prune flight sessions, their logs, and expired Django sessions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep",
            type=int,
            default=None,
            help="Flight sessions to keep per user (default: "
            "settings.CLEANUP_KEEP_SESSIONS_PER_USER).",
        )
        parser.add_argument(
            "--orphan-days",
            type=int,
            default=None,
            help="Delete ownerless sessions untouched for this many days "
            "(default: settings.CLEANUP_ORPHAN_DAYS).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the plan without deleting anything.",
        )
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_true",
            dest="noinput",
            help="Do not prompt for confirmation.",
        )

    def handle(self, *args, **options):
        keep = options["keep"]
        orphan_days = options["orphan_days"]
        dry_run = options["dry_run"]

        # A negative keep would delete every session a user has; zero is a
        # legitimate "keep nothing" and is left alone.
        if keep is not None and keep < 0:
            raise CommandError("--keep cannot be negative.")
        if orphan_days is not None and orphan_days < 0:
            raise CommandError("--orphan-days cannot be negative.")

        effective_keep = (
            keep
            if keep is not None
            else getattr(settings, "CLEANUP_KEEP_SESSIONS_PER_USER", 4)
        )
        effective_days = (
            orphan_days
            if orphan_days is not None
            else getattr(settings, "CLEANUP_ORPHAN_DAYS", 30)
        )

        self.stdout.write(
            f"retention: keep {effective_keep} session(s) per user, "
            f"drop ownerless sessions untouched for {effective_days} day(s)"
        )

        if not dry_run and not options["noinput"]:
            # This cascades into FlightSessionAttribute, FlightItemState and
            # RuleMissReport, and unlike the content import there is no fixture
            # to restore from.
            answer = input("This deletes data permanently. Continue? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                self.stdout.write(self.style.WARNING("aborted"))
                return

        report = run_cleanup(
            keep=keep, orphan_days=orphan_days, dry_run=dry_run
        )

        for line in report.lines():
            self.stdout.write(line)

        style = self.style.WARNING if dry_run else self.style.SUCCESS
        self.stdout.write(style("dry run — nothing written" if dry_run else "done"))
