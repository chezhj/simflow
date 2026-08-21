# Business Rules Reference

Extracted from the live codebase (`models.py`, `views.py`, `api_views.py`, `plugin_views.py`, `rules.py`, `detail.html`, `idle.html`). Intended as a reference for both humans and AI. Source locations noted where they matter.

* * *

## 1\. CheckItem Visibility

**Rule:** A `CheckItem` is visible if every one of its `attributes` is present in the session's active attribute ID list.

**Mandatory items** have no attributes at all and are always visible.

```python
# models.py — CheckItem.shouldshow()
def shouldshow(self, profile_list):
    attributes = self.attributes.values_list("id", flat=True)
    if attributes:
        return set(attributes) <= set(profile_list)  # all must match
    return True  # no attributes → mandatory, always show
```

**DualPilot injection:** Attribute pk=16 ("DualPilot") is dynamically added to `active_attr_ids` at render time when `pilot_role != "SOLO"`, and stripped when SOLO. It is never stored this way in the DB — it is injected in `procedure_detail` and `plugin_check_next` only.

* * *

## 2\. Attribute Activation — Priority Order

When computing which attributes are active for a session:  
Change:  
First load pilot defaults, then suggest based on OFP, show to the pilot then pilot determines/overrides

| Priority | Source | Condition |
| --- | --- | --- |
| 2   | `ofp_derived` | OFP-derived from SimBrief temperature / bleed |
| 1(highest) | `pilot_override` | Pilot selected on form AND not in their saved defaults |
| 3   | `user_default` | Saved user preference, or selected item that matches a saved default |
| auto | invisible default | `show=False` attribute auto-activated unless over-ruled or plugin-driven |

CHANGE: Priority
**Invisible defaults (show=False):** Automatically added unless:

- The attribute has `live_rule_mode` set — these start OFF and are activated by the plugin.
- The attribute's `over_ruled_by` is already active in the session.

Example: `AboveZero` (show=False, over_ruled_by=Anti-Ice Normal) is auto-activated whenever Anti-Ice Normal is NOT active.

Source: `views.py — _resolve_active_ids()`

* * *

## 3\. OFP Attribute Derivation (SimBrief)

Temperature bands (only the first matching rule applies):

| Condition | Attribute title activated |
| --- | --- |
| `temp < 0°C` | `Anti-Ice Normal` |
| `0 < temp < 11°C` | `ZeroToTen` |

Bleed:

| Condition | Attribute title activated |
| --- | --- |
| `bleed_setting == "OFF"` | `Short Runway` |

**Never auto-derived:** `{"VA", "Online"}` — these are always pilot-chosen.

Source: `views.py — _derive_ofp_attrib_ids()`, `_OFP_TEMP_RULES`, `_OFP_BLEED_OFF_TITLE`

## 3B. loading OFP

CHANGE: OFP load and attribute derivation should be triggered when  
`User is logged in AND`  
`user has simbrief_id AND`  
`starts new flight`

Or when

`User logs in AND`  
`user has simbrief_id AND``there is NO active session for that user`

If in above case there is an active session for the user and origin or destination do not match the OFP, the user should be asked if a he wants to start a new flight.

&nbsp;

* * *

## 4\. Flight Session Lifecycle

1.  Pilot clicks **Start Checklist** → `FlightSession` created with `is_active=True`.
2.  `session_key` format: `ABCD-1234` (stored in Django session as `flight_session_key`).
3.  `FlightSessionAttribute` — one row per `Attribute`, created eagerly at session start.
4.  `active_phase` — slug of the current procedure frontier, forward-only (never moves backward).
5.  Initial `active_phase`: first `Procedure` ordered by `step`.

**Continue vs. create:** If an existing active session is found on "Start Checklist", its attributes and role are updated in-place rather than creating a new session.

**Stale session cleanup:** On new session creation, any other `is_active=True` sessions for the same `user_profile` are deactivated. This ensures the plugin's `session_id` goes stale and it re-fetches.

**Deactivation triggers:** "Clear" action, "New Flight" action, or starting a replacement session.

CHANGE:  
**Removal triggers:** Question: what should remove sessions or clean them up?

Source: `views.py — _create_flight_session()`, `profile_view()`

* * *

## 5\. Procedure State Reset — Preserve by Default

