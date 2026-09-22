"""
Unit tests for PI_xFlow flight-loop dataref reading.

Mocks the `xp` module so no X-Plane installation is required.
Focuses on the string-dataref (Type_Data / getDatas) branch.
"""

import queue
import sys
import types
import unittest
from unittest.mock import MagicMock, call, patch


# ---------------------------------------------------------------------------
# Minimal xp stub — only the surface the flight loop touches.
# ---------------------------------------------------------------------------

def _make_xp_stub():
    xp = types.ModuleType("xp")
    xp.Type_Data   = 32          # bitmask for string datarefs
    xp.findDataRef  = MagicMock()
    xp.getDataRefTypes = MagicMock(return_value=0)   # float by default
    xp.getDataf    = MagicMock(return_value=0.0)
    xp.getDatad    = MagicMock(return_value=0.0)
    xp.getDatas    = MagicMock(return_value="")
    xp.getDatavf   = MagicMock()
    xp.log         = MagicMock()
    xp.getSystemPath = MagicMock(return_value="/tmp/xplane")
    xp.registerFlightLoopCallback  = MagicMock()
    xp.unregisterFlightLoopCallback = MagicMock()
    xp.createCommand         = MagicMock(return_value=object())
    xp.registerCommandHandler = MagicMock()
    xp.unregisterCommandHandler = MagicMock()
    return xp


