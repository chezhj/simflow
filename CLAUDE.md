# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Commands

**Dependencies are managed with Poetry** (`pyproject.toml` / `poetry.lock`; `poetry.toml` creates the venv in-project at `.venv/`). Poetry is available in the cloud container. If `django` or another import is missing, run `poetry install` and use `poetry run …` — do not build an ad-hoc virtualenv or `pip install` from a requirements file (`requirements*.txt` are untracked and generated from `poetry.lock` at release time). CI does the same (`poetry install` then `poetry run pytest`).

```bash
# Install dependencies (incl. dev group)
poetry install

# Run dev server
poetry run python manage.py runserver

# Run all tests (with coverage)
poetry run pytest

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

# Releases come from the Release workflow on GitHub, not from a local `cz bump` (see "Releasing")
# Check a commit message / PR title the way the pr-title check does
cz check --message "feat(scope): subject"
```

## Settings

Settings are split into `smart_training_checklist/settings/`:
- `base.py` — shared config (SQLite DB, installed apps, auth)
- `dev.py` — `DEBUG=True`, uses a WireMock URL for SimBrief instead of the real API
- `prod.py` — production overrides

`pytest.ini` points at `settings.dev` automatically. **`settings/dev.py` points `SIMBRIEF_URL` at the live SimBrief API** — a WireMock alternative is present but commented out. Tests are safe because they mock `requests.get`, not because the URL is fake; a test that forgets to mock it will reach the real service.

`MOCK_TOKEN` is read from a `.env` file via `python-decouple` and sent as `X-Auth-Token` header to the mock API.

---

## Deployment

Deploys are automated via GitHub Actions. The **Release workflow** (see "Releasing") bumps the version, tags `v<version>` and calls the deploy pipeline for that tag; pushing a `v*` tag by hand also deploys (manual fallback). The pipeline runs tests, creates a GitHub release, ships the built tree to the cPanel/CloudLinux server (`simflow.vdwaal.net` on `/home/vdwanet`), then activates it with an automatic smoke test and rollback.

Three moving parts:

