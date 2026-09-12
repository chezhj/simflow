# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring
"""
Tests for the checklist_content management command.

The import path is the one that runs unattended on every deploy
(POST_MIGRATE_COMMANDS in deploy/simflow_config.sh), so a failure there has to
leave the live content tables exactly as it found them.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from checklist.models import Attribute, CheckItem, Procedure, SOP


def _write_fixture(directory: Path, name: str, data) -> Path:
    path = directory / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestContentImportAtomicity(TestCase):
    def setUp(self):
        self.sop = SOP.objects.create(
            name="Test SOP", icao_code="TST", content_version="1.0.0"
        )
        self.attribute = Attribute.objects.create(title="Optional", order=1)
        self.procedure = Procedure.objects.create(
            title="Before Start", step=1, slug="before-start", sop=self.sop
        )
        self.item = CheckItem.objects.create(
            item="Doors", procedure=self.procedure, step=1, setting="Close All"
        )

    def test_failed_replace_import_leaves_content_intact(self):
        """A load that fails must roll the wipe back with it."""
        # Valid JSON, but the row references a procedure that does not exist —
        # loaddata accepts the file and fails on the FK.
        broken = [
            {
                "model": "checklist.checkitem",
                "pk": 9001,
                "fields": {
                    "item": "Orphan",
                    "procedure": 9999,
                    "step": 1,
                    "setting": "—",
                    "attributes": [],
                },
            }
        ]

        with TemporaryDirectory() as tmp:
            fixture = _write_fixture(Path(tmp), "broken.json", broken)
            with self.assertRaises(CommandError):
                call_command(
                    "checklist_content",
                    "import",
                    f"--fixture={fixture}",
                    "--replace",
                    "--noinput",
                )

        self.assertTrue(Attribute.objects.filter(pk=self.attribute.pk).exists())
        self.assertTrue(Procedure.objects.filter(pk=self.procedure.pk).exists())
        self.assertTrue(CheckItem.objects.filter(pk=self.item.pk).exists())
        self.assertFalse(CheckItem.objects.filter(pk=9001).exists())

    def test_successful_replace_import_swaps_content(self):
        """The happy path is unchanged: --replace still wipes and reloads."""
        replacement = [
            {
                "model": "checklist.attribute",
                "pk": 50,
                "fields": {"title": "Imported", "order": 1, "show": True},
            },
            {
                "model": "checklist.procedure",
                "pk": 60,
                "fields": {
                    "title": "Imported Procedure",
                    "step": 1,
                    "slug": "imported-procedure",
                    "sop": self.sop.pk,
                },
            },
            {
                "model": "checklist.checkitem",
                "pk": 70,
                "fields": {
                    "item": "Imported Item",
                    "procedure": 60,
                    "step": 1,
                    "setting": "SET",
                    "attributes": [50],
                },
            },
        ]

        with TemporaryDirectory() as tmp:
            fixture = _write_fixture(Path(tmp), "content.json", replacement)
            call_command(
                "checklist_content",
                "import",
                f"--fixture={fixture}",
                "--replace",
                "--noinput",
            )

        self.assertFalse(Procedure.objects.filter(pk=self.procedure.pk).exists())
        self.assertTrue(CheckItem.objects.filter(pk=70).exists())
        self.assertEqual(Attribute.objects.count(), 1)

    def test_missing_fixture_raises_before_touching_content(self):
        with self.assertRaises(CommandError):
            call_command(
                "checklist_content",
                "import",
                "--fixture=/nonexistent/checklist_content.json",
                "--replace",
                "--noinput",
            )
        self.assertTrue(Procedure.objects.filter(pk=self.procedure.pk).exists())
