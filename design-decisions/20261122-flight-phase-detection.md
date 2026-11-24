# ADR-004: Flight Phase Detection

**Date**: 2026-11-22
**Status**: Accepted (steps 1 to 3 built; approach leg trigger added 2026-11-23)
**Deciders**: h (project owner)

## Context

SimFlow should know which phase of flight the aircraft is in (stand, pushback,
taxi out, take-off, climb, cruise, descent, approach, landing, taxi in, on
block). First purely informational, later possibly to drive procedure logic.

"Phase" already means something else here: `phase.py`, `phase_state` and
`FlightSession.active_phase` are about how far the pilot has got through a
*checklist procedure* (ADR-002, ADR-003). The new concept needed a different name.

## Decision

1. **Named `flight_phase` everywhere.** Module `checklist/flight_phase.py`,
   field `FlightSession.flight_phase_state`, poll key `flight_phase`. Nothing in
   the new code is called `phase_state`, which `poll_view` already returns for
   checklist completion.
2. **A pure state machine.** `step(state, signals, now) -> (state, change)`. A table
   of transitions, each with a hold time that is its debounce. Missing data never
   fires a transition; a gap between ticks restarts every hold; the engine never
   sees a dataref path or an SI unit.
3. **Runs on the plugin tick, not the browser poll.** Every open tab polls every
   750 ms, so stepping there would run the machine several times a second and
   count debounce time wrongly. `plugin_state` is the one 1 Hz clock. The poll
   only reads (ADR-003: the browser renders, the server decides).
4. **State persisted on `FlightSession`**, in the same UPDATE that stores the
   snapshot, for the reason `last_datarefs` is: several Passenger workers.
5. **Aircraft-specific datarefs live in the fixture.** `SOP.phase_signals` maps each
   signal to `{dataref, scale}`, `{datarefs, combine}` or `{rule}` (an existing
   rule, so "on runway" is a `corridor`). It ships through the content import and
   needs a `content_version` bump like any content change. The import refuses a
   bad definition before writing. Thresholds and hold times stay in code: they
   describe flying, not the aircraft, and tests cover them.
6. **The plugin does not change.** It reads whatever the server's watch list
   names. Whole-array reads are not supported. An array entry whose index lives in
   another dataref is resolved on the server instead (decision 10).
   until needed.
7. **The record is a log, not a screen.** Each change appends a
   `flight_phase_change` line to `logs/session_<id>.jsonl` with the rule that
   fired, the values behind it, how long the phase just left lasted, and a
   snapshot of every signal. That is what tuning reads.
8. **Informational means failure-isolated.** An exception in the machine or the
   log write is logged and swallowed; it must not cost the pilot the auto-checks
   the same request performs.

9. **Two indicators, one script.** The phase shows in the connection bar on the pages that poll (`detail.html`, `idle.html`) and as a card on the in-flight hub. Under 900px it is a row flush below the bar; from 900px a pill inside it. Two elements rather than one that wraps, because adding `flex-wrap` to `.conn-bar` could push its existing items onto a second line at 320 to 360px, where they currently shrink. `flight-phase.js` fills both. The poll loops call it after the stale-response guard, so a late reply cannot paint an older phase. No time in phase is shown. A go-around is tagged only while still on the approach or in the flare. When the sim stops sending, the last phase stays, dimmed and tagged.

10. **Approach starts when the next waypoint is an approach leg.** In Descent, once
    the FMS leg type at `laminar/B738/fms/vnav_idx` is 7 for 3 s, the phase becomes
    Approach. `vnav_idx` is the index of the next waypoint (the one on top of the LEGS
    page) and indexes `fms_legs_rte_type` directly, with no shift. Verified in flight on
    2026-11-23: a route read 0 route, 1 SID, 2 STAR, 7 approach, and DK454, the IAF, was
    idx 15 and the first type 7. A type 8 sits between the STAR and the approach (idx 14);
    its meaning is not known, so it never counts. The first reading of the data assumed a
    one-leg shift and was wrong; the check with only the approach selected showed that
    type 7 appears only on approach legs.

    The type number is aircraft data, so the fixture holds it (`next_leg_is_approach`,
    with `in: [7]`) and the engine sees only true or false. The definition uses the
    notation `array[index/dataref]`. The plugin only reads fixed positions, so the server
    resolves it: the entry for the index in the current snapshot is looked up by its
    fixed name (a stale entry is never used for the wrong index), and the watch list asks
    the plugin for the one entry the latest snapshot's index points at. Cost: one tick in
    which the signal is unknown after each index change. No plugin change, one array
    entry on the wire instead of the whole array. `active_leg` (`vnav_idx`) is logged with
    every change so a log shows which leg was active.

    The distance fallback moved from 20 to 10 nm, and its rule is now `near_destination`
    (the old name encoded the number). Whichever condition holds first wins.

## Deviations from the whiteboard

All covered by tests in `test_flight_phase.py`.

- Take-off needs IAS above 40 kt, not above 0 (which also fires while lining up).
- Landing ends below 40 kt ground speed, not "above 0".
- Climb to Cruise needs 60 s level at the planned cruise level.
- Added: Cruise to Climb (step climb), Climb to Descent (short flights),
  Descent to Landing (safety net when no distance is available).
- A go-around is a counted event that must see thrust drop before it re-arms.
- A flare needs `y_agl` below 300 ft as well as radio altitude below 50 ft, in
  case the radio altimeter reads 0 out of range.
- `dist_to_dest` of 0 is ignored: an FMC with no route may read 0 (it did, at start-up, before a route was loaded).
- Descent to Approach fires on an approach leg (decision 10), with the distance as fallback, instead of on "reference waypoint passed".

## Out of scope for now

Showing time in phase, the strict form of the approach trigger (fire after the IAF
is passed, which needs the index of the first type-7 leg remembered), and handling
pause and replay.

## Consequences

- One migration (additive): `FlightSession.flight_phase_state`, `SOP.phase_signals`.
  The automatic rollback restores code, not the database, so dump the database
  before releasing it.
- `plugin_state` gains one query per tick (the SOP's signal definitions).
- An aircraft with no `phase_signals` has detection switched off: no state, no
  log, nothing added to the watch list, `flight_phase: null` in the poll.
- Thresholds are first guesses. Expect to tune them from real flights.
