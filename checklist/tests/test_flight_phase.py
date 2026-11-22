"""
Flight phase engine: scripted flights fed through ``step`` one tick a second.

No database and no sim. Time is the ``now`` argument, so a 60 s hold costs
nothing to test. A scenario is a list of ``(seconds, overrides)`` segments:
overrides are merged into the signals that persisted from the segment before.
"""

import pytest

from checklist import flight_phase as fp
from checklist.flight_phase import Phase, PhaseState, Signals, step

# ── Harness ───────────────────────────────────────────────────────────────────

PARKED = dict(
    ground_speed_kt=0,
    ias_kt=0,
    agl_ft=0,
    radio_alt_ft=0,
    altitude_ft=0,
    vs_fpm=0,
    mcp_alt_ft=0,
    cruise_alt_ft=0,
    parking_brake=1,
    n2_max_pct=0,
    thrust_min=0,
    on_runway=False,
    dist_to_dest_nm=250,
)


def run(segments, state=None, t0=1000.0, base=None):
    """Run segments at 1 Hz. Returns (final state, [Change], final time)."""
    state = state or PhaseState()
    current = dict(PARKED if base is None else base)
    now, changes = t0, []
    for seconds, overrides in segments:
        current.update(overrides)
        for _ in range(int(seconds)):
            state, change = step(state, Signals(**current), now)
            if change:
                changes.append(change)
            now += 1
    return state, changes, now


def phases(changes):
    return [c.to_phase for c in changes]


# A whole flight. Each segment is long enough for its hold time plus margin.
FLIGHT = [
    (5, {}),  # on stand, brake set
    (10, dict(parking_brake=0, ground_speed_kt=3, n2_max_pct=25)),  # pushback
    (10, dict(ground_speed_kt=10)),  # taxi
    (8, dict(on_runway=True, ias_kt=60, ground_speed_kt=60)),  # take-off roll
    (8, dict(on_runway=False, agl_ft=1500, vs_fpm=2000, altitude_ft=2000, ias_kt=200)),
    (
        70,
        dict(
            agl_ft=35000, vs_fpm=0, altitude_ft=36000, mcp_alt_ft=36000, cruise_alt_ft=36000
        ),
    ),
    (30, dict(vs_fpm=-1500, altitude_ft=30000, agl_ft=29000)),  # descent
    (8, dict(dist_to_dest_nm=15)),  # approach
    (4, dict(radio_alt_ft=30, agl_ft=100)),  # flare
    (10, dict(agl_ft=0, radio_alt_ft=0, vs_fpm=0, ground_speed_kt=120, ias_kt=120)),  # roll-out
    (10, dict(ground_speed_kt=20, ias_kt=20)),  # off the runway
    (10, dict(ground_speed_kt=0, ias_kt=0, parking_brake=1, n2_max_pct=0)),  # on block
]


# ── The whole flight ──────────────────────────────────────────────────────────


def test_full_flight_visits_every_phase_in_order():
    state, changes, _ = run(FLIGHT)
    assert phases(changes) == fp.PHASE_ORDER
    assert state.phase is Phase.ON_BLOCK


def test_each_change_names_the_rule_that_fired():
    _, changes, _ = run(FLIGHT)
    assert [c.rule for c in changes] == [
        "parking_brake_set",
        "movement",
        "gs_gt_8kt",
        "on_runway_ias_alive",
        "climbing_through_1000",
        "level_at_cruise",
        "vs_negative",
        "within_20nm_of_dest",
        "flare",
        "gs_lt_40kt",
        "brake_set_engines_off",
    ]


def test_reason_carries_the_values_and_the_hold():
    _, changes, _ = run(FLIGHT)
    taxi = next(c for c in changes if c.to_phase is Phase.TAXI_OUT)
    assert taxi.why == "ground speed 10 kt > 8 kt for 5 s"
    assert taxi.signals["ground_speed_kt"] == 10


def test_duration_of_the_phase_just_left_is_reported():
    _, changes, _ = run(FLIGHT)
    assert changes[0].duration_s is None  # nothing was left
    pushback = next(c for c in changes if c.to_phase is Phase.PUSHBACK)
    taxi = next(c for c in changes if c.to_phase is Phase.TAXI_OUT)
    assert taxi.duration_s == taxi.at - pushback.at


