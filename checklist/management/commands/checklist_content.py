"""
manage.py checklist_content export   — dump SOP, Attribute, Procedure, CheckItem to a fixture
manage.py checklist_content import   — upsert the fixture (rows absent from it are left alone)
manage.py checklist_content import --prune  — also delete content rows the fixture no longer has
manage.py checklist_content import --prune --noinput  — same, unattended (deploy scripts)
manage.py checklist_content import --prune --dry-run  — report the plan, write nothing

Content models: SOP, Attribute, Procedure, CheckItem

The import is an upsert keyed on the fixture's primary keys, followed — with
--prune — by deletion of exactly the content rows the fixture no longer carries.
It is deliberately NOT a wipe-and-reload: Attribute, Procedure and CheckItem are
referenced by user and session data (UserAttributeDefault, FlightSessionAttribute,
FlightItemState) with CASCADE, so deleting a row that is about to be re-created
destroys saved preferences and in-flight progress. Reloading content must not
delete anything, and a delete must mean the content is genuinely gone.

The fixture is the source of truth: edit it by hand, then import. Export is the
escape hatch for changes made through the Django admin — it overwrites the whole
fixture from the DB, so it asks for confirmation first.

Two rules the fixture has to honour, both enforced by the pre-flight validation:
primary keys are never recycled (a new row reusing a retired pk inherits the old
row's session state), and every reference resolves to a row that survives the
import.
"""

import io
import json
import sys
from pathlib import Path

from django.apps import apps
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

# The content models, in dependency order for safe loading.
CONTENT_MODELS = [
    "checklist.sop",
    "checklist.attribute",
    "checklist.procedure",
    "checklist.checkitem",
]

# Pruned in reverse dependency order so a delete never orphans a row that the
# same pass is about to remove anyway.
PRUNABLE_MODELS = [
    "checklist.checkitem",
    "checklist.procedure",
    "checklist.attribute",
]

# SOP is deliberately absent from PRUNABLE_MODELS: it carries content_version,
# which the plugin reads, and a fixture describes one SOP rather than the full set.

# Anchored to the app directory, not the working directory: deploy scripts run
# manage.py from wherever they happen to have cd'd to.
DEFAULT_FIXTURE = (
    Path(apps.get_app_config("checklist").path) / "fixtures" / "checklist_content.json"
)


def _get_model(label: str):
    return apps.get_model(*label.split("."))


def _fixture_pks(data) -> dict[str, list]:
    """Primary keys carried by the fixture, per model label."""
    pks: dict[str, list] = {}
    for obj in data:
        pks.setdefault(obj["model"], []).append(obj["pk"])
    return pks


def _fixture_slugs(data) -> list[str]:
    return [
        obj["fields"]["slug"]
        for obj in data
        if obj["model"] == "checklist.procedure" and obj["fields"].get("slug")
    ]


