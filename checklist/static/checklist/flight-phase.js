// flight-phase.js
// Renders the flight phase of the aircraft (stand … on block) from the
// `flight_phase` object the poll returns.
//
// The server decides the phase; this file only draws it (ADR-003, ADR-004). It
// is shared by detail.html and idle.html so neither poll loop carries its own
// copy, and the view model is a pure function so it can run under `node --test`.
//
// Not to be confused with `phase_state` in the same response: that is checklist
// completion and has nothing to do with this.
//
// Everything is written with textContent, never innerHTML.
(function (root, factory) {
    if (typeof module === 'object' && module.exports) {
        module.exports = factory();          // node --test
    } else {
        root.SimFlowPhase = factory();       // browser
    }
}(typeof self !== 'undefined' ? self : this, function () {
    'use strict';

    // A go-around is only worth a tag while the aircraft is still low on the
    // approach; once it is taxiing in, "Go-around x1" would just be noise.
    var GO_AROUND_PHASES = { approach: true, landing: true };

    /**
     * What to show for one poll response, or null to show nothing.
     *
     * `flightPhase` is the poll's flight_phase object (null until the sim has been
     * seen on an aircraft with signal definitions). `simConnected` is the poll's
     * sim_connected: without it the last known phase is kept but dimmed.
     */
    function view(flightPhase, simConnected) {
        if (!flightPhase || typeof flightPhase !== 'object') return null;

        var total = flightPhase.total > 0 ? flightPhase.total : 11;
        var index = flightPhase.index > 0 ? flightPhase.index : 0;
        var unknown = !flightPhase.key;
        var stale = !simConnected;

        var steps = [];
        for (var i = 1; i <= total; i++) {
            steps.push(i < index ? 'done' : (i === index ? 'current' : 'todo'));
        }

        var tag = '';
        if (stale) {
            tag = 'Sim offline';
        } else if (flightPhase.go_arounds > 0 && GO_AROUND_PHASES[flightPhase.key]) {
            tag = 'Go-around ×' + flightPhase.go_arounds;
        }

        return {
            unknown: unknown,
            stale: stale,
            label: unknown ? 'Phase unknown' : String(flightPhase.label),
            short: unknown ? 'Phase unknown' : String(flightPhase.short || flightPhase.label),
            eyebrow: unknown ? 'Flight phase' : 'Flight phase · ' + index + ' of ' + total,
            tag: tag,
            steps: steps,
            previous: flightPhase.previous ? String(flightPhase.previous) : '',
            next: flightPhase.waiting_for ? String(flightPhase.waiting_for) : ''
        };
    }

    function setText(el, text) {
        if (el && el.textContent !== text) el.textContent = text;
    }

    function setHidden(el, hidden) {
        if (!el) return;
        if (hidden) el.classList.add('js-hidden');
        else el.classList.remove('js-hidden');
    }

    function setClass(el, name, on) {
        if (!el) return;
        if (on) el.classList.add(name);
        else el.classList.remove(name);
    }

    /**
     * Draw one poll response. Safe to call on every poll and on a page that has
     * none of the elements (it then does nothing).
     *
     * Elements, all optional:
     *   [data-phase-indicator]  the connection-bar row and pill; children
     *                           [data-phase-name] and [data-phase-tag]
     *   #phase-card             the in-flight hub card (idle.html only)
     *   [data-phase-label]      plain text that should read the short phase name
     */
    function apply(data, doc) {
        doc = doc || (typeof document !== 'undefined' ? document : null);
        if (!doc) return;
        var v = view(data && data.flight_phase, !!(data && data.sim_connected));

        var indicators = doc.querySelectorAll('[data-phase-indicator]');
        for (var i = 0; i < indicators.length; i++) {
            var el = indicators[i];
            setHidden(el, !v);
            if (!v) continue;
            setText(el.querySelector('[data-phase-name]'), v.short);
            var tag = el.querySelector('[data-phase-tag]');
            setText(tag, v.tag);
            setHidden(tag, !v.tag);
            setClass(el, 'is-stale', v.stale);
            setClass(el, 'is-unknown', v.unknown);
        }

        if (!v) return;

        var labels = doc.querySelectorAll('[data-phase-label]');
        for (var j = 0; j < labels.length; j++) setText(labels[j], v.short);

        var card = doc.getElementById('phase-card');
        if (!card) return;
        setHidden(card, false);
        setClass(card, 'is-stale', v.stale);
        setText(card.querySelector('[data-phase-eyebrow]'), v.eyebrow);
        setText(card.querySelector('[data-phase-name]'), v.label);

        var steps = card.querySelectorAll('[data-step]');
        for (var k = 0; k < steps.length; k++) {
            var state = v.steps[k] || 'todo';
            setClass(steps[k], 'is-done', state === 'done');
            setClass(steps[k], 'is-current', state === 'current');
            if (state === 'current') steps[k].setAttribute('aria-current', 'step');
            else steps[k].removeAttribute('aria-current');
        }

        var prev = card.querySelector('[data-phase-previous]');
        setHidden(prev, !v.previous);
        setText(card.querySelector('[data-phase-previous-value]'), v.previous);
        var next = card.querySelector('[data-phase-next]');
        setHidden(next, !v.next);
        setText(card.querySelector('[data-phase-next-value]'), v.next);
        // No divider line above an empty footer (an unclassified session has neither).
        setHidden(card.querySelector('[data-phase-foot]'), !v.previous && !v.next);
    }

    return { view: view, apply: apply, GO_AROUND_PHASES: GO_AROUND_PHASES };
}));
