"""
xFlow — X-Plane plugin
Phase 3: flight loop dataref monitoring + manual check-next command.

Installation:
  1. Copy PI_xFlow.py to:
       X-Plane 12/Resources/plugins/PythonPlugins/

  2. Create the folder:
       X-Plane 12/Resources/plugins/PythonPlugins/xFlow/

  3. Copy config.ini into that folder:
       X-Plane 12/Resources/plugins/PythonPlugins/xFlow/config.ini

  Edit config.ini:
    api_key     — paste your key from the xFlow profile page
    backend_url — leave as-is for local dev; change for production
    log_level   — DEBUG / INFO / WARNING / ERROR (default: INFO)
                  DEBUG shows watch list contents, dataref values, raw responses
                  WARNING suppresses INFO but shows missing/broken datarefs

Commands registered:
  xFlow/check_next_item  — manually check the next checklist item
  xFlow/dump_watch       — log current watch list and dataref values (INFO)
  xFlow/report_miss      — report the first unchecked item as a rule miss
  Bind via X-Plane Settings → Keyboard or Joystick.
"""

from __future__ import annotations

import configparser
import json
import re
import threading
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

PLUGIN_VERSION = "1.0.0"

plugin_name = "xFlow"
plugin_sig = "xppython3.xflow"
plugin_desc = "xFlow – X-Plane checklist integration"

# ── Command ────────────────────────────────────────────────────────────────── #

_COMMAND_FULL = "xFlow/check_next_item"
_COMMAND_DESC = "xFlow – Check next checklist item"

_DUMP_COMMAND_FULL = "xFlow/dump_watch"
_DUMP_COMMAND_DESC = "xFlow – Dump watch list and current dataref values to log"

_MISS_COMMAND_FULL = "xFlow/report_miss"
_MISS_COMMAND_DESC = "xFlow – Report rule miss for the first unchecked checklist item"

# ── Flight loop interval ───────────────────────────────────────────────────── #

_LOOP_INTERVAL = 1.0  # seconds; reschedule rate returned from flight loop

# ── Log levels ─────────────────────────────────────────────────────────────── #