# ── Debounce ──────────────────────────────────────────────────────────────────


def test_condition_shorter_than_its_hold_does_not_fire():
    # Taxi needs 5 s above 8 kt; give it 4, then drop.
    state, changes, _ = run(
        [(5, {}), (10, dict(parking_brake=0, ground_speed_kt=3)), (4, dict(ground_speed_kt=10)),
         (6, dict(ground_speed_kt=3))]
    )
    assert state.phase is Phase.PUSHBACK
    assert Phase.TAXI_OUT not in phases(changes)


def test_a_flicker_restarts_the_hold():
    # 4 s above, 1 s below, 4 s above: never 5 in a row.
    state, _, _ = run(
        [(5, {}), (10, dict(parking_brake=0, ground_speed_kt=3)),
         (4, dict(ground_speed_kt=10)), (1, dict(ground_speed_kt=6)), (4, dict(ground_speed_kt=10))]
    )
    assert state.phase is Phase.PUSHBACK


def test_a_gap_between_ticks_restarts_every_hold():
    state, _, now = run([(5, {}), (10, dict(parking_brake=0, ground_speed_kt=3)),
                         (3, dict(ground_speed_kt=10))])
    assert state.phase is Phase.PUSHBACK
    # The sim vanishes for a minute, then comes back still showing 10 kt.
    # Those 60 s were not observed, so they must not count as "held".
    later = now + 60
    sig = Signals(**{**PARKED, "parking_brake": 0, "ground_speed_kt": 10})
    state, change = step(state, sig, later)
    assert change is None
    state, change = step(state, sig, later + 4)  # 4 s of observed time: still short
    assert change is None
    state, change = step(state, sig, later + 5)
    assert change and change.to_phase is Phase.TAXI_OUT


# ── Missing data never decides ────────────────────────────────────────────────


def test_unknown_signals_never_fire_a_transition():
    state = PhaseState(phase=Phase.PUSHBACK, since=0, last_tick=1)
    for t in range(2, 120):
        state, change = step(state, Signals(), float(t))
        assert change is None
    assert state.phase is Phase.PUSHBACK


def test_zero_distance_is_not_arrival():
    """An FMC with no route may read 0; that must not look like 'near the destination'."""
    state, changes, _ = run(
        [(5, {}), (10, dict(parking_brake=0, ground_speed_kt=3)), (10, dict(ground_speed_kt=10)),
         (8, dict(on_runway=True, ias_kt=60)),
         (8, dict(on_runway=False, agl_ft=1500, vs_fpm=2000, altitude_ft=2000)),
         (70, dict(agl_ft=35000, vs_fpm=0, altitude_ft=36000, mcp_alt_ft=36000, cruise_alt_ft=36000)),
         (30, dict(vs_fpm=-1500)), (20, dict(dist_to_dest_nm=0))]
    )
    assert state.phase is Phase.DESCENT


def test_descent_without_distance_still_reaches_landing():
    state, changes, _ = run(
        [(5, {}), (10, dict(parking_brake=0, ground_speed_kt=3)), (10, dict(ground_speed_kt=10)),
         (8, dict(on_runway=True, ias_kt=60)),
         (8, dict(on_runway=False, agl_ft=1500, vs_fpm=2000, altitude_ft=2000)),
         (70, dict(agl_ft=35000, vs_fpm=0, altitude_ft=36000, mcp_alt_ft=36000, cruise_alt_ft=36000)),
         (30, dict(vs_fpm=-1500, dist_to_dest_nm=None)),
         (5, dict(radio_alt_ft=30, agl_ft=100))]
    )
    assert state.phase is Phase.LANDING
    assert changes[-1].rule == "flare_from_descent"


# ── Take-off ──────────────────────────────────────────────────────────────────


def test_lining_up_on_the_runway_slowly_is_not_take_off():
    state, _, _ = run(
        [(5, {}), (10, dict(parking_brake=0, ground_speed_kt=3)), (10, dict(ground_speed_kt=10)),
         (30, dict(on_runway=True, ias_kt=15, ground_speed_kt=15))]
    )
    assert state.phase is Phase.TAXI_OUT


