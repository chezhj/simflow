# Pre-release plan

**Created**: 2026-09-18
**Branch**: `claude/pre-release-checklist-sf69mz`
**Goal**: get SimFlow to a state where it can be announced to a broader audience
(~50 registered users, realistically 5–10 concurrent pilots).

---

## How to run this plan

Each step is self-contained and ends at a **Gate** — a stopping point where the
work is verified and you decide whether to continue, change course, or park it.
Nothing in a later step depends on work in a later phase, so you can stop after
any gate and still have a consistent, deployable tree.

Steps are labelled by who does them:

| Label | Meaning |
|---|---|
| **[server]** | You run it on `simflow.vdwaal.net` — no SSH from here |
| **[decide]** | A choice only you can make; the plan records both branches |
| **[code]** | Code change, made here, with tests, committed to the branch |
| **[verify]** | A check with a pass/fail answer, run before moving on |

Status column: `[ ]` not started · `[~]` in progress · `[x]` done · `[-]` skipped.

**The one rule**: no step in Phase 2 or later ships to production until the
Phase 0 checks it depends on have answers. Those dependencies are named
explicitly on each step.

---

## Already done (pushed to this branch)

| | Change | Where |
|---|---|---|
| [x] | API key lookup narrowed by `api_key_prefix` instead of hashing every stored key | `checklist/plugin_views.py:104` |
| [x] | Regression tests: hash count stays flat at 20 accounts; unknown prefix hashes nothing | `checklist/tests/test_plugin_views.py` |
| [x] | Secure session/CSRF cookies, nosniff, referrer policy | `settings/prod.py` |
| [x] | `SECURE_SSL_REDIRECT` / `SECURE_HSTS_SECONDS` as opt-in `.env` toggles, default off | `settings/prod.py` |
| [x] | Rotating file log for `django.request` and `checklist` | `settings/prod.py` |

388 tests pass on Django 5.2.1 (the version prod installs from `poetry.lock`).
`manage.py check --deploy` is clean apart from the two deliberate opt-outs.

> **Caveat carried into Phase 0**: `settings/prod.py` now calls
> `_LOG_DIR.mkdir(exist_ok=True)` at *import* time. If the release directory is
> not writable by the app user, the settings module raises and the site fails to
> boot. Step 0.5 verifies this before it can bite.

---

## Phase 0 — Checks before anything ships — ✅ COMPLETE

Answers recorded 2026-09-18.

| | Check | Answer | Consequence |
|---|---|---|---|
| [x] | 0.1 Filesystem | **XFS** | SQLite stays. Phase 4 proceeds as written; no MySQL migration. |
| [x] | 0.2 Backup method | **Plain copy** | WAL blocked until `activate.sh` uses the backup API. Spec below. |
| [x] | 0.3 `is_secure()` | **True** (inferred, see below) | 6.2 is safe; `SECURE_PROXY_SSL_HEADER` not needed. |
| [x] | 0.4 Outbound mail | **Available** | 2.2 proceeds; SMTP details needed at that step. |
| [x] | 0.5 Writable release dir | **Confirmed** | The `LOGGING` block already pushed cannot break the boot. |
| [x] | 0.6 Baseline | **0.31 s median** | Lower than predicted. Revises 3.1 — see below. |

### 0.2 result — exact backup specification

`activate.sh` currently copies `db.sqlite3` as a plain file. That is safe today
(rollback journal mode keeps the file self-contained between transactions) but
becomes **unsafe the moment WAL is enabled**: recent commits live in the `-wal`
sidecar, and a plain copy silently omits them. Copying the `-wal` and `-shm`
files alongside is *not* a fix — there is no way to copy all three atomically
while the app is writing, so the result can be inconsistent.

Use SQLite's Online Backup API, which reads through a real connection and
therefore includes WAL content, and produces one consistent file even while the
app is running.

