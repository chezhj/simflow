# Fly.vdwaal.net – v2.0 Data Model Design

> This document captures decisions made during a design session and is intended
> to be read alongside SPEC.md when implementing v2.0 models in Django.
> It supersedes any model-related sections in SPEC.md where they conflict.
> Do not modify `dataref_expression` or `show_expression` on ChecklistItem.

---

## Design Principles

- Django app is the **source of truth** for all checklist logic
- Checklist item state moves from **frontend memory → server-side model**
- Item state is stored **lazily** (only rows that deviate from `unchecked`)
- Plugin is **stateless/dumb** — reads datarefs, posts state, fires manual check command
- `dataref_expression` and `show_expression` on existing models **must not be modified**

---

## Model Overview

```
Attribute  (admin-maintained, semi-fixed, existing model — do not modify)
    │
UserProfile
    │  (default attribute values via UserAttributeDefault)
    │
    └──▶ FlightSession ◀── (session_key, used by plugin + browser)
              │
              ├──▶ FlightInfo              (current flight conditions, seeded from OFP)
              │
              ├──▶ FlightSessionAttribute  (one row per Attribute, is_active)
              │         ▲
              │         └── seeding priority:
              │               1. UserProfile defaults
              │               2. FlightInfo/OFP-derived suggestions (pilot confirms)
              │               3. Pilot manual toggle
              │
              └──▶ FlightItemState         (per-item runtime state, lazy)
```

---

## Existing Model: `Attribute`

The constraint "do not modify" applies specifically to `dataref_expression` and
`show_expression` on `CheckItem` / `Procedure`, which are used by xChecklist export.
**Display-only fields may be added to `Attribute`** without affecting any existing
functionality. The `title` field is still used for internal logic (OFP derivation,
`get_action_label`); the new `label` field is UI-only.

```python
class Attribute(models.Model):
    title = models.CharField(max_length=30)       # internal key — do not change values
    label = models.CharField(max_length=60, blank=True)  # UI display name; falls back to title
    order = models.PositiveIntegerField()
    description = models.TextField(blank=True)
    show = models.BooleanField(default=True)
    is_user_preference = models.BooleanField(default=False)
    over_ruled_by = models.ForeignKey(
        "self", on_delete=models.SET_NULL, blank=True, null=True
    )
    btn_color = ColorField(default="#194D33")
```

Setup page splits visible attributes into two columns:
- **Conditions** (left): `show=True, is_user_preference=False` — flight-specific
- **General** (right): `show=True, is_user_preference=True` — preference defaults

`ATC` attribute changed to `is_user_preference=True` so it appears in General.

### Overrule Logic

Attributes form a mutual-exclusion group via `over_ruled_by`. Examples:

| Attribute      | `show` | `over_ruled_by` | Meaning                         |
| -------------- | ------ | --------------- | ------------------------------- |
| `above_10`     | False  | `zero_to_ten`   | Hidden normal-condition default |
| `zero_to_ten`  | True   | `below_zero`    | Temp 0–10°C                     |
| `below_zero`   | True   | —               | Temp below 0°C                  |
| `long_runway`  | False  | `short_runway`  | Hidden normal-condition default |
| `short_runway` | True   | —               | Short runway / above 10,000ft   |

When `below_zero` is active, `zero_to_ten` is implicitly deactivated (overruled).
When `short_runway` is active, `long_runway` (hidden) is deactivated.

### Toggle Rules for `FlightSessionAttribute`

When a pilot activates attribute X in a `FlightSession`:

1. Set `FlightSessionAttribute.is_active = True` for X
2. Find all attributes where `over_ruled_by = X` → set their rows to `is_active = False`
3. **UI must prevent** reactivating an attribute that is overruled by a currently
   active attribute — do not expose the toggle if the overruling attribute is on

This logic runs on every toggle (both activation and deactivation). Deactivating X
does **not** automatically reactivate overruled attributes — the pilot must
explicitly enable them.

---

## New Models

### `UserProfile`

Persistent user-level data. Optional — anonymous sessions are valid.

| Field         | Type            | Notes                                       |
| ------------- | --------------- | ------------------------------------------- |
| `user`        | OneToOne → User | Django auth user                            |
| `simbrief_id` | CharField       | User's SimBrief pilot ID (not OFP-specific) |

### `UserAttributeDefault` (through table)

One row per Attribute per user, storing the user's preferred default.

| Field          | Type             | Notes                                       |
| -------------- | ---------------- | ------------------------------------------- |
| `user_profile` | FK → UserProfile |                                             |
| `attribute`    | FK → Attribute   |                                             |
| `is_active`    | BooleanField     | User's preferred default for this attribute |

---

### `FlightSession`

Central session model. Replaces Django session storage for all flight state.
Created when user clicks "Start Live Session".

