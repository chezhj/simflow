# SOP Content Versioning — Implementation Plan

**Project**: SimFlow
**Date**: 2026-09-12
**Status**: Draft — not scheduled
**Scope**: Immutable per-version SOP content; `FlightSession` pinned to one version

---

## Goal

Make a content release non-destructive to flights already in progress, and make
deleting a procedure or check item an honest operation rather than a side effect
of a wipe-and-reload.

Today `checklist_content import --replace` deletes every `Attribute`, `Procedure`
and `CheckItem` row and reloads them. The deletes cascade into user and session
data (`UserAttributeDefault`, `FlightSessionAttribute`, `FlightItemState`), which
is how saved preferences disappear on every deploy.

After this change:

- Each content release is a **new, immutable `SOP` row** with its own procedures
  and check items. Nothing is deleted on import.
- A `FlightSession` is **pinned** to the SOP it started on, so a deploy mid-flight
  cannot move the pilot's checklist under them.
- Old versions are removed by an explicit, separate prune that refuses to touch a
  version any session still references.

---

## Prerequisites

This plan assumes the earlier remediation steps have landed. It can be built
without them, but the preference bug is not fixed by this document alone.

| # | Prerequisite | Why it blocks this plan |
|---|---|---|
| 1 | Import runs in one `transaction.atomic()` (today the wipe is outside it, `checklist_content.py:149-162`) | A half-applied version import is worse than a half-applied reload |
| 2 | Import is upsert + targeted prune, not wipe-and-reload | This plan replaces the reload path; the transactional shape is shared |
| 4 | `Attribute` decoupled from versioned content; hardcoded attribute PKs (`phase.py:15` `OPTIONAL_ATTR = 4`, `models.py:174` `_INFO_ATTR = 3`) resolved by title | **Hard dependency.** Attributes are the shared vocabulary between user preferences, session state and app logic. If they are versioned along with content, `UserAttributeDefault` breaks again for exactly the reason it broke the first time |

Step 3 (`content_version` snapshot + "content updated" banner) becomes
unnecessary for correctness once this lands, but is worth keeping as a soft
"a newer checklist version is available" notice on the setup page.

---

## Decisions

| Topic | Decision | Rationale |
|---|---|---|
| Versioned models | `Procedure` + `CheckItem` only | They churn; they are what a content release rewrites |
| Non-versioned models | `Attribute` (and `IdleDataref`) | Shared vocabulary referenced by user preferences and by app logic; must outlive any single content version |
| Version identity | `SOP(icao_code, content_version)`, unique together | Matches what the plugin already receives from `plugin_session` |
| Current version | Explicit `SOP.is_current` flag, one per `icao_code` | Makes a deploy rollback flip a pointer instead of re-importing content |
| Session pinning | `FlightSession.sop` FK, `on_delete=PROTECT`, set at creation | PROTECT is what makes "prune cannot break an active flight" a database guarantee rather than a convention |
| Content identity across versions | `content_key` on `Procedure` and `CheckItem` | DB PKs are per-version once rows are duplicated; a stable key is needed for diffing versions and for any future in-flight migration |
| Fixture format | **Unchanged** | The importer reinterprets the fixture's `pk` values as content keys. No fixture churn, no re-export |
| Re-import of the same version | Idempotent if content matches; **refused** if content differs | Prevents two different contents claiming one version string |
| Procedure slug uniqueness | `unique_together (sop, slug)` instead of global `unique=True` | Several versions of `before-start-procedure` must coexist |
| Slug resolution | Always scoped by the session's SOP | Slug stays the URL key; the session supplies the version |
| Old version removal | Separate `checklist_content prune` command, never automatic | Deletion is the one operation that destroys data — it should never be a deploy side effect |

---

## Data model changes

```python
class SOP(models.Model):
    # existing: name, icao_code, content_version, release_notes, updated_at
    is_current = models.BooleanField(default=False)
    content_hash = models.CharField(max_length=64, blank=True)   # sha256 of the fixture body
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("icao_code", "content_version")]
        constraints = [
            models.UniqueConstraint(
                fields=["icao_code"],
                condition=models.Q(is_current=True),
                name="one_current_sop_per_aircraft",
            ),
        ]


class Procedure(models.Model):
    # existing fields unchanged, except:
    slug = models.SlugField()                      # was unique=True
    content_key = models.CharField(max_length=60)  # stable across versions; == slug at first import

    class Meta:
        unique_together = [("sop", "slug"), ("sop", "content_key")]


class CheckItem(models.Model):
    # existing fields unchanged, plus:
    content_key = models.CharField(max_length=60)  # the fixture's pk, as a string


class FlightSession(models.Model):
    # existing fields, plus:
    sop = models.ForeignKey(SOP, on_delete=models.PROTECT, null=True, blank=True)
```

`FlightSession.sop` is nullable only so the backfill migration can run; treat it
as required in code from day one.

`CheckItem.procedure` and `FlightItemState.checklist_item` keep CASCADE. That is
correct here: once a version is prunable (no session references it), its item
states genuinely should go.

---

## Import pipeline