def test_taxiing_across_a_runway_is_not_take_off():
    state, _, _ = run(
        [(5, {}), (10, dict(parking_brake=0, ground_speed_kt=3)), (10, dict(ground_speed_kt=10)),
         (30, dict(on_runway=True, ias_kt=0, ground_speed_kt=10))]
    )
    assert state.phase is Phase.TAXI_OUT


def test_rejected_take_off_goes_back_to_taxi_out_and_can_try_again():
    state, changes, _ = run(
        [(5, {}), (10, dict(parking_brake=0, ground_speed_kt=3)), (10, dict(ground_speed_kt=10)),
         (6, dict(on_runway=True, ias_kt=70, ground_speed_kt=70)),
         (12, dict(ias_kt=10, ground_speed_kt=10)),  # stopped on the runway
         (6, dict(ias_kt=60, ground_speed_kt=60))]  # and goes again
    )
    assert phases(changes)[-3:] == [Phase.TAKEOFF, Phase.TAXI_OUT, Phase.TAKEOFF]
    assert changes[-2].rule == "rejected_takeoff"
    assert state.phase is Phase.TAKEOFF


def test_take_off_hysteresis_between_30_and_40_kt_holds_the_phase():
    state, _, _ = run(
        [(5, {}), (10, dict(parking_brake=0, ground_speed_kt=3)), (10, dict(ground_speed_kt=10)),
         (6, dict(on_runway=True, ias_kt=70, ground_speed_kt=70)),
         (20, dict(ias_kt=35, ground_speed_kt=35))]
    )
    assert state.phase is Phase.TAKEOFF


# ── Cruise, step climbs and short flights ─────────────────────────────────────

AT_CRUISE = dict(agl_ft=35000, vs_fpm=0, altitude_ft=36000, mcp_alt_ft=36000, cruise_alt_ft=36000)
DEPARTED = [
    (5, {}), (10, dict(parking_brake=0, ground_speed_kt=3)), (10, dict(ground_speed_kt=10)),
    (8, dict(on_runway=True, ias_kt=60)),
    (8, dict(on_runway=False, agl_ft=1500, vs_fpm=2000, altitude_ft=2000)),
]


def test_intermediate_level_off_is_not_cruise_when_a_cruise_level_is_planned():
    state, _, _ = run(
        DEPARTED + [(120, dict(agl_ft=9000, vs_fpm=0, altitude_ft=10000, mcp_alt_ft=10000,
                               cruise_alt_ft=36000))]
    )
    assert state.phase is Phase.CLIMB


def test_without_a_planned_cruise_level_a_long_level_off_counts():
    state, _, _ = run(
        DEPARTED + [(70, dict(agl_ft=9000, vs_fpm=0, altitude_ft=10000, mcp_alt_ft=10000,
                              cruise_alt_ft=0))]
    )
    assert state.phase is Phase.CRUISE


def test_level_for_less_than_a_minute_is_not_cruise():
    state, _, _ = run(DEPARTED + [(50, AT_CRUISE)])
    assert state.phase is Phase.CLIMB


def test_step_climb_goes_cruise_to_climb_and_back():
    state, changes, _ = run(
        DEPARTED
        + [(70, AT_CRUISE),
           (40, dict(vs_fpm=1000, altitude_ft=37000, agl_ft=36000)),
           (70, dict(vs_fpm=0, altitude_ft=38000, mcp_alt_ft=38000, cruise_alt_ft=38000))]
    )
    tail = phases(changes)[-3:]
    assert tail == [Phase.CRUISE, Phase.CLIMB, Phase.CRUISE]
    assert state.phase is Phase.CRUISE


def test_a_short_hop_can_descend_without_ever_cruising():
    state, changes, _ = run(DEPARTED + [(30, dict(vs_fpm=-1500, altitude_ft=9000, agl_ft=8000))])
    assert state.phase is Phase.DESCENT
    assert Phase.CRUISE not in phases(changes)
    assert changes[-1].rule == "climb_to_descent"


def test_turbulence_blip_does_not_start_a_descent():
    state, _, _ = run(
        DEPARTED + [(70, AT_CRUISE), (10, dict(vs_fpm=-1000)), (10, dict(vs_fpm=0))]
    )
    assert state.phase is Phase.CRUISE


