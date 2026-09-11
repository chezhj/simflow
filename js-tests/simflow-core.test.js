// Unit tests for the pure browser helpers. Run with: npm test  (node --test)
const test = require('node:test');
const assert = require('node:assert');
const SimFlow = require('../checklist/static/checklist/simflow-core.js');

test('escHtml neutralises markup', () => {
    assert.equal(SimFlow.escHtml('<b>&"'), '&lt;b&gt;&amp;&quot;');
    assert.equal(SimFlow.escHtml(42), '42');
});

test('connected wins over everything else', () => {
    assert.equal(SimFlow.connectionState(
        { simConnected: true, lastSeen: 0, now: 1000 }), 'connected');
});

test('never connected reads as manual, not disconnected', () => {
    assert.equal(SimFlow.connectionState(
        { simConnected: false, lastSeen: 0, now: 1000 }), 'manual');
});

test('a brief gap is reconnecting; a long one is disconnected', () => {
    const at = (age) => SimFlow.connectionState(
        { simConnected: false, lastSeen: 1000, now: 1000 + age });
    assert.equal(at(5), 'reconnecting');
    assert.equal(at(29), 'reconnecting');
    assert.equal(at(30), 'disconnected');   // boundary is inclusive
    assert.equal(at(120), 'disconnected');
});

test('initializing is reported while still inside the grace window', () => {
    assert.equal(SimFlow.connectionState(
        { simConnected: false, simInitializing: true, lastSeen: 1000, now: 1010 }),
        'initializing');
});

test('regression: now and lastSeen must share a clock', () => {
    // The bug: `now` came from the browser (Date.now()) while lastSeen came from
    // the server. A viewer's clock running 60s fast made a live sim look gone.
    const serverNow = 1000, lastSeen = 999;          // plugin reported 1s ago
    assert.equal(SimFlow.connectionState(
        { simConnected: false, lastSeen, now: serverNow }), 'reconnecting');
    const skewedBrowserNow = serverNow + 60;
    assert.equal(SimFlow.connectionState(
        { simConnected: false, lastSeen, now: skewedBrowserNow }), 'disconnected',
        'demonstrates why the caller must pass server_time');
});

// ─── CSRF token resolution ───────────────────────────────────────────────────

test('parseCsrfCookie pulls the token out of a cookie header', () => {
    assert.equal(SimFlow.parseCsrfCookie('csrftoken=abc123'), 'abc123');
    assert.equal(SimFlow.parseCsrfCookie('sessionid=x; csrftoken=abc123; other=y'), 'abc123');
    assert.equal(SimFlow.parseCsrfCookie(''), '');
    assert.equal(SimFlow.parseCsrfCookie(undefined), '');
});

test('parseCsrfCookie does not match a lookalike cookie name', () => {
    assert.equal(SimFlow.parseCsrfCookie('notthecsrftoken=nope'), '');
    assert.equal(SimFlow.parseCsrfCookie('xcsrftoken=nope'), '');
});

test('parseCsrfCookie url-decodes the value', () => {
    assert.equal(SimFlow.parseCsrfCookie('csrftoken=a%2Bb'), 'a+b');
});

const fakeDoc = ({ input, cookie }) => ({
    querySelector: () => (input === undefined ? null : { value: input }),
    cookie: cookie || '',
});

test('csrfToken prefers the rendered hidden input', () => {
    assert.equal(SimFlow.csrfToken(fakeDoc({ input: 'from-input', cookie: 'csrftoken=from-cookie' })),
        'from-input');
});

test('csrfToken falls back to the cookie when no input is rendered', () => {
    // idle.html renders no {% csrf_token %}, which is why it read the cookie.
    assert.equal(SimFlow.csrfToken(fakeDoc({ cookie: 'csrftoken=from-cookie' })), 'from-cookie');
});

test('csrfToken falls back when the input exists but is empty', () => {
    assert.equal(SimFlow.csrfToken(fakeDoc({ input: '', cookie: 'csrftoken=from-cookie' })),
        'from-cookie');
});

test('csrfToken survives CSRF_COOKIE_HTTPONLY, where the cookie is unreadable', () => {
    // The scenario that would have silently broken idle.html but not detail.html.
    assert.equal(SimFlow.csrfToken(fakeDoc({ input: 'from-input', cookie: '' })), 'from-input');
    assert.equal(SimFlow.csrfToken(fakeDoc({ cookie: '' })), '');   // nothing available
});

// ─── Config channel ──────────────────────────────────────────────────────────

test('readConfig maps data attributes onto a config object', () => {
    const c = SimFlow.readConfig({ dataset: {
        pollIntervalMs: '2000',
        urlPoll: '/api/poll/', urlCheck: '/api/check/',
        urlUncheck: '/api/uncheck/', urlAttrTransition: '/api/attribute-transition/',
    }});
    assert.equal(c.pollIntervalMs, 2000);
    assert.equal(c.urls.poll, '/api/poll/');
    assert.equal(c.urls.attrTransition, '/api/attribute-transition/');
});

test('readConfig falls back when the element is missing or unusable', () => {
    for (const el of [null, undefined, {}, { dataset: {} }, { dataset: { pollIntervalMs: 'x' } }]) {
        const c = SimFlow.readConfig(el);
        assert.equal(c.pollIntervalMs, SimFlow.DEFAULT_POLL_INTERVAL_MS);
        assert.equal(c.urls.poll, '');
    }
});

test('readConfig rejects a nonsensical interval rather than polling flat out', () => {
    assert.equal(SimFlow.readConfig({ dataset: { pollIntervalMs: '0' } }).pollIntervalMs,
        SimFlow.DEFAULT_POLL_INTERVAL_MS);
    assert.equal(SimFlow.readConfig({ dataset: { pollIntervalMs: '-5' } }).pollIntervalMs,
        SimFlow.DEFAULT_POLL_INTERVAL_MS);
});