Procedure check state (`FlightItemState`) is **preserved across plain visits and reloads**. Opening a procedure — manually from the picker, by reload after a connection drop, or by navigating back to it — never wipes its checks. State is cleared only by two explicit triggers:

1.  **Rising edge of a `show_rule`** (see Rule 16). When a conditional procedure's rule transitions false→true in `poll_view`, its state is cleared so the procedure starts fresh — these are exactly the procedures that get genuinely re-used (e.g. a go-around re-arming an approach).
2.  **Explicit "Restart procedure"** — the pilot taps Restart on the procedure page, which POSTs to `procedure_reset_view` (guarded by a `confirm()`), deleting that procedure's `FlightItemState` rows.

Most procedures (e.g. getting the aircraft ready for flight) are run once and never reused, so automatic clearing on visit would only destroy useful state. Re-use is either rule-driven (1) or a deliberate pilot action (2).

Source: `views.py — procedure_detail()` (no reset on visit), `views.py — procedure_reset_view()`, `api_views.py — poll_view()` (rising-edge clear)

* * *

## 6\. active_phase Frontier

The `FlightSession.active_phase` slug tracks how far the pilot has progressed:

- Advances when visiting a procedure with a higher `step` than the current frontier.
- Never moves backward when navigating to a previous procedure.
- The plugin uses `active_phase` to know which procedure's items to auto-check.

CHANGE:  
Not sure this is needed to check for items because  
\- when the pilot chooses to clear a previous procedure, then that procedure should also be watched, not the last "active_phase"  
\- procedures have information to decide if the next procedure should be loaded, auto_continue field. So I think the session does not need to know the phase

The only thing I can think of, why phase could be needed, is if the phase is needed to determine which procedure to show or give as option. Next to that it's nice to see the flight phase in the header of the page, but not needed now

Source: `views.py — procedure_detail()` lines 591–598

* * *

## 7\. Lowlight (Dual Mode)

An item gets the `lowlight` CSS class (visually dimmed, NOT hidden) when all three are true:

1.  Session is in `dual_mode`
2.  `item.role != "BOTH"`
3.  `item.role` is not in `[pilot_role, captain_role]`

Source: `views.py — procedure_detail()` lines 629–635

* * *

## 8\. Procedure Navigation — Always-Visible Grouped Picker

**Visibility is decoupled from auto-navigation.** Every procedure is always reachable in the nav, grouped by `Procedure.category` (see Rule 25). `show_rule` no longer controls visibility — it only drives auto-navigation and the "suggested" highlight (Rules 10 & 16).

- **Linear nav** (Prev/Next): only procedures where `show_rule` is `null`.
- **Picker / sidebar**: renders all procedures, grouped by category in `CATEGORY_ORDER`. Conditional procedures (`show_rule` set) are tappable at any time, even when their rule never fires — no dead-ends.
- The grouped structure (`procedure_groups`) is built in `views._build_procedure_groups()` and passed to both `detail.html` and `idle.html`.

CHANGE: the whole counter is not needed

* * *

## 9\. Auto-Advance

When all items of a procedure are checked and at least one item was checked **this visit** (`hasCheckedThisVisit`), navigate after 600 ms:

- `auto_continue == True` → next linear procedure (lowest `step > current.step` where `show_rule=null` and has visible items), or `/idle` if none.
- `auto_continue == False` → `/idle`.

`auto_continue` controls **destination only** — it no longer gates whether auto-advance fires.

The `hasCheckedThisVisit` guard prevents auto-advancing when a pilot revisits an already-complete procedure (e.g., after a connection drop and reload).

Note: scroll-to-next-unchecked within a procedure is a separate mechanism — see Rule 13.

Source: `detail.html` — `updateProgress()` JS

* * *

## 10\. Firing Rules — Suggested Highlight, Banner, Auto-Nav

`show_procedures` (slugs of conditional procedures whose `show_rule` currently fires, edge-detected per Rule 16) drives **behaviour, not visibility** — every procedure is always visible (Rule 8). Behaviour differs by page:

**`detail.html`** (pilot is mid-checklist — never interrupted):
- Each firing slug other than the current procedure gets a `.suggested` highlight on its nav item.
- A non-blocking **suggestion banner** appears at the top of `.proc-main` for the first firing slug ("⚠ X suggested — tap to open"). Tapping navigates; nothing is forced.
- When a slug stops firing, its highlight clears. Function: `applySuggestions()`.

