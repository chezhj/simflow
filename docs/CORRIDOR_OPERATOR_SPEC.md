# SimFlow — `corridor` Geo-Operator
## Specification: Rule Engine Extension

**Version:** 1.0
**Status:** Implemented
**Extends:** `NEAR_OPERATOR_SPEC.md` (the `near` operator stays as it is)
**Scope:** `checklist/rules.py` + Before Takeoff `show_rule` content

---

## 1. Problem Statement

Before Takeoff triggers on `near` — a circle around the FMC departure runway
threshold. That models a full-length departure. It does not model an
**intersection departure**, where the aircraft enters the runway at a taxiway
part-way down it and never comes within the threshold radius.

Widening the radius is not a fix. A circle big enough to reach an
intersection 2 km down the runway also reaches the parallel taxiway, the
apron, and the opposite threshold — all places where Before Takeoff must not
fire.

The shape that matches the real world is the runway itself: a long, narrow
rectangle. `corridor` is that shape.

---

## 2. Design

The corridor is defined by the threshold (which the FMC already publishes),
the runway course, and three distances:

```
                        half_width_m
                             ↕
  ┌──────────────────────────────────────────────────┐
  │                                                  │  ← corridor
  ●  threshold                                       │
  │                                                  │
  └──────────────────────────────────────────────────┘
  ↔                                                  ↔
  behind_m          along the runway course        ahead_m
```

The aircraft position is decomposed into **along-track** and **cross-track**
offsets from the threshold and tested against the rectangle. There is no
corner arithmetic and no bounding-box approximation; it is one rotation of a
local east/north offset.

### 2.1 Why the course needs a magnetic correction

`laminar/B738/fms/ref_runway_crs` is a **magnetic** course — it is compared
against the MCP heading dial elsewhere in the content (pk 53, pk 115). The
geometry needs a **true** bearing. Left uncorrected, the corridor is rotated
by the local variation; at 3° over a 3 km runway that is ~157 m of drift at
the far end, which is more than the corridor's own width.

The variation is recovered from the aircraft itself:

```
variation = sim/flightmodel/position/psi  −  sim/flightmodel/position/mag_psi
true_course = ref_runway_crs + variation
```

This is deliberate. X-Plane also publishes
`sim/flightmodel/position/magnetic_variation`, but its sign convention is
easy to get backwards and a sign error here is silent — the corridor simply
points the wrong way. Deriving it from two headings the sim reports for the
same aircraft is self-calibrating and cannot have the sign inverted.

A rule whose course dataref is already true sets `"crs_true": true` and skips
the correction (and the two heading datarefs drop out of the watch list).

### 2.2 Projection

Equirectangular, about the threshold:

```
north_m = (lat − ref_lat) · π/180 · R
east_m  = (lon − ref_lon) · π/180 · R · cos(ref_lat)

along_m =  north·cos(θ) + east·sin(θ)
cross_m = −north·sin(θ) + east·cos(θ)        θ = true course
```

Sub-metre error over the few kilometres a runway spans — far below the
tolerances involved — and no iteration, so it is as cheap as `near`.

`cross_m` is signed: positive is right of the centreline looking down the
runway. The session log keeps the sign; the test uses `abs()`.

---

## 3. Schema

```json
{
  "op": "corridor",
  "ref_lat": "<dataref_path>",
  "ref_lon": "<dataref_path>",
  "crs": "<dataref_path>",
  "ahead_m": <number>,
  "behind_m": <number>,
  "half_width_m": <number>,
  "crs_true": <bool>
}
```

| Field | Required | Description |
|---|---|---|
| `ref_lat` / `ref_lon` | yes | Datarefs for the reference point (runway threshold) |
| `crs` | yes | Dataref for the runway course, magnetic unless `crs_true` |
| `ahead_m` | yes | How far past the reference point the corridor runs |
| `behind_m` | no (0) | How far before it — tolerance for holding short of the threshold |
| `half_width_m` | yes | Lateral half-width either side of the centreline |
| `crs_true` | no (false) | Course is already true; skip the variation correction |

Aircraft position and heading are implicit, as with `near` —
`collect_datarefs` adds them so the plugin watch list is correct with no
change outside `rules.py`.

### 3.1 Degradation

Returns `False`, never raises:

| State | Reason |
|---|---|
| `ref_lat == 0.0 and ref_lon == 0.0` | FMC runway not programmed — the existing `near` sentinel |
| `crs` absent from the payload | Nothing to orient the rectangle by |
| `psi` / `mag_psi` absent (and not `crs_true`) | Variation cannot be recovered |

The procedure then simply does not auto-trigger and the pilot advances
manually, which is the same degradation `near` already has.

---

## 4. Before Takeoff `show_rule`

```json
{"all": [
  {"any": [
    {"op": "near", "ref_lat": "…ref_runway_start_lat",
     "ref_lon": "…ref_runway_start_lon", "meters": 500},
    {"op": "corridor", "ref_lat": "…ref_runway_start_lat",
     "ref_lon": "…ref_runway_start_lon", "crs": "…ref_runway_crs",
     "ahead_m": 4000, "behind_m": 150, "half_width_m": 90}
  ]},
  {"dataref": "sim/flightmodel/position/y_agl", "op": "lt", "value": 5}
]}
```

`near` is kept alongside the corridor rather than replaced. It covers the
approach to the threshold area from a taxiway that is off to the side and
therefore outside the corridor — today's behaviour — so this is a strict
superset: nothing that triggered before stops triggering.

The `y_agl` guard still applies to both, so the corridor does not re-fire as
the aircraft flies down the runway on rotation.

### 4.1 Tuning the numbers

These are content decisions, not code decisions:

| Value | Chosen | Trade-off |
|---|---|---|
| `ahead_m` 4000 | Covers the longest runways end to end | Too long is harmless — the runway ends first |
| `behind_m` 150 | Holding short of the threshold still counts | Too long reaches back into the taxiway system |
| `half_width_m` 90 | Reaches a CAT I holding position (~90 m) | Parallel taxiways are typically 150–190 m out; going much above 100 starts to catch them |

---

## 5. Tests

`checklist/tests/test_rules.py` — `TestCorridor*`, 18 cases:
threshold, intersection along the runway, holding short laterally, behind the
threshold inside and outside `behind_m`, past the far end, the parallel
taxiway rejection, the reciprocal threshold rejection, the FMC sentinel, each
missing-dataref path, the variation correction (and proof the uncorrected
course fails the same position), `crs_true`, watch-list contents with and
without the correction, and the session-log leaf shape.

---

## 6. Non-Goals

- Arrival/landing runway proximity — same operator would serve, no content uses it yet
- Runway length from the sim; `ahead_m` is authored, not measured
- Non-Zibo aircraft — `ref_runway_*` are Zibo datarefs, as for `near`
- Replacing `near`, which remains the right shape for a point of interest

---

*End of specification*
