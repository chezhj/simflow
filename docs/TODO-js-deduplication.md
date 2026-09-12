# TODO — finish de-duplicating the inline browser JS (stages 3 & 4)

**Created**: 2026-09-11
**Status**: Not started. Stages 1 and 2 are done and merged into the branch below.
**Branch the earlier work is on**: `claude/pensive-curie-l2l2sq` (4 commits, not yet merged to `master`)
**Background reading**: `design-decisions/20260911-server-authoritative-phase-state.md` (ADR-003)

---

## Why this exists

`detail.html` and `idle.html` each carried their own copy of nine functions. That
duplication is not cosmetic — it has already cost real bugs:

- The browser-clock bug in the connection badge existed **twice** and had to be
  found and fixed in both files (commit `4fac615`).
- `getCsrf` had silently **diverged into two different mechanisms**, one of which
  would have broken under `CSRF_COOKIE_HTTPONLY = True` (fixed in `d06ef61`).

The remaining copies have drifted too, so this is a reconcile-and-extract job,
not a straight move. Do not assume the two versions are the same.

## Ground state — what is already in place

Done in `000373a` and `d06ef61`, so the hard prerequisites are finished:

| Thing | Where | Notes |
|---|---|---|
| Shared JS module | `checklist/static/checklist/simflow-core.js` | UMD-ish: browser global `SimFlow`, or `require()` in Node |
| Unit tests | `js-tests/simflow-core.test.js` | 16 tests, `npm test` → Node's built-in `node --test`, **zero dependencies** |
| CI | `.github/workflows/ci.yaml` | jobs `test` (pytest) and `js` (npm test), on every push + PR |
| Config channel | `#js-config` in `base.html`, read by `SimFlow.config()` | **This was the blocker.** A static JS file cannot use the `url` tag; server values now arrive on `data-` attributes, generalising the `wakelock.js` pattern |
| Already shared | `escHtml`, `connectionState`, `csrfToken`, `parseCsrfCookie`, `readConfig`/`config` | |

Both templates already do `var CFG = SimFlow.config();` and `var escHtml = SimFlow.escHtml;`
near the top of their IIFE. There are **no** hardcoded `/api/...` paths or `{% url %}`
tags left inside either template's JavaScript.

Current inline JS: `detail.html` ~790 lines, `idle.html` ~299 lines.

---

## Stage 3 — move the attribute-transition modal

**What**: `navigateWithTransition` + `showTransitionModal` + the three modal button
handlers → a new `checklist/static/checklist/simflow-transition.js`.

**Locations** (line numbers drift — search by name):

| | detail.html | idle.html |
|---|---|---|
| `async function navigateWithTransition` | ~908 | ~365 |
| `function showTransitionModal` | ~922 | ~379 |
| accept-all / cancel / confirm handlers | ~946–955 | ~401–408 |

**Drift status**: cosmetic only — whitespace alignment and one shortened comment.
Nobody edited them on purpose; they just drifted. Either copy is a fine base.

**Why it is now easy**: the only real blocker was `{% url "checklist:api_attribute_transition" %}`
inside `navigateWithTransition`. That is already replaced by `CFG.urls.attrTransition`,
so the function no longer needs anything from the template.

**Important**: the modal **markup differs** between the two pages —

- `detail.html` uses `attr-modal-header`/`-body`/`-footer` with `btn-setup-secondary` / `btn-setup-primary`
- `idle.html` uses a flatter structure with `btn btn-ghost` / `btn btn-primary`

but **all five element IDs are identical in both** (`attr-modal`, `attr-modal-list`,
`attr-modal-accept-all`, `attr-modal-cancel`, `attr-modal-confirm`), and the JS only
ever addresses IDs. So the JS can be shared **without** touching the markup.

Optionally also fold the markup into a `checklist/templates/checklist/attr_modal.html`
partial and `{% include %}` it — but that is a visual change to one of the two pages
and should be judged on screen, so treat it as separate and optional.

**Steps**
1. Create `simflow-transition.js` exporting an init that takes `{attrTransitionUrl, csrfToken}`.
2. Delete both copies; call the init from each template.
3. Add the `<script src>` to both `{% block head %}` (after `simflow-core.js`).
4. Verify the modal still appears and Confirm still POSTs, on **both** pages.

**Risk**: medium. This code path only runs when an attribute `live_rule` fires on
navigation, so it is easy to leave broken without noticing. Exercise it deliberately.

---

## Stage 4 — parameterise `renderDebugRules`

**Do not naively de-duplicate this one.** The two versions genuinely differ:

| | detail.html (~397) | idle.html (~153) |
|---|---|---|
| size | ~100 lines | ~52 lines |
| signature | `(rules, attrs, showProcs)` | `(showProcs, attrs)` |
| sections rendered | Check items, Attributes, Conditional procedures | Conditional procedures, Attributes |