# ── Go-around ─────────────────────────────────────────────────────────────────

ON_APPROACH = DEPARTED + [
    (70, AT_CRUISE),
    (30, dict(vs_fpm=-1500, altitude_ft=30000, agl_ft=29000)),
    (6, dict(dist_to_dest_nm=15)),
]


def test_go_around_from_approach_is_counted_once_and_does_not_retrigger():
    state, changes, _ = run(ON_APPROACH + [(30, dict(thrust_min=0.95, vs_fpm=1500))])
    assert state.phase is Phase.APPROACH
    assert state.go_arounds == 1
    assert [c.go_around for c in changes].count(True) == 1


def test_go_around_rearms_when_thrust_comes_back():
    state, _, _ = run(
        ON_APPROACH
        + [(10, dict(thrust_min=0.95, vs_fpm=1500)),
           (10, dict(thrust_min=0.5, vs_fpm=-700)),
           (10, dict(thrust_min=0.95, vs_fpm=1500))]
    )
    assert state.go_arounds == 2


def test_go_around_from_the_flare_returns_to_approach():
    state, changes, _ = run(
        ON_APPROACH
        + [(4, dict(radio_alt_ft=30, agl_ft=100)),
           (6, dict(thrust_min=0.95, vs_fpm=1500, radio_alt_ft=80, agl_ft=150))]
    )
    assert phases(changes)[-2:] == [Phase.LANDING, Phase.APPROACH]
    assert state.go_arounds == 1


def test_one_lever_up_is_not_a_go_around():
    state, _, _ = run(ON_APPROACH + [(20, dict(thrust_min=0.2, vs_fpm=1500))])
    assert state.go_arounds == 0


# ── Landing ───────────────────────────────────────────────────────────────────


def test_radio_altimeter_reading_zero_at_altitude_is_not_a_flare():
    """Out of range it may read 0. y_agl is still high, so nothing flares."""
    state, changes, _ = run(ON_APPROACH + [(20, dict(radio_alt_ft=0))])
    assert state.phase is Phase.APPROACH
    assert Phase.LANDING not in phases(changes)


def test_radio_altimeter_reading_zero_in_descent_does_not_skip_the_approach():
    state, _, _ = run(
        DEPARTED + [(70, AT_CRUISE), (30, dict(vs_fpm=-1500, altitude_ft=30000, agl_ft=29000, radio_alt_ft=0))]
    )
    assert state.phase is Phase.DESCENT


def test_landing_roll_out_stays_landing_until_below_40_knots():
    state, _, _ = run(
        ON_APPROACH
        + [(4, dict(radio_alt_ft=30, agl_ft=100)),
           (30, dict(agl_ft=0, radio_alt_ft=0, vs_fpm=0, ground_speed_kt=120))]
    )
    assert state.phase is Phase.LANDING


def test_engines_still_running_keep_taxi_in_going_even_with_the_brake_set():
    state, _, _ = run(
        FLIGHT[:-1] + [(10, dict(ground_speed_kt=0, ias_kt=0, parking_brake=1, n2_max_pct=30))]
    )
    assert state.phase is Phase.TAXI_IN


# ── Start and joining mid-flight ──────────────────────────────────────────────


def test_start_on_the_stand_needs_the_brake():
    state, _, _ = run([(10, dict(parking_brake=0))])
    assert state.phase is None
    state, changes, _ = run([(3, dict(parking_brake=1))])
    assert state.phase is Phase.STAND


def test_engines_before_pushback_still_start_pushback_and_engine_start():
    state, changes, _ = run([(5, {}), (6, dict(n2_max_pct=20))])
    assert state.phase is Phase.PUSHBACK
    assert changes[-1].rule == "engine_start"


@pytest.mark.parametrize(
    "overrides, expected",
    [
        (dict(parking_brake=0, agl_ft=8000, vs_fpm=2200, altitude_ft=9000), Phase.CLIMB),
        (dict(parking_brake=0, agl_ft=8000, vs_fpm=-1800, altitude_ft=9000), Phase.DESCENT),
        (dict(parking_brake=0, **AT_CRUISE), Phase.CRUISE),
        (dict(parking_brake=0, on_runway=True, ias_kt=90), Phase.TAKEOFF),
    ],
)
def test_joining_mid_flight_classifies_the_obvious_cases(overrides, expected):
    state, changes, _ = run([(10, overrides)])
    assert state.phase is expected
    assert changes[-1].from_phase is None


