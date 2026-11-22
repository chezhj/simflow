"""
Flight phase detection: stand → pushback → taxi out → … → on block.

Not to be confused with ``phase.py``. There, "phase" means how far the pilot has
got through a *checklist procedure* (ADR-003). Here it means where the *aircraft*
is in its flight. The two never meet, which is why this module is named
``flight_phase`` and why nothing here is called ``phase_state``.

Three layers, each testable without the sim or a database:

* ``Signals``       normalised readings (knots, feet, fpm, booleans) for one tick.
* ``build_signals`` turns a dataref snapshot into ``Signals`` using per-aircraft
                    signal definitions. The definitions are data, so the
                    aircraft-specific dataref paths live in the content fixture,
                    never in this file.
* ``step``          the state machine: ``(state, signals, now) -> (state, change)``.

The machine runs on the plugin's 1 Hz tick, not on the browser poll: every open
tab polls, so stepping there would run it several times a second and count
debounce time wrongly. Each transition has a hold time, which is the debounce: its
condition must stay true that long before it fires.

Dataref paths and units are deliberately absent from the machine. X-Plane reports
``groundspeed`` in m/s and ``y_agl`` in metres; ``build_signals`` converts, and
everything below sees knots and feet.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from .rules import collect_datarefs, evaluate_rule

# ── Phases ────────────────────────────────────────────────────────────────────


class Phase(str, Enum):
    STAND = "stand"
    PUSHBACK = "pushback"
    TAXI_OUT = "taxi_out"
    TAKEOFF = "takeoff"
    CLIMB = "climb"
    CRUISE = "cruise"
    DESCENT = "descent"
    APPROACH = "approach"
    LANDING = "landing"
    TAXI_IN = "taxi_in"
    ON_BLOCK = "on_block"


GROUND, RUNWAY, AIRBORNE = "ground", "runway", "airborne"


@dataclass(frozen=True)
class PhaseInfo:
    label: str  # full name, for the hub card
    short: str  # for the connection bar
    group: str


PHASE_INFO: dict[Phase, PhaseInfo] = {
    Phase.STAND: PhaseInfo("Stand", "Stand", GROUND),
    Phase.PUSHBACK: PhaseInfo("Pushback & engine start", "Pushback", GROUND),
    Phase.TAXI_OUT: PhaseInfo("Taxi out", "Taxi out", GROUND),
    Phase.TAKEOFF: PhaseInfo("Take-off", "Take-off", RUNWAY),
    Phase.CLIMB: PhaseInfo("Climb", "Climb", AIRBORNE),
    Phase.CRUISE: PhaseInfo("Cruise", "Cruise", AIRBORNE),
    Phase.DESCENT: PhaseInfo("Descent", "Descent", AIRBORNE),
    Phase.APPROACH: PhaseInfo("Approach", "Approach", AIRBORNE),
    Phase.LANDING: PhaseInfo("Landing", "Landing", RUNWAY),
    Phase.TAXI_IN: PhaseInfo("Taxi in", "Taxi in", GROUND),
    Phase.ON_BLOCK: PhaseInfo("On block", "On block", GROUND),
}
PHASE_ORDER: list[Phase] = list(PHASE_INFO)

# ── Thresholds ────────────────────────────────────────────────────────────────
# First guesses, to be tuned from the session log. Named so a tuning change is
# one line and a test can refer to the same number.

M_TO_FT = 3.28084
MS_TO_KT = 1.943844

# "On the ground" is y_agl < 5 m, the same test the Before Takeoff rule uses. y_agl
# is the aircraft reference point, not the wheels, so it never reads 0 on the stand.
ON_GROUND_AGL_FT = 5 * M_TO_FT
# Obvious-case classification when a session starts airborne.
AIRBORNE_AGL_FT = 1000.0

MOVING_KT = 2.0  # Stand → Pushback
TAXI_KT = 8.0  # Pushback → Taxi out
ENGINE_RUNNING_N2 = 5.0  # either engine above this: starting
ENGINES_OFF_N2 = 10.0  # both engines below this: off
TAKEOFF_IAS_KT = 40.0  # whiteboard said "> 0", which also fires while lining up
REJECTED_IAS_KT = 30.0  # hysteresis below TAKEOFF_IAS_KT
CLIMB_AGL_FT = 1000.0
LEVEL_VS_FPM = 300.0  # |V/S| below this is level
LEVEL_ALT_TOLERANCE_FT = 100.0
STEP_CLIMB_FPM = 500.0
DESCENT_FPM = -300.0
APPROACH_NM = 20.0
FLARE_RADIO_ALT_FT = 50.0
# A radio altimeter can read 0 (or garbage) outside its range. y_agl is always
# valid, so a flare also needs the aircraft to be low by that measure.
FLARE_AGL_SANITY_FT = 300.0
THRUST_UP = 0.8  # levers are 0..1, both above this = go-around thrust
LANDING_ROLL_OUT_KT = 40.0

# A gap longer than this between ticks (sim paused, network, plugin restart)
# restarts every debounce: time that passed unobserved must not count as "held".
MAX_TICK_GAP_S = 5.0

# ── Signals ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Signals:
    """
    One tick of normalised readings. ``None`` means "not available", and a
    condition that needs a missing value is simply not met. Nothing here guesses.
    """

    ground_speed_kt: Optional[float] = None
    ias_kt: Optional[float] = None
    agl_ft: Optional[float] = None
    radio_alt_ft: Optional[float] = None
    altitude_ft: Optional[float] = None
    vs_fpm: Optional[float] = None
    mcp_alt_ft: Optional[float] = None
    cruise_alt_ft: Optional[float] = None  # planned cruise level from the FMC
    parking_brake: Optional[float] = None  # 1 = set
    n2_max_pct: Optional[float] = None  # the higher of the two engines
    thrust_min: Optional[float] = None  # the lower of the two levers, 0..1
    on_runway: Optional[bool] = None
    dist_to_dest_nm: Optional[float] = None


SIGNAL_NAMES = tuple(f.name for f in dataclasses.fields(Signals))
BOOL_SIGNALS = frozenset({"on_runway"})


def _gt(v, x):
    return v is not None and v > x


def _lt(v, x):
    return v is not None and v < x


def _ge(v, x):
    return v is not None and v >= x


def _le(v, x):
    return v is not None and v <= x


def _on_ground(s: Signals) -> bool:
    return _lt(s.agl_ft, ON_GROUND_AGL_FT)


def _flaring(s: Signals) -> bool:
    return _lt(s.radio_alt_ft, FLARE_RADIO_ALT_FT) and _lt(s.agl_ft, FLARE_AGL_SANITY_FT)


def _brake_set(s: Signals) -> bool:
    return _ge(s.parking_brake, 0.5)


def _level(s: Signals) -> bool:
    return s.vs_fpm is not None and abs(s.vs_fpm) < LEVEL_VS_FPM


def _at_cruise_level(s: Signals) -> bool:
    """
    Level at the selected altitude, and at the planned cruise level if the FMC
    has one. Without a planned level (short flights, nothing programmed) this
    falls back to "level at the MCP altitude", which an intermediate level-off
    can satisfy; that is the price of not requiring data that may not exist.
    """
    if s.altitude_ft is None or s.mcp_alt_ft is None or not _level(s):
        return False
    if abs(s.altitude_ft - s.mcp_alt_ft) > LEVEL_ALT_TOLERANCE_FT:
        return False
    if s.cruise_alt_ft is not None and s.cruise_alt_ft > 0:
        return s.altitude_ft >= s.cruise_alt_ft - LEVEL_ALT_TOLERANCE_FT
    return True


def _n(v, unit="", nd=1):
    """A reading for a reason string: '9.2 kt', or 'n/a'."""
    if v is None:
        return "n/a"
    text = f"{v:.{nd}f}".rstrip("0").rstrip(".") if nd else f"{v:.0f}"
    return f"{text} {unit}".strip()


# ── Transitions ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Transition:
    name: str  # logged as the rule that fired; keep stable
    src: frozenset  # Phase members, or None for "no phase yet"
    dst: Phase
    when: Callable[[Signals], bool]
    hold_s: float
    why: Callable[[Signals], str]  # the values behind the rule, in words
    waiting: str  # what this transition waits for, for the hub card
    forward: bool = True  # on the normal path (used for "what is next")
    go_around: bool = False


def _src(*phases):
    return frozenset(phases)


_START = None
_APPROACH_OR_LANDING = _src(Phase.APPROACH, Phase.LANDING)

TRANSITIONS: tuple[Transition, ...] = (
    # ── Start (and joining mid-flight) ───────────────────────────────────────
    Transition(
        "parking_brake_set",
        _src(_START),
        Phase.STAND,
        lambda s: _on_ground(s) and _brake_set(s) and _lt(s.ground_speed_kt, 1),
        0,
        lambda s: f"parking brake {_n(s.parking_brake)}, on ground, ground speed {_n(s.ground_speed_kt, 'kt')}",
        "parking brake set on the ground",
    ),
    Transition(
        "join_takeoff",
        _src(_START),
        Phase.TAKEOFF,
        lambda s: bool(s.on_runway) and _gt(s.ias_kt, TAKEOFF_IAS_KT),
        5,
        lambda s: f"joined on the runway, IAS {_n(s.ias_kt, 'kt')}",
        "",
        forward=False,
    ),
    Transition(
        "join_climb",
        _src(_START),
        Phase.CLIMB,
        lambda s: _gt(s.agl_ft, AIRBORNE_AGL_FT) and _gt(s.vs_fpm, STEP_CLIMB_FPM),
        5,
        lambda s: f"joined airborne, V/S {_n(s.vs_fpm, 'fpm', 0)}, {_n(s.agl_ft, 'ft AGL', 0)}",
        "",
        forward=False,
    ),
    Transition(
        "join_cruise",
        _src(_START),
        Phase.CRUISE,
        lambda s: _gt(s.agl_ft, AIRBORNE_AGL_FT) and _at_cruise_level(s),
        5,
        lambda s: f"joined level at {_n(s.altitude_ft, 'ft', 0)}, cruise level {_n(s.cruise_alt_ft, 'ft', 0)}",
        "",
        forward=False,
    ),
    Transition(
        "join_descent",
        _src(_START),
        Phase.DESCENT,
        lambda s: _gt(s.agl_ft, AIRBORNE_AGL_FT) and _lt(s.vs_fpm, DESCENT_FPM),
        5,
        lambda s: f"joined descending, V/S {_n(s.vs_fpm, 'fpm', 0)}, {_n(s.agl_ft, 'ft AGL', 0)}",
        "",
        forward=False,
    ),
    # ── Ground ───────────────────────────────────────────────────────────────
    Transition(
        "movement",
        _src(Phase.STAND),
        Phase.PUSHBACK,
        lambda s: _gt(s.ground_speed_kt, MOVING_KT),
        3,
        lambda s: f"ground speed {_n(s.ground_speed_kt, 'kt')} > {_n(MOVING_KT, 'kt')}",
        f"movement above {_n(MOVING_KT, 'kt')}, or an engine turning (N2 above {_n(ENGINE_RUNNING_N2, '%')})",
    ),
    Transition(
        "engine_start",
        _src(Phase.STAND),
        Phase.PUSHBACK,
        lambda s: _gt(s.n2_max_pct, ENGINE_RUNNING_N2),
        3,
        lambda s: f"N2 {_n(s.n2_max_pct, '%')} > {_n(ENGINE_RUNNING_N2, '%')}",
        "",
        forward=False,
    ),
    Transition(
        "gs_gt_8kt",
        _src(Phase.PUSHBACK),
        Phase.TAXI_OUT,
        lambda s: _gt(s.ground_speed_kt, TAXI_KT),
        5,
        lambda s: f"ground speed {_n(s.ground_speed_kt, 'kt')} > {_n(TAXI_KT, 'kt')}",
        f"ground speed above {_n(TAXI_KT, 'kt')}",
    ),
    Transition(
        "on_runway_ias_alive",
        _src(Phase.TAXI_OUT),
        Phase.TAKEOFF,
        lambda s: bool(s.on_runway) and _gt(s.ias_kt, TAKEOFF_IAS_KT),
        2,
        lambda s: f"inside the runway corridor, IAS {_n(s.ias_kt, 'kt')} > {_n(TAKEOFF_IAS_KT, 'kt')}",
        f"on the runway with IAS above {_n(TAKEOFF_IAS_KT, 'kt')}",
    ),
    Transition(
        "rejected_takeoff",
        _src(Phase.TAKEOFF),
        Phase.TAXI_OUT,
        lambda s: _on_ground(s) and _lt(s.ias_kt, REJECTED_IAS_KT),
        5,
        lambda s: f"on ground, IAS {_n(s.ias_kt, 'kt')} < {_n(REJECTED_IAS_KT, 'kt')}",
        "",
        forward=False,
    ),
    # ── Departure and climb ──────────────────────────────────────────────────
    Transition(
        "climbing_through_1000",
        _src(Phase.TAKEOFF),
        Phase.CLIMB,
        lambda s: _gt(s.vs_fpm, 0) and _gt(s.agl_ft, CLIMB_AGL_FT),
        3,
        lambda s: f"V/S {_n(s.vs_fpm, 'fpm', 0)} > 0 and {_n(s.agl_ft, 'ft AGL', 0)} > {_n(CLIMB_AGL_FT, 'ft', 0)}",
        f"climbing through {_n(CLIMB_AGL_FT, 'ft', 0)} AGL",
    ),
    Transition(
        "level_at_cruise",
        _src(Phase.CLIMB),
        Phase.CRUISE,
        _at_cruise_level,
        60,
        lambda s: (
            f"level (V/S {_n(s.vs_fpm, 'fpm', 0)}) at {_n(s.altitude_ft, 'ft', 0)}, "
            f"MCP {_n(s.mcp_alt_ft, 'ft', 0)}, cruise level {_n(s.cruise_alt_ft, 'ft', 0)}"
        ),
        "level at the cruise altitude",
    ),
    Transition(
        "step_climb",
        _src(Phase.CRUISE),
        Phase.CLIMB,
        lambda s: _gt(s.vs_fpm, STEP_CLIMB_FPM),
        30,
        lambda s: f"V/S {_n(s.vs_fpm, 'fpm', 0)} > {_n(STEP_CLIMB_FPM, 'fpm', 0)}",
        "",
        forward=False,
    ),
    # ── Descent and arrival ──────────────────────────────────────────────────
    Transition(
        "vs_negative",
        _src(Phase.CRUISE),
        Phase.DESCENT,
        lambda s: _lt(s.vs_fpm, DESCENT_FPM),
        20,
        lambda s: f"V/S {_n(s.vs_fpm, 'fpm', 0)} < {_n(DESCENT_FPM, 'fpm', 0)}",
        f"V/S below {_n(DESCENT_FPM, 'fpm', 0)}",
    ),
    Transition(
        # Short flights descend before they ever level at a cruise altitude.
        "climb_to_descent",
        _src(Phase.CLIMB),
        Phase.DESCENT,
        lambda s: _lt(s.vs_fpm, DESCENT_FPM),
        20,
        lambda s: f"V/S {_n(s.vs_fpm, 'fpm', 0)} < {_n(DESCENT_FPM, 'fpm', 0)} before reaching cruise",
        "",
        forward=False,
    ),
    Transition(
        # 0 is excluded: an FMC with no route loaded may read 0, and a real
        # distance never is until after touchdown.
        "within_20nm_of_dest",
        _src(Phase.DESCENT),
        Phase.APPROACH,
        lambda s: s.dist_to_dest_nm is not None and 0 < s.dist_to_dest_nm <= APPROACH_NM,
        3,
        lambda s: f"{_n(s.dist_to_dest_nm, 'nm')} from destination <= {_n(APPROACH_NM, 'nm', 0)}",
        f"within {_n(APPROACH_NM, 'nm', 0)} of the destination",
    ),
    Transition(
        "flare",
        _src(Phase.APPROACH),
        Phase.LANDING,
        _flaring,
        1,
        lambda s: f"radio altitude {_n(s.radio_alt_ft, 'ft', 0)} < {_n(FLARE_RADIO_ALT_FT, 'ft', 0)}",
        f"radio altitude below {_n(FLARE_RADIO_ALT_FT, 'ft', 0)}",
    ),
    Transition(
        # Safety net: no distance reading (or a route with no destination) must
        # not leave the phase stuck in Descent all the way to the runway.
        "flare_from_descent",
        _src(Phase.DESCENT),
        Phase.LANDING,
        _flaring,
        2,
        lambda s: f"radio altitude {_n(s.radio_alt_ft, 'ft', 0)} < {_n(FLARE_RADIO_ALT_FT, 'ft', 0)} without an approach phase",
        "",
        forward=False,
    ),
    Transition(
        # A self-transition when it fires from Approach: the phase does not
        # change, but the go-around is counted and logged.
        "go_around",
        _APPROACH_OR_LANDING,
        Phase.APPROACH,
        lambda s: _gt(s.thrust_min, THRUST_UP) and _gt(s.vs_fpm, 0),
        3,
        lambda s: f"both thrust levers {_n(s.thrust_min, '', 2)} > {THRUST_UP:g}, V/S {_n(s.vs_fpm, 'fpm', 0)} > 0",
        "",
        forward=False,
        go_around=True,
    ),
    Transition(
        "gs_lt_40kt",
        _src(Phase.LANDING),
        Phase.TAXI_IN,
        lambda s: _on_ground(s) and _lt(s.ground_speed_kt, LANDING_ROLL_OUT_KT),
        5,
        lambda s: f"on ground, ground speed {_n(s.ground_speed_kt, 'kt')} < {_n(LANDING_ROLL_OUT_KT, 'kt', 0)}",
        f"ground speed below {_n(LANDING_ROLL_OUT_KT, 'kt', 0)}",
    ),
    Transition(
        "brake_set_engines_off",
        _src(Phase.TAXI_IN),
        Phase.ON_BLOCK,
        lambda s: _brake_set(s) and _lt(s.n2_max_pct, ENGINES_OFF_N2),
        5,
        lambda s: f"parking brake {_n(s.parking_brake)}, N2 {_n(s.n2_max_pct, '%')} < {_n(ENGINES_OFF_N2, '%', 0)}",
        "parking brake set and engines off",
    ),
)

# ── State ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PhaseState:
    """Everything the machine remembers between ticks. JSON-serialisable."""

    phase: Optional[Phase] = None  # None: not classified yet
    since: float = 0.0  # when the current phase was entered
    last_tick: float = 0.0  # 0 until the first tick: nothing has been seen yet
    pending: dict = field(default_factory=dict)  # rule name -> first tick it was true
    latched: tuple = ()  # self-transitions that fired and must see False before re-arming
    go_arounds: int = 0

    def to_dict(self) -> dict:
        return {
            "phase": self.phase.value if self.phase else None,
            "since": self.since,
            "last_tick": self.last_tick,
            "pending": dict(self.pending),
            "latched": list(self.latched),
            "go_arounds": self.go_arounds,
        }

    @classmethod
    def from_dict(cls, data) -> "PhaseState":
        """Tolerant: a missing or damaged blob is the same as a fresh session."""
        if not isinstance(data, dict):
            return cls()
        try:
            phase = Phase(data["phase"]) if data.get("phase") else None
            pending = {
                str(k): float(v) for k, v in (data.get("pending") or {}).items()
            }
            return cls(
                phase=phase,
                since=float(data.get("since") or 0.0),
                last_tick=float(data.get("last_tick") or 0.0),
                pending=pending,
                latched=tuple(str(x) for x in (data.get("latched") or ())),
                go_arounds=int(data.get("go_arounds") or 0),
            )
        except (ValueError, TypeError, AttributeError):
            return cls()


@dataclass(frozen=True)
class Change:
    """One phase change (or counted go-around), as it is logged."""

    at: float
    from_phase: Optional[Phase]
    to_phase: Phase
    rule: str
    why: str  # the values behind the rule, in words
    duration_s: Optional[float]  # how long the phase just left lasted
    go_around: bool
    signals: dict

    def to_log(self) -> dict:
        """
        The line written to logs/session_<id>.jsonl, in the same shape as that
        file's other entries (``event``), minus ``ts``, which the caller adds.
        """
        return {
            "event": "flight_phase_change",
            "at": self.at,
            "from": self.from_phase.value if self.from_phase else None,
            "to": self.to_phase.value,
            "rule": self.rule,
            "why": self.why,
            "duration_s": None if self.duration_s is None else round(self.duration_s, 1),
            "go_around": self.go_around,
            "signals": self.signals,
        }


def step(
    state: PhaseState, signals: Signals, now: float
) -> tuple[PhaseState, Optional[Change]]:
    """
    Advance the machine by one tick. Pure: the same inputs give the same result.

    A transition fires once its condition has held for its hold time; at most
    one fires per tick, the first in table order. Conditions that need a missing
    value are not met, so a gap in the data never causes a transition.
    """
    gap = state.last_tick and (now - state.last_tick) > MAX_TICK_GAP_S
    carried = {} if gap else state.pending

    candidates = [t for t in TRANSITIONS if state.phase in t.src]
    latched = set(state.latched)
    pending: dict = {}
    fired: Optional[Transition] = None

    for t in candidates:
        holds = bool(t.when(signals))
        if t.name in latched:
            # Fired earlier and the condition has not let go since: not re-armed.
            if not holds:
                latched.discard(t.name)
            continue
        if not holds:
            continue
        pending[t.name] = carried.get(t.name, now)
        if fired is None and now - pending[t.name] >= t.hold_s:
            fired = t

    # Latches belong to the phase they fired in.
    still_latched = tuple(
        t.name for t in candidates if t.name in latched and t.when(signals)
    )

    if fired is None:
        return (
            dataclasses.replace(
                state, pending=pending, latched=still_latched, last_tick=now
            ),
            None,
        )

    held = now - pending[fired.name]
    why = fired.why(signals)
    if held > 0:
        why = f"{why} for {held:.0f} s"
    change = Change(
        at=now,
        from_phase=state.phase,
        to_phase=fired.dst,
        rule=fired.name,
        why=why,
        duration_s=(now - state.since) if state.phase is not None else None,
        go_around=fired.go_around,
        signals=dataclasses.asdict(signals),
    )
    new_state = PhaseState(
        phase=fired.dst,
        since=now,
        last_tick=now,
        pending={},
        latched=(fired.name,) if fired.dst == state.phase else (),
        go_arounds=state.go_arounds + (1 if fired.go_around else 0),
    )
    return new_state, change


# ── What the browser is shown ─────────────────────────────────────────────────


def waiting_for(phase: Optional[Phase]) -> Optional[str]:
    """What the next phase is waiting for, from the same table the machine runs."""
    for t in TRANSITIONS:
        if t.forward and phase in t.src and t.waiting and t.dst is not phase:
            return f"{PHASE_INFO[t.dst].label} · {t.waiting}"
    return None


def describe(state: PhaseState) -> Optional[dict]:
    """
    The ``flight_phase`` object for the poll response, or None until the sim has
    been seen at least once. The browser renders this and decides nothing.
    """
    if not state.last_tick:
        return None
    total = len(PHASE_ORDER)
    if state.phase is None:
        return {
            "key": None,
            "label": "Phase unknown",
            "short": "Unknown",
            "group": None,
            "index": 0,
            "total": total,
            "go_arounds": state.go_arounds,
            "waiting_for": None,
        }
    info = PHASE_INFO[state.phase]
    return {
        "key": state.phase.value,
        "label": info.label,
        "short": info.short,
        "group": info.group,
        "index": PHASE_ORDER.index(state.phase) + 1,
        "total": total,
        "go_arounds": state.go_arounds,
        "waiting_for": waiting_for(state.phase),
    }


# ── Signal definitions: dataref snapshot → Signals ────────────────────────────
#
# A definition says where one signal comes from, per aircraft. Two shapes:
#
#   {"dataref": "<path>", "scale": 1.943844}
#   {"datarefs": ["<path>", "<path>"], "combine": "max", "scale": 1}   (or "min")
#   {"rule": {...}}      a true/false signal built from the existing rule JSON,
#                        for example a corridor for "on runway"
#
# ``scale`` is optional and defaults to 1.


def _value(defn: dict, snapshot: dict) -> Optional[float]:
    paths = [defn["dataref"]] if "dataref" in defn else list(defn["datarefs"])
    values = []
    for path in paths:
        raw = snapshot.get(path)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None  # one missing input makes a combined signal unknown
        values.append(raw)
    if not values:
        return None
    combine = defn.get("combine")
    picked = max(values) if combine == "max" else min(values) if combine == "min" else values[0]
    return picked * defn.get("scale", 1)


def build_signals(definitions: dict, snapshot: dict) -> Signals:
    """
    Normalised ``Signals`` for one dataref snapshot. Signals with no definition,
    or whose datarefs are absent, are None. Never raises on a bad snapshot.
    """
    out: dict = {}
    for name in SIGNAL_NAMES:
        defn = (definitions or {}).get(name)
        if not isinstance(defn, dict):
            continue
        try:
            if "rule" in defn:
                out[name] = bool(evaluate_rule(defn["rule"], snapshot))
            else:
                out[name] = _value(defn, snapshot)
        except (KeyError, TypeError, ValueError):
            out[name] = None
    return Signals(**out)


def watched_datarefs(definitions: dict) -> list[str]:
    """Every dataref path the definitions read, for the plugin's watch list."""
    seen: list[str] = []
    for defn in (definitions or {}).values():
        if not isinstance(defn, dict):
            continue
        if "rule" in defn:
            paths = collect_datarefs(defn["rule"])
        elif "dataref" in defn:
            paths = [defn["dataref"]]
        else:
            paths = list(defn.get("datarefs", ()))
        seen.extend(p for p in paths if p not in seen)
    return seen


