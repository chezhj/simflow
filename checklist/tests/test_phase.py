"""Tests for checklist/phase.py — the authoritative phase state (ADR-003)."""

# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring

from django.test import TestCase

from checklist.models import (
    Attribute,
    FlightItemState,
    FlightSession,
    FlightSessionAttribute,
    Procedure,
)
from checklist.phase import OPTIONAL_ATTR, phase_state
from checklist.tests.testFactories import CheckItemFactory, SOPFactory

RULE = {"dataref": "sim/test/x", "op": "eq", "value": 1}


class _Base(TestCase):
    def setUp(self):
        self.sop = SOPFactory()
        self.procedure = Procedure.objects.create(
            title="Engine Start", step=1, slug="engine-start", sop=self.sop
        )
        self.session = FlightSession.objects.create(active_phase="engine-start")

    def _state(self, datarefs=None):
        return phase_state(self.session, self.procedure, datarefs or {})

    def _done(self, item):
        FlightItemState.objects.create(
            flight_session=self.session, checklist_item=item,
            status="checked", source="manual",
        )


class TestPhaseCompletion(_Base):

    def test_unchecked_item_blocks(self):
        item = CheckItemFactory(procedure=self.procedure, step=10)
        s = self._state()
        self.assertFalse(s["phase_complete"])
        self.assertEqual(s["blocking_item_ids"], [item.pk])

    def test_all_checked_completes(self):
        item = CheckItemFactory(procedure=self.procedure, step=10)
        self._done(item)
        s = self._state()
        self.assertTrue(s["phase_complete"])
        self.assertEqual(s["blocking_item_ids"], [])

    def test_gate_is_the_first_unchecked_item_in_step_order(self):
        first = CheckItemFactory(procedure=self.procedure, step=10)
        CheckItemFactory(procedure=self.procedure, step=20)
        self.assertEqual(self._state()["blocking_item_ids"], [first.pk])


class TestOptionalItems(_Base):

    def setUp(self):
        super().setUp()
        attr = Attribute.objects.create(title="Optional", order=1, pk=OPTIONAL_ATTR)
        # The attribute must be active, or the item is not visible at all and
        # would drop out of the sequence for the wrong reason.
        FlightSessionAttribute.objects.create(
            flight_session=self.session, attribute=attr, is_active=True
        )

    def _optional(self, step):
        item = CheckItemFactory(procedure=self.procedure, step=step)
        item.attributes.add(Attribute.objects.get(pk=OPTIONAL_ATTR))
        return item

    def test_optional_item_does_not_block_by_default(self):
        self._optional(step=10)
        self.assertTrue(self._state()["phase_complete"])

    def test_optional_item_blocks_under_require_all_visible(self):
        item = self._optional(step=10)
        self.session.require_all_visible = True
        self.session.save()
        s = self._state()
        self.assertFalse(s["phase_complete"])
        self.assertEqual(s["blocking_item_ids"], [item.pk])


class TestWarnItemsBlockCompletion(_Base):
    """
    Regression for the bug that skipped "Engine (n) stable / Starter cutoff"
    mid-start on two real flights: an unresolved warn item is hidden from the
    list but is still an unfinished safety step, so the phase is not complete.
    """

    def setUp(self):
        super().setUp()
        # attr 3 is CheckItem._INFO_ATTR; inactive => the item is a warn row
        self.info = Attribute.objects.create(title="NoActionNeed", order=1, pk=3)

    def _warn(self, step):
        item = CheckItemFactory(procedure=self.procedure, step=step, auto_check_rule=RULE)
        item.attributes.add(self.info)
        return item

    def test_unresolved_warn_item_blocks_completion(self):
        warn = self._warn(step=10)
        s = self._state({"sim/test/x": 0})          # rule failing
        self.assertFalse(s["phase_complete"])
        self.assertEqual(s["blocking_item_ids"], [warn.pk])
        self.assertEqual(s["active_warn_ids"], [warn.pk])

    def test_warn_item_blocks_even_when_its_rule_passes_but_it_is_unchecked(self):
        """Rule passing is not the same as resolved — the plugin still has to check it."""
        warn = self._warn(step=10)
        s = self._state({"sim/test/x": 1})          # rule holds
        self.assertFalse(s["phase_complete"])
        self.assertEqual(s["active_warn_ids"], [])  # nothing to warn about
        self.assertEqual(s["blocking_item_ids"], [warn.pk])

    def test_resolved_warn_item_completes_the_phase(self):
        warn = self._warn(step=10)
        self._done(warn)
        self.assertTrue(self._state({"sim/test/x": 0})["phase_complete"])

    def test_warn_item_after_the_gate_is_not_surfaced(self):
        CheckItemFactory(procedure=self.procedure, step=10)   # blocks first
        self._warn(step=20)
        self.assertEqual(self._state({"sim/test/x": 0})["active_warn_ids"], [])
