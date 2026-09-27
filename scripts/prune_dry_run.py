#!/usr/bin/env python
"""
Report what `checklist_prune` would delete from a COPY of the production
database, without deploying anything and without writing to it.

Why this exists: the obvious plan — "dry-run on the server before the deploy"
— is impossible. `checklist_prune` ships *with* the release, and
`POST_MIGRATE_COMMANDS` runs it in the same activate step, so there is no
moment on the server where the command exists but has not yet run. The
flight-start trigger would fire on the first flight afterwards in any case.

So the inspection happens here, against a copy pulled down beforehand:

    scp <user>@<host>:domains/shared/simflow/db.sqlite3 /tmp/prod-copy.sqlite3
    python scripts/prune_dry_run.py /tmp/prod-copy.sqlite3

It is dry-run only. There is no flag to make it delete: this script exists to
answer "what would happen", and a script that can also do the thing invites
being run with the wrong argument on the wrong file.

The copy is opened read-only where SQLite allows it, and the file is never
migrated — if the copy predates a migration the command needs, that is worth
seeing as an error rather than silently working on a half-upgraded schema.
"""

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("database", help="path to a COPY of the production db.sqlite3")
    parser.add_argument(
        "--keep", type=int, default=None, help="override CLEANUP_KEEP_SESSIONS_PER_USER"
    )
    parser.add_argument(
        "--orphan-days", type=int, default=None, help="override CLEANUP_ORPHAN_DAYS"
    )
    args = parser.parse_args()

    source = Path(args.database).expanduser().resolve()
    if not source.is_file():
        sys.exit(f"ERROR: no such file: {source}")

    live = REPO_ROOT / "db.sqlite3"
    if source == live.resolve():
        sys.exit(
            "ERROR: that is this checkout's own db.sqlite3, not a copy of "
            "production. Pull the server's file down first."
        )

    # Work on a throwaway duplicate as well. --dry-run already writes nothing,
    # but the cost of being wrong about that is someone's flight history, and
    # a temp copy makes the question moot.
    #
    # Not TemporaryDirectory(): its cleanup raises PermissionError on Windows
    # if anything still holds the file open, and Django's SQLite connection
    # does. POSIX allows unlinking an open file, so this only ever showed up
    # on Windows — as a traceback AFTER the report had already printed, which
    # is a confusing way to be told the run succeeded. Closing the connection
    # is the real fix; ignore_errors is there so a stray handle can never turn
    # a successful inspection into a failure.
    tmp = Path(tempfile.mkdtemp(prefix="simflow-prune-"))
    try:
        scratch = tmp / "inspect.sqlite3"
        shutil.copy2(source, scratch)

        sys.path.insert(0, str(REPO_ROOT))
        os.environ.setdefault(
            "DJANGO_SETTINGS_MODULE", "smart_training_checklist.settings.dev"
        )
        import django

        django.setup()

        from django.conf import settings

        settings.DATABASES["default"]["NAME"] = str(scratch)

        from django.core.management import call_command

        from checklist.models import FlightSession

        print(f"inspecting a copy of: {source}")
        print(f"flight sessions in it: {FlightSession.objects.count()} "
              f"({FlightSession.objects.filter(user_profile__isnull=True).count()} "
              f"with no owner)")
        print()

        call_command(
            "checklist_prune",
            dry_run=True,
            keep=args.keep,
            orphan_days=args.orphan_days,
        )

        print()
        print("Nothing was written, to the copy or to production.")
    finally:
        try:
            from django.db import connections

            connections.close_all()
        except Exception:  # noqa: BLE001 — cleanup must not mask the report
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