`loaddata` cannot be used for versioned import — it writes the fixture's PKs
straight into the DB, and those PKs now belong to one version only. Replace it
with a small loader in `checklist_content.py`:

```
import(fixture):
    content_hash = sha256(canonical_json(procedures + checkitems + attributes))
    icao, version = fixture SOP row

    existing = SOP.objects.filter(icao_code=icao, content_version=version).first()
    if existing:
        if existing.content_hash != content_hash:
            raise CommandError("v{version} already exists with different content — bump content_version")
        make_current(existing)          # idempotent re-run / rollback re-activation
        return

    with transaction.atomic():
        upsert attributes (global, keyed by fixture pk — prerequisite 4 owns this)
        validate: every attributes:[...] reference resolves
        validate: procedure slugs unique within the fixture
        sop = SOP.objects.create(..., content_hash=content_hash, is_current=False)
        proc_id_map = {}
        for p in fixture procedures:
            row = Procedure.objects.create(sop=sop, content_key=p.pk_or_slug, **fields)
            proc_id_map[p.pk] = row.pk
        for i in fixture checkitems:
            row = CheckItem.objects.create(procedure_id=proc_id_map[i.procedure],
                                           content_key=str(i.pk), **fields)
            row.attributes.set(i.attributes)    # global attribute PKs
        make_current(sop)                        # clears is_current on the old row
```

`make_current()` clears `is_current` on any other SOP for the same `icao_code`
inside the same transaction.

**Export** must round-trip: emit `content_key` in the `pk` position so
`export` → `import` is stable. Export takes `--version` (default: current).

**Dry run**: `checklist_content import --dry-run` prints the version being
created, the per-model row counts, and the content-key diff against the current
version (added / removed / changed), then exits without writing.

---

## Query scoping

Every content query must be scoped to a SOP. Unscoped call sites today
(non-test, non-migration):

| File | Lines | What needs the SOP |
|---|---|---|
| `views.py` | 186, 386, 424 (`first_proc`), 681, 686 (next/prev), 717, 779, 864, 867, 872, 904 | Session's SOP; `profile_view` before a session exists uses the current SOP |
| `api_views.py` | 113, 137, 189, 267 | Session's SOP |
| `plugin_views.py` | 159, 182, 366, 516, 576, 593 | Session's SOP |
| `plugin_views.py` | 258 (`SOP.objects.first()`) | **Bug once versioned**: must return the *session's* `content_version`, not the first row |
| `context_processors.py` | 18 | Session's SOP when a session is active, else current |
| `export_view.py` | 77 | Current SOP, or `?version=` |
| `phase.py` | 52 | Receives a `procedure` — inherits scope from the caller |

Recommended shape — one helper, used everywhere, rather than 18 hand-written
filters:

```python
# checklist/content.py
def current_sop(icao=None): ...
def session_sop(flight_session): ...        # flight_session.sop or current_sop()
def procedures_for(sop): ...                # Procedure.objects.filter(sop=sop)
def procedure_by_slug(sop, slug): ...       # get_object_or_404(Procedure, sop=sop, slug=slug)
```

Add a regression test that greps for bare `Procedure.objects.` / `CheckItem.objects.`
outside `content.py` and the import command, so new unscoped queries do not creep back.

---

## Session lifecycle

- **Create** (`_create_flight_session`, `views.py:166`): set `sop=current_sop()`;
  derive `active_phase` from that SOP's first procedure.
- **Read**: all content lookups go through `session_sop(session)`.
- **`procedure_detail`** (`views.py:667`): resolve slug within the session's SOP;
  on a miss with an active session, redirect to idle rather than 404.
- **Reconfigure** (`start_checklist` continuing an existing session,
  `views.py:359-395`): keeps its pinned SOP. Only *New Flight* adopts the current
  version — that is the user-visible contract worth stating in the UI.
- **`plugin_session`** (`plugin_views.py:230`): return the session's
  `content_version` and `aircraft_type`, so the plugin's cached item IDs and the
  server agree on which version they are talking about.

Already resilient, no change needed: `poll_view` tolerates an unknown slug
(`api_views.py:189-190`), `check_view` / `uncheck_view` tolerate an unknown item
id (`api_views.py:335, 388`), `idle_view` tolerates a missing `active_phase`
(`views.py:867-871`).

---

## Retention and pruning

```
manage.py checklist_content prune [--keep N] [--dry-run]
```

Deletes SOP rows that are **all** of:

- not `is_current`
- referenced by no `FlightSession` (PROTECT enforces this at the DB level)
- outside the N most recent by `created_at` (default `--keep 5`)

Never run from `POST_MIGRATE_COMMANDS`. Manual, or a separate scheduled job once
the behaviour is trusted.

Storage is not a real constraint: 362 items + 23 procedures per version is roughly
400 rows, so 20 retained versions is ~8k rows in SQLite. The reason to prune at all
is admin clutter, not disk.

Note: a `FlightSession` is never deleted today, so every version a pilot ever flew
stays pinned forever. If that becomes a problem, the answer is a session-retention
policy, not weakening PROTECT. Out of scope here.

---

## Deploy and rollback behaviour

