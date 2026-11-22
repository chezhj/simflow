// Unit tests for flight-phase.js. Run with: npm test  (node --test)
const test = require('node:test');
const assert = require('node:assert');
const Phase = require('../checklist/static/checklist/flight-phase.js');

const cruise = {
    key: 'cruise', label: 'Cruise', short: 'Cruise', group: 'airborne',
    index: 6, total: 11, go_arounds: 0, previous: 'Climb',
    waiting_for: 'Descent · V/S below -300 fpm',
};

// ── view() ──────────────────────────────────────────────────────────────────

test('nothing to show until the server has a phase', () => {
    assert.equal(Phase.view(null, true), null);
    assert.equal(Phase.view(undefined, true), null);
    assert.equal(Phase.view('x', true), null);
});

test('a live phase: name, position, steps and the two foot lines', () => {
    const v = Phase.view(cruise, true);
    assert.equal(v.short, 'Cruise');
    assert.equal(v.label, 'Cruise');
    assert.equal(v.eyebrow, 'Flight phase · 6 of 11');
    assert.equal(v.stale, false);
    assert.equal(v.unknown, false);
    assert.equal(v.tag, '');
    assert.equal(v.previous, 'Climb');
    assert.equal(v.next, 'Descent · V/S below -300 fpm');
});

test('steps are done before the current one, todo after', () => {
    const v = Phase.view(cruise, true);
    assert.equal(v.steps.length, 11);
    assert.deepEqual(v.steps.slice(0, 5), ['done', 'done', 'done', 'done', 'done']);
    assert.equal(v.steps[5], 'current');
    assert.deepEqual(v.steps.slice(6), ['todo', 'todo', 'todo', 'todo', 'todo']);
});

test('the first phase has nothing done, the last has everything done', () => {
    const first = Phase.view({ key: 'stand', label: 'Stand', index: 1, total: 11 }, true);
    assert.equal(first.steps[0], 'current');
    assert.ok(first.steps.slice(1).every((s) => s === 'todo'));
    const last = Phase.view({ key: 'on_block', label: 'On block', index: 11, total: 11 }, true);
    assert.equal(last.steps[10], 'current');
    assert.ok(last.steps.slice(0, 10).every((s) => s === 'done'));
});

test('an unclassified session reads Phase unknown with an empty track', () => {
    const v = Phase.view({ key: null, label: 'Phase unknown', index: 0, total: 11 }, true);
    assert.equal(v.unknown, true);
    assert.equal(v.short, 'Phase unknown');
    assert.equal(v.eyebrow, 'Flight phase');
    assert.ok(v.steps.every((s) => s === 'todo'));
});

test('without the sim the last phase is kept, dimmed and tagged', () => {
    const v = Phase.view(cruise, false);
    assert.equal(v.stale, true);
    assert.equal(v.short, 'Cruise');
    assert.equal(v.tag, 'Sim offline');
});

test('a go-around is tagged on the approach and in the flare, with its count', () => {
    const at = (key, n) => Phase.view({ ...cruise, key, label: key, short: key, go_arounds: n }, true).tag;
    assert.equal(at('approach', 1), 'Go-around ×1');
    assert.equal(at('landing', 2), 'Go-around ×2');
});

test('a go-around is not tagged once the aircraft is taxiing, or when there was none', () => {
    const at = (key, n) => Phase.view({ ...cruise, key, label: key, short: key, go_arounds: n }, true).tag;
    assert.equal(at('taxi_in', 1), '');
    assert.equal(at('cruise', 1), '');
    assert.equal(at('approach', 0), '');
});

test('offline outranks a go-around tag', () => {
    const v = Phase.view({ ...cruise, key: 'approach', go_arounds: 1 }, false);
    assert.equal(v.tag, 'Sim offline');
});

test('a short name is used for the bar, falling back to the label', () => {
    const v = Phase.view({ key: 'pushback', label: 'Pushback & engine start', short: 'Pushback', index: 2, total: 11 }, true);
    assert.equal(v.short, 'Pushback');
    assert.equal(v.label, 'Pushback & engine start');
    const w = Phase.view({ key: 'pushback', label: 'Pushback & engine start', index: 2, total: 11 }, true);
    assert.equal(w.short, 'Pushback & engine start');
});

