# ADR-003: Server-Authoritative Phase State, and a Testing Strategy for Browser Logic

**Date**: 2026-09-11
**Status**: Accepted
**Deciders**: h (project owner)

> Numbering note: two existing records are both titled ADR-001
> (`20260322-phase1-step5-checked-state.md`, `20260326-phase3-alpha-plugin-bootstrap.md`).
> The highest distinct number in use is 002, so this is 003.

## Context

Three production bugs in one week all lived in the ~1,100 lines of inline browser
JavaScript in `detail.html` (785) and `idle.html` (303):

1. A per-process dataref cache made polls answer from whichever worker served
   them, flickering warn rows (fixed in `4fac615`).
2. `plugin_check_next` disagreed with the gate about item visibility
   (fixed in `19329c5`).
3. Auto-advance abandoned an unchecked safety item — "Engine (n) stable /
   Starter cutoff" — because the browser re-derived "is this procedure complete"
   by counting DOM nodes, on a view one poll out of date (fixed in `1b1bd73`).

Bug 3 was confirmed on two real flights from `logs/session_105.jsonl` and
`logs/session_106.jsonl`. None of the three could be caught by the existing
suite: 4,465 lines of pytest cannot execute browser JavaScript.

Two structural facts shaped this decision:

- **There was no CI on push or PR.** Both workflows triggered on tags only, so
  `pytest` ran exactly once — inside the release pipeline, as a deploy gate.
- **Nine functions are duplicated verbatim** between the two templates, including
  `updateConnectionBadge`. The browser-clock bug in `4fac615` existed twice and
  had to be fixed in both copies.

## Decision

### 1. The browser may render optimistically, but must never compute completion

The recurring root cause is the browser re-deriving state the server already
owns. `poll_view` computes `_poll_gate_step`, `_poll_done_ids` and
`_poll_visible_items` on every poll; the code already treats `active_warn_ids`
as "the authoritative list". Completion is therefore computed server-side and
sent to the browser, which only acts on it.

A single shared function produces that state, and **every** endpoint that can
change or report it returns the same shape:

```python
def phase_state(session, procedure) -> dict:
    """Authoritative view of the pilot's position in a procedure."""
    return {
        "phase_complete":    bool,      # nothing unresolved is blocking
        "blocking_item_ids": [int],     # what is holding it open
        "active_warn_ids":   [int],     # warn rows currently failing
    }
```

Used by `poll_view`, `check_view` and `uncheck_view`.

### 2. Mutations return the new state, so there is no added latency

Making completion server-authoritative would otherwise delay a pilot's final tap
by up to one poll (1500 ms). Instead the write returns the state it produced:
`/api/check/` already performs the DB write, so it returns `phase_state` in its
own response and the tap is answered immediately.

Sim-driven events gain no latency at all — they already travel
plugin (1 Hz) → server → poll (1.5 s), and auto-advance already waited for that
poll because `markItem()` is invoked from the poll handler.

### 3. Polling stays. The transport is not the problem

Measured cost of one poll: **31 queries for a 10-item procedure, 71 for 30** —
roughly 2 per item, from `shouldshow()`/`should_warn()` calling
`self.attributes.values_list(...)`, which bypasses the `prefetch_related`
the queryset already performs. (`_is_optional()` uses `.all()` and does hit the
cache.) Fix that before ever shortening the interval.

### 4. Testing strategy: server-side logic in pytest, pure JS in `node --test`

- Decision logic lives in Python and is covered by the existing pytest suite,
  with its existing factories and conventions.
- Shared browser code moves out of the templates into
  `checklist/static/checklist/`, following the pattern `wakelock.js` already
  establishes (config via `data-` attributes, since static files cannot use
  Django template tags). This removes the duplication that caused one bug to
  exist in two places.
- Pure functions extracted there are unit-tested with Node's built-in
  `node --test` — **no test framework dependency**.

### 5. CI runs on push and pull request

A `ci.yaml` workflow runs the same `poetry run pytest` the release gate runs, on
every push and PR, so regressions surface at commit time rather than at deploy.

## Options Considered

**jsdom against the rendered template.** Used ad hoc to prove bug 3, and correct
for that. Rejected as the standing strategy: a second language and runner, a
synthetic DOM fixture that drifts from the real template, and heavy scaffolding
(`runScripts: outside-only`, timer stubs, navigation caught via `jsdomError`).
High maintenance for a solo maintainer.

**Playwright end-to-end.** Rejected for now. If adopted later it should be
`pytest-playwright`, keeping one runner and driving `StaticLiveServerTestCase` —
worth a few smoke tests, not a suite.

**SSE or WebSockets to cut latency.** Rejected on hosting grounds. Under
Passenger/cPanel an SSE stream occupies a WSGI worker for its lifetime, so a
couple of open tabs starves the app; WebSockets need ASGI + Channels, which is
incompatible with the current deployment. Polling is effectively forced here.

**Shorter poll interval.** Rejected as a first move — the poll is too expensive
to shorten until the N+1 above is fixed.

## Consequences

- One implementation of completion, in the tested layer. The browser cannot
  disagree with the server about whether a procedure is finished.
- `/api/check/` and `/api/uncheck/` responses grow a `phase_state` object.
  Additive, so existing callers are unaffected.
- Optimistic row rendering from ADR-001 is preserved; only the completion
  decision moves.
- Adds Node as a dev dependency for the JS unit layer. Not required to run the
  Python suite, and not required to deploy.
- Browser logic moved to static files can no longer use `{% url %}` or
  `{{ poll_interval_ms }}`; those become `data-` attributes.

## Assumptions at Time of Decision

- Up to one poll of latency is acceptable for sim-driven completion — already
  true today, and unchanged by this decision (high confidence).
- The inline JS keeps growing with the v2.0 roadmap, so deduplication pays off
  (medium confidence).
- SQLite remains the database, so poll query count matters more than it would on
  a server-based engine (high confidence).

## Related

- Supersedes nothing, but see the drift noted below.
- **ADR-002 drift**: that record states `active_phase` is a forward-only
  frontier. `views.py:702` now reads
  `# Track currently open procedure (not forward-only — follows pilot navigation)`.
  The behaviour was reversed and the ADR was never amended. Flagged here;
  reconciling it is out of scope for this decision.
