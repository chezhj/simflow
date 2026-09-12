# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring
"""
Tests for the checklist_content management command.

The import path runs unattended on every deploy (POST_MIGRATE_COMMANDS in
deploy/simflow_config.sh), so two things have to hold: a failure leaves the live
content tables exactly as it found them, and a success does not take user or
session data with it.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from checklist.models import (
    Attribute,
    CheckItem,
    FlightItemState,
    FlightSession,
    Procedure,
    SOP,
    UserAttributeDefault,
)

SHIPPED_FIXTURE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "checklist_content.json"
)


def _write_fixture(directory: Path, name: str, data) -> Path:
    path = directory / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _import(fixture_path, *flags):
    call_command(
        "checklist_content", "import", f"--fixture={fixture_path}", "--noinput", *flags
    )


class ContentCommandTestCase(TestCase):
    """Shared content: one SOP, one attribute, one procedure, one check item."""

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

    def _fixture_of_current_content(self, **overrides):
        """The fixture that describes exactly what setUp created."""
        procedure_fields = {
            "title": "Before Start",
            "step": 1,
            "slug": "before-start",
            "sop": self.sop.pk,
        }
        procedure_fields.update(overrides.get("procedure", {}))
        return [
            {
                "model": "checklist.attribute",
                "pk": self.attribute.pk,
                "fields": {"title": "Optional", "order": 1, "show": True},
            },
            {
                "model": "checklist.procedure",
                "pk": self.procedure.pk,
                "fields": procedure_fields,
            },
            {
                "model": "checklist.checkitem",
                "pk": self.item.pk,
                "fields": {
                    "item": "Doors",
                    "procedure": self.procedure.pk,
                    "step": 1,
                    "setting": "Close All",
                    "attributes": [self.attribute.pk],
                },
            },
        ]


class TestContentImportPreservesUserData(ContentCommandTestCase):
    """The regression this command was rewritten for."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("bob", password="x")
        self.preference = UserAttributeDefault.objects.create(
            user_profile=self.user.profile, attribute=self.attribute
        )
        self.flight = FlightSession.objects.create(user_profile=self.user.profile)
        self.progress = FlightItemState.objects.create(
            flight_session=self.flight, checklist_item=self.item, status="checked"
        )

    def test_import_keeps_saved_preferences(self):
        """Reloading the same content must not touch UserAttributeDefault."""
        with TemporaryDirectory() as tmp:
            _import(
                _write_fixture(
                    Path(tmp), "content.json", self._fixture_of_current_content()
                ),
                "--prune",
            )

        self.assertTrue(
            UserAttributeDefault.objects.filter(pk=self.preference.pk).exists()
        )

    def test_import_keeps_in_flight_progress(self):
        with TemporaryDirectory() as tmp:
            _import(
                _write_fixture(
                    Path(tmp), "content.json", self._fixture_of_current_content()
                ),
                "--prune",
            )

        self.assertTrue(FlightItemState.objects.filter(pk=self.progress.pk).exists())

    def test_edited_content_updates_rows_in_place(self):
        """An edited item keeps its pk, so session state stays attached to it."""
        data = self._fixture_of_current_content()
        data[2]["fields"]["setting"] = "Close And Lock"

        with TemporaryDirectory() as tmp:
            _import(_write_fixture(Path(tmp), "content.json", data), "--prune")

        self.item.refresh_from_db()
        self.assertEqual(self.item.setting, "Close And Lock")
        self.assertTrue(FlightItemState.objects.filter(pk=self.progress.pk).exists())

    def test_removing_an_attribute_drops_it_from_preferences(self):
        """A genuine deletion must still cascade — that is honest pruning."""
        data = [obj for obj in self._fixture_of_current_content()
                if obj["model"] != "checklist.attribute"]
        data[1]["fields"]["attributes"] = []

        with TemporaryDirectory() as tmp:
            _import(_write_fixture(Path(tmp), "content.json", data), "--prune")

        self.assertFalse(Attribute.objects.filter(pk=self.attribute.pk).exists())
        self.assertFalse(
            UserAttributeDefault.objects.filter(pk=self.preference.pk).exists()
        )

    def test_shipped_fixture_reimport_keeps_preferences_and_progress(self):
        """
        End-to-end on the real content that ships to production, in the shape a
        deploy actually takes: content is already loaded, a user has preferences
        and a flight in progress, and the same fixture is imported again.
        """
        _import(SHIPPED_FIXTURE, "--prune")

        attribute = Attribute.objects.get(title="Optional")
        item = CheckItem.objects.first()
        preference = UserAttributeDefault.objects.create(
            user_profile=self.user.profile, attribute=attribute
        )
        progress = FlightItemState.objects.create(
            flight_session=self.flight, checklist_item=item, status="checked"
        )
        item_count = CheckItem.objects.count()

        _import(SHIPPED_FIXTURE, "--prune")

        self.assertTrue(UserAttributeDefault.objects.filter(pk=preference.pk).exists())
        self.assertTrue(FlightItemState.objects.filter(pk=progress.pk).exists())
        self.assertEqual(CheckItem.objects.count(), item_count)