The difference is real: the idle page has no check items, so it has no check-item
section. Collapsing them into one function would either render an empty section on
idle or reintroduce a branch.

**The genuinely shared part** is the nested `conditionRows(conditions)` helper
(detail ~409, idle ~160). Diffed: **functionally identical**, differing only in line
wrapping. It renders one condition row, including the `near` operator special case.

**Proposed shape** — move the primitive, keep assembly per-page as *data*:

```js
// simflow-debug.js
SimFlow.debug.conditionRows(conditions)        // the shared primitive
SimFlow.debug.render(el, sections)             // sections: [{title, items}]
```

then each page supplies only its own sections:

```js
// detail.html
SimFlow.debug.render(list, [
    {title: 'Check items',            items: rules},
    {title: 'Attributes',             items: attrs},
    {title: 'Conditional procedures', items: showProcs},
]);
// idle.html — same call, two sections
```

The per-item header differs slightly too (detail shows GATE/WARN/OPT tags, idle
shows a pass/fail icon), so either pass a small formatter per section or keep the
header builders page-local and share only `conditionRows`. **Sharing only
`conditionRows` is the smaller, safer win** — take that if time is short.

**Risk**: low-ish. This panel only renders when `DEBUG` is true, so a mistake will
not reach production users — but that also means CI will not notice it. Check it by
eye with `DEBUG=True`.

---

## How to verify

Three layers, all of which must stay green:

```bash
poetry run pytest                       # 329 tests
npm test                                # 16 tests (node --test, no deps)
poetry run djlint checklist/templates/  # see caveat below
```

**djlint caveat**: it reports **5 pre-existing errors** in *other* templates
(inline styles, blank lines). `detail.html`, `idle.html` and `base.html` are clean.
That is why djlint is deliberately **not** in CI — a red-on-arrival CI is worse than
none. Fix those five and adding it is a two-line change.

### Verifying template JavaScript (not committed — recreate if needed)

pytest cannot execute browser JS; it renders templates to strings. The technique
used to validate stages 1–3 of the earlier fixes was a throwaway jsdom harness:

1. Render the real page in a Django test, extract the last `<script>` block, and
   concatenate `simflow-core.js` in front of it (the real page loads it separately).
2. Load it in jsdom with a minimal fake DOM containing the element IDs the script
   touches — **including `#js-config`**, or every URL comes back empty.
3. Stub `window.fetch` to return canned poll / check payloads.

Hard-won gotchas, all of which cost time:

- jsdom needs `runScripts: 'outside-only'`, or `window.eval` does not run in the
  window's own context and `document` is undefined.
- `window.location` is **non-configurable** — you cannot overwrite it to capture
  navigation. Instead attach a `VirtualConsole` and listen for `jsdomError`
  matching `/navigation/`.
- Draining microtasks with `await Promise.resolve()` does **not** flush jsdom's
  promise chain. Use real macrotasks: `await new Promise(r => setTimeout(r, 0))`.
- Stub `document.getElementById('checklist-scroll').scrollTo` — jsdom has no
  implementation and the scroll helper will throw.

Consider committing that harness if stage 3 or 4 turns out to need real verification;
it needs `npm i -D jsdom`, which would be the project's first runtime-ish dev dep.

---

## Traps already hit — do not repeat

- **Multi-line `{# ... #}` is not a block comment.** Django's `{# #}` is
  single-line only. A multi-line one containing a literal `{% url %}` got parsed
  and broke 23 tests. Use `{% comment %}...{% endcomment %}`.
- **Python name shadowing.** Assigning to a name *anywhere* in a function makes it
  local for the whole function. Locals called `visible_items` and `gate_item`
  shadowed the newly imported helpers of the same name in `api_views.py` and
  `plugin_views.py`. Both are renamed with a comment; do not reintroduce them.
- **Alias ordering.** `var escHtml = SimFlow.escHtml;` must sit above its first use
  in the IIFE, not below `pollChecklist()`. It worked by accident only because the
  first poll resolves asynchronously.

---

## Related, still open (not part of this task)

- **ADR-002 drift**: it states `active_phase` is a forward-only frontier, but
  `views.py:702` now says `not forward-only — follows pilot navigation`. The
  behaviour was reversed and the ADR never amended. Decide which is correct.
- ~~**Poll N+1**~~ — **fixed**. `shouldshow()`/`should_warn()` now use `.all()`, so
  the `prefetch_related` the callers already pay for is actually used, and
  `poll_view` gathers its phase context once instead of twice. The poll went from
  `15 + 3n` queries to a flat **10**, and `/api/plugin/state/` likewise stopped
  scaling. `checklist/tests/test_query_counts.py` guards both against regression.
  Shortening the poll interval is now affordable, if it is ever wanted.
- **Not deployed**: the four commits on the branch are not on `master` and not
  released. Deploy is triggered by `cz bump` pushing a `v*` tag.
