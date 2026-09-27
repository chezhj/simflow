"""
The bounded gate cache in plugin_views.

`_last_gate_item` tracks the last gate item per flight session so a change can
be logged once rather than on every state POST. It was a plain dict keyed by
session id and never pruned, so it grew for the life of the worker process —
one entry per session that worker ever served.

Small (two ints an entry) and cleared by a restart, so this was housekeeping
rather than a leak that would ever bite. These tests pin the bound, and pin
that bounding it did not break the thing it exists to do.
"""

# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring

from django.test import TestCase

from checklist import plugin_views
from checklist.plugin_views import (
    _GATE_UNSEEN,
    _last_gate_item,
    _previous_gate,
    _remember_gate,
)


class _GateCacheBase(TestCase):
    def setUp(self):
        _last_gate_item.clear()

    def tearDown(self):
        _last_gate_item.clear()


class TestItStillDetectsGateChanges(_GateCacheBase):

    def test_an_unseen_session_reads_as_unseen(self):
        self.assertEqual(_previous_gate(1), _GATE_UNSEEN)

    def test_a_remembered_gate_reads_back(self):
        _remember_gate(1, 42)
        self.assertEqual(_previous_gate(1), 42)

    def test_none_is_a_real_value_distinct_from_unseen(self):
        """
        None means "no gate item in this phase", which is different from
        "this worker has not seen this session". Collapsing the two would
        either lose a gate_changed line or emit a spurious one.
        """
        _remember_gate(1, None)
        self.assertIsNone(_previous_gate(1))
        self.assertNotEqual(_previous_gate(1), _GATE_UNSEEN)

    def test_sessions_do_not_share_an_entry(self):
        _remember_gate(1, 10)
        _remember_gate(2, 20)
        self.assertEqual(_previous_gate(1), 10)
        self.assertEqual(_previous_gate(2), 20)


class TestItIsBounded(_GateCacheBase):

    def test_it_never_exceeds_the_cap(self):
        for pk in range(plugin_views._GATE_CACHE_MAX * 3):
            _remember_gate(pk, pk)
        self.assertEqual(len(_last_gate_item), plugin_views._GATE_CACHE_MAX)

    def test_the_oldest_entry_is_the_one_evicted(self):
        for pk in range(plugin_views._GATE_CACHE_MAX + 1):
            _remember_gate(pk, pk)

        self.assertEqual(_previous_gate(0), _GATE_UNSEEN)  # evicted
        self.assertEqual(
            _previous_gate(plugin_views._GATE_CACHE_MAX),
            plugin_views._GATE_CACHE_MAX,
        )

    def test_reading_a_session_keeps_it_alive(self):
        """
        Eviction is least-recently-USED, not least-recently-written: a session
        being polled every few seconds must not be evicted by a burst of new
        ones just because its gate has not moved.
        """
        _remember_gate(999, 1)
        for pk in range(plugin_views._GATE_CACHE_MAX - 1):
            _remember_gate(pk, pk)
            _previous_gate(999)          # keep touching it

        _remember_gate(100_000, 7)       # forces one eviction

        self.assertEqual(_previous_gate(999), 1)
        self.assertEqual(_previous_gate(0), _GATE_UNSEEN)

    def test_rewriting_a_session_does_not_grow_the_cache(self):
        for _ in range(50):
            _remember_gate(1, 5)
        self.assertEqual(len(_last_gate_item), 1)
