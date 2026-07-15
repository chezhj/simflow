# Wake Lock Spec — Keep Screen On During Procedure Execution

**Status**: Ready for implementation
**Scope**: Small, client-side only. No `show_rule` / rule engine involvement.

## 1. Problem Statement

Pilots running the checklist on a phone during a live session lose the screen to
auto-lock mid-procedure (same problem recipe sites solve). Need an opt-in toggle
that keeps the screen awake while — and only while — the procedure execution view
is open.

## 2. Constraints (from project architecture)

- No SPA framework, no build tooling (no SASS/Vite). Vanilla JS only.
- Procedure execution view updates step completion via the existing 2–3s polling
  loop (DOM patched in place) — the page itself does not reload while stepping
  through a procedure.
- Navigating away from procedure execution to Profile/Account or Session Setup
  **is** a full Django page load (separate view/URL) — confirmed, not a DOM swap.
- `show_expression` / `dataref_expression` are legacy and untouched by this work;
  `show_rule` is unaffected — this feature doesn't touch the rule engine at all.

## 3. Decisions Already Made

| Decision | Value |
|---|---|
| Activation | Manual toggle in user preferences (not auto, not phase-gated) |
| Scope | Procedure execution view only — not Setup, not Profile |
| Denial/unsupported handling | Fail silent — no visible error, no indicator |
| Persistence | Pre-selected per profile, like `Optional` / `NoActionNeed` |

## 4. Data Model

Add one field to the existing Profile model, alongside `optional` / `no_action_need` / `safety_tests`:

```python
keep_screen_on = models.BooleanField(default=False)
```

Standard migration. No relation to session state or `show_rule` — this is a pure
user preference, same tier as the other display toggles on the profile screen.

## 5. Template Scope

Only the procedure execution template renders the flag and loads the script.
Setup/Profile templates never reference it.

```html
<!-- procedure_execution.html, on <body> or a wrapping container -->
<body data-keep-screen-on="{{ profile.keep_screen_on|yesno:'true,false' }}">
  ...
  <script src="{% static 'js/wakelock.js' %}"></script>
</body>
```

> **Open question for implementer**: confirm actual static file path convention
> in the repo (`static/js/` vs per-app static dirs) — I don't have the live repo
> in this session to check.

## 6. Client-Side Module — `wakelock.js`

```javascript
// wakelock.js
// Screen Wake Lock — keeps device screen on during procedure execution.
// Scope: procedure execution view only. Fails silently if unsupported or denied.

(function () {
  let wakeLock = null;

  async function requestWakeLock() {
    if (!('wakeLock' in navigator)) return; // unsupported browser — silent no-op
    try {
      wakeLock = await navigator.wakeLock.request('screen');
      wakeLock.addEventListener('release', () => {
        wakeLock = null;
      });
    } catch (err) {
      // Denied (e.g. iOS Low Power Mode) or any other failure — fail silent.
      wakeLock = null;
    }
  }

  function handleVisibilityChange() {
    if (wakeLock === null && document.visibilityState === 'visible') {
      requestWakeLock();
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    const enabled = document.body.dataset.keepScreenOn === 'true';
    if (!enabled) return;
    requestWakeLock();
    document.addEventListener('visibilitychange', handleVisibilityChange);
  });
})();
```

> **Why the `visibilitychange` listener?** The Wake Lock API auto-releases
> whenever the tab is hidden — screen lock, app switch to X-Plane, phone call,
> etc. Without re-acquiring on return-to-visible, the lock silently stops
> working after the first backgrounding event, which would be confusing since
> nothing tells the pilot it turned off.

## 7. Lifecycle / Data Flow

1. Procedure execution page loads → if `keep_screen_on` true, request lock immediately.
2. Polling loop continues to patch DOM for step completion — unrelated, no interaction with wake lock.
3. Tab hidden (backgrounded) → OS/browser releases lock automatically.
4. Tab visible again → `visibilitychange` fires → lock silently re-requested.
5. Pilot navigates to Profile/Setup → full page unload → lock released natively. No teardown code needed.

## 8. Error Handling

Every failure path (unsupported API, permission denied, Low Power Mode refusal)
resolves to `wakeLock = null` and nothing else. No console noise beyond what the
browser itself may log, no UI indicator, no retry loop beyond the visibilitychange
re-request.

## 9. Testing Strategy

Manual, since this is a browser API feature, not app logic:

- Android Chrome/Edge — toggle on, background the tab, confirm screen stays on while foregrounded, confirm normal sleep behavior resumes if toggle is off
- iOS Safari 16.4+ — same, plus explicitly test with Low Power Mode on (expected: silent denial, screen sleeps normally, no error surfaced)
- Desktop Chrome/Firefox — harmless no-op path if desktop screens don't sleep aggressively; confirm no console errors
- Confirm lock is *not* requested on Profile/Setup pages even with toggle enabled

## 10. Explicitly Not Covered

- No fallback library (NoSleep.js or similar) for browsers lacking the API — out of scope, fail silent instead
- No visual "screen lock active" indicator (reuse of the connection-status dot pattern was considered and declined)
- No server-side enforcement or session-state involvement — this is pure client preference
- No phase-gating (e.g. auto-on during Descent) — rejected in favor of a simple manual toggle

## 11. Open Questions

1. Confirm static file path convention against the live repo before wiring the `<script>` tag.