`POST_MIGRATE_COMMANDS` stays `checklist_content import --replace --noinput`
(the `--replace` flag becomes a no-op and should be dropped from
`deploy/simflow_config.sh:34` once the wipe path is gone).

| Scenario | Result |
|---|---|
| Deploy with new `content_version` | New SOP created, becomes current. In-flight sessions keep their pinned version. New flights get the new one |
| Deploy with unchanged `content_version` and unchanged content | Hash matches → no-op, current pointer re-affirmed |
| Deploy with unchanged `content_version` but edited content | **Import fails loudly** with "bump content_version". Deploy fails the smoke test and `activate.sh` rolls back — which is the correct outcome for an unversioned content change |
| Rollback to an older tag | Its fixture's version already exists with a matching hash → that SOP is simply made current again. No rows created, no data touched |
| Rollback with `--restore-db` | Unchanged from today |

This makes `cz bump` and the content version two separate release axes, which
they already are — the discipline it adds is that a fixture edit now *requires* a
`content_version` bump. Worth a line in `CLAUDE.md`'s content workflow.

---

## Migration plan

Ordered, each independently deployable:

1. `00XX_sop_versioning_fields` — add `SOP.is_current`, `content_hash`,
   `created_at`; `Procedure.content_key`; `CheckItem.content_key`;
   `FlightSession.sop` (nullable).
2. `00XX_backfill_sop_versioning` (RunPython) —
   `SOP.objects.update(is_current=True)` for the single existing row;
   `Procedure.content_key = slug`; `CheckItem.content_key = str(pk)`;
   `FlightSession.objects.update(sop=<that SOP>)`. Reverse: clear the fields.
3. `00XX_procedure_slug_per_sop` — drop `unique=True` on `Procedure.slug`, add
   `unique_together (sop, slug)` and `(sop, content_key)`, add the
   one-current-per-aircraft constraint.

SQLite rebuilds the table for 1 and 3; `activate.sh` backs the DB up before
`migrate`, so the existing safety net covers it.

---

## Suggested split

Two PRs, so the risky half lands on top of proven groundwork.

**5a — pin and scope** (no behaviour change, single version still)
- Migrations 1 and 2
- `checklist/content.py` helpers
- Convert all call sites in the table above
- `plugin_session` returns the session's version
- `procedure_detail` redirects to idle on an unresolvable slug
- Tests: every existing test still passes; new tests assert a session resolves
  content through its own SOP

**5b — multiple versions**
- Migration 3
- New importer (create-version path, hash, `make_current`, validation, `--dry-run`)
- Export round-trip via `content_key`
- `prune` command
- Admin: `is_current` in `SOPAdmin.list_display`, non-current versions read-only
- Drop `--replace` from `deploy/simflow_config.sh`

Rough size: 5a is mechanical but touches ~18 call sites across 6 files; 5b is
~300 lines of new command code plus tests.

---

## Tests

- Import v1.0.0 → import v1.1.0 → a session created before the second import
  still resolves its original procedures and items; its `FlightItemState` rows
  are intact.
- Re-importing an identical fixture is a no-op (row counts unchanged, current
  pointer unchanged).
- Re-importing a changed fixture under the same `content_version` raises
  `CommandError` and writes nothing.
- Importing an older fixture after a newer one makes the older SOP current
  without creating rows (the rollback case).
- `prune` refuses a version referenced by a `FlightSession`; PROTECT raises if
  code tries to delete it directly.
- `UserAttributeDefault` rows survive any number of imports (the original bug).
- Two SOP versions can hold the same procedure slug.
- `plugin_session` reports the pinned session's `content_version`, not the
  current one.

---

## Risks and open questions

| Risk | Mitigation |
|---|---|
| Admin edits to the current version still mutate in-flight sessions pinned to it | Make non-current versions read-only in admin; document the fixture as the source of truth. Full immutability would mean blocking admin edits entirely — probably too strict for a single-operator app |
| `content_key` for check items is the fixture's `pk`, which is only as stable as the fixture discipline | The `--dry-run` diff surfaces accidental key reuse before it lands; the existing "never recycle a PK" rule still applies |
| 18 call sites converted by hand — one missed site silently reads the wrong version | The grep-based regression test; plus 5a lands while only one version exists, so a miss is harmless until 5b |
| Multi-aircraft (a second `icao_code`) is half-anticipated here | `is_current` is already per-aircraft; session pinning already carries the aircraft. Choosing *which* aircraft a session uses is out of scope |
| `FlightSession` rows are never deleted, so versions pin forever | Accept for now; revisit with a session-retention policy |

**Open question**: should a pilot be able to adopt a new content version
mid-flight ("update checklist now")? `content_key` makes it technically possible
to re-point `FlightItemState` rows across versions, but the semantics of a
half-completed procedure that changed shape are genuinely unclear. Recommend
explicitly *not* supporting it, and letting *New Flight* be the only adoption path.

---

## What this does not solve

- The preference-wipe bug itself — prerequisites 1, 2 and 4 do that. This plan
  prevents the class of bug from returning, it is not the fix.
- Session data retention or growth.
- Multi-aircraft selection.
- The legacy xChecklist export (`export_view.py`), which gets version scoping
  here but no other attention.
