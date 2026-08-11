# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Commands

```bash
# Run dev server
python manage.py runserver

# Run all tests (with coverage)
pytest

# Run a single test file
pytest checklist/tests/test_views.py

# Run a single test
pytest checklist/tests/test_views.py::TestProfileView::test_called_with_template

# Django migrations
python manage.py makemigrations
python manage.py migrate

# Coverage (manual flow from README)
py -m coverage erase
py -m coverage run manage.py test
py -m coverage lcov

# Lint Django templates
djlint checklist/templates/

# Bump version (uses commitizen + post-hook to push)
cz bump
```

## Settings

Settings are split into `smart_training_checklist/settings/`:
- `base.py` — shared config (SQLite DB, installed apps, auth)
- `dev.py` — `DEBUG=True`, uses a WireMock URL for SimBrief instead of the real API
- `prod.py` — production overrides

`pytest.ini` points at `settings.dev` automatically. The dev SimBrief URL hits a WireMock mock (`my-simbrief-mock.wiremockapi.cloud`) so tests never hit the real SimBrief API.

`MOCK_TOKEN` is read from a `.env` file via `python-decouple` and sent as `X-Auth-Token` header to the mock API.

---

## Deployment

Deploys are automated via GitHub Actions and triggered by pushing a `v*` tag — which `cz bump` does for you (the `post_bump_hooks` `push_all` step pushes the tag). The pipeline runs tests, creates a GitHub release, ships the built tree to the cPanel/CloudLinux server (`simflow.vdwaal.net` on `/home/vdwanet`), then activates it with an automatic smoke test and rollback.

Three moving parts:

