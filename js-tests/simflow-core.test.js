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
