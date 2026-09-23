"""
xFlow — X-Plane plugin
Phase 3: flight loop dataref monitoring + manual check-next command.

Installation:
  1. Copy PI_xFlow.py to:
       X-Plane 12/Resources/plugins/PythonPlugins/

  2. Create the folder:
       X-Plane 12/Resources/plugins/PythonPlugins/xFlow/

  3. Copy config.ini.example to config.ini in that folder:
       X-Plane 12/Resources/plugins/PythonPlugins/xFlow/config.ini

     The release zip ships config.ini.example rather than config.ini, so
     extracting an upgrade over an existing install cannot overwrite settings
     that are already there.

  Edit config.ini:
    api_key       — paste your key from the SimFlow profile page
    backend_url   — leave as-is for local dev; change for production
    log_level     — DEBUG / INFO / WARNING / ERROR (default: INFO)
                    DEBUG shows watch list contents, dataref values, raw responses
                    WARNING suppresses INFO but shows missing/broken datarefs
    poll_interval — seconds between dataref reads (default 0.5, range 0.1-4.0)

Commands registered:
  xFlow/check_next_item  — manually check the next checklist item
  xFlow/dump_watch       — log current watch list and dataref values (INFO)
  xFlow/report_miss      — report the first unchecked item as a rule miss
  Bind via X-Plane Settings → Keyboard or Joystick.
"""

from __future__ import annotations

import configparser
import json
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

_ARRAY_RE = re.compile(r"^(.+)\[(\d+)\]$")

try:
    from XPPython3 import xp
except ImportError:
    print("xp module not found")

try:
    import requests

    _USE_REQUESTS = True
except ImportError:
    _USE_REQUESTS = False

# ── Plugin identity & version ──────────────────────────────────────────────── #

PLUGIN_VERSION = "1.1.2"

plugin_name = "xFlow"
plugin_sig = "xppython3.xflow"
plugin_desc = "xFlow – X-Plane integration for SimFlow, the smart checklist"

# ── Command ────────────────────────────────────────────────────────────────── #

_COMMAND_FULL = "xFlow/check_next_item"
_COMMAND_DESC = "xFlow – Check next checklist item"

_DUMP_COMMAND_FULL = "xFlow/dump_watch"
_DUMP_COMMAND_DESC = "xFlow – Dump watch list and current dataref values to log"

_MISS_COMMAND_FULL = "xFlow/report_miss"
_MISS_COMMAND_DESC = "xFlow – Report rule miss for the first unchecked checklist item"

# ── Flight loop interval ───────────────────────────────────────────────────── #

# Seconds between flight-loop ticks. 0.5 halves the wait for a switch change
# to be noticed; the measured main-thread cost of a tick is ~1-2 ms in the
# largest phase, after 3.3 removed the per-tick type lookups.
_DEFAULT_LOOP_INTERVAL = 0.5
# Below this the loop re-reads the whole watch list absurdly often for no
# further gain in responsiveness.
_MIN_LOOP_INTERVAL = 0.1
# The server marks the sim disconnected when the last contact is 5 s old
# (`sim_connected = age < 5` in checklist/api_views.py). An interval at or
# above that would make the connection badge flap, so the ceiling sits below
# it with room for the request itself.
_MAX_LOOP_INTERVAL = 4.0

# Durations that used to be written directly as tick counts, which was only
# correct while a tick was exactly one second. They are converted with
# _ticks_for() against the configured interval.
_SESSION_RETRY_SECONDS = 30.0
_SESSION_REVALIDATE_SECONDS = 300.0

# ── HTTP worker ────────────────────────────────────────────────────────────── #
#
# Bounded so a stalled network cannot grow the backlog without limit. Every
# request carries timeout=5, so the worker can be blocked for at most that long
# and the queue drains again; 8 slots is several ticks of headroom without
# letting minutes of stale work accumulate.
_WORK_QUEUE_MAX = 8
# Slightly over the HTTP timeout, so a disable during an in-flight request
# waits for it rather than abandoning it.
_WORKER_JOIN_TIMEOUT = 6.0
# Seconds between repeats of the backlog warning. Rate-limited by time rather
# than by a flag: a queue oscillating between full and not-full resets a flag
# on every successful put, which logged at the tick rate instead of once.
_FULL_LOG_INTERVAL = 60.0

