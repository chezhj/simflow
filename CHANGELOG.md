## v2.1.0 (2026-07-19)

### Feat

- **checklist**: keep device screen awake during a procedure

### Fix

- **checklist**: keep scroll position steady when the suggestion banner appears
- **content**: correct "decend" typo in ATC descend checklist items
- **content**: repair mojibake and force ASCII on fixture export

## v2.0.1 (2026-07-11)

## v2.0.0 (2026-07-08)

### Feat

- **checklist**: add warn-mode rows for mandatory informational items
- **nav**: add always-visible grouped procedure picker with suggestion banner
- **plugin**: add simflow/report_miss diagnostic command
- **fixtures**: restructure descent into two linked procedures
- **fixtures**: add post-takeoff flight phase procedures and items
- **rules**: add `near` geo-operator for runway proximity
- **checklist**: persist check state across visits and add explicit Reset
- independent plugin/SOP versioning, compatibility handshake, GitHub release workflow
- **ui**: reveal all conditional procedures on idle when sim disconnected
- **ui**: route non-auto-continue procedures to idle and show next steps
- **procedures**: add conditional procedures, idle page, and universal reset
- **debug**: add live rule evaluator panel to the info panel
- **rules**: add ref_index array indexing and abs_diff_lte tolerance op
- **ui**: scroll to next unchecked item on check and page load
- **fixtures**: add live_rule data to attributes and CenterTanks attribute
- **attributes**: add live_rule evaluation and pilot transition prompt
- **logging**: replace 1Hz debug table with per-session audit log
- rebrand project to SimFlow
- **fixture**: add VA attribute and expand auto-check rules
- **rules**: add head: N modifier to fmc_line rule
- **ui**: show skipped items with a distinct dimmed style
- **ui**: add X-Plane initializing connection state
- **fixture**: refine CDU auto-check rules for preflight procedure
- **rules**: add tail and count_gte modifiers to fmc_line rule
- **fixture**: fill auto_check_rules for CDU preflight procedure
- **rules**: add fmc_line op for CDU screen-buffer datarefs
- **plugin**: add gate logic and auto-skip for optional items
- **fixture**: fill auto_check_rule for all items with a dataref_expression
- **rules**: add `ref` and `delta` support to rule evaluator
- **plugin**: implement phase 3 — flight loop, state POST, and session endpoint
- **api**: add Phase 2 rule engine — /api/plugin/state and Zibo datarefs
- **plugin**: phase 3-alpha — xFlow plugin bootstrap and check-next API
- **api**: add JS polling loop and /api/poll endpoint
- **ui**: redesign setup and account pages for large-screen layout
- **simbrief**: parse callsign, block fuel and FINRES+ALTN; harden error handling
- **data**: add Attribute.label and extend FlightInfo with OFP fields
- **checklist**: persist checked state and resume flight at active procedure
- **profile**: save attribute preferences per user account
- **api**: add /api/check and /api/uncheck endpoints
- **session**: add FlightSession models and redesign checklist flow
- **auth**: add UserProfile model and account management pages
- **auth**: add registration, login, logout, and password reset
- **ui**: implement Phase 1 Step 0 frontend redesign

### Fix