_LEVELS = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}

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
        self._drefs: dict[str, object] = {}  # path → xp.findDataRef() result

        # Last sent values — used to detect changes before POSTing
        self._last_values: dict[str, float | str] = {}

        # Ticks since last session-fetch attempt (used to retry when no session)
        self._no_session_ticks: int = 0
        self._SESSION_RETRY_TICKS: int = 30  # retry every ~30 s when session_id is None

        # Ticks since last session re-validation (catches server-side session replacement)
        self._active_ticks: int = 0
        self._SESSION_REVALIDATE_TICKS: int = (
            300  # re-check every ~5 min when connected
        )

        # Exponential backoff for state POST network errors.
        # _post_error_count counts consecutive failures; _post_skip_ticks is the
        # number of flight-loop ticks to skip before spawning the next POST.
        # Schedule: 10s → 20s → 40s → 60s cap. Resets to 0 on first success.
        # Written by _post_state (background thread), read+decremented by
        # _flight_loop (XP main thread) — both under _lock.
        self._post_error_count: int = 0
        self._post_skip_ticks: int = 0

        # Serialise all HTTP work onto one daemon thread
        self._lock = threading.Lock()

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
        # Register the flight loop callback
        xp.registerFlightLoopCallback(self._flight_loop, _LOOP_INTERVAL, 0)
        # Kick off session discovery in the background so we don't block enable
        threading.Thread(target=self._fetch_session, daemon=True).start()
        return 1

    def XPluginDisable(self):
        xp.unregisterFlightLoopCallback(self._flight_loop, 0)

    def XPluginStop(self):
        self._check_cmd.destroy()
        self._dump_cmd.destroy()
        self._miss_cmd.destroy()

    # ── Flight loop ────────────────────────────────────────────────────────── #

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
            return _LOOP_INTERVAL

        if self._session_id is None:
            self._no_session_ticks += 1
            if self._no_session_ticks >= self._SESSION_RETRY_TICKS:
                self._no_session_ticks = 0
                self._log("INFO", "no session — retrying session lookup")
                threading.Thread(target=self._fetch_session, daemon=True).start()
            return _LOOP_INTERVAL

        self._no_session_ticks = 0
        self._active_ticks += 1
        if self._active_ticks >= self._SESSION_REVALIDATE_TICKS:
            self._active_ticks = 0
            self._log("INFO", "re-validating session with server")
            threading.Thread(target=self._fetch_session, daemon=True).start()

        state: dict[str, float | str] = {}
        changed = False

        for path in self._watch:
            m = _ARRAY_RE.match(path)
            if m:
                base, idx = m.group(1), int(m.group(2))
                dref = self._drefs.get(path)
                if dref is None:
                    dref = xp.findDataRef(base)
                    if dref is None:
                        self._log("WARNING", f"dataref not found: {base}")
                        continue
                    self._drefs[path] = dref

                dtype = xp.getDataRefTypes(dref)

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
                dref = self._drefs.get(path)
                if dref is None:
                    dref = xp.findDataRef(path)
                    if dref is None:
                        self._log("WARNING", f"dataref not found: {path}")
                        continue
                    self._drefs[path] = dref
                # XPLM type bits: Int=1, Float=2, Double=4, FloatArray=8,
                #                 IntArray=16, Data/string=32.
                # Priority: Double > Float > Int-only, so that datarefs typed as
                # both Int+Float (e.g. FMS lat/lon) use getDataf and keep precision.
                # getDataf returns 0.0 for purely-int datarefs, so Int is last.
                dtype = xp.getDataRefTypes(dref)
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
                return _LOOP_INTERVAL

        # Always POST — keeps last_plugin_contact fresh (heartbeat) and
        # bootstraps the watch list on first tick. Dataref data is small enough
        # that 1 POST/s to a local server is negligible.
        self._log("DEBUG", f"POSTing {len(state)} datarefs (changed={changed})")
        snapshot = dict(state)
        threading.Thread(target=self._post_state, args=(snapshot,), daemon=True).start()

        return _LOOP_INTERVAL

    # ── Command handler ────────────────────────────────────────────────────── #

    def _on_check_next(self, phase: int):
        if phase != 0:  # 0=BEGIN (key down); ignore CONTINUE and END
            return
        threading.Thread(target=self._post_check_next, daemon=True).start()

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
            dref = drefs.get(path)
            if dref is not None:
                dtype = xp.getDataRefTypes(dref)
                type_label = _TYPE_LABELS.get(dtype, f"type={dtype}")
            else:
                type_label = "not found"
            self._log("INFO", f"  {path} = {val!r}  [{type_label}]")
        if not watch:
            self._log("INFO", "  (watch list is empty — no active session or no rules)")

    def _on_report_miss(self, phase: int):
        if phase != 0:
            return
        threading.Thread(target=self._post_report_miss, daemon=True).start()

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
        try:
            status, data = self._http_post_json(url, headers, body)
        except Exception as exc:
            with self._lock:
                self._post_error_count += 1
                n = self._post_error_count
                backoff = min(10 * (2 ** (n - 1)), 60)  # 10 → 20 → 40 → 60s cap
                self._post_skip_ticks = int(backoff)
            self._log(
                "ERROR", f"{_classify_error(exc)} — retry in {backoff}s (error #{n})"
            )
            return

        self._log("DEBUG", f"state response status {status}")

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
        elif status == 401:
            self._log("ERROR", "authentication failed — check api_key in config.ini")
        elif status == 404:
            self._log("INFO", "session expired — re-fetching session")
            with self._lock:
                self._session_id = None
            threading.Thread(target=self._fetch_session, daemon=True).start()
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
