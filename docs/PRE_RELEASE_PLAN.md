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
| [x] | 0.2 Backup method | **Plain copy** → **backup API, www_installer v1.3.0** | Released 2026-09-22. 4.1 unblocked; 4.2 confirms it on the server. |
| [x] | 0.3 `is_secure()` | **True** (inferred, see below) | 6.2 is safe; `SECURE_PROXY_SSL_HEADER` not needed. |
| [x] | 0.4 Outbound mail | **Available** | 2.2 proceeds; SMTP details needed at that step. |
| [x] | 0.5 Writable release dir | **Confirmed** | The `LOGGING` block already pushed cannot break the boot. |
| [x] | 0.6 Baseline | **0.31 s median** | Lower than predicted. Revises 3.1 — see below. |

### 0.2 result — exact backup specification

> **✅ Shipped in www_installer v1.3.0** (2026-09-22, PR chezhj/www_installer#4).
> `activate.sh` backs up through SQLite's backup API (verified, then renamed into
> place), keeps the newest `SQLITE_BACKUP_KEEP` (default 10), and
> `rollback.sh --restore-db` restores through the API, with a safety copy taken
> first. Server `sqlite3` is 3.26.0. Plan and as-built notes:
> [`docs/SQLITE_BACKUP_PLAN.md`](https://github.com/chezhj/www_installer/blob/main/docs/SQLITE_BACKUP_PLAN.md).
> The spec below is kept for history; the shipped version differs in detail.
>
> Two corrections to what follows: (1) a plain copy is *not* safe today,
> because `activate.sh` takes it before stopping the app, so the copy can catch
> a commit halfway through; (2) the `rollback.sh --restore-db` path needs the same
> fix. A `cp` restore leaves a stale `-wal` next to the file it restored, and
> SQLite would replay it onto the restored data. Both are fixed in v1.3.0.

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
| [x] | 2.3 **[verify]** End-to-end reset on production | Confirmed working. |

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

### [x] 2.3 **[verify]** End-to-end reset on production — CONFIRMED

Register a throwaway account, request a reset, confirm the mail arrives (check
spam — a new sending domain often lands there) and the link works. Delete the
account afterwards.

> If mail lands in spam, SPF and DKIM for `simflow.vdwaal.net` are the next
> thing to check in cPanel. Not a blocker, but a reset users never see is the
> same as no reset.

### 🚦 Gate 2 — passed

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

### [x] 3.1 **[code]** SHA-256 API keys — DONE (hard cutover)

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

### [x] 3.2 **[code]** `POLL_INTERVAL_MS` 1500 → 750 — DONE

−375 ms average, browser side. `poll_view` is read-only (its one
`session.save()` is guarded by `if new_state != prev_state`,
`api_views.py:155`) and its query count is already flat and ceiling-tested
(`test_query_counts.py`), so this costs reads only. One line in `base.py`.

### [x] 3.3 **[code]** Cache `getDataRefTypes` in the plugin — DONE

`PI_xFlow.py:250` and `:285` call `xp.getDataRefTypes(dref)` **every tick for
every dataref**. A dataref's type never changes. Caching it next to the handle
in `self._drefs` halves the main-thread XPLM call count for free.

Context for the FPS budget: the watch list is **27 datarefs baseline**, worst
case **109** (preflight — on the ground, where frames are cheap). In the air it
is well under half that. Main-thread cost is ~1–2 ms per tick, i.e. one
slightly-long frame once per second.

### [x] 3.4 **[code]** Persistent worker thread instead of one per tick — DONE

`PI_xFlow.py:317` spawns a `threading.Thread` every tick. A single daemon worker
reading a queue removes the per-tick allocation from the main thread. Small, but
it is the prerequisite for 3.5 doubling the tick rate.

### [x] 3.5 **[code]** Flight loop 1 Hz → 2 Hz, configurable — DONE

−250 ms average. Do this **after** 3.3 and 3.4, and expose it in `config.ini`
(`cfg.get("xflow", "poll_interval", fallback=...)`, matching the existing
pattern at `:125`) so anyone on a weak rig can back off. **[decide]** what the
shipped default should be — 2 Hz for responsiveness, or 1 Hz with 2 Hz opt-in.
Recommend shipping 2 Hz: the measured cost is ~2 ms of main thread per tick.

### [-] 3.6 **[code]** Conditional heartbeat write — DROPPED

**It would save nothing.** The step assumed a cockpit can sit with unchanged
datarefs between ticks. It cannot: **12 of the 27 always-streamed datarefs vary
continuously**, in every phase of every session, because they back the
`show_rule`s and attribute `live_rule`s rather than any one procedure —

```
sim/flightmodel/position/latitude, longitude, y_agl, psi, mag_psi
sim/flightmodel/position/indicated_airspeed, vh_ind_fpm
sim/cockpit2/gauges/indicators/altitude_ft_pilot
laminar/B738/autopilot/altitude, altitude_mode
laminar/B738/fuel/center_tank_kgs
sim/weather/temperature_sealevel_c
```

Parked on stand with the APU running, position jitters on float noise, fuel
burns and sea-level temperature drifts. `changed` would be true on effectively
every tick, so the step would add a ~100-key dict comparison per request to skip
a write that is almost never skipped. Net negative.

A version that worked would have to quantise the continuous values before
comparing, or compare only the discrete ones — which changes what "unchanged"
means while rules compare exact values. Not a risk worth taking pre-launch for
something **4.1 (WAL)** solves properly.

> **Consequence**: this was the hedge for WAL being blocked on the
> `backup_sqlite` change in `www_installer` (shipped in v1.3.0, so 4.1 is unblocked). With the hedge gone, that change is
> what write concurrency now rests on — at 2 Hz across ten concurrent pilots,
> ~20 writes/sec against a rollback-journal database where writers block
> readers.

### [ ] 3.7 **[verify]** Re-measure

Re-run 0.6 and time a real switch-flip to GUI update in the sim. Target:
**~1.9 s → ~0.7 s typical**.

### 🚦 Gate 3
Latency measurably improved, all tests green, plugin still behaves in the sim.

---

## Phase 4 — Database tuning — *depends on 0.1 and 0.2*

**If 0.1 said NFS, this phase is replaced by a MySQL migration** and everything
below is void. Otherwise:

> **Measured 2026-09-24 — the latency case for this phase is dead.**
> `scripts/probe_server.py`, run four times from the dev machine against
> production with an active session and the server's own 63-dataref watch list:
>
> | endpoint | work done | median |
> |---|---|---|
> | `GET /` | read-only, no auth | 62–70 ms |
> | `GET /api/plugin/session/` | 3 queries, 1 write | 68–79 ms |
> | `POST /api/plugin/state/` | 15 queries, 1 write, rule evaluation | **93–100 ms** |
>
> All samples returned 200. The write costs 5–10 ms and the whole endpoint
> costs ~30 ms above a static page, so **the 593 ms floor in the flight log is
> not the database and not the server**. fsync-per-commit was the hypothesis
> WAL was meant to fix; it is disproved. Connection setup, by contrast, is
> real: a fresh connection costs 39–58 ms against 19–27 ms for a reused one,
> and the plugin opens a fresh one every call.
>
> The remaining ~500 ms is on the sim side. `xFlow/net_probe` (plugin ≥ next
> release) decomposes it from inside X-Plane's process — see 4.3.

### [ ] 4.1 **[code]** SQLite IMMEDIATE transactions, busy timeout — WAL optional

~~Blocked until 0.2 confirms the backup is WAL-safe.~~ **Unblocked**: the
backup and restore are WAL-safe since www_installer v1.3.0. Make sure the server's
`~/deploy-tools` is on v1.3.0 or later before the release that turns WAL on.

**Re-scoped after the measurement above.** `transaction_mode` and `timeout`
still earn their place: they are about multiple Passenger workers colliding on
writes, which the probe did not test and which shows up as "database is locked"
rather than as latency. WAL is now a nice-to-have for reader/writer
concurrency, not a fix for a problem we have observed — decide it on its own
merits, not on the 593 ms.

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
locked" even at low load. WAL also stops readers blocking writers — worth having
at 50 users, but no longer the load-bearing reason for this phase.

### [ ] 4.2 **[verify]** Confirm WAL is live and the backup round-trips

```bash
sqlite3 db.sqlite3 'PRAGMA journal_mode;'   # expect: wal
```

Then check the backup that deploy's `activate.sh` took. Its log line is
`backup ok: …/db.sqlite3.backup_<ts> (N bytes, T s)`:

```bash
cd ~/domains/shared/simflow
b=$(ls -1 db.sqlite3.backup_* | sort | tail -1)
sqlite3 "$b" 'PRAGMA integrity_check;'                       # expect: ok
sqlite3 "$b" 'SELECT max(id) FROM checklist_flightsession;'  # compare with the live db
```

The restore path does not need testing against the live app. www_installer's
`tests/sqlite_backup_test.sh` covers it: a stale `-wal`, a refused safety copy, and
`--force-restore`.

### [x] 4.3 **[done]** Sim-side 500 ms found: a new TLS context per request

`xFlow/net_probe`, run twice on the sim PC against production:

| stage | median | note |
|---|---|---|
| DNS resolve | 15 ms | |
| TCP connect | 32 ms | RTT ~3x the dev machine's |
| TCP + TLS handshake (raw socket, shared context) | 94 ms | so TLS itself ≈ 62 ms |
| `GET /` fresh connection via `requests` | **579 ms** | |
| `GET /` reused connection | **110 ms** | |

The arithmetic is the finding. `requests` spent 579 − 110 = **469 ms** setting up
a connection that raw sockets set up in **94 ms**. The 375 ms difference moves no
packets: the raw-socket stage built its `SSLContext` once, outside the timing,
while `requests.get()` at module level builds a new `Session`, pool and
`SSLContext` for every call.

> **Correction, measured after the fix.** `SSLContext` creation was the named
> suspect and it is *not* the cost: a dedicated probe stage puts it at **31 ms**.
> Across three runs, `requests`' per-connection setup was 438–454 ms while DNS,
> TCP, the TLS handshake and context creation together accounted for only
> 109–126 ms. **~340 ms remains unidentified.** The likeliest remaining candidate
> is urllib3 loading certifi's CA bundle — a ~290 KB PEM parsed per connection,
> which the context stage does not cover because `ssl.create_default_context()`
> reads the OS trust store rather than certifi's file. Unverified.
>
> The diagnosis (per-call connection setup) and the fix (pool it) were right;
> the mechanism named for it was wrong. Pooling makes it moot — whatever the
> 340 ms is, it is now paid once per session instead of twice a second.

**Fix**: one pooled `requests.Session`, built on first use and closed on
disable (`_http_session`). Verified against a real socket, not mocks: 10 GETs
and 10 POSTs now share **one** TCP connection where the old path opened ten,
and a connection dropped by the server is recovered rather than raising.

**Confirmed on the sim PC**, three runs of the probe carrying both paths:

| | median |
|---|---|
| `GET /` unpooled (old path) | 563–594 ms |
| `GET /` pooled (current) | **125–140 ms** |
| `GET /api/plugin/session/` pooled | 125–156 ms |

**4.4× faster**, comfortably inside the 500 ms tick.

The adapter carries one `connect` retry. The plugin polls every 500 ms so the
connection is rarely idle long enough to go stale, but it does idle out during
an error backoff — and without the retry the first call afterwards fails and
starts a 10 s backoff of its own.

Two things deliberately *not* concluded from this: WAL is still not indicated
(4.1 above), and the remaining gap between the sim PC's 110 ms and the dev
machine's 20 ms is mostly the 3x RTT, not server work.

### [x] 4.4 **[done]** Re-run `net_probe` on the sim PC after the pooling fix

Done — the figures are in the table above. The pooled line landed where the old
reused-connection figure predicted.

### [ ] 4.5 **[verify]** Confirm the win in flight, not just in the probe

Fly and read `state response status 200 in N ms` in `XPPython3Log.txt`. That is
the line that read 593 ms; expect ~125–160 ms. The probe times `GET /`, not the
state POST with a real payload, so this is the number that actually settles it.

The `HTTP backlog full` warnings should also stop: at a 500 ms tick a 593 ms
round trip could not keep up, and ~140 ms has room to spare.

### [ ] 4.6 **[release]** Ship the pooling fix as a plugin release

The fix is only on the sim PC as a hand-copied file. It needs a plugin bump and
release before the announce, or new users get the 593 ms path.

**Optional, not blocking**: the ~340 ms above is unidentified. A probe stage
timing `load_verify_locations(certifi.where())` would confirm or kill the CA
bundle theory. Worth knowing, worth nothing to users — pooling already removed
it from the hot path.

<details>
<summary>Original 4.3 plan, before the probe answered it</summary>

### Find the sim-side 500 ms with `xFlow/net_probe`

Bind `xFlow/net_probe` to a key in X-Plane (Settings → Keyboard), run it on the
sim PC with the sim loaded, and read the block it writes to `XPPython3Log.txt`.
It runs on the HTTP worker thread, so it costs no frames; it times DNS, TCP,
TLS, `GET /`, `GET /api/plugin/session/` and a reused connection, all from
inside X-Plane's own interpreter.

Compare against the dev-machine figures in the box above. What each outcome means:

| net_probe shows | conclusion |
|---|---|
| DNS in the hundreds of ms | name resolution per call — cache the address, or pool the connection |
| TCP+TLS in the hundreds of ms | connection setup on that network path — switch to a pooled `requests.Session` |
| all stages fast, `GET /` still slow | the cost is inside X-Plane's process, not the network |
| everything fast (~60–100 ms) | the 593 ms was transient, or specific to the state payload |

A pooled `requests.Session` is the likely fix in two of those four rows, and the
probe already shows it is worth 20–30 ms per call on its own.

</details>

### 🚦 Gate 4

---

## Phase 5 — Operational hygiene

None of these are launch blockers, but all of them bite within the first weeks.

> **Three corrections to the original outline, from reading the code.** They
> change what 5.2 is actually about.
>
> 1. **The log files are not in the database backup.** `_LOG_DIR` is
>    `BASE_DIR / "logs"` (`plugin_views.py:43`, `settings/prod.py:74`), and
>    `BASE_DIR` is the *release* directory. `SHARED_PATHS=()` in
>    `deploy/simflow_config.sh`, so `logs/` is not symlinked out of the
>    release. Nothing under `logs/` reaches `db.sqlite3` or the backup taken
>    before each `migrate`.
> 2. **`logs/django.log` is therefore discarded on every deploy** — the error
>    log added in Phase 1, gone at each release. Not what was intended, and it
>    is a separate defect from retention. New item 5.5.
> 3. **The session `.jsonl` files are bounded by the release, not unbounded
>    forever.** They still grow without limit *within* a long-lived release,
>    which is the case between releases, so retention is still worth having —
>    but the urgency is lower than the outline implied, and the fix is
>    different once 5.5 moves them somewhere persistent.

---

### [x] 5.1 / 5.2 **[done]** Retention: flight sessions, logs, expired sessions

**Decided**: keep **4** flight sessions per account, drop ownerless sessions
untouched for **30** days.

Built as one command, `checklist_prune`, over `checklist/maintenance.py`, doing
all three growing things at once: `FlightSession` and its cascade, the
`logs/session_<id>.jsonl` files that 5.5 made persistent, and expired
`django_session` rows.

**The clock is usage, not release cadence.** `run_cleanup_if_due()` is called
at the start of a flight, behind a once-per-24-h gate. The deploy hook alone
would have fired as often as releases happen, which for a settled product may
be twice a year — and these tables only grow when the app is used.
`POST_MIGRATE_COMMANDS` keeps `checklist_prune --noinput` as belt-and-braces
for the case where nobody flies but visitors keep browsing.

**The gate is a conditional UPDATE, not a cached timestamp.** No `CACHES` is
configured, so `LocMemCache` is per-Passenger-worker and each worker would run
its own daily sweep. `MaintenanceState.claim()` is one atomic statement, so of
ten simultaneous callers exactly one wins — asserted directly in
`test_exactly_one_of_many_simultaneous_claims_wins`. The claim is taken
*before* the work, so a crashed run cannot hold the gate open.

**Three protections, each with a test:**

| rule | why |
|---|---|
| `is_active` sessions survive the keep-N sweep | starting a flight deactivates a user's others, so the active one is always newest — but deleting a session a pilot is flying is not a risk worth taking on an invariant holding |
| `is_active` is **ignored** for orphans | an anonymous session is only deactivated through its own browser session key; lose the cookie and it stays active forever, so honouring the flag would make the sweep a no-op for exactly the rows it collects |
| age is `last_plugin_contact`, falling back to `created_at` | the plugin stamps it at 2 Hz, so a session opened 60 days ago and still being flown is never collected on age alone |

A stray log file for a session that no longer exists is bounded by mtime as
well as existence, so a file written just after the pk query is never caught by
the race. The trigger swallows and logs every failure: a pilot who cannot start
a checklist because housekeeping failed is a far worse outcome than a table
that grows for another day.

**Verified end to end** against a real SQLite file and real migrations, not
only in tests: 7 owned sessions → 4 kept, a 45-day orphan deleted, a 5-day
orphan kept, 4 log files removed, and `--dry-run` wrote nothing. 29 tests in
`checklist/tests/test_maintenance.py`.

**Knobs** in `settings/base.py`: `CLEANUP_KEEP_SESSIONS_PER_USER`,
`CLEANUP_ORPHAN_DAYS`, `CLEANUP_MIN_INTERVAL_HOURS`.

**Remaining gap, unchanged**: browsing traffic with no flying traffic fires
nothing between deploys. Bounded (~400 bytes a row, ~4 MB per 10,000 visitors)
and only a daily cron closes it completely. Not worth the cPanel dependency
now; revisit if the numbers ever say so.

**Before the first production run**: `manage.py checklist_prune --dry-run` on a
copy of the live database, to see the real counts before anything is deleted.

### [x] 5.3 **[done]** Custom 404, 500 and CSRF-failure pages

Three templates at `checklist/templates/` — the app template root, since
`DIRS: []` and `APP_DIRS: True` means Django resolves the bare names. Nothing
installed ships those names, and `checklist` is first in `INSTALLED_APPS`.

Wording is in the aviation register the rest of the UI uses: *Off the charts*,
*Unable to comply*, *Clearance expired*. Each offers one way out, home.

**The design constraint, verified in the installed Django source:**

| handler | renders with | context processors |
|---|---|---|
| `page_not_found` | `template.render(context, request)` | **yes** |
| `server_error` | `template.render()` | **no** |
| `csrf_failure` | `t.render(request=request)` | yes, but no context dict |

So `404.html` and `403_csrf.html` extend `base.html` and keep the nav, while
**`500.html` is standalone**. Extending base there would resolve `sop`,
`user` and every `request.session.sb_*` key to empty — Django does not raise on
a missing variable, so it would render as a shell with the conn-bar reading
"ORIG → DEST", no SOP and a signed-out nav. `Test500IsStandalone` guards this
directly: one test renders it against a genuinely empty context, another
asserts it contains none of `conn-brand`, `info-panel` or `id="page"`, so a
later tidy-up that makes the three files look alike fails before it ships.

`403_csrf.html` was added beyond the original scope: an expired login form
currently shows Django's *"CSRF verification failed. Request aborted."*, which
reads as an accusation rather than a stale token. Note `csrf_failure` passes no
context dict, so `reason`, `no_referer` and `no_cookie` are **not** available —
a test asserts the template does not reference them.

`{% static %}` is safe inside the 500 handler: no `STATICFILES_STORAGE` or
`STORAGES` is configured, so it is plain concatenation with `STATIC_URL` and
cannot raise on a missing manifest. Only `tokens.css` and `components.css` are
pulled in, with a few lines of inline centring standing in for `layout.css`'s
`.page`, which assumes a flex body the standalone page does not have. The home
link is hardcoded to `/` so a `NoReverseMatch` cannot drop the pilot to
Django's bare fallback.

**Two Django rules cost a round each and are worth remembering**: `{# #}` is
single-line only, so a multi-line one leaves any `{% %}` inside it to parse as
a real tag; and `{% extends %}` must be the first tag, so a `{% comment %}`
header has to sit below it.

**Verified by rendering, not only by assertion**: all three pages were rendered
against a real database and read back — 404 and 403 with the full shell and the
SOP row populated, 500 standalone and correct with no context at all. 13 tests
in `checklist/tests/test_error_pages.py`; `djlint` clean.

**Not done**: `400.html`. It is almost entirely bots with bad `Host` headers,
renders with no context like 500, and no real user sees it.

### [x] 5.4 **[done]** Login throttling — django-axes

**Decided**: `django-axes` 8.3.1, for the reason the options table gave — its
default handler is `AxesDatabaseHandler`, so counters live in the database. A
cache-backed limiter would fall back to `LocMemCache`, which is per-Passenger
worker, and an attacker would get `failure_limit × n_workers` attempts.

**Every configured value overrides an axes default that is wrong here.**
Dropping any line is a bug, not a simplification, and each has a test:

| setting | axes default | ours | why |
|---|---|---|---|
| `AXES_FAILURE_LIMIT` | 3 | **5** | 3 is reachable by honest mistyping on a phone |
| `AXES_COOLOFF_TIME` | `None` | **30 min** | `None` means the lockout **never expires** and needs `manage.py axes_reset` by hand |
| `AXES_RESET_ON_SUCCESS` | `False` | **True** | otherwise failures bank up across successful logins forever |
| `AXES_LOCKOUT_PARAMETERS` | `["ip_address"]` | **`[["username", "ip_address"]]`** | see below |

**The lockout parameter is the one that matters.** A nested list is AND in
axes; a flat list is OR.

- `ip_address` alone — one NAT'd club, household or mis-detected proxy locks
  out everyone behind it.
- `username` alone — trivial denial of service: anyone who knows a username
  can lock that pilot out of their own checklist.
- the combination — an attacker locks only their own address against that
  username; the real pilot, from their own address, is unaffected.

The cost is that a distributed attacker gets 5 tries per address. For ~50
accounts that is the right trade: password strength is the actual defence, and
locking a real pilot out mid-preflight is the failure that would hurt.
Two tests cover exactly these two failure modes, so a later "simplification"
to a flat list fails loudly.

`AXES_IPWARE_META_PRECEDENCE_ORDER` is left at `("REMOTE_ADDR",)`. Trusting
`X-Forwarded-For` without a known proxy count lets a client spoof its address
and bypass the lockout; a test asserts a forwarded header cannot move the
recorded address.

**Lockout page**: `checklist/templates/lockout.html`, "Hold short", 429. axes
renders it through `render(request, ...)`, so context processors run and it
extends `base.html`. The wait is rendered by a new `natural_duration` filter
reading axes' own `cooloff_timedelta` — "30 minutes" rather than "0:30:00",
and from the same setting the lockout uses, so the copy cannot drift. The
attempted username is not echoed, for the same reason as the 404 path.

Lockouts now reach `logs/django.log`: the `axes` logger is not under `django`,
so without an explicit entry in `prod.py` it propagated to a handler-less root
and Python's lastResort sent it to stderr — the server error log, not ours.

> **A real bug this caught, which had nothing to do with throttling.** Adding
> `AxesStandaloneBackend` gave the project two `AUTHENTICATION_BACKENDS`, and
> `login(request, user)` can only infer the backend when there is exactly one.
> `register_view` calls `login()` directly on a freshly created user, never
> through `authenticate()`, so it began raising `AttributeError: 'User' object
> has no attribute 'backend'` — **registration was broken outright**. Fixed by
> naming `ModelBackend` explicitly (`AxesStandaloneBackend` only ever raises
> for a locked-out attempt; it never returns a user, so it can never be the
> backend a session was established with). Four existing tests caught it, and
> a dedicated test now lives beside the change that caused it.

**Two gaps this does NOT close**, stated rather than implied:

- **Registration spam.** axes throttles failed *authentication*. Bulk account
  creation at `/register/` is untouched. If it ever happens, the answer is a
  CAPTCHA or email confirmation before activation, not axes.
- **Password-reset email flooding.** The reset form is not an authentication
  attempt, so it is not counted. Django's reset view already refuses to
  confirm whether an account exists, so enumeration is covered; volume is not.

17 tests in `checklist/tests/test_login_throttling.py`. Verified by driving a
real lockout end to end against a live database: five failures returned 429
with the custom page, and the *correct* password was then refused.

**On the server**: `manage.py axes_reset` clears all lockouts,
`axes_reset_username <name>` clears one. Worth knowing before the first
support message.

### [x] 5.5 **[done]** Stop discarding `logs/` on every deploy

`logs/django.log` — `django.request` errors at ERROR and everything from
`checklist` at INFO — was written inside the release directory and was not in
`SHARED_PATHS`, so each deploy started a new empty one and the previous
release's history went with the old release tree. The same for
`logs/session_<id>.jsonl`. That undercut the Phase 1 logging work: the first
thing wanted after an incident is the log from *before* the fix was deployed.

**Done**: `SHARED_PATHS=("logs")` in `deploy/simflow_config.sh`, so
`activate.sh` symlinks `~/domains/shared/simflow/logs` into each release.

The two preconditions were confirmed by the operator rather than by me:
`activate.sh` creates a shared path that does not yet exist, and the directory
is writable by the Passenger user (media lives in the same place for other
apps on this host). `settings/prod.py:75` calls `_LOG_DIR.mkdir(exist_ok=True)`
at import time, so a *dangling* symlink would raise `FileExistsError` during
startup and take the site down — `exist_ok` only suppresses when the path is
already a directory. Worth knowing if the shared directory is ever moved.

**Consequence for 5.2**: the `.jsonl` files are now genuinely unbounded rather
than bounded by the release, so retention has to cover them. Factored in below.

**Verify after the next deploy**: `ls -l <release>/logs` shows a symlink, and
`django.log` still holds entries written before that deploy.

### [x] 5.7 **[done]** The compatibility window is derived, not configured

`PLUGIN_MIN_VERSION` and `PLUGIN_WARN_BELOW` are gone. The rule is now
semantic, in `plugin_status_for_version`:

| reported version | status |
|---|---|
| a **major** behind | `blocked` — the wire protocol changed |
| a **minor** behind | `warn` — still works, missing fixes |
| a **patch** behind | `ok` — patch releases never move the window |
| equal or **newer** | `ok` |

**Newer is deliberately `ok`, not blocked.** A plugin release is tagged
*before* the app release that accompanies it, so there is always a window
where a keen pilot runs a plugin this app has not heard of. Blocking them
would punish them for the project's own release order, and the app is the one
behind. An unreadable version stays `warn`, as before.

> **The planned implementation would not have worked.** 5.7 named "parse
> `PI_xFlow.py` at startup" as closest to "no constants". It is not viable:
> `.github/workflows/release-deploy.yaml` line 134 excludes `xplane_plugin`
> from the rsync, so `PI_xFlow.py` **is not present in a deployed release**.
> Parsing it would have worked in dev and in tests and silently done nothing
> in production — the worst of the three outcomes.

So the source of truth is `checklist/plugin_version.py`, a generated file
holding one string, written by `scripts/bump_plugin.py` in the same commit
that bumps `PI_xFlow.py`. That is a derived fact rather than a policy knob:
the two thresholds that had to be reasoned about are gone, and what remains
cannot be got wrong by hand because nothing maintains it by hand.
`test_the_marker_matches_the_plugin_source` fails if the two ever drift —
the only place both files exist together is a test run from the repo.

**This closes the deferred decision from the 1.2.0 release.** With
`CURRENT_PLUGIN_VERSION = "1.2.0"`, verified against the real released
versions: `0.7.0` blocked, `1.0.2`/`1.1.0`/`1.1.2` warned, `1.2.0` and above
ok. 1.1.x pilots are now nudged toward 1.2.0 automatically, which is what the
hand slide would have done — without the hand slide, and without the risk of
doing it in the wrong order.

Note this is *more lenient* than the old sliding-window policy would have
been: 1.0.2 now warns rather than being blocked, because it shares a major
with the current release. That is the right answer — 1.0.2 works, it is just
old — and it is now a consequence of the rule rather than a judgement call
made per release.

### [x] 5.6 **[done]** `_last_gate_item` is bounded

`plugin_views.py` kept a module-level `dict[int, int | None]` keyed by
flight-session id, written on every gate change and never pruned. It grew for
the life of the worker process, one entry per session that worker ever served.

Now an `OrderedDict` capped at 256 entries with least-recently-**used**
eviction — reads count as use, so a session being polled every few seconds is
not evicted by a burst of new ones just because its gate has not moved. The
only consequence of an eviction is a duplicate `gate_changed` line if a
long-dormant session returns, which is harmless: the log is an audit trail,
not a state machine.

`None` is a real value (no gate item in this phase) and is kept distinct from
the unseen sentinel `_GATE_UNSEEN = -1`; collapsing the two would either lose
a `gate_changed` line or emit a spurious one. 8 tests in
`checklist/tests/test_gate_cache.py`.

Always was housekeeping rather than a leak that would bite — two ints an
entry, cleared by a restart — but unbounded growth in a long-lived process is
not worth keeping.

### 🚦 Gate 5

**Done**: all of 5.1–5.7.

**Decisions still needed**: none.

**Phase 5 is complete.** Next: Phase 6 (server-side SSL redirect and the
HSTS ramp), then Phase 7 — the actual announce blocker.

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

### [ ] 7.2 **[code]** Add a `LICENSE` — **decided: MIT**
A public repo with no licence means nobody knows what they may do with it.
~~**[decide]** which~~ — **MIT**, chosen 2026-09-27. Add `LICENSE` at the repo
root with the standard text, and name it in the README and in
`pyproject.toml`'s `license` field.

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
0.2 (backup)     → 4.1 (WAL must not precede a WAL-safe backup) — met: www_installer v1.3.0
0.3 (is_secure)  → 6.1, 6.2
0.4 (SMTP)       → 2.2
0.5 (writable)   → validates the LOGGING block already pushed
0.6 (baseline)   → makes 3.7 meaningful

3.3, 3.4         → 3.5 (do not raise tick rate before reducing per-tick cost)
3.1              → dropping legacy key columns (deferred)
```

Everything in Phases 1, 2, 5 and 7 is independent and can be reordered freely.

---

## S4 / S5 as built

**Where the banner lives.** Flush under `.conn-bar`, not inside it. `.conn-bar`
is a flex row whose layout is re-asserted at both the 600 px and 900 px
breakpoints, so turning it into a column wrapper to hold a second child would
have fought both. Full width and hard against it, so it reads as part of the
connection bar.

**Unreadable version warns, it does not block.** Six existing tests failed when
an absent `X-Plugin-Version` was first treated as `(0, 0, 0)` and therefore
below the minimum. They were right to: blocking withholds session data and
stops the checklist following the sim, which is far too much to do to a client
that merely failed to identify itself. Only a version explicitly below
`PLUGIN_MIN_VERSION` is blocked. An unknown version still never passes as
current.

**The window.** `MIN = (1, 0, 2)`, `WARN_BELOW = (1, 1, 0)`. Setting MIN to the
current release instead would leave the warn band empty — `blocked` is tested
first and would catch everything below — so nobody would ever see the warning.
`test_the_warn_band_is_not_empty` guards that.

**Dismissal is per page load**, deliberately not persisted: the notice should
come back next session while the plugin is still out of date.

---

## Plugin packaging fix (alongside 3.5)

The release zip contained `xFlow/config.ini` — the exact path the plugin reads
— while its own README said to extract into `PythonPlugins/`. Every upgrade
therefore overwrote the installed config, resetting `api_key`, `backend_url`
and `poll_interval` to template values, after which the plugin logs
`api_key not set — edit config.ini` and does nothing.

It now ships `config.ini.example`. A name the plugin does not read cannot
clobber anything. Verified by building the zip as the workflow does and
extracting it over an install holding a real key.

The same install steps lived in three places — the workflow's README, the
`PI_xFlow.py` module docstring, and `docs/RELEASE.md`'s zip layout. All three
are updated.

> **Affects step 4 of the release run**: the zip no longer supplies
> `config.ini`, so a first install now needs `cp config.ini.example config.ini`.
> An existing install just extracts over the top and keeps its settings.