| Field                 | Type                       | Notes                                                   |
| --------------------- | -------------------------- | ------------------------------------------------------- |
| `session_key`         | CharField, unique          | Short code entered into X-Plane plugin (e.g. ABCD-1234) |
| `user_profile`        | FK → UserProfile, nullable | Null for anonymous sessions                             |
| `pilot_role`          | CharField                  | `PF` / `PM` / `SOLO`                                    |
| `pilot_function`      | CharField                  | `C` / `FO` / `BOTH`                                     |
| `active_phase`        | CharField                  | Current checklist phase slug                            |
| `created_at`          | DateTimeField              | auto_now_add                                            |
| `last_plugin_contact` | DateTimeField, nullable    | Updated on each plugin POST                             |
| `is_active`           | BooleanField               | False on session end or timeout                         |

**Notes:**

- Django session stores only the `session_key` reference — no flight state in session
- `pilot_role` and `pilot_function` can be changed mid-session (pilot confirms, page reloads)
- Sessions are throw-away — no save/resume in v2.0
- Stale sessions expired via background task using `last_plugin_contact`

---

### `FlightInfo`

Current flight conditions for this session. Initially seeded from SimBrief OFP
but **mutable** — real conditions often differ from planned ones.

| Field              | Type                     | Notes                                            |
| ------------------ | ------------------------ | ------------------------------------------------ |
| `flight_session`   | OneToOne → FlightSession |                                                  |
| `origin_icao`      | CharField                |                                                  |
| `destination_icao` | CharField                |                                                  |
| `alternate_icao`   | CharField, nullable      |                                                  |
| `oat`              | IntegerField, nullable   | Outside air temp °C — drives anti-ice suggestion |
| `departure_runway` | CharField, nullable      | e.g. `18R`                                       |
| `departure_stand`  | CharField, nullable      | Drives pushback suggestion                       |
| `flaps_setting`    | CharField, nullable      | Flap setting from TLR (e.g. `25`)                |
| `callsign`         | CharField, nullable      | ATC callsign from `atc/callsign`                 |
| `block_fuel_kg`    | IntegerField, nullable   | Block fuel kg from `fuel/plan_ramp`              |
| `finres_altn_kg`   | IntegerField, nullable   | FINRES+ALTN kg (`fuel/reserve` + `fuel/alternate_burn`) |
| `ofp_loaded`       | BooleanField             | True if seeded from SimBrief OFP                 |

**Notes:**

- OFP-derived conditions (anti-ice, ZeroToTen, short runway) are **auto-applied**
  to `FlightSessionAttribute` when SimBrief is loaded — no pilot confirmation step.
  The pilot can toggle them off manually after the fact.
- `departure_stand` and pushback suggestion are not yet wired to SimBrief parsing.

---

### `FlightSessionAttribute`

Active attribute set for this flight. One row per `Attribute` per session,
created **eagerly** on session start.

| Field            | Type               | Notes                                             |
| ---------------- | ------------------ | ------------------------------------------------- |
| `flight_session` | FK → FlightSession |                                                   |
| `attribute`      | FK → Attribute     |                                                   |
| `is_active`      | BooleanField       | Current active state for this flight              |
| `source`         | CharField          | `user_default` / `ofp_derived` / `pilot_override` |

**Seeding sequence on session creation:**

1. One row created per `Attribute` in the table
2. `is_active` seeded from `UserAttributeDefault` (or `Attribute.default_value` for anonymous)
3. After OFP load: rows updated where OFP implies a different value; `source` = `ofp_derived`;
   overrule logic applied. Not sure `source` is needed
4. Pilot toggle at any time: `is_active` flipped, `source` = `pilot_override`;
   overrule logic applied; checklist rebuilt

---

### `FlightItemState`

Runtime state per checklist item. **Lazy** — only rows that differ from `unchecked`
are stored. Absence of a row = item is `unchecked`.

| Field            | Type                    | Notes                                             |
| ---------------- | ----------------------- | ------------------------------------------------- |
| `flight_session` | FK → FlightSession      |                                                   |
| `checklist_item` | FK → ChecklistItem      | Existing model — untouched                        |
| `status`         | CharField               | See Status Values below                           |
| `source`         | CharField, nullable     | `manual` / `auto` — only when status is `checked` |
| `checked_at`     | DateTimeField, nullable | Timestamp of state change                         |

**Status Values:**

| Value     | Meaning                                                                                                                                                               |
| --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `checked` | Completed. `source` distinguishes manual vs auto.                                                                                                                     |
| `skipped` | Bypassed by downstream auto-check. Optional items only — required items can never be skipped.                                                                         |
| `pending` | Sim state satisfies rule but a required item above is unresolved. Becomes `checked` when blocker clears (if sim state still valid), otherwise reverts to `unchecked`. |