def test_joining_somewhere_ambiguous_stays_unknown():
    # Taxiing with no brake: taxi out or taxi in? Not obvious, so not guessed.
    state, changes, _ = run([(60, dict(parking_brake=0, ground_speed_kt=15))])
    assert state.phase is None and not changes


# ── State, description and persistence ────────────────────────────────────────


def test_state_survives_a_json_round_trip_mid_hold():
    state, _, now = run([(5, {}), (10, dict(parking_brake=0, ground_speed_kt=3)),
                         (3, dict(ground_speed_kt=10))])
    restored = PhaseState.from_dict(state.to_dict())
    assert restored == state
    # and the restored copy finishes the hold exactly as the original would
    sig = Signals(**{**PARKED, "parking_brake": 0, "ground_speed_kt": 10})
    _, change = step(restored, sig, now + 2)
    assert change and change.to_phase is Phase.TAXI_OUT


@pytest.mark.parametrize("junk", [None, "x", [], {"phase": "warp"}, {"phase": "cruise", "since": "no"},
                                  {"pending": {"a": "b"}}])
def test_damaged_state_means_a_fresh_start(junk):
    assert PhaseState.from_dict(junk) == PhaseState()


def test_describe_is_none_until_the_sim_has_been_seen():
    assert fp.describe(PhaseState()) is None


def test_describe_unknown_after_the_first_tick():
    state, _, _ = run([(3, dict(parking_brake=0, ground_speed_kt=15))])
    d = fp.describe(state)
    assert d["key"] is None and d["label"] == "Phase unknown" and d["index"] == 0


def test_describe_current_phase_and_what_it_waits_for():
    state, _, _ = run(FLIGHT[:6])
    d = fp.describe(state)
    assert d["key"] == "cruise" and d["index"] == 6 and d["total"] == 11
    assert d["group"] == "airborne"
    assert d["waiting_for"] == "Descent · V/S below -300 fpm"
    assert fp.describe(PhaseState(phase=Phase.ON_BLOCK, last_tick=1))["waiting_for"] is None


def test_phase_order_matches_the_whiteboard():
    assert [fp.PHASE_INFO[p].label for p in fp.PHASE_ORDER] == [
        "Stand", "Pushback & engine start", "Taxi out", "Take-off", "Climb", "Cruise",
        "Descent", "Approach", "Landing", "Taxi in", "On block",
    ]
    assert len(fp.PHASE_ORDER) == 11


def test_every_transition_names_unique_and_leads_somewhere_real():
    names = [t.name for t in fp.TRANSITIONS]
    assert len(names) == len(set(names))
    for t in fp.TRANSITIONS:
        assert isinstance(t.dst, Phase)
        assert all(s is None or isinstance(s, Phase) for s in t.src)


def test_log_line_shape():
    _, changes, _ = run(FLIGHT[:2])
    line = changes[-1].to_log()
    assert line["event"] == "flight_phase_change"
    assert (line["from"], line["to"], line["rule"]) == ("stand", "pushback", "movement")
    assert set(line) == {"event", "at", "from", "to", "rule", "why", "duration_s", "go_around", "signals"}


# ── Signal definitions ────────────────────────────────────────────────────────

