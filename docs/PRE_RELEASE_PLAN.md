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

## Phase 0 — Checks before anything ships

These gate later decisions. None of them change anything. Run them all, record
the answers here, then we plan around the results.

### [ ] 0.1 **[server]** Filesystem type — SQLite vs MySQL

```bash
stat -f -c %T ~
df -T ~ | tail -1
```

| Answer | Consequence |
|---|---|
| `ext2/ext3`, `ext4`, `xfs`, `btrfs` | Stay on SQLite. Phase 4 proceeds as written. |
| `nfs`, `nfs4`, or anything network | **Stop.** SQLite's advisory locking over NFS is unreliable — that is corruption, not slowness. Phase 4 is replaced by a MySQL migration, and it becomes a launch blocker regardless of user count. |

### [ ] 0.2 **[server]** How `activate.sh` backs up the database

`activate.sh` lives in [`www_installer`](https://github.com/chezhj/www_installer)
and is not visible from this repo. Find the backup command:

```bash
grep -n -A5 -i 'backup\|cp .*db.sqlite3\|sqlite3' ~/deploy-tools/scripts/activate.sh
```

**Why it matters**: a WAL-mode database keeps recent commits in a `-wal` sidecar
file. A plain `cp db.sqlite3 backup.sqlite3` can silently produce a backup
missing the most recent transactions. If the backup is a plain copy, WAL (step
4.1) must not be enabled until it is changed to either checkpoint first
(`PRAGMA wal_checkpoint(TRUNCATE)`) or use `sqlite3 db.sqlite3 ".backup ..."` /
`VACUUM INTO`.

### [ ] 0.3 **[server]** Does Django see requests as HTTPS?

Gates `SECURE_SSL_REDIRECT` (step 6.2). If Apache terminates TLS and hands
Passenger a plain HTTP request that Django cannot recognise as secure, enabling
the redirect produces an infinite loop and takes the whole site down at once.

```bash
cd ~/domains/simflow_current  # adjust to the live release path
source /home/vdwanet/virtualenv/domains/simflow.vdwaal.net/3.11/bin/activate
python - <<'EOF'
import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "smart_training_checklist.settings.prod")
django.setup()
from django.test import RequestFactory
print("check which of these Apache actually sets, on a real request")
EOF
```

Simpler and more reliable — log it from a live request. Temporarily add to any
view, hit the site over HTTPS, then read `logs/django.log`:

```python
logger.error("is_secure=%s scheme=%s xfp=%r",
             request.is_secure(), request.scheme,
             request.META.get("HTTP_X_FORWARDED_PROTO"))
```

| Answer | Consequence |
|---|---|
| `is_secure=True` | Safe to enable `SECURE_SSL_REDIRECT`. |
| `is_secure=False`, `xfp='https'` | Set `SECURE_PROXY_SSL_HEADER` (already stubbed in `prod.py`), *then* enable the redirect. |
| `is_secure=False`, no header | Do not enable the redirect. Investigate the Apache config first. |

### [ ] 0.4 **[server]** Outbound mail

Gates the password-reset fix (step 2.2).

```bash
# Is there a local MTA?
command -v sendmail; ls -la /usr/sbin/sendmail 2>/dev/null
# Does cPanel offer SMTP credentials for this domain? (check the cPanel UI:
# Email Accounts → Connect Devices for host/port/auth)
```

Record: SMTP host, port, whether auth is required, and which From address the
host will accept without it being marked as spam.

### [ ] 0.5 **[verify]** Release directory is writable by the app user

Confirms the new `LOGGING` block cannot break the boot.

```bash
cd ~/domains/simflow_current && touch ./_writetest && rm ./_writetest && echo WRITABLE
```

If it is not writable, the fix is to point `_LOG_DIR` at `~/domains/shared/simflow/logs`
(which also makes logs survive deploys, a bonus) rather than the release tree.

### [ ] 0.6 **[server]** Baseline measurement — before changing anything

So that every later change can be shown to have helped. With a valid API key:

```bash
KEY="fvw_..."   # from the SimFlow profile page
for i in $(seq 1 10); do
  curl -s -o /dev/null -w '%{time_total}\n' \
    -X POST https://simflow.vdwaal.net/api/plugin/state/ \
    -H "Authorization: Bearer $KEY" \
    -H 'Content-Type: application/json' \
    -d '{"session_id": 1, "datarefs": {}}'
done
```

Record the median. Expect roughly **0.6–0.8 s** — dominated by PBKDF2, not by
network or database. This is the number step 3.1 should collapse.

### 🚦 Gate 0

Report the six answers. Two of them can change the shape of the rest of the
plan (0.1 → MySQL; 0.2 → WAL blocked), so we re-plan here if needed before any
code ships.

---

## Phase 1 — Security blockers

Independent of Phase 0. Can start immediately.

### [ ] 1.1 **[decide + server]** Rotate the two account passwords

`db.sqlite3` is committed to a **public** repo and carries the `admin` and `hjw`
password hashes. `/admin/` is live and unthrottled.

- Change both passwords on production now (`manage.py changepassword <user>`).
- Assume the old ones are compromised and do not reuse them anywhere.

### [ ] 1.2 **[code]** Stop tracking `db.sqlite3`

`checklist/fixtures/checklist_content.json` is the content source of truth and
production symlinks its own database, so the committed file is redundant as well
as a disclosure.

```
git rm --cached db.sqlite3
echo 'db.sqlite3' >> .gitignore
```

**Note**: this removes it from future commits, not from history. Purging history
means a force-push and rewritten hashes on a public repo. Given the content is
two accounts whose passwords 1.1 has already rotated, **the recommendation is to
leave history alone** — the cost and breakage outweigh the residual risk. Your
call; flag it if you want the rewrite.

### [ ] 1.3 **[verify]** Confirm no other secrets are tracked

```bash
git ls-files | grep -iE '\.env|secret|credential|\.key|\.pem'
git log --all --oneline -S 'SECRET_KEY' -- smart_training_checklist/ | head
```

### 🚦 Gate 1
Passwords rotated, database untracked, tests still green.

---

## Phase 2 — Correctness blockers

### [ ] 2.1 **[code]** Delete the stale `requirements.txt`

A UTF-16 file from 2023 pinning **Django 4.1** (EOL December 2023). Production
is unaffected — the deploy regenerates it from `poetry.lock` (5.2.1) and CI uses
`poetry install` — but anyone cloning the repo and running `pip install -r
requirements.txt` gets a four-year-old Django. It is also what made my own first
test run measure the wrong numbers.

Delete it; the release workflow writes a correct one as a release asset.

### [ ] 2.2 **[code]** Make password reset work — *depends on 0.4*

The reset flow is fully wired (`checklist/auth_urls.py`) but no `EMAIL_BACKEND`
or SMTP settings exist in `prod.py` or `base.py`. Django falls back to
localhost:25, so the view 500s. Two parts:

1. Configure SMTP in `prod.py` from `.env` (`EMAIL_HOST`, `EMAIL_PORT`,
   `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`,
   `DEFAULT_FROM_EMAIL`), per 0.4's answers.
2. **[decide]** `email` is currently optional at registration
   (`auth_views.py:74`), so those users have *no* recovery path at all. Options:
   - **(a) Make it required** — recommended. One-line form change, plus a
     migration plan for existing accounts without one.
   - **(b) Leave optional, warn at registration** — "without an email address
     you cannot recover this account."

   Recommend **(a)** for a public launch: option (b) generates support requests
   you cannot resolve.

### [ ] 2.3 **[verify]** End-to-end reset on production

Register a throwaway account, request a reset, confirm the mail arrives and the
link works. Then delete the account.

### 🚦 Gate 2
Reset works end to end on the live site.

---

## Phase 3 — Latency and capacity

This is the phase that fixes "feels laggy". Measured budget today:

| Stage | Typical | Worst |
|---|---|---|
| Wait for next flight-loop tick (1 Hz) | 500 ms | 1000 ms |
| Read datarefs + spawn thread | ~2 ms | ~3 ms |
| Network round trip | 30–80 ms | 150 ms |
| **Server: PBKDF2 on the API key** | **600 ms** | **600 ms** |
| Server: UPDATE + rule evaluation | 5–20 ms | 50 ms |
| Wait for next browser poll (1.5 Hz) | 750 ms | 1500 ms |
| **Total** | **~1.9 s** | **~3.3 s** |

Do these **in order**, re-running the 0.6 measurement after each, so each
change's effect is attributable.

### [ ] 3.1 **[code]** Replace PBKDF2 with SHA-256 for API keys

**The single biggest win — for latency and for capacity.**

`generate_api_key` mints `secrets.token_urlsafe(32)` — 256 bits of CSPRNG
output. A slow KDF exists to make *low-entropy, human-chosen* passwords
expensive to brute-force offline. A 256-bit random token has no brute-force
surface, so the 600 ms buys nothing. Measured here: **601 ms vs 0.7 µs**.

Consequences:
- ~600 ms off every plugin round trip (about a third of the perceived lag).
- Server CPU per flying pilot drops from **60% of one core** to ~0. Right now
  **1.7 concurrent pilots saturate a core** — that, not SQLite, is the real
  ceiling on a CloudLinux LVE.
- An indexed exact-match lookup replaces the prefix scan entirely.

Design:
- Add `api_key_sha256 = CharField(max_length=64, null=True, db_index=True)`.
- Store `hashlib.sha256(raw.encode()).hexdigest()`; compare with
  `hmac.compare_digest`.
- **Lazy migration, no user disruption**: look up by SHA-256 first. On a miss,
  fall back to the existing prefix-narrowed PBKDF2 path; on a successful
  fallback the raw key is in hand, so write the SHA-256 then. The first request
  after deploy costs 600 ms, every one after that is instant. Same pattern
  Django uses to upgrade password hashers on login.
- Keep `api_key_prefix` — the profile page displays it
  (`registration/profile.html:123`).
- Keep `api_key_hash` and the fallback for one release, then remove both.

Tests: SHA-256 path resolves; legacy PBKDF2 key still authenticates; legacy key
is upgraded in place after first use; unknown key 401s without hashing; the
existing flat-cost test still passes.

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
| Dropping the legacy `api_key_hash` column and fallback | One release after 3.1, once every active key has been upgraded. |
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