test('missing or nonsense numbers do not break the view', () => {
    const v = Phase.view({ key: 'cruise', label: 'Cruise', index: 'x', total: null }, true);
    assert.equal(v.steps.length, 11);
    assert.ok(v.steps.every((s) => s === 'todo'));
});

// ── apply(): a tiny fake DOM ────────────────────────────────────────────────

function el(attrs = {}) {
    const classes = new Set(attrs.classes || []);
    const kids = {};
    const attributes = {};
    return {
        attributes,
        setAttribute: (k, v) => { attributes[k] = v; },
        removeAttribute: (k) => { delete attributes[k]; },
        textContent: attrs.text || '',
        classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            contains: (c) => classes.has(c),
        },
        kids,
        querySelector: (sel) => kids[sel] || null,
        querySelectorAll: (sel) => (kids[sel] ? [].concat(kids[sel]) : []),
    };
}

function page() {
    const mk = () => {
        const ind = el({ classes: ['js-hidden'] });
        ind.kids['[data-phase-name]'] = el();
        ind.kids['[data-phase-tag]'] = el({ classes: ['js-hidden'] });
        return ind;
    };
    const row = mk(), pill = mk();
    const card = el({ classes: ['js-hidden'] });
    card.kids['[data-phase-eyebrow]'] = el();
    card.kids['[data-phase-name]'] = el();
    card.kids['[data-step]'] = Array.from({ length: 11 }, () => el());
    card.kids['[data-phase-foot]'] = el({ classes: ['js-hidden'] });
    card.kids['[data-phase-previous]'] = el({ classes: ['js-hidden'] });
    card.kids['[data-phase-previous-value]'] = el();
    card.kids['[data-phase-next]'] = el({ classes: ['js-hidden'] });
    card.kids['[data-phase-next-value]'] = el();
    const eyebrow = el({ text: 'In Flight' }), navLabel = el({ text: 'In Flight' });
    const doc = {
        querySelectorAll: (sel) => ({
            '[data-phase-indicator]': [row, pill],
            '[data-phase-label]': [eyebrow, navLabel],
        }[sel] || []),
        getElementById: (id) => (id === 'phase-card' ? card : null),
    };
    return { doc, row, pill, card, eyebrow, navLabel };
}

test('apply reveals both indicators and writes the short name', () => {
    const p = page();
    Phase.apply({ flight_phase: cruise, sim_connected: true }, p.doc);
    for (const ind of [p.row, p.pill]) {
        assert.equal(ind.classList.contains('js-hidden'), false);
        assert.equal(ind.kids['[data-phase-name]'].textContent, 'Cruise');
        assert.equal(ind.kids['[data-phase-tag]'].classList.contains('js-hidden'), true);
        assert.equal(ind.classList.contains('is-stale'), false);
    }
});

test('apply replaces the fixed "In Flight" labels on the hub', () => {
    const p = page();
    Phase.apply({ flight_phase: cruise, sim_connected: true }, p.doc);
    assert.equal(p.eyebrow.textContent, 'Cruise');
    assert.equal(p.navLabel.textContent, 'Cruise');
});

test('apply leaves the fixed labels alone when there is no phase', () => {
    const p = page();
    Phase.apply({ flight_phase: null, sim_connected: true }, p.doc);
    assert.equal(p.eyebrow.textContent, 'In Flight');
    assert.equal(p.navLabel.textContent, 'In Flight');
});

test('apply hides what it had shown when the phase goes away', () => {
    const p = page();
    Phase.apply({ flight_phase: cruise, sim_connected: true }, p.doc);
    Phase.apply({ flight_phase: null, sim_connected: true }, p.doc);
    assert.equal(p.row.classList.contains('js-hidden'), true);
    assert.equal(p.pill.classList.contains('js-hidden'), true);
});