B738 = {
    "ground_speed_kt": {"dataref": "sim/flightmodel/position/groundspeed", "scale": fp.MS_TO_KT},
    "ias_kt": {"dataref": "sim/flightmodel/position/indicated_airspeed"},
    "agl_ft": {"dataref": "sim/flightmodel/position/y_agl", "scale": fp.M_TO_FT},
    "radio_alt_ft": {"dataref": "sim/cockpit2/gauges/indicators/radio_altimeter_height_ft_pilot"},
    "altitude_ft": {"dataref": "sim/cockpit2/gauges/indicators/altitude_ft_pilot"},
    "vs_fpm": {"dataref": "sim/flightmodel/position/vh_ind_fpm"},
    "mcp_alt_ft": {"dataref": "laminar/B738/autopilot/mcp_alt_dial"},
    "cruise_alt_ft": {"dataref": "laminar/B738/autopilot/fmc_cruise_alt"},
    "parking_brake": {"dataref": "laminar/B738/parking_brake_pos"},
    "n2_max_pct": {
        "datarefs": ["laminar/B738/engine/indicators/N2_percent_1",
                     "laminar/B738/engine/indicators/N2_percent_2"],
        "combine": "max",
    },
    "thrust_min": {
        "datarefs": ["laminar/B738/engine/thrust1_leveler", "laminar/B738/engine/thrust2_leveler"],
        "combine": "min",
    },
    "on_runway": {
        "rule": {
            "op": "corridor",
            "ref_lat": "laminar/B738/fms/ref_runway_start_lat",
            "ref_lon": "laminar/B738/fms/ref_runway_start_lon",
            "crs": "laminar/B738/fms/ref_runway_crs",
            "ahead_m": 4000,
            "behind_m": 150,
            "half_width_m": 120,
        }
    },
    "dist_to_dest_nm": {"dataref": "laminar/B738/fms/dist_to_dest"},
}


def test_build_signals_converts_units():
    snap = {
        "sim/flightmodel/position/groundspeed": 10.0,  # m/s
        "sim/flightmodel/position/y_agl": 5.0,  # m
        "sim/flightmodel/position/indicated_airspeed": 123.0,
    }
    s = fp.build_signals(B738, snap)
    assert s.ground_speed_kt == pytest.approx(19.43844)
    assert s.agl_ft == pytest.approx(16.4042)
    assert s.ias_kt == 123.0


def test_build_signals_combines_engines():
    snap = {
        "laminar/B738/engine/indicators/N2_percent_1": 0.0,
        "laminar/B738/engine/indicators/N2_percent_2": 22.5,
        "laminar/B738/engine/thrust1_leveler": 0.9,
        "laminar/B738/engine/thrust2_leveler": 0.4,
    }
    s = fp.build_signals(B738, snap)
    assert s.n2_max_pct == 22.5  # one engine turning is "starting"
    assert s.thrust_min == 0.4  # both must be up for a go-around


def test_one_missing_engine_reading_makes_the_combined_signal_unknown():
    snap = {"laminar/B738/engine/indicators/N2_percent_1": 0.0}
    assert fp.build_signals(B738, snap).n2_max_pct is None


def test_missing_datarefs_and_non_numbers_are_none_not_errors():
    s = fp.build_signals(B738, {"sim/flightmodel/position/groundspeed": "n/a",
                                "sim/flightmodel/position/indicated_airspeed": True})
    assert s.ground_speed_kt is None and s.ias_kt is None and s.vs_fpm is None


def test_a_signal_with_no_definition_is_none():
    assert fp.build_signals({}, {"anything": 1}) == Signals()
    assert fp.build_signals(None, {}) == Signals()


def test_on_runway_uses_the_corridor_with_the_runway_course():
    # Runway 18R-ish, heading 184 true: aircraft 1 km down the runway on the centreline.
    lat0, lon0 = 52.30, 4.77
    base = {
        "laminar/B738/fms/ref_runway_start_lat": lat0,
        "laminar/B738/fms/ref_runway_start_lon": lon0,
        "laminar/B738/fms/ref_runway_crs": 180.0,
        "sim/flightmodel/position/psi": 180.0,
        "sim/flightmodel/position/mag_psi": 180.0,
    }
    on = {**base, "sim/flightmodel/position/latitude": lat0 - 0.009,  # ~1 km south
          "sim/flightmodel/position/longitude": lon0}
    off = {**base, "sim/flightmodel/position/latitude": lat0 - 0.009,
           "sim/flightmodel/position/longitude": lon0 + 0.01}  # ~680 m sideways
    behind = {**base, "sim/flightmodel/position/latitude": lat0 + 0.002,  # ~220 m before the start
              "sim/flightmodel/position/longitude": lon0}
    assert fp.build_signals(B738, on).on_runway is True
    assert fp.build_signals(B738, off).on_runway is False
    assert fp.build_signals(B738, behind).on_runway is False  # margin is 150 m


def test_unprogrammed_runway_reads_as_not_on_runway():
    s = fp.build_signals(B738, {"laminar/B738/fms/ref_runway_start_lat": 0.0,
                                "laminar/B738/fms/ref_runway_start_lon": 0.0})
    assert s.on_runway is False