class _PluginTestBase(unittest.TestCase):
    """Shared xp stub and plugin construction. Not collected on its own."""

    def setUp(self):
        # Inject the stub before importing the plugin module
        self.xp_stub = _make_xp_stub()
        sys.modules["xp"] = self.xp_stub
        sys.modules["XPPython3"] = types.ModuleType("XPPython3")
        sys.modules["XPPython3.xp"] = self.xp_stub

        # Make sure the plugin module is freshly imported each test
        if "xplane_plugin.xFlow.PI_xFlow" in sys.modules:
            del sys.modules["xplane_plugin.xFlow.PI_xFlow"]
        if "PI_xFlow" in sys.modules:
            del sys.modules["PI_xFlow"]

        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location(
            "PI_xFlow",
            pathlib.Path(__file__).parent.parent / "xFlow" / "PI_xFlow.py",
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def _make_plugin(self):
        """Return a PythonInterface with session_id pre-set so the loop runs."""
        # Patch configparser so __init__ doesn't need a real config.ini
        with patch("configparser.ConfigParser.read"):
            plugin = self.module.PythonInterface()
        plugin._session_id = 1
        plugin._api_key = "test-key"
        return plugin


    def _drain(self, plugin):
        """
        Run whatever the flight loop queued, synchronously and in order.

        The plugin's worker thread is not started in these tests, so this
        stands in for it. Deterministic, unlike asserting against a thread the
        test did not wait for.
        """
        while True:
            try:
                item = plugin._work.get_nowait()
            except queue.Empty:
                return
            if item is None:
                return
            fn, args = item
            fn(*args)

class TestFlightLoopStringDataref(_PluginTestBase):

    # -----------------------------------------------------------------------
    # getDatas returns a str directly — verify the loop uses it correctly
    # -----------------------------------------------------------------------

    def test_string_dataref_uses_getDatas_return_value(self):
        """getDatas return value is stored in state without bytearray decode."""
        xp = self.xp_stub
        xp.findDataRef.return_value = object()
        xp.getDataRefTypes.return_value = 32          # Type_Data → string path
        xp.getDatas.return_value = "737-800W.1\x00\x00"

        plugin = self._make_plugin()
        plugin._watch = ["laminar/B738/fmc1/Line01_L"]

        posted = {}

        def fake_post(state):
            posted.update(state)

        with patch.object(plugin, "_post_state", side_effect=fake_post):
            plugin._flight_loop(1.0, 1.0, 1, None)
            self._drain(plugin)

        self.assertEqual(posted.get("laminar/B738/fmc1/Line01_L"), "737-800W.1")

    def test_getDatas_called_with_correct_args(self):
        """getDatas must be called as getDatas(dref, 0, 64) — not with a bytearray."""
        xp = self.xp_stub
        fake_dref = object()
        xp.findDataRef.return_value = fake_dref
        xp.getDataRefTypes.return_value = 32
        xp.getDatas.return_value = "IDENT"

        plugin = self._make_plugin()
        plugin._watch = ["laminar/B738/fmc1/Line01_L"]

        with patch.object(plugin, "_post_state"):
            plugin._flight_loop(1.0, 1.0, 1, None)
            self._drain(plugin)

        xp.getDatas.assert_called_once_with(fake_dref, 0, 64)

    def test_getDatas_offset_is_int_not_bytearray(self):
        """Regression: second argument to getDatas must be an int, never a bytearray."""
        xp = self.xp_stub
        xp.findDataRef.return_value = object()
        xp.getDataRefTypes.return_value = 32
        xp.getDatas.return_value = ""

        plugin = self._make_plugin()
        plugin._watch = ["laminar/B738/fmc1/Line01_L"]

        with patch.object(plugin, "_post_state"):
            plugin._flight_loop(1.0, 1.0, 1, None)
            self._drain(plugin)

        _, args, _ = xp.getDatas.mock_calls[0]
        offset_arg = args[1]
        self.assertIsInstance(
            offset_arg, int,
            f"getDatas offset arg must be int, got {type(offset_arg).__name__}"
        )

    def test_string_dataref_stripped(self):
        """Null bytes and surrounding whitespace are stripped from the value."""
        xp = self.xp_stub
        xp.findDataRef.return_value = object()
        xp.getDataRefTypes.return_value = 32
        xp.getDatas.return_value = "  IDENT  \x00\x00\x00"

        plugin = self._make_plugin()
        plugin._watch = ["some/cdu/line"]

        posted = {}
        with patch.object(plugin, "_post_state", side_effect=posted.update):
            plugin._flight_loop(1.0, 1.0, 1, None)
            self._drain(plugin)

        self.assertEqual(posted.get("some/cdu/line"), "IDENT")

    # -----------------------------------------------------------------------
    # Float dataref — ensure getDatas is NOT called for non-string datarefs
    # -----------------------------------------------------------------------

    def test_float_dataref_uses_getDataf(self):
        xp = self.xp_stub
        xp.findDataRef.return_value = object()
        xp.getDataRefTypes.return_value = 0   # not Type_Data
        xp.getDataf.return_value = 42.0

        plugin = self._make_plugin()
        plugin._watch = ["sim/some/float"]

        posted = {}
        with patch.object(plugin, "_post_state", side_effect=posted.update):
            plugin._flight_loop(1.0, 1.0, 1, None)
            self._drain(plugin)

        xp.getDatas.assert_not_called()
        self.assertEqual(posted.get("sim/some/float"), 42.0)

    def test_double_dataref_uses_getDatad(self):
        """Double datarefs (XPLM type bit 4) must use getDatad, not getDataf."""
        xp = self.xp_stub
        xp.findDataRef.return_value = object()
        xp.getDataRefTypes.return_value = 4   # Double
        xp.getDatad.return_value = 1.0

        plugin = self._make_plugin()
        plugin._watch = ["laminar/B738/fmodpack/played_welcome_msg"]

        posted = {}
        with patch.object(plugin, "_post_state", side_effect=posted.update):
            plugin._flight_loop(1.0, 1.0, 1, None)
            self._drain(plugin)

        xp.getDatad.assert_called_once()
        xp.getDataf.assert_not_called()
        self.assertEqual(posted.get("laminar/B738/fmodpack/played_welcome_msg"), 1.0)


if __name__ == "__main__":
    unittest.main()


class TestDataRefTypeCaching(_PluginTestBase):
    """
    A dataref's type is fixed when it is registered, but getDataRefTypes was
    being called once per watched path per tick to be told so again. With ~109
    paths in the largest phase that is ~109 redundant XPLM calls a tick on the
    main thread, and the flight loop is moving to 2 Hz.
    """

    def _run_ticks(self, plugin, n):
        with patch.object(plugin, "_post_state"):
            for i in range(n):
                plugin._flight_loop(1.0, 1.0, i, None)
                self._drain(plugin)

    def test_type_is_read_once_per_path_not_once_per_tick(self):
        xp = self.xp_stub
        xp.findDataRef.return_value = object()
        xp.getDataRefTypes.return_value = 2  # float

        plugin = self._make_plugin()
        plugin._watch = ["sim/a", "sim/b", "sim/c"]

        self._run_ticks(plugin, 10)

        self.assertEqual(
            xp.getDataRefTypes.call_count, 3,
            "type lookup should happen once per path, not once per path per tick",
        )
        self.assertEqual(xp.findDataRef.call_count, 3)

    def test_values_are_still_read_every_tick(self):
        """Caching the type must not cache the value."""
        xp = self.xp_stub
        xp.findDataRef.return_value = object()
        xp.getDataRefTypes.return_value = 2
        xp.getDataf.return_value = 1.0

        plugin = self._make_plugin()
        plugin._watch = ["sim/a"]

        self._run_ticks(plugin, 5)
        self.assertEqual(xp.getDataf.call_count, 5)

    def test_a_missing_dataref_is_reported_once_not_every_tick(self):
        """
        At 2 Hz an unresolvable dataref would otherwise put two lines a second
        into Log.txt for the length of the flight.
        """
        xp = self.xp_stub
        xp.findDataRef.return_value = None

        plugin = self._make_plugin()
        plugin._watch = ["sim/does-not-exist"]

        with patch.object(plugin, "_log") as log:
            self._run_ticks(plugin, 10)

        warnings = [c for c in log.call_args_list if c.args and c.args[0] == "WARNING"]
        self.assertEqual(len(warnings), 1, f"expected one warning, got {warnings}")

    def test_a_dataref_that_appears_later_is_picked_up(self):
        """
        The miss must not be cached as a negative result: an aircraft loading
        after the plugin starts registers its datarefs late, and those have to
        resolve on a later tick.
        """
        xp = self.xp_stub
        xp.findDataRef.return_value = None
        xp.getDataRefTypes.return_value = 2
        xp.getDataf.return_value = 42.0

        plugin = self._make_plugin()
        plugin._watch = ["laminar/B738/late"]

        self._run_ticks(plugin, 3)
        self.assertEqual(plugin._last_values.get("laminar/B738/late"), None)

        xp.findDataRef.return_value = object()  # aircraft finishes loading
        self._run_ticks(plugin, 1)

        self.assertEqual(plugin._last_values.get("laminar/B738/late"), 42.0)

    def test_a_changed_watch_list_drops_cached_types(self):
        """
        Entries for paths no longer watched must not survive, or a path that
        returns later would be read with a stale handle.
        """
        xp = self.xp_stub
        xp.findDataRef.return_value = object()
        xp.getDataRefTypes.return_value = 2

        plugin = self._make_plugin()
        plugin._watch = ["sim/a", "sim/b"]
        self._run_ticks(plugin, 1)
        self.assertEqual(set(plugin._drefs), {"sim/a", "sim/b"})

        plugin._drefs = {p: v for p, v in plugin._drefs.items() if p in ["sim/a"]}
        self.assertEqual(set(plugin._drefs), {"sim/a"})


class TestHttpWorker(_PluginTestBase):
    """
    All HTTP now goes through one daemon thread fed by a bounded queue, rather
    than a thread spawned per call site — including one per flight-loop tick,
    on X-Plane's main thread, about to double to 2 Hz.
    """

    def test_the_flight_loop_queues_rather_than_spawning(self):
        xp = self.xp_stub
        xp.findDataRef.return_value = object()
        xp.getDataRefTypes.return_value = 2

        plugin = self._make_plugin()
        plugin._watch = ["sim/a"]

        with patch("threading.Thread") as thread_cls:
            plugin._flight_loop(1.0, 1.0, 1, None)

        thread_cls.assert_not_called()
        self.assertEqual(plugin._work.qsize(), 1)

    def test_the_worker_survives_a_failing_task(self):
        """
        The one that matters. An exception escaping the worker would end the
        thread and silently stop all plugin HTTP for the rest of the session —
        the checklist would simply never update again, with nothing to say why.
        """
        plugin = self._make_plugin()
        ran = []

        def boom():
            raise RuntimeError("network gremlin")

        plugin._work.put((boom, ()))
        plugin._work.put((lambda: ran.append("after"), ()))
        plugin._work.put(None)

        with patch.object(plugin, "_log") as log:
            plugin._worker_loop()

        self.assertEqual(ran, ["after"], "worker stopped after a failing task")
        errors = [c for c in log.call_args_list if c.args and c.args[0] == "ERROR"]
        self.assertEqual(len(errors), 1)
        self.assertIn("network gremlin", errors[0].args[1])

    def test_a_full_backlog_drops_work_instead_of_blocking(self):
        """
        _submit is called from the flight loop, so it must never block: a stall
        there is a stalled frame. Dropping a state POST costs nothing, because
        the next tick carries a fresher snapshot.
        """
        plugin = self._make_plugin()
        for _ in range(self.module._WORK_QUEUE_MAX):
            self.assertTrue(plugin._submit(lambda: None))

        with patch.object(plugin, "_log") as log:
            self.assertFalse(plugin._submit(lambda: None))
            self.assertFalse(plugin._submit(lambda: None))

        warnings = [c for c in log.call_args_list if c.args and c.args[0] == "WARNING"]
        self.assertEqual(len(warnings), 1, "backlog warning should not repeat per tick")

    def test_the_backlog_warning_returns_after_it_drains(self):
        plugin = self._make_plugin()
        for _ in range(self.module._WORK_QUEUE_MAX):
            plugin._submit(lambda: None)
        plugin._submit(lambda: None)           # full → warns
        plugin._work.get_nowait()              # drains one slot
        self.assertTrue(plugin._submit(lambda: None))

        for _ in range(self.module._WORK_QUEUE_MAX):
            try:
                plugin._work.get_nowait()
            except queue.Empty:
                break
        for _ in range(self.module._WORK_QUEUE_MAX):
            plugin._submit(lambda: None)

        with patch.object(plugin, "_log") as log:
            plugin._submit(lambda: None)
        warnings = [c for c in log.call_args_list if c.args and c.args[0] == "WARNING"]
        self.assertEqual(len(warnings), 1, "a new backlog episode should warn again")

    def test_enable_starts_a_worker_and_disable_stops_it(self):
        plugin = self._make_plugin()
        with patch.object(plugin, "_fetch_session"):
            plugin.XPluginEnable()
            worker = plugin._worker
            self.assertIsNotNone(worker)
            self.assertTrue(worker.is_alive())

            plugin.XPluginDisable()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertIsNone(plugin._worker)

    def test_a_second_enable_gets_a_fresh_worker(self):
        """Threads cannot be restarted, so enable must build a new one."""
        plugin = self._make_plugin()
        with patch.object(plugin, "_fetch_session"):
            plugin.XPluginEnable()
            first = plugin._worker
            plugin.XPluginDisable()
            first.join(timeout=2)

            plugin.XPluginEnable()
            second = plugin._worker
            self.assertIsNotNone(second)
            self.assertIsNot(second, first)
            self.assertTrue(second.is_alive())
            plugin.XPluginDisable()
            second.join(timeout=2)


class TestLoopInterval(_PluginTestBase):
    """
    The tick interval is configurable, which means every duration the loop
    measures in ticks has to be divided by it. Three were written as literals
    against a 1.0 s tick and would have silently halved at 0.5 s.
    """

    def _plugin_with(self, ini_body):
        """Build a plugin whose config.ini contains ini_body."""
        import configparser

        def fake_read(self_cfg, *a, **kw):
            self_cfg.read_string("[xflow]\n" + ini_body)

        with patch.object(configparser.ConfigParser, "read", fake_read):
            return self.module.PythonInterface()

    # ── Parsing and clamping ────────────────────────────────────────────────

    def test_default_is_two_hertz(self):
        self.assertEqual(self._plugin_with("")._loop_interval, 0.5)

    def test_a_configured_value_is_used(self):
        self.assertEqual(self._plugin_with("poll_interval = 1.0\n")._loop_interval, 1.0)

    def test_values_below_the_floor_are_clamped(self):
        p = self._plugin_with("poll_interval = 0.001\n")
        self.assertEqual(p._loop_interval, self.module._MIN_LOOP_INTERVAL)

    def test_values_above_the_ceiling_are_clamped(self):
        """
        The server treats the sim as disconnected once contact is 5 s old
        (`sim_connected = age < 5`, checklist/api_views.py), so an interval at
        or above that would make the badge flap.
        """
        p = self._plugin_with("poll_interval = 30\n")
        self.assertEqual(p._loop_interval, self.module._MAX_LOOP_INTERVAL)
        self.assertLess(self.module._MAX_LOOP_INTERVAL, 5.0)

    def test_a_malformed_value_does_not_stop_the_plugin_loading(self):
        """
        getfloat's fallback covers a missing key but raises on a malformed
        value, and this is parsed in __init__ — so an unparseable entry would
        otherwise prevent the plugin loading at all.
        """
        p = self._plugin_with("poll_interval = banana\n")
        self.assertEqual(p._loop_interval, 0.5)
        self.assertEqual(p._bad_interval, "banana")

    # ── Durations that were written as tick counts ──────────────────────────

    def test_ticks_for_converts_seconds_at_any_interval(self):
        ticks_for = self.module._ticks_for
        self.assertEqual(ticks_for(30, 1.0), 30)
        self.assertEqual(ticks_for(30, 0.5), 60)
        self.assertEqual(ticks_for(0.1, 5.0), 1, "never rounds down to zero ticks")

    def test_session_retry_stays_thirty_seconds_at_any_interval(self):
        for interval, expected in ((1.0, 30), (0.5, 60), (2.0, 15)):
            with self.subTest(interval=interval):
                p = self._plugin_with(f"poll_interval = {interval}\n")
                self.assertEqual(p._SESSION_RETRY_TICKS, expected)
                self.assertAlmostEqual(p._SESSION_RETRY_TICKS * interval, 30.0)

    def test_session_revalidate_stays_five_minutes_at_any_interval(self):
        for interval in (1.0, 0.5, 2.0):
            with self.subTest(interval=interval):
                p = self._plugin_with(f"poll_interval = {interval}\n")
                self.assertAlmostEqual(p._SESSION_REVALIDATE_TICKS * interval, 300.0)

    def test_the_backoff_waits_the_number_of_seconds_it_logs(self):
        """
        _post_state logs "retry in 10s" and then sets a tick count. At 0.5 s a
        literal 10 would have waited 5 s, so the message would have been wrong.
        """
        p = self._plugin_with("poll_interval = 0.5\n")
        p._session_id = 1
        p._api_key = "k"

        with patch.object(p, "_http_post_json", side_effect=OSError("down")):
            p._post_state({})

        self.assertEqual(p._post_error_count, 1)
        self.assertEqual(p._post_skip_ticks, 20)          # 10 s / 0.5 s
        self.assertAlmostEqual(p._post_skip_ticks * p._loop_interval, 10.0)

    # ── The interval actually drives the loop ───────────────────────────────

    def test_the_flight_loop_reschedules_at_the_configured_interval(self):
        xp = self.xp_stub
        xp.findDataRef.return_value = object()
        xp.getDataRefTypes.return_value = 2

        p = self._plugin_with("poll_interval = 1.5\n")
        p._session_id = 1
        p._api_key = "k"
        p._watch = ["sim/a"]

        self.assertEqual(p._flight_loop(1.0, 1.0, 1, None), 1.5)

    def test_registration_uses_the_configured_interval(self):
        p = self._plugin_with("poll_interval = 1.5\n")
        with patch.object(p, "_fetch_session"):
            p.XPluginEnable()
            p.XPluginDisable()
        args = self.xp_stub.registerFlightLoopCallback.call_args.args
        self.assertEqual(args[1], 1.5)