# ── Log levels ─────────────────────────────────────────────────────────────── #

_LEVELS = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}


def _ticks_for(seconds: float, interval: float) -> int:
    """
    Flight-loop ticks spanning `seconds`, at least one.

    The loop counts ticks, not time, so every duration has to be divided by
    the interval. Before the interval was configurable these divisions were
    written out as literals against a 1.0 s tick.
    """
    return max(1, round(seconds / interval))

# ── Config sentinel ────────────────────────────────────────────────────────── #
#
# PLACEHOLDER is the literal string that ships in config.ini.
# Guards against firing requests on an unconfigured install.

PLACEHOLDER = "paste-your-key-here"


# ── Command wrapper ────────────────────────────────────────────────────────── #


class CheckCommand:
    """
    Creates and registers a single X-Plane command.
    The callback receives the phase (0=begin, 1=continue, 2=end).
    The handler always returns 1 (pass-through) so other plugins and
    X-Plane itself can still process the same binding.
    """

    def __init__(self, name: str, description: str, callback):
        self._cmd = xp.createCommand(name, description)
        self._handler = self._on_command
        self._callback = callback
        xp.registerCommandHandler(self._cmd, self._handler, 1, 0)

    def _on_command(self, cmd, phase, ref):
        self._callback(phase)
        return 1  # always pass through

    def destroy(self):
        xp.unregisterCommandHandler(self._cmd, self._handler, 1, 0)


# ── Plugin ─────────────────────────────────────────────────────────────────── #