test('apply fills the hub card: name, eyebrow, steps, previous and next', () => {
    const p = page();
    Phase.apply({ flight_phase: cruise, sim_connected: true }, p.doc);
    const c = p.card;
    assert.equal(c.classList.contains('js-hidden'), false);
    assert.equal(c.kids['[data-phase-name]'].textContent, 'Cruise');
    assert.equal(c.kids['[data-phase-eyebrow]'].textContent, 'Flight phase · 6 of 11');
    const steps = c.kids['[data-step]'];
    assert.equal(steps[4].classList.contains('is-done'), true);
    assert.equal(steps[5].classList.contains('is-current'), true);
    assert.equal(steps[5].classList.contains('is-done'), false);
    assert.equal(steps[6].classList.contains('is-done'), false);
    assert.equal(c.kids['[data-phase-previous-value]'].textContent, 'Climb');
    assert.equal(c.kids['[data-phase-previous]'].classList.contains('js-hidden'), false);
    assert.equal(c.kids['[data-phase-next-value]'].textContent, 'Descent · V/S below -300 fpm');
});

test('apply marks only the current step for screen readers', () => {
    const p = page();
    Phase.apply({ flight_phase: cruise, sim_connected: true }, p.doc);
    const steps = p.card.kids['[data-step]'];
    assert.equal(steps[5].attributes['aria-current'], 'step');
    assert.equal(steps.filter((e) => 'aria-current' in e.attributes).length, 1);
    Phase.apply({ flight_phase: { ...cruise, index: 7, key: 'descent', label: 'Descent', short: 'Descent' }, sim_connected: true }, p.doc);
    assert.equal('aria-current' in steps[5].attributes, false);
    assert.equal(steps[6].attributes['aria-current'], 'step');
});

test('apply moves the current marker as the phase advances', () => {
    const p = page();
    Phase.apply({ flight_phase: cruise, sim_connected: true }, p.doc);
    Phase.apply({ flight_phase: { ...cruise, key: 'descent', label: 'Descent', short: 'Descent', index: 7, previous: 'Cruise' }, sim_connected: true }, p.doc);
    const steps = p.card.kids['[data-step]'];
    assert.equal(steps[5].classList.contains('is-current'), false);
    assert.equal(steps[5].classList.contains('is-done'), true);
    assert.equal(steps[6].classList.contains('is-current'), true);
});

test('apply hides the previous and next lines when there is nothing to say', () => {
    const p = page();
    Phase.apply({ flight_phase: { key: 'stand', label: 'Stand', short: 'Stand', index: 1, total: 11, previous: null, waiting_for: null }, sim_connected: true }, p.doc);
    assert.equal(p.card.kids['[data-phase-previous]'].classList.contains('js-hidden'), true);
    assert.equal(p.card.kids['[data-phase-next]'].classList.contains('js-hidden'), true);
});

test('apply shows the footer only when it has something to say', () => {
    const p = page();
    Phase.apply({ flight_phase: cruise, sim_connected: true }, p.doc);
    assert.equal(p.card.kids['[data-phase-foot]'].classList.contains('js-hidden'), false);
    Phase.apply({ flight_phase: { key: null, label: 'Phase unknown', index: 0, total: 11 }, sim_connected: true }, p.doc);
    assert.equal(p.card.kids['[data-phase-foot]'].classList.contains('js-hidden'), true);
});

test('apply dims and tags everything when the sim stops', () => {
    const p = page();
    Phase.apply({ flight_phase: cruise, sim_connected: false }, p.doc);
    assert.equal(p.row.classList.contains('is-stale'), true);
    assert.equal(p.row.kids['[data-phase-tag]'].textContent, 'Sim offline');
    assert.equal(p.row.kids['[data-phase-tag]'].classList.contains('js-hidden'), false);
    assert.equal(p.card.classList.contains('is-stale'), true);
});

test('apply on a page with none of the elements does nothing and does not throw', () => {
    const empty = { querySelectorAll: () => [], getElementById: () => null };
    Phase.apply({ flight_phase: cruise, sim_connected: true }, empty);
    Phase.apply(null, empty);
});