| Part | Where | Role |
|---|---|---|
| `.github/workflows/release-deploy.yaml` | this repo | Self-contained pipeline: test → release → rsync ship → activate/smoke/rollback. Trigger: `v*` tags only (`plugin-v*` / `sop*` do **not** match). |
| `deploy/simflow_config.sh` | this repo | Secret-free per-app config, rsynced to `~/domains/simflow_config.sh` each deploy. Sets `DOMAIN`, `PYTHON_ENV`, `DJANGO_SETTINGS_MODULE=…settings.prod`, `DATABASE_SOURCE=production`, `POST_MIGRATE_COMMANDS`, `SMOKE_URL`. |
| `~/deploy-tools/scripts/{activate,rollback}.sh` | server (from [`www_installer`](https://github.com/chezhj/www_installer)) | Bring a shipped release live: symlink shared `db.sqlite3`/`.env`, back up DB, `migrate`, run post-migrate commands, `collectstatic`, restart. Clone-bootstrapped by the workflow if missing. |

**Persistent state** (`db.sqlite3`, `.env`) lives in `~/domains/shared/simflow/` and is symlinked into each release, so it survives deploys. Because `DATABASE_SOURCE=production`, the live DB is backed up before every `migrate`.

**Checklist content ships with the app**: after `migrate`, `POST_MIGRATE_COMMANDS` runs `checklist_content import --replace --noinput`, which wipes and reloads the content tables from `checklist/fixtures/checklist_content.json` in that release. Edit content via the fixture (see the content workflow), not the destructive export. User/session data is never touched.

**Requires** the `SSH_HOST` / `SSH_USER` / `SSH_PRIVATE_KEY` / `SSH_KNOWN_HOSTS` (and optional `SSH_PORT`) repo secrets. `passenger_wsgi.py` defaults `DJANGO_SETTINGS_MODULE` to prod so the live app runs prod settings.

**Rollback** is automatic on a failed activate/smoke. Manual rollback on the server:

```bash
cd ~/domains && ~/deploy-tools/scripts/rollback.sh simflow
# add --restore-db to also revert data if a migration ran
```

---

## Architecture

Single Django app: **`checklist`**, mounted at `/` in `smart_training_checklist/urls.py`.

### Data model (`checklist/models.py`)

```
Procedure  ──< CheckItem >── Attribute (M2M)
```

- **Procedure**: a named phase (e.g. "Before Start"). Has `step` (order), `slug` (URL key), `show_expression` (future use), `auto_continue`.
- **CheckItem**: one row in the checklist. Has `dataref_expression` (legacy xChecklist syntax — do **not** touch), `role` (PF/PM/C/FO/BOTH), `attributes` (M2M filter — item only shows when all its attributes are in the session's `attrib` list).
- **Attribute**: user-selectable profile option. `show=False` attributes are non-visible defaults. `over_ruled_by` creates mutual-exclusion pairs. `btn_color` (hex) is data-driven and the only place inline `background-color` is acceptable.

`CheckItem.shouldshow(profile_list)` — returns `True` if all of the item's attributes are in the supplied list (mandatory items have no attributes and always show).

### Session state

The Django session stores the user's profile state instead of a DB record (for anonymous mode):

| Key | Content |
|---|---|
| `attrib` | `list[int]` — active attribute IDs |
| `dual_mode` | `bool` — PF/PM crew role split active |
| `pilot_role` / `captain_role` | `str` — role assignment |
| `sb_origin`, `sb_destination`, `sb_runway`, `sb_temp`, `sb_flaps` | Cached SimBrief fields shown in the sidebar |

### SimBrief integration (`checklist/simbrief.py`)

`SimBrief(pilot_id).fetch_data()` fetches an XML plan and parses origin, destination, runway, temperature, altimeter, flap/bleed settings. `update_profile_with_simbrief()` in `views.py` translates temperature and bleed state into attribute IDs added to the session.

### Views (`checklist/views.py`)

- `profile_view` — GET shows profile form; POST with `Clean` flushes session; POSTing a `simbrief_id` fetches SimBrief data and caches it in session. Sets a 31-day `simbrief_pilot_id` cookie when "remember me" is checked.
- `update_profile` — POSTed from profile form; rebuilds `session['attrib']` from selected checkboxes + non-visual defaults + over-rule logic; redirects to index.
- `procedure_detail` — loads `Procedure` by slug, filters `CheckItem`s via `shouldshow()`, annotates each with a `lowlight` flag (dual mode items not in current role), redirects away from empty procedures.
- `update_session_role` — JSON endpoint for the role toggle switches in the info panel.

### Frontend (`checklist/static/checklist/css/`)

CSS is split into four files, loaded in order:

| File | Contents |
|---|---|
| `tokens.css` | `:root` custom properties (colors, fonts, radii, transitions) |
| `layout.css` | Page shell, conn-bar, info-panel, proc-layout, sidebar, nav-bar (mobile-first, media queries at 600px and 900px) |
| `components.css` | All UI components: CI rows, check-box, badges, progress ring, flight-info cards, info panel internals |
| `states.css` | Visual states: `.ci-manual`, `.ci-auto`, `.ci.lowlight`, sim-dot animations, `.js-hidden` |

**No inline styles** except `background-color` on attribute badge buttons (data-driven from `Attribute.btn_color`).

### Templates

- `base.html` — full page shell: conn-bar, info panel (with role toggles and legend), `{% block content %}` inside `.page`
- `detail.html` — extends base; `{% block page_class %}page-checklist{% endblock %}` adds overflow-hidden to `.page`; sidebar, checklist scroll area, nav bar; progress ring and counters driven by inline JS IIFE
- `toggle_switches.html` / `toggle_switches.js` — PF/PM and C/FO role toggles; JS calls `update_session_role` endpoint then reloads
- `attrib_detail.html` — renders a single attribute badge (`<span class="badge">`)

`{% load environment_tags %}` provides `{{ 'KEY'|setting }}` and `{{ 'KEY'|env }}` template filters.

### JS in `detail.html` and `idle.html`

The inline IIFE in `detail.html` tracks `checkedCount`, `autoCount`, `manualCount` and exposes:
- `window.markItem(id, source)` — marks a row as `ci-manual` or `ci-auto`, updates counters
- `window.updateConnectionBadge(simConnected, aircraft, reconnecting)` — updates conn-bar dot and status text

Both templates poll `/api/poll/` on `POLL_INTERVAL_MS`. The response drives two behaviours:

**Conditional procedure reveal / auto-navigation** (`applyShowProcedures`):

`show_procedures` — slugs whose `Procedure.show_rule` currently evaluates to true against the latest plugin dataref snapshot. When `sim_connected=true` these are auto-navigated; when false, a manual fallback reveals all conditional procedures so the pilot can tap them.

Two sets track state to avoid conflicts:
- `revealedSlugs` — which procedures are currently visible in the nav (display tracking). The manual fallback (autoNav=false) adds to this set so buttons stay visible.
- `navigatedSlugs` — which procedures we have already auto-navigated to this page load. **Never populated by the manual fallback.** This ensures that once the sim reconnects and a rule fires, auto-navigation always runs even if the slug was already in `revealedSlugs` from a prior disconnected-state fallback. When a procedure's rule stops firing and it is hidden, its slug is deleted from both sets so it can re-trigger later.

Without the two-set split, a sim reconnect after a disconnect would silently fail to navigate because `revealedSlugs.has(slug)` short-circuits the entire block.

### Export (`checklist/export_view.py`)

`ExportChecklistView` generates xChecklist-format `.txt` output from the DB — legacy feature, not part of the v2.0 redesign.

---

## v2.0 Roadmap (SPEC.md)

The `docs/SPEC.md` file is the canonical implementation plan. Steps 1–7 follow Phase 1 Step 0 (frontend redesign, already done):

1. Django auth (registration/login/logout)
2. UserProfile model — saves/loads attribute selections per user account
3. `SimSession` and `CheckedItem` models + migrations
4. `/api/check` and `/api/uncheck` JSON endpoints
5. `procedure_detail` annotates items with server-side checked state
6. `data-item-id` on CI rows (already in place)
7. JS polling loop (`/api/poll`, 2–3s interval)

**Important SPEC constraint**: the existing `CheckItem.dataref_expression` field stores legacy xChecklist syntax and must not be altered. A new `auto_check_rule` JSONField will be added separately for v2.0 automation.