**`idle.html`** (no active procedure — a firing rule *should* fire):
- Each firing slug reveals its prominent "What's next" button **and** marks its sidebar item `.suggested`.
- **Auto-navigates** to the first firing slug (lowest `step`); `navigatedSlugs` prevents re-navigating to the same slug within a page load. The loop breaks after the first navigation so two simultaneous rules don't race.
- When a slug stops firing before the pilot acts, its button is re-hidden and its `.suggested` highlight cleared. Function: `applyShowProcedures()`.

**Completion:** a completed conditional procedure stays visible in the nav (marked `done`). The server's edge detection (Rule 16) stops listing it in `show_procedures`, so the suggested highlight clears on the next poll. It re-suggests when `show_rule` fires again (rising edge).

Source: `detail.html` — `applySuggestions()`; `idle.html` — `applyShowProcedures()`

* * *

## 11\. Sim Connection States (Browser)

Derived from `last_seen` unix timestamp and `sim_connected`/`sim_initializing` fields in `/api/poll/` response:

| State | Condition | Display |
| --- | --- | --- |
| Connected | `sim_connected == true` | Green dot, "X-Plane Live · 1 Hz" |
| Reconnecting | `!sim_connected && last_seen > 0 && age < 30s` | Amber dot, "Reconnecting…" |
| Initializing | `sim_initializing == true` | Pulsing dot, "X-Plane initializing…" |
| Disconnected | `!sim_connected && last_seen > 0 && age >= 30s` | "Simulator disconnected" |
| Manual only | `last_seen == 0` | "Manual only" |

Server-side thresholds (`api_views.py — poll_view()`):

- `sim_connected = age < 5s`
- `sim_initializing = not connected && age < 15s`

* * *

## 12\. No-Sim Behaviour

There is no longer a manual fallback that reveals hidden conditionals when the sim is disconnected — **every procedure is always reachable** (Rule 8), online or offline. With no live datarefs, `show_rule`s evaluate `False`, so `show_procedures` is empty: no auto-navigation, no suggested highlight, no banner. The pilot navigates manually via the grouped picker.

Source: `detail.html` / `idle.html` — poll handler

* * *

## 13\. Item Check/Uncheck State Machine

| Current state | User action | Result |
| --- | --- | --- |
| unchecked | tap/click | → `ci-manual`, POST `/api/check/` |
| `ci-manual` | tap/click | → unchecked, POST `/api/uncheck/` |
| `ci-auto` | tap/click | no action (auto state is read-only) |
| `ci-skipped` | tap/click | no action |

Auto-advance: after marking an item, scroll to first unchecked item after it.

CHANGE: name this auto-scroll

Source: `detail.html` — `toggleItem()` JS

* * *

## 14\. Plugin Gate / Active Zone Logic

This determines which items the plugin evaluates auto-check rules for. Behaviour
depends on the per-flight **`FlightSession.require_all_visible`** snapshot — taken
at session start from the `RequireAllVisible` user-preference attribute (a
General toggle, attached to no check items, so it never affects visibility).

**Default mode (`require_all_visible = False`):**

1.  **Optional attribute:** `Attribute pk=4` — items with this attribute are "optional" (non-blocking).
2.  **Gate:** The first visible, not-done, **required** (non-optional) item in the procedure.
3.  **Active zone:** All not-done visible items with `step <= gate_step` (includes optional items before the gate).
4.  Auto-check rules are only evaluated for items **in the active zone**.
5.  Watch datarefs are collected from **all visible items** with rules (not just active zone) so the plugin keeps streaming them.

**RequireAllVisible mode (`require_all_visible = True`):**

- The **gate** is the first visible, not-done item — optional or required alike.
- The active zone is therefore just the current gate item; each visible row must
  be individually satisfied (auto-rule or manual tap) before the gate advances.
- No item is ever auto-skipped (see §15). Informational/verify items simply
  auto-confirm in sequence as the gate reaches them.

Source: `plugin_views.py — plugin_state()` (`is_gate_candidate`)

* * *

## 15\. Auto-Skip Logic (default mode only)

**Only in default mode** (`require_all_visible = False`). When a required item's
auto-check rule fires:

1.  Any preceding optional items that are not-done are auto-skipped first — unless
    their own auto-check rule fires, in which case they are auto-checked instead.
2.  The triggering item itself is auto-checked.

Auto-skipped items get `status="skipped"`, `source="auto"` in `FlightItemState`. The browser renders them as `ci-skipped` (em-dash).

In **RequireAllVisible mode** this step is disabled entirely: the gate stops at the
first not-done item, so there is never a preceding not-done optional to resolve,
and `status="skipped"` is never produced by the engine.

Source: `plugin_views.py — plugin_state()`

* * *

## 16\. Conditional Procedure Edge Detection (poll)

`poll_view` tracks the last-known `show_rule` result per procedure in `FlightSession.show_rule_state` (a JSON dict `{str(proc.pk): bool}`). On every poll, for each conditional procedure:

| Rule result | Previous result | Items done? | Action |
| --- | --- | --- | --- |
| False | any | — | Hidden — not in `show_procedures`. `False` recorded in `show_rule_state`. |
| True | False (rising edge) | any | `FlightItemState` rows for procedure deleted. Slug added to `show_procedures`. Browser auto-navigates. |
| True | True (continuously true) | No | Slug added to `show_procedures`. Pilot is mid-checklist. |
| True | True (continuously true) | Yes | Silently skipped. States preserved. No loop. |

`show_rule_state` is written for **every** poll evaluation — including when the rule is `False`. This is essential: if `False` were not recorded, the next `True` evaluation would not be recognized as a rising edge, and the procedure would never re-trigger.

`show_rule_state` is persisted to the DB (saved only when changed). Preserved through server restarts.

**Re-trigger semantics**: a procedure re-triggers only when its `show_rule` transitions from `False` to `True`. Descent re-triggers after a go-around (rule went false during climb). Waypoint procedures re-trigger at each new waypoint (aircraft moved away then approached a new one). Emergency procedures re-trigger each time the condition starts fresh.

Source: `api_views.py — poll_view()`, `models.py — FlightSession.show_rule_state`

* * *

## 17\. Attribute Transition (live_rule)

Triggered before every navigation (all `.js-nav-btn` clicks are intercepted). Calls `GET /api/attribute-transition/`.

Two attribute modes:

| Mode | Behaviour |
| --- | --- |
| `activate_only` | If rule fires AND attribute is currently inactive → activate silently. No prompt. |
| `prompt_on_change` | If rule result ≠ current active state → present prompt to pilot. |

**Pilot overrides:** Stored in `FlightSession.pilot_overrides` as `{str(attr_id): bool}`. An attribute with an entry here is never prompted again in the same session.

**Accepting a prompt:** Re-evaluates `live_rule` and applies the result to `FlightSessionAttribute` with `source="live_rule"`.

**Rejecting a prompt:** Records rejection in `pilot_overrides`, attribute state unchanged.  
<br/>

Source: `api_views.py — attribute_transition_view()`, `views.py — _apply_attribute_overrides()`

* * *

## 18\. Rule Engine (rules.py)

### Composite operators

- `{"all": [rule, ...]}` — all sub-rules must be true (AND)
- `{"any": [rule, ...]}` — at least one sub-rule must be true (OR)

### Leaf shapes

```json
{"dataref": "path/to/ref", "op": "eq", "value": 1}
{"dataref": "path/to/ref", "op": "gt", "ref": "another/ref"}
{"dataref": "path/to/ref", "op": "lte", "ref": "array/ref", "ref_index": 0}
{"dataref": "path/to/ref", "op": "eq", "ref": "other/ref", "ref_index": 0, "delta": 10}
{"dataref": "path/to/ref", "op": "abs_diff_lte", "ref": "other/ref", "tolerance": 5}
{"fmc_line": "path/to/ref", "contains": "substring"}
{"fmc_line": "path/to/ref", "not_contains": "substring"}
{"fmc_line": "path/to/ref", "contains": "X", "tail": 8}
{"fmc_line": "path/to/ref", "contains": "X", "head": 4}
{"fmc_line": "path/to/ref", "contains": "X", "count_gte": 2}
```

### Comparison operators

`eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `abs_diff_lte`

### Missing dataref

Any rule referencing a dataref not present in the plugin's snapshot evaluates to `False`.

* * *

## 19\. Watch List (plugin response)

The plugin's `POST /api/plugin/state/` response includes a `watch` list of dataref paths the plugin must keep streaming. Built from:

1.  All visible items with `auto_check_rule` (not just active zone)
2.  All `Attribute.live_rule` datarefs
3.  All `IdleDataref.dataref_path` values (for the idle page display)
4.  All `Procedure.show_rule` datarefs (for conditional procedure unlocking)

Deduplicated, order-preserving.

Source: `plugin_views.py — plugin_state()` lines 339–414

* * *

## 20\. CheckItem Action Label

`CheckItem.get_action_label()` returns the display string used on the item badge:

1.  If `action_label` is set → return it verbatim.
2.  Else if item has attribute `NoActionNeed` → return `"CHECKED"`.
3.  Otherwise → return `"SET"`.

* * *

## 21\. Attribute Form — Conditions vs. General

Attributes displayed on the profile/setup form are split into two groups by `is_user_preference`:

| Group | Filter | Purpose |
| --- | --- | --- |
| Conditions | `show=True, is_user_preference=False` | Flight-specific (temp, bleed, runway) |
| General | `show=True, is_user_preference=True` | Persistent user preferences (VA, Online, etc.) |

Only `is_user_preference=True` attributes can be saved as user defaults in `UserAttributeDefault`.

* * *

## 22\. Session Keys Cleared on Reset

The following Django session keys are removed on "Clear" or "New Flight" (flight state only — auth state preserved):

```
flight_session_key, dual_mode, pilot_role, captain_role,
sb_origin, sb_destination, sb_runway, sb_temp, sb_flaps, sb_bleed,
sb_callsign, sb_block_fuel, sb_finres_altn, sb_derived_attribs,
sb_simbrief_id, sb_error
```

Source: `views.py — _FLIGHT_SESSION_KEYS`

* * *

## 23\. API Authentication

- **Browser endpoints** (`/api/check/`, `/api/uncheck/`, `/api/poll/`, `/api/attribute-transition/`): Django session auth + CSRF.
- **Plugin endpoints** (`/api/plugin/state/`, `/api/plugin/session/`, `/api/plugin/check-next/`): `Authorization: Bearer <raw_api_key>` header. CSRF exempt. Key is bcrypt-hashed in `UserProfile.api_key_hash`; only prefix stored for display.

* * *

## 24\. Logging (session JSONL)

Each flight session logs to `logs/session_<id>.jsonl`. Events logged:

| Event | When |
| --- | --- |
| `gate_changed` | The blocking gate item changes |
| `gate_cleared` | All required items done (gate = None) |
| `auto_checked` | Plugin auto-checks an item |
| `auto_skipped` | Optional item skipped because a later required item fired |
| `manual_checked` | Plugin `check-next` endpoint marks an item manually |

Each entry includes `ts` (ISO timestamp), `item_id`, `item` name, and the relevant rule + dataref values at the time.

* * *

## 25\. Procedure Category — Grouping & Emergency Section

`Procedure.category` (`normal` / `situational` / `emergency` / `reference`, default `normal`, indexed) buckets procedures for the picker. The picker renders groups in `Procedure.CATEGORY_ORDER` (`normal`, `situational`, `emergency`, `reference`); empty groups are dropped, and any unrecognised category falls into a trailing "Other" group. The **emergency** group is flagged `is_emergency` and styled with a red header/accent.

`category` is **presentation only** — it controls grouping and emergency styling, nothing else. All runtime behaviour (auto-nav, the suggested highlight, the banner) is driven by `show_rule` (Rules 8, 10, 16), independent of category. A `situational` procedure is just one that happens to carry a `show_rule`; the data migration seeds `situational` for every procedure with a non-null `show_rule` and leaves the rest `normal`. Emergency procedures are assigned `emergency` in fixtures.

Because grouping is purely category-driven in the template, finer phase buckets (Preflight / Departure / Cruise / Arrival) can be introduced later by extending `CATEGORY_CHOICES` and re-tagging fixtures — no view or template change required.

Source: `models.py — Procedure.CATEGORY_CHOICES / CATEGORY_ORDER`, `views.py — _build_procedure_groups()`, migration `0033_procedure_category`

&nbsp;

Other  
in the idle page this could show:  current speed, distance to destination, altitude, for future