**Notes:**

- Item order is stable — no need to snapshot order on the state row
- `pending` is transitional — cleared on any attribute-driven rebuild
- Pilot can always manually check any item (including pending/blocked) via web UI or plugin command

---

## Cascade & Blocking Rules

### When an auto-check fires on item N (same for manual check):

1. **Optional unchecked items above N** → `skipped`
2. **Required unchecked items above N** → remain `unchecked`, surface visually,
   **block progression**
3. **Items below N** → evaluated sequentially from watch list data

### Blocking:

- Required unchecked item above N stores N as `pending`
- When blocker resolves (manual or auto):
  - Sim state still valid → `pending` → `checked`
  - Sim state changed → `pending` → `unchecked`
- Pilot can always manually override via web interface or plugin command

### NoActionNeed and Required items:

| Has dataref        | Behaviour                                                       |
| ------------------ | --------------------------------------------------------------- |
| No                 | Filtered by user preference, never evaluated                    |
| Yes, correct state | Auto-checked, ignored, silently via watch list                  |
| Yes, wrong state   | Surfaces as visible unchecked required item, blocks progression |

---

## Watch List Design

Django responds to each plugin POST with a watch list — the set of datarefs to monitor.

- Includes **all pending auto-checkable items in the current phase**, not just the next one
- Ensures cascade items resolve within one poll cycle (2–3 seconds) — no active reads needed
- Auto-check rules live in `ChecklistItem.auto_check_rule` (JSONField, v2.0 addition)
- `dataref_expression` / `show_expression` on `ChecklistItem` are **never modified**

---

## Plugin Responsibilities (v2.0)

### 1. Dataref state push

- Read watch list datarefs at 1–2 Hz
- POST minimal state to `/api/plugin/push/`
- Receive updated watch list in response

### 2. Manual check command

- Register X-Plane command: `fly.vdwaal/check_item`
- Joystick/keyboard bindable by pilot in X-Plane joystick settings
- On trigger: POST `{"session_key": "...", "action": "manual_check_next"}` to Django
- Django finds topmost unchecked non-skipped item in active phase
- Creates `FlightItemState`: `status=checked, source=manual`
- Single press sufficient — pilot can uncheck via web interface if needed

---

## Session Lifecycle

**UI entry point:** `/` serves the "New Flight" setup page.
If a `FlightSession` is already active in the Django session, the view offers
"Continue" (update existing) or "New Flight" (deactivate + create fresh).
On "Continue" / "Start Checklist", the view creates the `FlightSession` and
redirects to the first (or active) procedure.

```
1. Browser: "Start Checklist" / "Continue" on New Flight page
   → FlightSession created, session_key generated
   → FlightSessionAttribute rows created eagerly (one per Attribute)
     → seeded from UserProfile defaults or Attribute.default_value for anon
     → overrule logic applied to initial set
   → Django session stores session_key reference only

2. Optional: SimBrief OFP loaded
   → FlightInfo created and populated
   → Attribute suggestions surfaced in UI (e.g. OAT → anti-ice recommendation)
   → Pilot confirms or ignores each suggestion
   → Confirmed → FlightSessionAttribute updated (source=ofp_derived)
     → overrule logic applied

3. User enters session_key into X-Plane plugin
   → Plugin begins POSTing to /api/plugin/push/
   → FlightSession.last_plugin_contact updated on each POST
   → Django returns current watch list in response
   → Alternative, key is stable for a user across sessions, just add key once

4. Mid-flight attribute change
   → FlightSessionAttribute updated (source=pilot_override)
   → Overrule logic applied
   → Checklist rebuilt: item list recalculated, FlightItemState overlaid,
     pending rows cleared

5. Session end
   → FlightSession.is_active = False
   → Stale sessions auto-expired via last_plugin_contact timeout
```

---

## What This Replaces

| v1.x                                | v2.0                                       |
| ----------------------------------- | ------------------------------------------ |
| Flight attributes in Django session | `FlightSessionAttribute` rows (proper FK)  |
| Checklist item state in frontend JS | `FlightItemState` rows (lazy, server-side) |
| No session concept                  | `FlightSession` with session_key           |
| No user persistence                 | `UserProfile` with `UserAttributeDefault`  |
| Manual-only checklist               | Auto-check via plugin + watch list         |
| No hardware input                   | Plugin command `fly.vdwaal/check_item`     |

---

## Future Considerations (not v2.0)

- **Dual browser mode** (v3.0): `FlightSession` already supports it — role is modelled,
  multiple browsers can reference the same session_key
- **WebSockets** (v3.0): replace browser polling
- **Session resume**: persist `FlightItemState` beyond session end + resume flow
- **Extended FlightInfo**: additional OFP fields as use cases emerge

---

_End of document_