- **content**: update speedbrake dataref and add Before Takeoff warn items
- **content**: correct and extend auto_check_rule definitions
- **content**: move Verify LEGS page above Activate in CDU Preflight
- **ui**: show lat/lon coordinates in near-rule debug and fix auto-advance
- **poll**: prevent conditional procedures re-triggering after completion
- **fixtures**: update checklist content
- **debug**: render `near` conditions correctly in debug panel
- **fixtures**: correct vnav_td_dist dataref and stand/gate description
- **plugin**: auto-check optionals whose rule fires instead of always skipping
- **fixtures**: add descent prep show_rule and simplify heading bug rule
- **ui**: correct conditional procedure auto-nav and add idle debug panel
- **fixtures**: restore fmodpack dataref paths to lowercase b738
- **fixtures**: correct go-around rule, fueltruck threshold, waypoint step
- **ui**: prevent manual fallback from blocking auto-nav on reconnect
- **checklist**: enforce DualPilot attribute gate in solo mode
- **plugin**: read double-type datarefs via getDatad
- **ui**: scroll to show one "just completed" item above next unchecked
- **fixtures**: correct dataref casing and push_button typo
- **profile**: don't re-activate user defaults the pilot unchecked
- **fixtures**: require APU_starter_switch=1 for pk=29 auto-check
- **plugin**: trigger and auto-navigate conditional procedures correctly
- **fixtures**: correct auto-check rules and add DualPilot attribute
- **plugin**: detect XPLM type for array datarefs; treat scalar int as bitmask
- **checklist**: Go Around auto-check rules, guard OFP from overriding VA/Online, fix JSON escaping
- **plugin**: correct XPLM type detection, add dump_watch command and WARNING level
- **fixtures**: set cdu-preflight-procedure auto_continue to true
- **fixtures**: add and correct auto_check_rules for multiple items
- **attributes**: prevent plugin-driven attributes from auto-activating
- **fixtures**: add rule and Optional attribute to item 31 (Fuel Pump)
- **fixtures**: gate items 6 and 31 on CenterTanks attribute
- **plugin**: back off exponentially when server is unreachable
- **session**: auto-activate invisible default attributes
- **fixture**: correct auto-check rules for preflight and ground items
- **plugin**: recover automatically when session is replaced
- **plugin**: restore watch list for done items; allow manual promotion of skipped
- **plugin**: read CDU string datarefs via getDatas return value
- **fixture**: resolve duplicate step numbers in CDU procedure
- **setup**: auto-derive OFP conditions and fix session flow
- **ui**: fix role toggle switch styling in info panel
- **tests**: resolve hardcoded absolute path in SimBrief fixture

### Refactor

- **brand**: rebrand simFlow to xFlow across web app and plugin
- **plugin**: extract @require_api_key decorator and add session view

## v0.14.0 (2025-05-21)

### Refactor

- Upgrade Django and Poetry

## v0.13.1 (2025-05-10)

### Fix

- **settings**: Fix bug in static root path

## v0.13.0 (2025-04-23)

### Feat

- Added dual pilot mode
- **checklist**: Added PF/PM/CP/FO
- **simbrief**: Added wire mock & security

## v0.12.1 (2025-03-23)

### Fix

- **modules**: Anyio module dependecy upgrade

## v0.12.0 (2025-03-22)

### Feat

- Added error message to user
- **Profile**: Added simbrief load to set profile

## v0.11.2 (2025-03-01)

### Fix

- Skip empty procedure & data_ref changes

## v0.11.1 (2025-02-20)

### Fix

- **Database**: Fixed multiple references

## v0.11.0 (2025-02-05)

### Feat

- Fixed typos

## v0.10.1 (2025-01-24)

### Fix

- **data**: Fixed attribute of de-icing location

## v0.10.0 (2025-01-21)

### Feat

- File checkils and Icing procedure

### Fix

- **buld**: Added quotes to be sure

## v0.9.2 (2024-01-21)

### Fix

- **database**: Changed conditions below 10

## v0.9.1 (2023-11-21)

### Fix

- **procedures**: Fixed typo and improved testcode

## v0.9.0 (2023-11-11)

### Feat

- **database**: Improved Pushpack checks

### Fix

- Going back with empty checklist

## v0.8.0 (2023-10-20)

### Feat

- **ChecklistsData**: Added recommended safety checks

## v0.7.1 (2023-10-13)

### Fix

- **Profile-Template**: added title and pylint warnings
- **index-template**: Cleaned code & layout

### Refactor

- **Attributes**: Moved color of attributes button to database

## v0.7.0 (2023-10-03)

## v0.6.0alpha (2023-09-22)

## 0.1.0 (2023-09-09)
