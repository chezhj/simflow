"""
manage.py checklist_content export   — dump Attribute, Procedure, CheckItem to a fixture
manage.py checklist_content import   — load that fixture (safe: uses natural keys, skips conflicts)
manage.py checklist_content import --replace  — wipe content tables first, then load

Content models: Attribute, Procedure, CheckItem
User/session models are never touched.
"""

import io
import json
import sys
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

# The three content models, in dependency order for safe loading.
CONTENT_MODELS = [
    "checklist.attribute",
    "checklist.procedure",
    "checklist.checkitem",
]

DEFAULT_FIXTURE = Path("checklist") / "fixtures" / "checklist_content.json"


class Command(BaseCommand):
    help = (
        "Export or import checklist content data (Attribute, Procedure, CheckItem). "
        "User and session data are never affected."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "action",
            choices=["export", "import"],
            help="'export' writes a fixture; 'import' loads it.",
        )
        parser.add_argument(
            "--fixture",
            default=str(DEFAULT_FIXTURE),
            help=f"Path to the fixture file (default: {DEFAULT_FIXTURE})",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help=(
                "import only: delete all existing content rows before loading. "
                "Use this when IDs may clash (e.g. a clean production deploy)."
            ),
        )

    def handle(self, *args, **options):
        action = options["action"]
        fixture_path = Path(options["fixture"])

        if action == "export":
            self._export(fixture_path)
        else:
            self._import(fixture_path, replace=options["replace"])

    # ------------------------------------------------------------------ #
    #  Export                                                              #
    # ------------------------------------------------------------------ #

    def _export(self, fixture_path: Path):
        fixture_path.parent.mkdir(parents=True, exist_ok=True)

        self.stdout.write("Exporting checklist content …")
        # Buffer dumpdata's output, then re-write it as pure 7-bit ASCII.
        # Django's JSON serializer defaults to ensure_ascii=False, so dumpdata
        # emits raw UTF-8 (° — – ≤). A raw-UTF-8 fixture is prone to cp1252
        # re-corruption on Windows — e.g. ° becomes "Â°", — becomes "â€"" —
        # which then bakes mojibake into the DB on the next import. Escaping
        # non-ASCII to \uXXXX keeps the committed fixture corruption-proof.
        buffer = io.StringIO()
        call_command(
            "dumpdata",
            *CONTENT_MODELS,
            indent=2,
            stdout=buffer,
            natural_foreign=True,   # uses title/slug instead of raw PKs where possible
            natural_primary=True,
        )
        data = json.loads(buffer.getvalue())

        # encoding="ascii" asserts purity — it errors if any non-ASCII slips through.
        with open(fixture_path, "w", encoding="ascii") as f:
            json.dump(data, f, indent=2, ensure_ascii=True)

        counts = {}
        for obj in data:
            counts[obj["model"]] = counts.get(obj["model"], 0) + 1

        self.stdout.write(self.style.SUCCESS(f"Fixture written to: {fixture_path}"))
        for model, count in sorted(counts.items()):
            self.stdout.write(f"  {model}: {count} records")

    # ------------------------------------------------------------------ #
    #  Import                                                              #
    # ------------------------------------------------------------------ #

    def _import(self, fixture_path: Path, replace: bool):
        if not fixture_path.exists():
            raise CommandError(
                f"Fixture not found: {fixture_path}\n"
                "Run 'manage.py checklist_content export' first."
            )

        with open(fixture_path, encoding="utf-8") as f:
            data = json.load(f)

        counts = {}
        for obj in data:
            counts[obj["model"]] = counts.get(obj["model"], 0) + 1

        self.stdout.write(f"Fixture: {fixture_path}")
        for model, count in sorted(counts.items()):
            self.stdout.write(f"  {model}: {count} records")

        if replace:
            self._confirm_replace()
            self._wipe_content_tables()

        self.stdout.write("Loading fixture …")
        try:
            with transaction.atomic():
                call_command("loaddata", str(fixture_path), verbosity=1)
        except Exception as exc:
            raise CommandError(
                f"Load failed: {exc}\n"
                "No changes were made (transaction rolled back)."
            ) from exc

        self.stdout.write(self.style.SUCCESS("Checklist content loaded successfully."))

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _confirm_replace(self):
        """Ask for confirmation before wiping content tables."""
        self.stdout.write(
            self.style.WARNING(
                "\n--replace will DELETE all existing Attribute, Procedure and "
                "CheckItem rows before loading.\nThis cannot be undone.\n"
            )
        )
        answer = input("Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            self.stdout.write("Aborted.")
            sys.exit(0)

    @staticmethod
    def _wipe_content_tables():
        """Delete content rows in reverse dependency order to avoid FK errors."""
        from checklist.models import CheckItem, Procedure, Attribute  # noqa: PLC0415

        CheckItem.objects.all().delete()
        Procedure.objects.all().delete()
        Attribute.objects.all().delete()