class PythonInterface:

    def __init__(self):
        config_path = (
            Path(xp.getSystemPath())
            / "Resources/plugins/PythonPlugins/xFlow/config.ini"
        )
        cfg = configparser.ConfigParser()
        cfg.read(config_path)
        self._api_key = cfg.get("xflow", "api_key", fallback=PLACEHOLDER)
        self._backend_url = cfg.get(
            "xflow", "backend_url", fallback="http://cortado:8300"
        )
        raw_level = cfg.get("xflow", "log_level", fallback="INFO").upper()
        self._log_level = _LEVELS.get(raw_level, _LEVELS["INFO"])

        # getfloat's fallback covers a missing key but not a malformed value —
        # it raises ValueError on one — and this runs during __init__, so an
        # unparseable entry would stop the plugin loading at all.
        try:
            raw_interval = cfg.getfloat(
                "xflow", "poll_interval", fallback=_DEFAULT_LOOP_INTERVAL
            )
        except ValueError:
            raw_interval = _DEFAULT_LOOP_INTERVAL
            self._bad_interval = cfg.get("xflow", "poll_interval", fallback="")
        else:
            self._bad_interval = ""
        self._loop_interval = min(
            max(raw_interval, _MIN_LOOP_INTERVAL), _MAX_LOOP_INTERVAL
        )
        self._configured_interval = raw_interval
        self._check_cmd = CheckCommand(
            _COMMAND_FULL, _COMMAND_DESC, self._on_check_next
        )
        self._dump_cmd = CheckCommand(
            _DUMP_COMMAND_FULL, _DUMP_COMMAND_DESC, self._on_dump_watch
        )
        self._miss_cmd = CheckCommand(
            _MISS_COMMAND_FULL, _MISS_COMMAND_DESC, self._on_report_miss
        )

        # Session state — populated by _fetch_session()
        self._session_id: int | None = None
        # Set to True when the server rejects this plugin version as too old.
        # The flight loop stops all activity until the plugin is updated.
        self._blocked: bool = False

        # Watch list: dataref path → cached XP DataRef handle
        # Populated from server responses; starts empty.
        self._watch: list[str] = []
        # path → (xp.findDataRef() result, XPLM type bitmask). The type is
        # cached with the handle rather than read per tick: a dataref's type is
        # fixed when it is registered, so getDataRefTypes was being called once
        # per watched path per tick to be told the same thing every time. The
        # two share a lifetime, so caching them together makes them impossible
        # to invalidate separately.
        self._drefs: dict[str, tuple] = {}
        # Paths already reported as unresolvable, so the warning is logged once
        # rather than every tick. Cleared whenever the watch list or session
        # changes, so a reloaded aircraft reports afresh.
        self._missing_drefs: set[str] = set()

        # Last sent values — used to detect changes before POSTing
        self._last_values: dict[str, float | str] = {}

        # Ticks since last session-fetch attempt (used to retry when no session)
        self._no_session_ticks: int = 0
        self._SESSION_RETRY_TICKS: int = _ticks_for(
            _SESSION_RETRY_SECONDS, self._loop_interval
        )

        # Ticks since last session re-validation (catches server-side session replacement)
        self._active_ticks: int = 0
        self._SESSION_REVALIDATE_TICKS: int = _ticks_for(
            _SESSION_REVALIDATE_SECONDS, self._loop_interval
        )

        # Exponential backoff for state POST network errors.
        # _post_error_count counts consecutive failures; _post_skip_ticks is the
        # number of flight-loop ticks to skip before spawning the next POST.
        # Schedule: 10s → 20s → 40s → 60s cap. Resets to 0 on first success.
        # Written by _post_state (background thread), read+decremented by
        # _flight_loop (XP main thread) — both under _lock.
        self._post_error_count: int = 0
        self._post_skip_ticks: int = 0

        # Serialise all HTTP work onto one daemon thread. Previously every
        # caller spawned its own, including the flight loop, which meant a
        # thread object allocated on X-Plane's main thread once per tick — and
        # twice as often once the loop runs at 2 Hz.
        self._lock = threading.Lock()
        self._work: queue.Queue = queue.Queue(maxsize=_WORK_QUEUE_MAX)
        self._worker: threading.Thread | None = None
        self._last_full_log = 0.0

        # State posts do not queue. The newest snapshot replaces whatever was
        # waiting, and at most one state job sits in _work at a time. Queuing
        # them FIFO meant a server slower than the tick rate put the plugin
        # permanently behind, POSTing snapshots seconds out of date — worse
        # than not posting at all, since the server acts on what it is sent.
        self._pending_state: dict | None = None
        self._state_queued = False

    # ── Background HTTP worker ─────────────────────────────────────────────── #

    def _submit(self, fn, *args) -> bool:
        """
        Hand work to the HTTP thread. Never blocks: this is called from the
        flight loop, where blocking would stall a frame.

        Returns False if the backlog is full and the work was dropped.

        State posts do not come through here directly — see _submit_state,
        which coalesces them so a slow server cannot build a backlog of stale
        snapshots.
        """
        try:
            self._work.put_nowait((fn, args))
        except queue.Full:
            now = time.monotonic()
            if now - self._last_full_log >= _FULL_LOG_INTERVAL:
                self._last_full_log = now
                self._log(
                    "WARNING",
                    "HTTP backlog full — dropping work until it drains "
                    f"(further reports suppressed for {int(_FULL_LOG_INTERVAL)}s)",
                )
            return False
        return True

    def _submit_state(self, snapshot: dict) -> None:
        """
        Offer the latest dataref snapshot to the HTTP thread.

        Replaces any snapshot not yet sent rather than queueing behind it, and
        keeps at most one state job in the queue. If the server is slower than
        the tick rate the effect is that intermediate samples are skipped and
        the one actually sent is always the newest — where a FIFO queue would
        have sent every sample, each progressively more out of date.
        """
        with self._lock:
            self._pending_state = snapshot
            if self._state_queued:
                return          # a job is already queued or running; it will
                                # pick up this snapshot instead of the old one
            self._state_queued = True

        if not self._submit(self._run_pending_state):
            with self._lock:
                self._state_queued = False

    def _run_pending_state(self) -> None:
        """Post whatever the newest snapshot is at the moment this runs."""
        with self._lock:
            snapshot, self._pending_state = self._pending_state, None
            self._state_queued = False
        if snapshot is not None:
            self._post_state(snapshot)

    def _worker_loop(self) -> None:
        """
        Run submitted work in order until the sentinel arrives.

        Every task is wrapped: an exception escaping here would end the thread
        and silently stop all plugin HTTP for the rest of the session, with the
        checklist simply never updating again and nothing to say why.
        """
        while True:
            item = self._work.get()
            if item is None:
                return
            fn, args = item
            try:
                fn(*args)
            except Exception as exc:  # pylint: disable=broad-except
                name = getattr(fn, "__name__", repr(fn))
                self._log("ERROR", f"background task {name} failed: {exc!r}")

    # ── Logging helper ─────────────────────────────────────────────────────── #

    def _log(self, level: str, msg: str) -> None:
        """Emit msg to X-Plane Log.txt only if level >= configured log_level."""
        if _LEVELS.get(level, 0) >= self._log_level:
            xp.log(f"[xFlow] {msg}")

    # ── XPPython3 lifecycle ────────────────────────────────────────────────── #

    def XPluginStart(self):
        return plugin_name, plugin_sig, plugin_desc

    def XPluginEnable(self):
        self._log("INFO", f"ready — backend: {self._backend_url}")
        if self._bad_interval:
            self._log(
                "WARNING",
                f"poll_interval {self._bad_interval!r} is not a number — "
                f"using {self._loop_interval}s",
            )
        elif self._configured_interval != self._loop_interval:
            self._log(
                "WARNING",
                f"poll_interval {self._configured_interval}s is outside "
                f"{_MIN_LOOP_INTERVAL}-{_MAX_LOOP_INTERVAL}s — "
                f"using {self._loop_interval}s",
            )
        self._log("INFO", f"flight loop interval: {self._loop_interval}s")
        # Register the flight loop callback
        xp.registerFlightLoopCallback(self._flight_loop, self._loop_interval, 0)

        # A fresh queue and thread on every enable: threads cannot be
        # restarted, and work left over from a previous enable is stale. The
        # coalescing flags go with it — a _state_queued left True by a disable
        # would refer to a job in the discarded queue, and every state post
        # after re-enable would be skipped as already-queued.
        self._work = queue.Queue(maxsize=_WORK_QUEUE_MAX)
        with self._lock:
            self._pending_state = None
            self._state_queued = False
        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="xFlow-http"
        )
        self._worker.start()

        # Kick off session discovery in the background so we don't block enable
        self._submit(self._fetch_session)
        return 1

    def XPluginDisable(self):
        xp.unregisterFlightLoopCallback(self._flight_loop, 0)
        worker, self._worker = self._worker, None
        if worker is not None:
            try:
                self._work.put_nowait(None)
            except queue.Full:
                # The thread is a daemon and will not outlive X-Plane, so a
                # full queue here is not worth draining to make room.
                pass
            worker.join(timeout=_WORKER_JOIN_TIMEOUT)

    def XPluginStop(self):
        self._check_cmd.destroy()
        self._dump_cmd.destroy()
        self._miss_cmd.destroy()

    # ── Flight loop ────────────────────────────────────────────────────────── #

    def _resolve_dref(self, path: str, lookup_name: str):
        """
        Return the cached (handle, type) for path, resolving it on first use.
        Returns None — having logged once, on the miss — if X-Plane does not
        know the dataref.
        """
        entry = self._drefs.get(path)
        if entry is None:
            dref = xp.findDataRef(lookup_name)
            if dref is None:
                # Retried on the next tick rather than cached: an aircraft that
                # loads after the plugin registers its datarefs late, and a
                # negative cache would mean never picking them up. But the
                # warning is emitted once per path — at 2 Hz an unresolvable
                # dataref would otherwise fill Log.txt at two lines a second
                # for the whole flight.
                if lookup_name not in self._missing_drefs:
                    self._missing_drefs.add(lookup_name)
                    self._log("WARNING", f"dataref not found: {lookup_name}")
                return None
            self._missing_drefs.discard(lookup_name)
            entry = (dref, xp.getDataRefTypes(dref))
            self._drefs[path] = entry
        return entry

    def _flight_loop(
        self, since_last: float, elapsed: float, counter: int, ref
    ) -> float:
        """
        Called by X-Plane at ~1 Hz. Reads the current watch list datarefs
        and POSTs to /api/plugin/state/. If the watch list is empty the POST
        still fires (with datarefs: {}) so the server can return the initial
        watch list and record last_plugin_contact (keeps the connection badge alive).
        """
        # Hard stop — server has rejected this plugin version.
        with self._lock:
            blocked = self._blocked
        if blocked:
            return self._loop_interval

        if self._session_id is None:
            self._no_session_ticks += 1
            if self._no_session_ticks >= self._SESSION_RETRY_TICKS:
                self._no_session_ticks = 0
                self._log("INFO", "no session — retrying session lookup")
                self._submit(self._fetch_session)
            return self._loop_interval

        self._no_session_ticks = 0
        self._active_ticks += 1
        if self._active_ticks >= self._SESSION_REVALIDATE_TICKS:
            self._active_ticks = 0
            self._log("INFO", "re-validating session with server")
            self._submit(self._fetch_session)

        state: dict[str, float | str] = {}
        changed = False

        for path in self._watch:
            m = _ARRAY_RE.match(path)
            if m:
                base, idx = m.group(1), int(m.group(2))
                entry = self._resolve_dref(path, base)
                if entry is None:
                    continue
                dref, dtype = entry

                if dtype & 16:
                    ibuf = [0] * (idx + 1)
                    n_read = xp.getDatavi(dref, ibuf, 0, idx + 1)
                    if idx >= n_read:
                        continue
                    val = ibuf[idx]
                elif dtype & 8:
                    buf = [0.0] * (idx + 1)
                    n_read = xp.getDatavf(dref, buf, 0, idx + 1)
                    if idx >= n_read:
                        continue
                    val = buf[idx]
                elif dtype & 1:
                    # Scalar int with [idx] notation — treat as bitmask bit.
                    scalar = xp.getDatai(dref)
                    val = int(bool(scalar & (1 << idx)))
                else:
                    self._log(
                        "WARNING", f"unhandled XPLM type {dtype} for {path} — skipping"
                    )
                    continue
            else:
                entry = self._resolve_dref(path, path)
                if entry is None:
                    continue
                dref, dtype = entry
                # XPLM type bits: Int=1, Float=2, Double=4, FloatArray=8,
                #                 IntArray=16, Data/string=32.
                # Priority: Double > Float > Int-only, so that datarefs typed as
                # both Int+Float (e.g. FMS lat/lon) use getDataf and keep precision.
                # getDataf returns 0.0 for purely-int datarefs, so Int is last.
                if dtype & 32:
                    val = xp.getDatas(dref, 0, 64).rstrip("\x00").strip()
                elif dtype & 4:
                    val = xp.getDatad(dref)
                elif dtype & 2:
                    val = xp.getDataf(dref)
                elif dtype & 1:
                    val = xp.getDatai(dref)
                else:
                    val = xp.getDataf(dref)

            state[path] = val
            if self._last_values.get(path) != val:
                changed = True

        self._last_values = state

        # Backoff: skip POST if a previous network error set a cooldown.
        with self._lock:
            if self._post_skip_ticks > 0:
                self._post_skip_ticks -= 1
                return self._loop_interval

        # Always POST — keeps last_plugin_contact fresh (heartbeat) and
        # bootstraps the watch list on first tick. Dataref data is small enough
        # that 1 POST/s to a local server is negligible.
        self._log("DEBUG", f"POSTing {len(state)} datarefs (changed={changed})")
        snapshot = dict(state)
        self._submit_state(snapshot)

        return self._loop_interval

    # ── Command handler ────────────────────────────────────────────────────── #

    def _on_check_next(self, phase: int):
        if phase != 0:  # 0=BEGIN (key down); ignore CONTINUE and END
            return
        self._submit(self._post_check_next)

    def _on_dump_watch(self, phase: int):
        if phase != 0:
            return
        with self._lock:
            watch = list(self._watch)
            values = dict(self._last_values)
            drefs = dict(self._drefs)
        _TYPE_LABELS = {
            1: "int",
            2: "float",
            3: "int+float",
            4: "double",
            8: "float[]",
            16: "int[]",
            32: "str",
        }
        self._log("INFO", f"=== watch dump: {len(watch)} dataref(s) ===")
        for path in watch:
            val = values.get(path, "<not yet read>")
            entry = drefs.get(path)
            if entry is not None:
                dtype = entry[1]
                type_label = _TYPE_LABELS.get(dtype, f"type={dtype}")
            else:
                type_label = "not found"
            self._log("INFO", f"  {path} = {val!r}  [{type_label}]")
        if not watch:
            self._log("INFO", "  (watch list is empty — no active session or no rules)")

    def _on_report_miss(self, phase: int):
        if phase != 0:
            return
        self._submit(self._post_report_miss)

    # ── HTTP workers (daemon threads) ──────────────────────────────────────── #

    def _post_report_miss(self) -> None:
        if not self._api_key or self._api_key == PLACEHOLDER:
            self._log("ERROR", "api_key not set — edit config.ini")
            return

        with self._lock:
            session_id = self._session_id
        if session_id is None:
            self._log("WARNING", "report_miss: no active session")
            return

        url = self._backend_url.rstrip("/") + "/api/plugin/report-miss/"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Plugin-Version": PLUGIN_VERSION,
        }

        self._log("DEBUG", f"POST {url}")
        try:
            status, data = self._http_post_json(url, headers, {"session_id": session_id})
        except Exception as exc:
            self._log("ERROR", _classify_error(exc))
            return

        self._log("DEBUG", f"report-miss response status {status}")

        if status == 200:
            self._log("INFO", f"rule miss reported (report_id={data.get('report_id')})")
        elif status == 204:
            self._log("INFO", "report_miss: procedure complete, nothing to report")
        elif status == 401:
            self._log("ERROR", "authentication failed — check api_key in config.ini")
        elif status == 404:
            self._log("INFO", "report_miss: no active session or phase")
        elif status == 422:
            self._log("WARNING", f"report_miss: {data.get('detail', 'no cached state')}")
        else:
            self._log("INFO", f"report_miss: unexpected status {status}")

    def _fetch_session(self) -> None:
        """
        GET /api/plugin/session/ — discover active session id on startup.
        Retries silently; the flight loop simply does nothing until session_id
        is set. Called again automatically when state POST returns 404.
        """
        if not self._api_key or self._api_key == PLACEHOLDER:
            self._log("ERROR", "api_key not set — edit config.ini")
            return

        url = self._backend_url.rstrip("/") + "/api/plugin/session/"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "X-Plugin-Version": PLUGIN_VERSION,
        }

        self._log("DEBUG", f"GET {url}")
        try:
            status, body = self._http_get(url, headers)
        except Exception as exc:
            self._log("ERROR", _classify_error(exc))
            return

        self._log("DEBUG", f"session response status {status}")

        if status == 200:
            plugin_status = body.get("plugin_status", "ok")

            if plugin_status == "blocked":
                self._log(
                    "ERROR",
                    f"plugin v{PLUGIN_VERSION} is too old — server has blocked it. "
                    f"Download the latest version: {body.get('update_url', '')}",
                )
                with self._lock:
                    self._blocked = True
                return

            if plugin_status == "warn":
                self._log(
                    "WARNING",
                    f"plugin v{PLUGIN_VERSION} is outdated — consider updating: "
                    f"{body.get('update_url', '')}",
                )

            new_id = body.get("session_id")
            with self._lock:
                self._blocked = False
                if new_id != self._session_id:
                    self._log(
                        "INFO",
                        f"session changed {self._session_id} → {new_id}, resetting watch list",
                    )
                    self._missing_drefs.clear()
                    self._session_id = new_id
                    self._watch = []
                    self._drefs = {}
                    self._last_values = {}
                else:
                    self._session_id = new_id

            aircraft = body.get("aircraft_type", "")
            content_ver = body.get("content_version", "")
            self._log(
                "INFO",
                f"connected — {aircraft} SOP v{content_ver} "
                f"| plugin v{PLUGIN_VERSION} | session {self._session_id} "
                f"| phase: {body.get('active_phase')}",
            )
        elif status == 404:
            self._log(
                "INFO", "no active session — waiting for pilot to start checklist"
            )
        elif status == 401:
            self._log("ERROR", "authentication failed — check api_key in config.ini")
        else:
            self._log("INFO", f"session lookup returned {status}")

    def _post_state(self, state: dict[str, float]) -> None:
        """
        POST /api/plugin/state/ with current dataref values.
        Updates the watch list from the server response.
        On 404, clears session_id so _fetch_session is retried next loop.
        """
        with self._lock:
            session_id = self._session_id

        if session_id is None:
            return

        url = self._backend_url.rstrip("/") + "/api/plugin/state/"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Plugin-Version": PLUGIN_VERSION,
        }
        body = {"session_id": session_id, "datarefs": state}

        self._log("DEBUG", f"POST {url}")
        started = time.monotonic()
        try:
            status, data = self._http_post_json(url, headers, body)
        except Exception as exc:
            with self._lock:
                self._post_error_count += 1
                n = self._post_error_count
                backoff = min(10 * (2 ** (n - 1)), 60)  # 10 → 20 → 40 → 60s cap
                self._post_skip_ticks = _ticks_for(backoff, self._loop_interval)
            self._log(
                "ERROR", f"{_classify_error(exc)} — retry in {backoff}s (error #{n})"
            )
            return

        # The round trip, so a backlog can be diagnosed from the log instead
        # of inferred. At 2 Hz anything approaching 500 ms means the worker
        # cannot keep up and snapshots are being coalesced away.
        self._log(
            "DEBUG",
            f"state response status {status} in "
            f"{(time.monotonic() - started) * 1000:.0f} ms",
        )

        if status == 200:
            newly_checked = data.get("checked", [])
            new_watch = data.get("watch", [])
            if newly_checked:
                self._log("DEBUG", f"auto-checked items: {newly_checked}")
            with self._lock:
                if self._post_error_count > 0:
                    self._log("INFO", "connection restored after network errors")
                    self._post_error_count = 0
                    self._post_skip_ticks = 0
                if new_watch != self._watch:
                    self._watch = new_watch
                    # Clear stale dref handles for paths no longer watched
                    self._drefs = {
                        p: v for p, v in self._drefs.items() if p in new_watch
                    }
                    self._missing_drefs.clear()
        elif status == 401:
            self._log("ERROR", "authentication failed — check api_key in config.ini")
        elif status == 404:
            self._log("INFO", "session expired — re-fetching session")
            with self._lock:
                self._session_id = None
            self._submit(self._fetch_session)
        else:
            self._log("INFO", f"unexpected status {status}")

    def _post_check_next(self) -> None:
        if not self._api_key or self._api_key == PLACEHOLDER:
            self._log("ERROR", "api_key not set — edit config.ini")
            return

        url = self._backend_url.rstrip("/") + "/api/plugin/check-next/"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "X-Plugin-Version": PLUGIN_VERSION,
        }

        self._log("DEBUG", f"POST {url}")

        try:
            status, _ = self._http_post_json(url, headers, None)
        except Exception as exc:
            self._log("ERROR", _classify_error(exc))
            return

        self._log("DEBUG", f"check-next response status {status}")

        if status == 200:
            pass  # success — visible at DEBUG via the line above
        elif status == 204:
            self._log("INFO", "phase complete — nothing left to check")
        elif status == 401:
            self._log("ERROR", "authentication failed — check api_key in config.ini")
        elif status == 404:
            self._log("INFO", "no active flight session")
        else:
            self._log("INFO", f"unexpected status {status}")

    # ── HTTP primitives ────────────────────────────────────────────────────── #

    @staticmethod
    def _http_get(url: str, headers: dict) -> tuple[int, dict]:
        """GET url. Returns (status_code, parsed_json_body)."""
        if _USE_REQUESTS:
            resp = requests.get(url, headers=headers, timeout=5)
            try:
                body = resp.json()
            except Exception:
                body = {}
            return resp.status_code, body

        req = urllib.request.Request(url, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                try:
                    body = json.loads(raw)
                except Exception:
                    body = {}
                return resp.status, body
        except urllib.error.HTTPError as exc:
            return exc.code, {}

    @staticmethod
    def _http_post_json(url: str, headers: dict, body) -> tuple[int, dict]:
        """
        POST url with optional JSON body.
        Returns (status_code, parsed_json_body).
        Raises on network/timeout errors.
        """
        if _USE_REQUESTS:
            resp = requests.post(url, headers=headers, json=body, timeout=5)
            try:
                data = resp.json()
            except Exception:
                data = {}
            return resp.status_code, data

        payload = json.dumps(body).encode() if body is not None else b""
        h = dict(headers)
        h.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, method="POST", headers=h, data=payload)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                try:
                    data = json.loads(raw)
                except Exception:
                    data = {}
                return resp.status, data
        except urllib.error.HTTPError as exc:
            return exc.code, {}


# ── Error classifier (module-level, no state needed) ──────────────────────── #


def _classify_error(exc: Exception) -> str:
    """
    Map a network exception to a human-readable log fragment.
    Works for both requests and urllib exceptions without importing
    requests-specific types when requests is unavailable.
    """
    exc_type = type(exc).__name__.lower()
    if "timeout" in exc_type or isinstance(exc, TimeoutError):
        return "request timed out"
    if isinstance(exc, OSError):
        return "could not reach backend"
    return f"request error: {exc}"