def validate_definitions(definitions) -> list[str]:
    """
    Problems with a set of signal definitions, as readable strings; empty when
    fine. For the content import to refuse a bad fixture before it is written.
    """
    if not isinstance(definitions, dict):
        return ["phase_signals must be an object"]
    errors = []
    for name, defn in definitions.items():
        if name not in SIGNAL_NAMES:
            errors.append(f"{name}: not a known signal ({', '.join(SIGNAL_NAMES)})")
            continue
        if not isinstance(defn, dict):
            errors.append(f"{name}: definition must be an object")
            continue
        shapes = [k for k in ("dataref", "datarefs", "rule") if k in defn]
        if len(shapes) != 1:
            errors.append(f"{name}: needs exactly one of dataref, datarefs or rule")
            continue
        if "rule" in defn and name not in BOOL_SIGNALS:
            errors.append(f"{name}: a rule gives true/false, which only {sorted(BOOL_SIGNALS)} accept")
        if "rule" not in defn and name in BOOL_SIGNALS:
            errors.append(f"{name}: must be a rule")
        if "datarefs" in defn:
            if not defn["datarefs"] or defn.get("combine") not in ("max", "min"):
                errors.append(f"{name}: datarefs needs a non-empty list and combine of max or min")
        scale = defn.get("scale", 1)
        if isinstance(scale, bool) or not isinstance(scale, (int, float)):
            errors.append(f"{name}: scale must be a number")
    return errors
