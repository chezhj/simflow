# ADR-004: Flight Phase Detection

**Date**: 2026-11-22
**Status**: Accepted (steps 1 to 3 of 4 built)
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
   names. Whole-array reads (FMS legs) are not supported and are out of scope
   until needed.
7. **The record is a log, not a screen.** Each change appends a
   `flight_phase_change` line to `logs/session_<id>.jsonl` with the rule that
   fired, the values behind it, how long the phase just left lasted, and a
   snapshot of every signal. That is what tuning reads.
8. **Informational means failure-isolated.** An exception in the machine or the
   log write is logged and swallowed; it must not cost the pilot the auto-checks
   the same request performs.

9. **Two indicators, one script.** The phase shows in the connection bar on the pages that poll (`detail.html`, `idle.html`) and as a card on the in-flight hub. Under 900px it is a row flush below the bar; from 900px a pill inside it. Two elements rather than one that wraps, because adding `flex-wrap` to `.conn-bar` could push its existing items onto a second line at 320 to 360px, where they currently shrink. `flight-phase.js` fills both. The poll loops call it after the stale-response guard, so a late reply cannot paint an older phase. No time in phase is shown. A go-around is tagged only while still on the approach or in the flare. When the sim stops sending, the last phase stays, dimmed and tagged.

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
- `dist_to_dest` of 0 is ignored: an FMC with no route may read 0.

## Out of scope for now

Showing time in phase, the reference-waypoint trigger for Descent to Approach
(needs the FMS legs datarefs and the active leg index), and handling pause and
replay.

## Consequences

- One migration (additive): `FlightSession.flight_phase_state`, `SOP.phase_signals`.
  The automatic rollback restores code, not the database, so dump the database
  before releasing it.
- `plugin_state` gains one query per tick (the SOP's signal definitions).
- An aircraft with no `phase_signals` has detection switched off: no state, no
  log, nothing added to the watch list, `flight_phase: null` in the poll.
- Thresholds are first guesses. Expect to tune them from real flights.