def test_watched_datarefs_lists_every_path_once():
    paths = fp.watched_datarefs(B738)
    assert len(paths) == len(set(paths))
    for expected in (
        "sim/flightmodel/position/groundspeed",
        "laminar/B738/engine/indicators/N2_percent_2",
        "laminar/B738/fms/dist_to_dest",
        "laminar/B738/fms/ref_runway_crs",
        "sim/flightmodel/position/latitude",  # implicit dependency of the corridor
    ):
        assert expected in paths


def test_validate_accepts_the_b738_definitions():
    assert fp.validate_definitions(B738) == []


@pytest.mark.parametrize(
    "bad, fragment",
    [
        ("nope", "must be an object"),
        ({"warp_speed": {"dataref": "x"}}, "not a known signal"),
        ({"ias_kt": "x"}, "must be an object"),
        ({"ias_kt": {}}, "exactly one of"),
        ({"ias_kt": {"dataref": "x", "rule": {}}}, "exactly one of"),
        ({"ias_kt": {"rule": {"dataref": "x", "op": "gt", "value": 1}}}, "true/false"),
        ({"on_runway": {"dataref": "x"}}, "must be a rule"),
        ({"n2_max_pct": {"datarefs": ["a", "b"]}}, "combine"),
        ({"n2_max_pct": {"datarefs": [], "combine": "max"}}, "combine"),
        ({"ias_kt": {"dataref": "x", "scale": "2"}}, "scale"),
    ],
)
def test_validate_rejects_bad_definitions(bad, fragment):
    errors = fp.validate_definitions(bad)
    assert errors and any(fragment in e for e in errors)


# ── What the screens need ─────────────────────────────────────────────────────


def test_previous_phase_follows_the_flight():
    # replay by hand so the state can be read right after each change
    current, now, seen = PhaseState(), 1000.0, []
    base = dict(PARKED)
    for seconds, overrides in FLIGHT:
        base.update(overrides)
        for _ in range(seconds):
            before = current.phase
            current, change = step(current, Signals(**base), now)
            if change:
                seen.append((before, current.previous, current.phase))
            now += 1
    assert seen[0] == (None, None, Phase.STAND)
    assert all(prev == before for before, prev, _ in seen)
    assert seen[-1] == (Phase.TAXI_IN, Phase.TAXI_IN, Phase.ON_BLOCK)


def test_a_go_around_keeps_the_phase_before_the_approach_as_previous():
    state, _, _ = run(ON_APPROACH + [(30, dict(thrust_min=0.95, vs_fpm=1500))])
    assert state.phase is Phase.APPROACH
    assert state.previous is Phase.DESCENT


def test_describe_names_the_previous_phase():
    state, _, _ = run(FLIGHT[:6])
    assert fp.describe(state)["previous"] == "Climb"
    assert fp.describe(PhaseState(last_tick=1))["previous"] is None


def test_previous_survives_the_json_round_trip():
    state, _, _ = run(FLIGHT[:6])
    assert PhaseState.from_dict(state.to_dict()).previous is Phase.CLIMB


def test_a_state_saved_before_previous_existed_still_loads():
    old = {"phase": "cruise", "since": 5.0, "last_tick": 6.0, "pending": {}, "latched": [], "go_arounds": 0}
    assert PhaseState.from_dict(old).phase is Phase.CRUISE
    assert PhaseState.from_dict(old).previous is None


def test_steps_for_the_stepper():
    steps = fp.steps()
    assert [s["key"] for s in steps] == [p.value for p in fp.PHASE_ORDER]
    assert steps[1]["label"] == "Pushback & engine start" and steps[1]["short"] == "Pushback"


def test_group_captions_span_every_step_exactly_once():
    groups = fp.step_groups()
    assert [(g["group"], g["span"]) for g in groups] == [
        ("ground", 3), ("runway", 1), ("airborne", 4), ("runway", 1), ("ground", 2),
    ]
    assert sum(g["span"] for g in groups) == len(fp.PHASE_ORDER)
    assert [g["caption"] for g in groups] == ["Ground", "Rwy", "Airborne", "Rwy", "Ground"]