**This change belongs in the [`www_installer`](https://github.com/chezhj/www_installer)
repo, not this one.** Drop-in replacement for the copy step:

```bash
# --- SQLite backup (replaces: cp "$DB" "$DEST") ---
# Consistent even while the app is writing, and WAL-safe.
# Requires the sqlite3 CLI (any version since 3.6.11).
backup_sqlite() {
  local db="$1" dest="$2"

  if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "ERROR: sqlite3 CLI not found - cannot take a consistent backup" >&2
    return 1
  fi

  # .timeout makes the backup wait for a concurrent writer instead of failing
  # with SQLITE_BUSY. 30s is generous; the app's writes are single-digit ms.
  if ! sqlite3 "$db" <<SQL
.timeout 30000
.backup '$dest'
SQL
  then
    echo "ERROR: sqlite3 .backup failed for $db" >&2
    return 1
  fi

  # Never trust a backup that has not been read back.
  local check
  check=$(sqlite3 "$dest" 'PRAGMA integrity_check;' 2>&1)
  if [ "$check" != "ok" ]; then
    echo "ERROR: backup failed integrity_check: $check" >&2
    rm -f "$dest"
    return 1
  fi

  echo "backup ok: $dest ($(stat -c %s "$dest") bytes)"
}
```

Notes:

- The heredoc form avoids shell-quoting problems with the destination path.
  `.backup` takes the target filename; quote it inside the SQL as shown.
- `sqlite3` follows symlinks, so pointing it at the release-tree symlink into
  `shared/simflow/db.sqlite3` works unchanged.
- `VACUUM INTO '<dest>'` is an equivalent one-liner (SQLite 3.27+, 2019) and also
  defragments. It holds a read lock for the whole operation, whereas `.backup`
  yields between pages — prefer `.backup` for a live database.
- A restored backup is a normal database file. Its journal mode is whatever the
  destination defaults to, which does not matter: step 4.1's `init_command` sets
  WAL on every connection.
- **Verify once before trusting it** (step 4.2): take a backup, restore it to a
  scratch path, open it, and confirm the most recent writes are present.

### 0.3 result — how it was established

The probe script in the original plan was a stub that printed a message and
tested nothing. The question is answerable without deploying anything:

Django's CSRF middleware builds the origin it expects from `request.is_secure()`
(`CsrfViewMiddleware._origin_verified`: `"https" if request.is_secure() else
"http"`, plus `request.get_host()`). Browsers send an `Origin` header on POST.
There is no `CSRF_TRUSTED_ORIGINS` in the settings. So if Django believed these
requests were plain HTTP, **every form POST on the live site would already be
failing** with "Origin checking failed — https://simflow.vdwaal.net does not
match any trusted origins".

Logging in on production works, therefore `is_secure()` is already `True`,
therefore `SECURE_PROXY_SSL_HEADER` is unnecessary and `SECURE_SSL_REDIRECT`
(6.2) will not loop.

> Confirm by logging in on production once more before enabling 6.2. If login
> ever starts returning a CSRF failure page, that inference has broken and 6.2
> must be reverted.

### 0.6 result — measured 0.31 s, not the predicted 0.6–0.8 s

The 601 ms I measured for `check_password` was on this build container, which is
**2.3x slower per core** than the production host. Working back from the measured
round trip (0.31 s total, ~45 ms of it network), PBKDF2 on the production
hardware costs **~265 ms**, not 601 ms.

What that changes, and what it does not:

| | Predicted | Actual |
|---|---|---|
| Share of perceived GUI latency | ~⅓ | **~17%** (265 ms of a ~1560 ms average) |
| CPU per flying pilot at 1 Hz | 60% of a core | **26% of a core** |
| Concurrent pilots to saturate one core | 1.7 | **3.8** |

**Step 3.1 is therefore re-framed from a latency fix to a capacity fix.** See the
revised step for the decision.

### 🚦 Gate 0 — passed

## Phase 1 — Security blockers — ✅ COMPLETE

| | Step | Result |
|---|---|---|
| [x] | 1.1 Rotate the two account passwords | Done on production. |
| [x] | 1.2 Stop tracking `db.sqlite3` | Untracked and gitignored. History deliberately left alone — purging it means a force-push on a public repo to scrub two hashes whose passwords are already changed. |
| [x] | 1.3 Confirm no other secrets tracked | Clean. No `.env` tracked; the only name matches were password-reset *template* files. The `.env` rule was broadened: only `settings/.env` was ignored, but python-decouple also searches the repo root, so a root `.env` had been committable. |

> **Follow-on for 7.1**: with `db.sqlite3` untracked, a fresh clone has no
> database. The README must document the bootstrap — `migrate`, then
> `checklist_content import`.

### 🚦 Gate 1 — passed

---

## Phase 2 — Correctness blockers

| | Step | Result |
|---|---|---|
| [x] | 2.1 Delete the stale `requirements.txt` | Deleted and gitignored. Verified safe first: the `ship` job regenerates it from `poetry.lock` **before** the rsync, and the rsync does not exclude it, so the server always receives a correct one. |
| [x] | 2.2 Make password reset work — *code* | `prod.py` reads the `EMAIL_*` settings from `.env`; registration now requires an email address. |
| [ ] | 2.2b **[server]** Fill in `.env` | **Outstanding — see below.** |
| [ ] | 2.3 **[verify]** End-to-end reset on production | Blocked on 2.2b. |

### 2.2 — what shipped

- `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`,
  `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `EMAIL_USE_SSL`,
  `DEFAULT_FROM_EMAIL`, `SERVER_EMAIL` — all from `.env`.
- `EMAIL_TIMEOUT=10`. Django ships **no** default, so a black-holed SMTP host
  holds a Passenger worker open indefinitely; a few reset requests against a
  dead mail server would take the site down.
- Registration requires an email (option (a)). Both existing accounts already
  have one, so nothing to backfill.
- Tests: rejected without an email and with a malformed one; address is stored;
  a reset delivers a message from the configured sender; the link in it actually
  resets the password; an unknown address sends nothing but returns the same
  response as a hit.

Four existing tests posted an empty email while asserting some *other* failure.
Two would have started passing for the wrong reason and two would have broken
outright, so each now sends a valid address.

### [x] 2.2b **[server]** Set the mail variables in `.env` — DONE

`EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` and `FROM_EMAIL` are set, which is what
the other apps on this server use. **That is now enough**, but only after a fix:
the settings as first written would have failed silently.

| Setting | I had written | Django's default | Now |
|---|---|---|---|
| `EMAIL_PORT` | 587 | **25** | 25 |
| `EMAIL_USE_TLS` | True | **False** | False |
| from-address key | `DEFAULT_FROM_EMAIL` | — | `DEFAULT_FROM_EMAIL` (the `.env` uses this name) |

The other apps work on three variables because they run on Django's defaults —
the cPanel host's local Exim on `localhost:25`, which accepts mailbox
credentials. Sending to `localhost:587` with STARTTLS would simply not have
arrived, with nothing in the `.env` to explain why.

Every default now matches Django's exactly, so this app behaves like its
neighbours. `EMAIL_TIMEOUT=10` is the one deliberate departure — Django ships no
timeout, and a black-holed host would hold a Passenger worker open forever.

`.env.example` now documents every variable the app reads and which three
actually matter.

### [ ] 2.3 **[verify]** End-to-end reset on production

Register a throwaway account, request a reset, confirm the mail arrives (check
spam — a new sending domain often lands there) and the link works. Delete the
account afterwards.

> If mail lands in spam, SPF and DKIM for `simflow.vdwaal.net` are the next
> thing to check in cPanel. Not a blocker, but a reset users never see is the
> same as no reset.

### 🚦 Gate 2 — blocked on 2.2b and 2.3

---

## Phase 3 — Latency and capacity

This is the phase that fixes "feels laggy". Measured budget today:

| Stage | Typical | Worst |
|---|---|---|
| Wait for next flight-loop tick (1 Hz) | 500 ms | 1000 ms |
| Read datarefs + spawn thread | ~2 ms | ~3 ms |
| Network round trip | ~45 ms | 150 ms |
| **Server: PBKDF2 on the API key** | **265 ms** | **530 ms** (2 accounts, O(n) scan) |
| Server: UPDATE + rule evaluation | 5–20 ms | 50 ms |
| Wait for next browser poll (1.5 Hz) | 750 ms | 1500 ms |
| **Total** | **~1.56 s** | **~3.2 s** |

Measured against production in step 0.6 (0.31 s median for the POST). The two
polling waits are **80% of it** — which is why 3.2 and 3.5 matter more for
perceived responsiveness than 3.1 does.

Do these **in order**, re-running the 0.6 measurement after each, so each
change's effect is attributable.

### [ ] 3.1 **[decide + code]** Replace PBKDF2 with SHA-256 for API keys

**Re-framed after 0.6: this is a capacity fix, not a latency fix.** On the real
hardware PBKDF2 costs ~265 ms, which is only ~17% of perceived GUI latency. It
is still 26% of a CPU core per flying pilot.

**What must ship regardless**: the prefix-narrowed lookup already on this branch.
Production still runs the O(n) scan from v2.7.0, which at today's two accounts
costs ~0.53 s per plugin request and scales linearly:

| Accounts with keys | Per plugin request, deployed code |
|---|---|
| 2 (today) | 0.53 s |
| 10 | 2.65 s |
| 25 | 6.62 s |
| 50 | 13.25 s |

The plugin posts every second, so past a handful of accounts the backlog
compounds rather than settling. **That alone makes releasing this branch a
blocker**, independent of the decision below.

**The decision**: with the prefix fix alone, each plugin request costs one
~265 ms hash — **3.8 concurrent pilots saturate one CPU core**. On a CloudLinux
LVE (commonly capped at 100–200% CPU) that cap is reachable on a weekend evening
with 50 registered users, and when it is hit the host throttles *everything*, not
just the plugin endpoint.

| Option | Ceiling | Cost |
|---|---|---|
| **(a) Prefix fix only** | ~4 concurrent pilots per core | Already done |
| **(b) + SHA-256** (recommended) | No meaningful ceiling | Migration + lazy upgrade |

Recommend **(b)**. `generate_api_key` mints `secrets.token_urlsafe(32)` — 256
bits of CSPRNG output. A slow KDF exists to make *low-entropy, human-chosen*
passwords expensive to brute-force offline; a 256-bit random token has no
brute-force surface, so the 265 ms buys nothing. This is what DRF's token auth
and GitHub PATs do.

Design — **hard cutover, no bridge**:
- Add `api_key_sha256` (indexed, unique, nullable) and **remove `api_key_hash`
  in the same migration**. Nothing was deployed yet, so this is one migration
  rather than an add now and a drop later.
- Store `hashlib.sha256(raw.encode()).hexdigest()`; resolve with one indexed
  lookup, inline in `require_api_key`.
- **Existing keys are invalidated.** Every current holder regenerates on the
  profile page and pastes into `config.ini`, then restarts X-Plane (the plugin
  reads its config at start). The plugin already logs
  `authentication failed — check api_key in config.ini` at ERROR on a 401.
- Keep `api_key_prefix` — the profile page displays it
  (`registration/profile.html:123`).

A lazy-upgrade bridge was built first and then removed. It worked, but it cost
16 lines of production code and 100 of test, plus a column that had to be
dropped later on a judgement call — a count that would never reach zero on its
own, because a key only upgrades when it is used. Spending that to spare one
known user a single paste was the wrong trade while the user count is still
countable on one hand. Doing it before announcing is the same reasoning that
pulls the plugin work forward: free today, a support thread per user later.

Tests: a current key resolves with 20 other accounts present; an unknown key
401s; and a PBKDF2 hash sitting in the digest column does **not** authenticate —
the cutover contract, which fails if anyone re-adds a fallback and puts the KDF
back on a path the plugin hits every half second.

> **Lighter alternative if you want no migration before launch**: a per-worker
> in-memory cache of `sha256(raw_key) → profile_id` with a short TTL. ~15 lines,
> no schema change, keeps PBKDF2 as the source of truth. The tradeoff is that a
> revoked key keeps working until its TTL expires. Mentioned for completeness;
> (b) is cleaner and has no revocation lag.

### [ ] 3.2 **[code]** `POLL_INTERVAL_MS` 1500 → 750

−375 ms average, browser side. `poll_view` is read-only (its one
`session.save()` is guarded by `if new_state != prev_state`,
`api_views.py:155`) and its query count is already flat and ceiling-tested
(`test_query_counts.py`), so this costs reads only. One line in `base.py`.

### [ ] 3.3 **[code]** Cache `getDataRefTypes` in the plugin

`PI_xFlow.py:250` and `:285` call `xp.getDataRefTypes(dref)` **every tick for
every dataref**. A dataref's type never changes. Caching it next to the handle
in `self._drefs` halves the main-thread XPLM call count for free.

Context for the FPS budget: the watch list is **27 datarefs baseline**, worst
case **109** (preflight — on the ground, where frames are cheap). In the air it
is well under half that. Main-thread cost is ~1–2 ms per tick, i.e. one
slightly-long frame once per second.

### [ ] 3.4 **[code]** Persistent worker thread instead of one per tick

`PI_xFlow.py:317` spawns a `threading.Thread` every tick. A single daemon worker
reading a queue removes the per-tick allocation from the main thread. Small, but
it is the prerequisite for 3.5 doubling the tick rate.

### [ ] 3.5 **[code + decide]** Flight loop 1 Hz → 2 Hz, configurable

−250 ms average. Do this **after** 3.3 and 3.4, and expose it in `config.ini`
(`cfg.get("xflow", "poll_interval", fallback=...)`, matching the existing
pattern at `:125`) so anyone on a weak rig can back off. **[decide]** what the
shipped default should be — 2 Hz for responsiveness, or 1 Hz with 2 Hz opt-in.
Recommend shipping 2 Hz: the measured cost is ~2 ms of main thread per tick.

### [ ] 3.6 **[code]** Conditional heartbeat write

The plugin already computes `changed` at `:303` and then only logs it. Use it:

- **changed** → write both `last_plugin_contact` and `last_datarefs`. This is
  exactly when latency matters, so nothing is ever deferred.
- **unchanged** → write only if the stamp is older than ~5 s. The connection
  badge tolerates 30 s (`_PLUGIN_TIMEOUT_SECONDS`).

POST rate unchanged, detection latency unchanged. A stable cockpit writes once
per 5 s instead of 5 times; while you are flipping switches it writes every
tick. Writes are skipped only when, by definition, nothing happened.

**Important**: skip the *write*, never the *evaluation*. The gate can move
because the pilot checked something in the browser, so rules must still be
evaluated on every POST even when datarefs are identical.

### [ ] 3.7 **[verify]** Re-measure

Re-run 0.6 and time a real switch-flip to GUI update in the sim. Target:
**~1.9 s → ~0.7 s typical**.

### 🚦 Gate 3
Latency measurably improved, all tests green, plugin still behaves in the sim.

---

## Phase 4 — Database tuning — *depends on 0.1 and 0.2*

**If 0.1 said NFS, this phase is replaced by a MySQL migration** and everything
below is void. Otherwise:

### [ ] 4.1 **[code]** SQLite WAL, IMMEDIATE transactions, busy timeout

Blocked until 0.2 confirms the backup is WAL-safe.

```python
"OPTIONS": {
    "timeout": 20,
    "transaction_mode": "IMMEDIATE",
    "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
}
```

Django 5.1+ supports `init_command` and `transaction_mode` natively, and you are
on 5.2.1. `IMMEDIATE` is the one that matters most under Passenger: multiple
worker processes on deferred transactions is the classic route to "database is
locked" even at low load. WAL also stops readers blocking writers, which is what
lets 3.2's doubled poll rate stay free.

### [ ] 4.2 **[verify]** Confirm WAL is live and the backup round-trips

```bash
sqlite3 db.sqlite3 'PRAGMA journal_mode;'   # expect: wal
```

Then take a backup by whatever route `activate.sh` uses, restore it to a scratch
copy, and confirm the most recent writes are present.

### 🚦 Gate 4

---

## Phase 5 — Operational hygiene

None of these are launch blockers, but all of them bite within the first weeks.

### [ ] 5.1 **[code + server]** Session table cleanup
No `clearsessions` anywhere, and sessions are database-backed, so every
anonymous visitor leaves a row that never expires out of the table. Add a cron
entry (daily `manage.py clearsessions`).

### [ ] 5.2 **[code]** Retention for `FlightSession` and session logs
`FlightSession` rows are marked inactive but never deleted, each carrying a
`last_datarefs` JSON blob. `logs/session_<id>.jsonl` is one file per session,
forever. Both land in the SQLite file that gets backed up on every deploy.
Proposal: a management command deleting inactive sessions and their log files
after N days, wired into the same cron.

### [ ] 5.3 **[code]** Custom 404 and 500 templates
With `DEBUG=False`, public visitors currently get Django's bare white pages.

### [ ] 5.4 **[decide + code]** Rate limiting
Nothing throttles `/login/`, `/register/` or `/admin/`. Options: `django-axes`
(full-featured, adds a dependency and tables), a small middleware, or Apache/
cPanel-level throttling. **[decide]** which fits the shared-hosting constraints.

### 🚦 Gate 5

---

## Phase 6 — Production hardening — *depends on 0.3*

### [ ] 6.1 **[server]** Set `SECURE_PROXY_SSL_HEADER` if 0.3 requires it
Already stubbed in `prod.py`; uncomment only if 0.3 showed `is_secure=False`
with a usable forwarded header.

### [ ] 6.2 **[server]** Enable `SECURE_SSL_REDIRECT`
Set in `.env` once 0.3 confirms Django sees requests as secure. Verify
immediately afterwards that the site still loads — this is the setting that
loops if the answer was wrong.

### [ ] 6.3 **[server]** Ramp HSTS
`SECURE_HSTS_SECONDS`, in stages: `3600` → one day → one week → one year,
checking the site between each. Browsers honour it even if the certificate later
lapses, which is why it ramps rather than jumping to a year. Do **not** add
`includeSubDomains` until every host under `vdwaal.net` is HTTPS.

### 🚦 Gate 6

---

## Phase 7 — Documentation and launch readiness

This is what decides whether people who arrive from the announcement stay.

### [ ] 7.1 **[code]** Rewrite `README.md` — *closes issue #30*
Currently three lines and a coverage snippet. Needs: what SimFlow is, that it is
737/Zibo-specific, the X-Plane + XPPython3 requirement, where to get the plugin,
how the API key works, and a screenshot.

### [ ] 7.2 **[decide + code]** Add a `LICENSE`
A public repo with no licence means nobody knows what they may do with it.
**[decide]** which — MIT and Apache-2.0 are the usual choices for something like
this.

### [ ] 7.3 **[code]** Fix stale documentation
- `docs/RELEASE.md` §1 step 2 still describes a manual `install.sh`/`deploy.sh`
  flow; the tag-triggered pipeline replaced it.
- `docs/RELEASE.md` §3 step 4's "`deploy.sh` does not load the fixture yet"
  warning is stale — `POST_MIGRATE_COMMANDS` does it now.
- `CLAUDE.md` says dev uses a WireMock URL so tests never hit the real SimBrief;
  `settings/dev.py` actually points at the live API. Tests mock `requests.get`,
  so they are safe, but the claim is wrong and someone will trust it.
- `docs/TODO-js-deduplication.md` says its branch is unmerged; `6f1a670` is in
  master. Stages 3 and 4 are genuinely still open.

### [ ] 7.4 **[verify]** Fresh-clone smoke test
From a clean clone, follow the README exactly. Anything that does not work as
written is a bug in the README.

### 🚦 Gate 7

---

## Phase 8 — Release

### [ ] 8.1 **[verify]** Full check
`pytest` (388+), `npm test` (16), `manage.py check --deploy`, `djlint checklist/templates/`.

### [ ] 8.2 **[decide]** Content version
If any fixture content moved, `scripts/check_content_bump.py` will refuse the
bump — run `scripts/bump_content_version.py B738 <version>` first.

### [ ] 8.3 `cz bump` and confirm the deploy
Watch the pipeline through activate + smoke. Confirm the tag actually pushed:
`git ls-remote --tags origin "v*"`.

### [ ] 8.4 **[verify]** Post-deploy on production
Register → confirm → log in → reset password → generate API key → connect the
plugin → fly a short leg. Then check `logs/django.log` is empty of errors.

### [ ] 8.5 Announce.

---

## Explicitly deferred — not pre-launch

| Item | Why it can wait |
|---|---|
| SSE / long-poll instead of browser polling | The real fix for the remaining ~375 ms, but a substantial change. Revisit once Phase 3 lands. |
| JS de-duplication stages 3 & 4 (`docs/TODO-js-deduplication.md`) | Internal quality; no user-visible effect. |
| MySQL | Unless 0.1 says NFS. Revisit if you outgrow one app server. |
| Purging `db.sqlite3` from git history | See 1.2 — recommended against. |
| Open issues #17, #21, #24, #25, #28, #29, #32 | Feature work, unrelated to launch readiness. (#30 is closed by 7.1.) |

---

## Dependency summary

```
0.1 (NFS)        → Phase 4 entirely (SQLite tuning vs MySQL migration)
0.2 (backup)     → 4.1 (WAL must not precede a WAL-safe backup)
0.3 (is_secure)  → 6.1, 6.2
0.4 (SMTP)       → 2.2
0.5 (writable)   → validates the LOGGING block already pushed
0.6 (baseline)   → makes 3.7 meaningful

3.3, 3.4         → 3.5 (do not raise tick rate before reducing per-tick cost)
3.1              → dropping legacy key columns (deferred)
```

Everything in Phases 1, 2, 5 and 7 is independent and can be reordered freely.
