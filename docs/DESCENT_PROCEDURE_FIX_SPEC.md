# Descent Procedure False Retrigger Fix

> **Status: IMPLEMENTED (2026-07-26).** `show_rule` for pk=11 updated in
> `checklist/fixtures/checklist_content.json` and imported via
> `manage.py checklist_content import`. DB verified. Remaining: confirm on a
> test flight that the checklist still fires once at the first TOD (activation
> side — see Verification Status).

## Summary
`Procedure` pk=11 ("Descent Procedure", slug=`descent-procedure`) re-triggers its
`show_rule` during stepped/leveled descents instead of only at the first top of
descent. Fix is a one-leaf addition to the existing `show_rule` JSON — no rule
engine code changes required.

## Problem
On arrivals with a stepped descent profile (level off at an intermediate STAR
altitude, then descend again), the Descent Procedure re-opens at each
subsequent descent segment instead of staying dismissed after its first
appearance.

## Root Cause
Current `show_rule` for pk=11 has no flight-phase gate:

```json
{
  "any": [
    {
      "all": [
        { "dataref": "laminar/B738/fms/vnav_td_dist", "op": "gt", "value": 0 },
        { "dataref": "laminar/B738/fms/vnav_td_dist", "op": "lt", "value": 60 },
        { "dataref": "sim/flightmodel/position/y_agl", "op": "gt", "value": 5 },
        { "dataref": "laminar/b738/fmodpack/flightphase_landed", "op": "eq", "value": 0 }
      ]
    }
  ]
}
```

`vnav_td_dist` is recomputed continuously by the FMC. On a stepped descent,
each new segment can present a fresh "distance to next descent point" that
falls back into the `(0, 60)` window, re-satisfying this rule with no memory
of having fired already.

## Fix
Add a `flightphase_cruise` gate so the rule can only go true once — on the
transition out of cruise at the actual first TOD — not on every subsequent
descent segment:

```json
{
  "all": [
    { "dataref": "laminar/b738/fmodpack/flightphase_cruise", "op": "eq", "value": 1 },
    { "dataref": "laminar/B738/fms/vnav_td_dist", "op": "gt", "value": 0 },
    { "dataref": "laminar/B738/fms/vnav_td_dist", "op": "lt", "value": 60 },
    { "dataref": "sim/flightmodel/position/y_agl", "op": "gt", "value": 5 },
    { "dataref": "laminar/b738/fmodpack/flightphase_landed", "op": "eq", "value": 0 }
  ]
}
```

(Outer `any` wrapper is no longer needed with a single clause — collapsed to
one `all`.)

## Verification Status
Live-tested in X-Plane by Hendrik-Jan: `flightphase_cruise` holds at `0`
through step-down/level-off segments during a stepped descent and does not
revert to `1`. The phase gate is confirmed safe to rely on as a one-shot
activation guard — no session-state/latch mechanism is needed for this case.

**Still to confirm on the next test flight (activation side):** the live test
above covers the *dismiss* side (phase stays `0` through the descent). It does
not explicitly confirm `flightphase_cruise` reads `1` in the 60 nm-before-first-TOD
window. Standard Zibo CRZ behaviour holds the phase to `1` until TOD, so this is
expected — but note the failure mode if it does not: a missing/false dataref makes
the whole `all` evaluate False (`rules.py`), so the Descent Procedure would never
appear. Verify it still fires once at the first TOD.

## Implementation Notes
- **Content-only change.** This is a `show_rule` data update on an existing
  `Procedure` row (pk=11), not a code change. `dataref`/`eq` leaf type is
  already used elsewhere (pk 21, pk 24 both use `flightphase_landed` with
  `eq`), so the rule engine already supports this leaf shape as-is.
- Deploy via whatever the current `checklist_content` export/import
  convention is for this project (fixture edit + reimport, or direct DB edit
  — confirm which matches actual practice, since `RELEASE.md` is known to
  diverge from actual deploy behavior here).
- **Plugin watch list — RESOLVED, no action needed.** `plugin_state`
  (`plugin_views.py`) auto-derives the streamed watch list from every
  `Procedure.show_rule` via `collect_datarefs`, which walks the `all`/`any`
  tree and returns each leaf `dataref`. Adding the `flightphase_cruise` leaf
  therefore adds it to the plugin's stream automatically — the watch list is
  not a separate static list.
- Do **not** touch `show_expression` on this procedure — it's frozen legacy
  and unrelated to this fix.

## Explicitly Deferred (not part of this fix)
- `approach_flaps` / `approach_speed` exclusion from the old xChecklist
  expression — different failure mode (retrigger deep in approach), not
  observed here, and pk 24 already handles the sub-10,500ft phase.
- `altitude > 10000` floor — likely redundant given the phase gate and pk 24's
  existing altitude handling.
- General session-flag/latch leaf type for "activate once per flight" rules —
  not needed now that the phase gate is verified; revisit only if a future
  procedure needs this and a live-tested dataref gate isn't available for it.

## Acceptance Test
1. Fly a full flight including a STAR with a genuine stepped descent
   (level off at an intermediate altitude, then descend again).
2. Descent Procedure (pk=11) should appear exactly once, at the first TOD.
3. Confirm it does **not** reappear during the subsequent level-off/descent
   segment(s) prior to the 10,000ft handoff to pk=24.