class TestContentImportPrune(ContentCommandTestCase):
    def test_prune_removes_rows_absent_from_fixture(self):
        extra = CheckItem.objects.create(
            item="Retired", procedure=self.procedure, step=2, setting="—"
        )

        with TemporaryDirectory() as tmp:
            _import(
                _write_fixture(
                    Path(tmp), "content.json", self._fixture_of_current_content()
                ),
                "--prune",
            )

        self.assertFalse(CheckItem.objects.filter(pk=extra.pk).exists())
        self.assertTrue(CheckItem.objects.filter(pk=self.item.pk).exists())

    def test_without_prune_extra_rows_are_left_alone(self):
        extra = CheckItem.objects.create(
            item="Local", procedure=self.procedure, step=2, setting="—"
        )

        with TemporaryDirectory() as tmp:
            _import(
                _write_fixture(
                    Path(tmp), "content.json", self._fixture_of_current_content()
                )
            )

        self.assertTrue(CheckItem.objects.filter(pk=extra.pk).exists())

    def test_replace_is_accepted_as_an_alias_for_prune(self):
        """A deploy config from an older release still works."""
        extra = CheckItem.objects.create(
            item="Retired", procedure=self.procedure, step=2, setting="—"
        )

        with TemporaryDirectory() as tmp:
            _import(
                _write_fixture(
                    Path(tmp), "content.json", self._fixture_of_current_content()
                ),
                "--replace",
            )

        self.assertFalse(CheckItem.objects.filter(pk=extra.pk).exists())

    def test_a_replacement_procedure_may_reuse_a_retired_slug(self):
        """Procedure.slug is unique — the retired row is released before the load."""
        data = self._fixture_of_current_content()
        data[1]["pk"] = self.procedure.pk + 100          # new procedure, same slug
        data[2]["fields"]["procedure"] = self.procedure.pk + 100

        with TemporaryDirectory() as tmp:
            _import(_write_fixture(Path(tmp), "content.json", data), "--prune")

        self.assertFalse(Procedure.objects.filter(pk=self.procedure.pk).exists())
        self.assertEqual(
            Procedure.objects.get(slug="before-start").pk, self.procedure.pk + 100
        )

    def test_dry_run_writes_nothing(self):
        extra = CheckItem.objects.create(
            item="Retired", procedure=self.procedure, step=2, setting="—"
        )
        data = self._fixture_of_current_content()
        data[2]["fields"]["setting"] = "Never Written"

        with TemporaryDirectory() as tmp:
            _import(
                _write_fixture(Path(tmp), "content.json", data), "--prune", "--dry-run"
            )

        self.item.refresh_from_db()
        self.assertEqual(self.item.setting, "Close All")
        self.assertTrue(CheckItem.objects.filter(pk=extra.pk).exists())


class TestContentImportValidation(ContentCommandTestCase):
    def _assert_refused(self, data):
        with TemporaryDirectory() as tmp:
            fixture = _write_fixture(Path(tmp), "bad.json", data)
            with self.assertRaises(CommandError):
                _import(fixture, "--prune")
        # Nothing written, nothing deleted.
        self.assertTrue(Attribute.objects.filter(pk=self.attribute.pk).exists())
        self.assertTrue(Procedure.objects.filter(pk=self.procedure.pk).exists())
        self.assertTrue(CheckItem.objects.filter(pk=self.item.pk).exists())

    def test_refuses_reference_to_a_pruned_attribute(self):
        data = [obj for obj in self._fixture_of_current_content()
                if obj["model"] != "checklist.attribute"]
        self._assert_refused(data)

    def test_refuses_reference_to_a_missing_procedure(self):
        data = self._fixture_of_current_content()
        data[2]["fields"]["procedure"] = 9999
        self._assert_refused(data)

    def test_refuses_duplicate_pks(self):
        data = self._fixture_of_current_content()
        data.append(dict(data[2]))
        self._assert_refused(data)

    def test_refuses_two_procedures_sharing_a_slug(self):
        data = self._fixture_of_current_content()
        clone = json.loads(json.dumps(data[1]))
        clone["pk"] = self.procedure.pk + 100
        data.append(clone)
        self._assert_refused(data)

    def test_refuses_a_record_without_a_pk(self):
        data = self._fixture_of_current_content()
        data[0]["pk"] = None
        self._assert_refused(data)

    def test_refuses_a_dangling_over_ruled_by(self):
        data = self._fixture_of_current_content()
        data[0]["fields"]["over_ruled_by"] = 9999
        self._assert_refused(data)


class TestContentImportAtomicity(ContentCommandTestCase):
    def test_failed_load_leaves_content_intact(self):
        """A load that fails must roll back everything, prune included."""
        data = self._fixture_of_current_content()
        # Passes validation, fails in the database: step is not nullable.
        data[2]["fields"]["step"] = None

        with TemporaryDirectory() as tmp:
            fixture = _write_fixture(Path(tmp), "broken.json", data)
            with self.assertRaises(CommandError):
                _import(fixture, "--prune")

        self.assertTrue(Attribute.objects.filter(pk=self.attribute.pk).exists())
        self.assertTrue(Procedure.objects.filter(pk=self.procedure.pk).exists())
        self.assertTrue(CheckItem.objects.filter(pk=self.item.pk).exists())

    def test_missing_fixture_raises_before_touching_content(self):
        with self.assertRaises(CommandError):
            _import("/nonexistent/checklist_content.json", "--prune")
        self.assertTrue(Procedure.objects.filter(pk=self.procedure.pk).exists())


class TestSlugReuseWithoutPrune(ContentCommandTestCase):
    def test_refuses_a_reused_slug_when_nothing_may_be_deleted(self):
        """Without --prune the holder cannot be retired, so say so before loading."""
        data = self._fixture_of_current_content()
        data[1]["pk"] = self.procedure.pk + 100
        data[2]["fields"]["procedure"] = self.procedure.pk + 100

        with TemporaryDirectory() as tmp:
            fixture = _write_fixture(Path(tmp), "content.json", data)
            with self.assertRaises(CommandError):
                _import(fixture)

        self.assertTrue(Procedure.objects.filter(pk=self.procedure.pk).exists())