| Part | Where | Role |
|---|---|---|
| `.github/workflows/release-deploy.yaml` | this repo | Self-contained pipeline (display name "Build & Deploy"): test → release → rsync ship → activate/smoke/rollback. Triggers: `workflow_call` from `release.yml` with a `version` input, and pushed `v*` tags as a manual fallback (`plugin-v*` / `sop*` do **not** match). `TAG` (`inputs.version || github.ref_name`) is the one source for which release is built; every checkout uses `ref: ${{ env.TAG }}`. |
| `deploy/simflow_config.sh` | this repo | Secret-free per-app config, rsynced to `~/domains/simflow_config.sh` each deploy. Sets `DOMAIN`, `PYTHON_ENV`, `DJANGO_SETTINGS_MODULE=…settings.prod`, `DATABASE_SOURCE=production`, `POST_MIGRATE_COMMANDS`, `SMOKE_URL`. |
| `~/deploy-tools/scripts/{activate,rollback}.sh` | server (from [`www_installer`](https://github.com/chezhj/www_installer)) | Bring a shipped release live: symlink shared `db.sqlite3`/`.env`, back up DB, `migrate`, run post-migrate commands, `collectstatic`, restart. Clone-bootstrapped by the workflow if missing. |

**Persistent state** (`db.sqlite3`, `.env`) lives in `~/domains/shared/simflow/` and is symlinked into each release, so it survives deploys. Because `DATABASE_SOURCE=production`, the live DB is backed up before every `migrate`.

**Checklist content ships with the app**: after `migrate`, `POST_MIGRATE_COMMANDS` runs `checklist_content import --prune --noinput`, which upserts the content tables from `checklist/fixtures/checklist_content.json` in that release and then deletes the rows that fixture no longer carries. Edit content via the fixture (see the content workflow), not the destructive export. A fixture edit requires a `content_version` bump: the bump (`cz bump`, which the Release workflow runs) first runs `scripts/check_content_bump.py` and refuses the release when the content hash moved but the version did not. The Release workflow has no `SIMFLOW_SKIP_CONTENT_CHECK` override; bump `content_version` and run it again.

The import is deliberately not a wipe-and-reload. `Attribute`, `Procedure` and `CheckItem` are referenced with CASCADE by `UserAttributeDefault`, `FlightSessionAttribute` and `FlightItemState`, so deleting a row that is about to be re-created destroys saved preferences and in-flight progress — which is exactly what happened on every deploy before the import was rewritten. Rows that survive keep their pk and are updated in place; a delete now means the content is genuinely gone, and the cascade that follows is intended. Two rules the fixture must honour, both enforced by pre-flight validation that aborts before writing: never recycle a pk, and every reference must resolve to a row that survives the import. `checklist_content import --prune --dry-run` reports the add/update/delete plan without writing.

**Requires** the `SSH_HOST` / `SSH_USER` / `SSH_PRIVATE_KEY` / `SSH_KNOWN_HOSTS` (and optional `SSH_PORT`) repo secrets. `passenger_wsgi.py` defaults `DJANGO_SETTINGS_MODULE` to prod so the live app runs prod settings.

**Rollback** is automatic on a failed activate/smoke. Manual rollback on the server:

```bash
cd ~/domains && ~/deploy-tools/scripts/rollback.sh simflow
# add --restore-db to also revert data if a migration ran
```

---

## Commits, PR titles and releasing

### Commit and PR-title style (commitizen)

`CHANGELOG.md` and the version are generated from conventional commits (`cz_conventional_commits`, tag `v$version`). PRs are squash-merged, so the **PR title becomes the one commit on `master`** and is the only thing `cz bump` reads:

    <type>(<scope>): <subject>

- Subject: lowercase (or a digit) first, imperative, no trailing period; the whole title at most 72 characters (the squash adds ` (#N)`). Scope is optional but preferred.
- `feat` → minor bump. `fix`, `refactor`, `perf` → patch bump. `docs`, `chore`, `test`, `build`, `ci`, `style` → no bump. `!` / `BREAKING CHANGE` only for genuinely breaking changes.
- The `pr-title` workflow runs `cz check --message` on every PR (and again when the title is edited) and also rejects titles starting with `Merge`, `Revert`, `Pull request`, `fixup!` or `squash!` (commitizen accepts them, `cz bump` ignores them) plus the style rules above. It cannot judge whether the *type* is right: `fix` on a PR that adds a feature costs the minor bump, so pick it deliberately.
- Check a title before opening the PR: `cz check --message "feat(menu): add a thing"`.
- Always squash-merge, and keep Settings → General → Pull requests → default squash commit message on "Pull request title".

### The checks

`test` (CI: `manage.py check` + `pytest`, plus the `js` job) and `pr-title` run on every PR, but **nothing enforces them**: no ruleset requires them, so a red check does not disable the merge button. Look at the checks before squash-merging and do not merge on red.

Why no required-status-checks rule: the Release workflow pushes its bump commit with `GITHUB_TOKEN`, and `github-actions[bot]` cannot be added to a ruleset's bypass list. A brand-new, untested commit would therefore be rejected by an enforced `required_status_checks` rule (GitHub's branch-protection docs say commits must pass the checks before they reach the branch; not verified for rulesets), and the Release would then fail at the push. Enforcing the checks would need the bump to be pushed with a token of a bypass user (e.g. a fine-grained PAT of the repo owner); because a token-pushed tag *does* trigger the `v*` deploy, the chained `deploy` job in `release.yml` would then have to go, or every release would deploy twice. If you ever enable it, the context names are `test` and `pr-title` (not `Tests`).

### Releasing

- Web-app releases are made by the **Release workflow** (GitHub: Actions → Release → Run workflow, on `master`). It runs `ci.yaml`, then `commitizen-action` (bump, `CHANGELOG.md`, commit, `v*` tag, push), then `release-deploy.yaml` for that tag. Do not run `cz bump` or create tags locally for a normal release.
- `increment`: leave it on `auto` so the version comes from the conventional commits since the last tag. Choose `PATCH` or `MINOR` only to force a release when nothing bumpable has landed. With nothing to bump the run ends with "Nothing to release" and pushes and deploys nothing.
- A warning step lists commits since the last tag that `cz bump` cannot read (a bad title that slipped through, a direct push, a `tools:`-style type). It only warns; those commits are missing from the bump and the changelog.
- A local `cz bump` still works (it bumps, commits and tags, and runs the content-version check) but pushes nothing; `post_bump_hooks` is only an echo. To ship such a bump by hand: `git push && git push --tags`; the pushed `v*` tag deploys.
- Take a database dump *before* a release that contains migrations: the automatic rollback restores code, not the database.
- Do not hand-write changelog text under a version header: the workflow generates it. Fix-ups go through a `docs(changelog): ...` PR.
- Plugin (`plugin-v*`) and SOP (`sop-*`) releases are separate and unchanged, see `docs/RELEASE.md`.
- Never push to `master` directly for a web-app change; work on a branch and open a PR.

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