class Command(BaseCommand):
    help = (
        "Export or import checklist content data (SOP, Attribute, Procedure, CheckItem). "
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
            # --replace is the historical spelling, kept so a deploy config from
            # an older release keeps working. It no longer wipes anything.
            "--prune",
            "--replace",
            action="store_true",
            dest="prune",
            help=(
                "import only: after loading, delete the content rows the fixture "
                "no longer contains. Without it, extra rows in the database are "
                "left untouched."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            help="import only: validate and report what would change, then exit.",
        )
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Do not prompt for confirmation. Use in deploy scripts.",
        )

    def handle(self, *args, **options):
        action = options["action"]
        fixture_path = Path(options["fixture"])
        interactive = options["interactive"]

        if action == "export":
            self._export(fixture_path, interactive=interactive)
        else:
            self._import(
                fixture_path,
                prune=options["prune"],
                interactive=interactive,
                dry_run=options["dry_run"],
            )

    # ------------------------------------------------------------------ #
    #  Export                                                              #
    # ------------------------------------------------------------------ #

    def _export(self, fixture_path: Path, interactive: bool = True):
        fixture_path.parent.mkdir(parents=True, exist_ok=True)

        if interactive:
            self._confirm_export(fixture_path)

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

    def _import(
        self,
        fixture_path: Path,
        prune: bool,
        interactive: bool = True,
        dry_run: bool = False,
    ):
        if not fixture_path.exists():
            raise CommandError(
                f"Fixture not found: {fixture_path}\n"
                "Run 'manage.py checklist_content export' first."
            )

        with open(fixture_path, encoding="utf-8") as f:
            data = json.load(f)

        fixture_pks = _fixture_pks(data)
        fixture_slugs = _fixture_slugs(data)
        self._validate(data, fixture_pks, prune)

        self.stdout.write(f"Fixture: {fixture_path}")
        plan = self._plan(fixture_pks, prune)
        self._report_plan(plan)

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing was written."))
            return

        # Confirmation is asked before the transaction opens: prompting inside it
        # would hold the write lock open for as long as the operator takes to answer.
        if interactive and any(p["delete"] for p in plan.values()):
            self._confirm_prune(plan)

        self.stdout.write("Loading fixture …")
        try:
            # Everything mutating belongs INSIDE one transaction. With the old
            # wipe outside it, a failing load left the content tables empty while
            # this command reported "No changes were made".
            with transaction.atomic():
                # A slug freed by a pruned procedure has to be released before the
                # load, or a fixture row claiming it trips the unique constraint.
                released = (
                    self._release_reused_slugs(fixture_pks, fixture_slugs)
                    if prune
                    else {}
                )
                call_command("loaddata", str(fixture_path), verbosity=1)
                deleted = self._prune(fixture_pks) if prune else {}
        except Exception as exc:
            raise CommandError(
                f"Load failed: {exc}\n"
                "No changes were made (transaction rolled back)."
            ) from exc

        for label, count in sorted(deleted.items()):
            if count:
                self.stdout.write(f"  pruned {label}: {count} rows")
        for label, count in sorted(released.items()):
            if count:
                self.stdout.write(f"  replaced {label} (slug reused): {count} rows")

        self.stdout.write(self.style.SUCCESS("Checklist content loaded successfully."))

    # ------------------------------------------------------------------ #
    #  Import — validation, planning, pruning                              #
    # ------------------------------------------------------------------ #

    def _validate(self, data, fixture_pks, prune: bool):
        """
        Refuse a fixture that would corrupt content, before anything is written.

        Catches the failures that are silent or confusing at load time: a row
        without a pk (natural-key fixtures cannot be diffed), the same pk twice,
        two procedures fighting over one slug, and references to rows that either
        do not exist or are about to be pruned out from under the referrer.
        """
        errors = []

        for obj in data:
            if obj.get("pk") is None:
                errors.append(
                    f"{obj['model']}: a record has no pk — this command diffs on "
                    "primary keys, so natural-key fixtures cannot be imported."
                )
                break

        for model, pks in fixture_pks.items():
            duplicates = sorted({pk for pk in pks if pks.count(pk) > 1})
            if duplicates:
                errors.append(f"{model}: duplicate pk(s) in fixture: {duplicates}")

        slugs = {}
        for obj in data:
            if obj["model"] != "checklist.procedure":
                continue
            slug = obj["fields"].get("slug")
            if slug in slugs:
                errors.append(
                    f"checklist.procedure: slug {slug!r} used by pk {slugs[slug]} "
                    f"and pk {obj['pk']}"
                )
            slugs[slug] = obj["pk"]

        errors.extend(self._reference_errors(data, fixture_pks, prune))

        if not prune:
            # With --prune the holder is retired before the load (see
            # _release_reused_slugs). Without it, nothing may be deleted, so the
            # unique constraint would fail the load — say so up front instead.
            wanted = set(fixture_pks.get("checklist.procedure", []))
            held = (
                _get_model("checklist.procedure")
                .objects.exclude(pk__in=wanted)
                .filter(slug__in=slugs)
                .values_list("pk", "slug")
            )
            errors.extend(
                f"checklist.procedure: slug {slug!r} is held by existing pk {pk}, "
                f"which the fixture does not contain — re-run with --prune to retire it"
                for pk, slug in held
            )

        if errors:
            raise CommandError(
                "Fixture validation failed — nothing was written:\n  "
                + "\n  ".join(errors)
            )

    def _reference_errors(self, data, fixture_pks, prune: bool):
        """
        Every FK and M2M in the fixture must point at a row that exists once the
        import is done. Without --prune a row already in the database qualifies;
        with it, only rows the fixture itself carries do — anything else would be
        deleted moments later, silently dropping the link.
        """
        def resolvable(model_label):
            allowed = set(fixture_pks.get(model_label, []))
            if not prune or model_label not in PRUNABLE_MODELS:
                allowed |= set(_get_model(model_label).objects.values_list("pk", flat=True))
            return allowed

        procedures = resolvable("checklist.procedure")
        attributes = resolvable("checklist.attribute")
        sops = resolvable("checklist.sop")

        errors = []
        for obj in data:
            fields, pk = obj["fields"], obj["pk"]
            if obj["model"] == "checklist.procedure":
                if fields.get("sop") not in sops:
                    errors.append(
                        f"checklist.procedure pk {pk}: sop {fields.get('sop')!r} "
                        "does not exist"
                    )
            elif obj["model"] == "checklist.checkitem":
                if fields.get("procedure") not in procedures:
                    errors.append(
                        f"checklist.checkitem pk {pk}: procedure "
                        f"{fields.get('procedure')!r} is missing or would be pruned"
                    )
                for attr_id in fields.get("attributes", []):
                    if attr_id not in attributes:
                        errors.append(
                            f"checklist.checkitem pk {pk}: attribute {attr_id} "
                            "is missing or would be pruned"
                        )
            elif obj["model"] == "checklist.attribute":
                over_ruled_by = fields.get("over_ruled_by")
                if over_ruled_by is not None and over_ruled_by not in attributes:
                    errors.append(
                        f"checklist.attribute pk {pk}: over_ruled_by "
                        f"{over_ruled_by} is missing or would be pruned"
                    )
        return errors

    def _plan(self, fixture_pks, prune: bool):
        """Per-model counts of what the import will add, update and delete."""
        plan = {}
        for label in CONTENT_MODELS:
            model = _get_model(label)
            pks = set(fixture_pks.get(label, []))
            existing = set(model.objects.values_list("pk", flat=True))
            prunable = prune and label in PRUNABLE_MODELS
            plan[label] = {
                "add": len(pks - existing),
                "update": len(pks & existing),
                "delete": len(existing - pks) if prunable else 0,
            }
        return plan

    def _report_plan(self, plan):
        for label, counts in plan.items():
            line = (
                f"  {label}: +{counts['add']} new, "
                f"{counts['update']} updated, -{counts['delete']} deleted"
            )
            self.stdout.write(self.style.WARNING(line) if counts["delete"] else line)

    def _release_reused_slugs(self, fixture_pks, fixture_slugs):
        """
        Delete procedures whose slug a *different* fixture pk now claims.

        Procedure.slug is unique, so retiring a procedure and introducing a
        replacement under the same slug would otherwise fail the load on a
        constraint violation. Such a row is on its way out regardless; deleting it
        first is the same end state, just ordered so the load can succeed.
        """
        Procedure = _get_model("checklist.procedure")  # noqa: N806
        wanted = set(fixture_pks.get("checklist.procedure", []))
        collisions = Procedure.objects.exclude(pk__in=wanted).filter(
            slug__in=fixture_slugs
        )
        count = collisions.count()
        if count:
            collisions.delete()
        return {"checklist.procedure": count}

    def _prune(self, fixture_pks):
        """Delete the content rows the fixture no longer carries."""
        deleted = {}
        for label in PRUNABLE_MODELS:
            model = _get_model(label)
            pks = fixture_pks.get(label, [])
            stale = model.objects.exclude(pk__in=pks)
            deleted[label] = stale.count()
            if deleted[label]:
                stale.delete()
        return deleted

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _confirm_export(self, fixture_path: Path):
        """
        Ask before overwriting the fixture.

        The fixture is hand-edited and is the source of truth for checklist content.
        Export rewrites it wholesale from the DB, so anything in the fixture that is
        not currently in this database is lost.
        """
        if not fixture_path.exists():
            return

        self.stdout.write(
            self.style.WARNING(
                f"\nExport will OVERWRITE {fixture_path} with the current database "
                "contents.\nThe fixture is the source of truth — any hand-edits not "
                "imported into this DB will be lost.\nOnly do this to capture changes "
                "made through the Django admin.\n"
            )
        )
        answer = input("Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            self.stdout.write("Aborted.")
            sys.exit(0)

    def _confirm_prune(self, plan):
        """
        Ask before deleting content rows.

        Only deletions need confirming — the upsert half replaces nothing a user
        depends on. Deleting an Attribute cascades into every user's saved
        preferences; deleting a CheckItem cascades into in-flight progress. Both
        are the right thing to do when the content is genuinely gone, and both
        deserve a second look first.
        """
        lines = [
            f"  {label}: {counts['delete']} rows"
            for label, counts in plan.items()
            if counts["delete"]
        ]
        self.stdout.write(
            self.style.WARNING(
                "\nThe following content rows are not in the fixture and will be "
                "DELETED:\n" + "\n".join(lines) + "\n\nDeleting an attribute also "
                "removes it from every user's saved preferences; deleting a check "
                "item also removes it from any flight in progress.\n"
                "This cannot be undone.\n"
            )
        )
        answer = input("Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            self.stdout.write("Aborted.")
            sys.exit(0)
