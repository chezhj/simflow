// simflow-core.js
// Pure helpers shared by detail.html and idle.html.
//
// These live here, outside the templates, for two reasons: the logic was
// previously copy-pasted into both pages (the browser-clock bug in the
// connection badge therefore existed twice and had to be fixed twice), and
// pure functions can be unit-tested under `node --test` without a DOM.
//
// Anything in this file must stay free of DOM, fetch and timers.
(function (root, factory) {
    if (typeof module === 'object' && module.exports) {
        module.exports = factory();          // node --test
    } else {
        root.SimFlow = factory();            // browser
    }
}(typeof self !== 'undefined' ? self : this, function () {
    'use strict';

    /** Escape text for interpolation into innerHTML. */
    function escHtml(s) {
        return String(s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // How stale the plugin contact may get before each state applies, seconds.
    var RECONNECT_GRACE = 30;

    /**
     * Which of the five connection states to show.
     *
     * `now` and `lastSeen` must come from the SAME clock — pass the server's
     * server_time, never Date.now(). Mixing them meant a viewer whose PC clock
     * ran fast by more than RECONNECT_GRACE reported the simulator as gone.
     *
     * Returns 'connected' | 'reconnecting' | 'initializing' | 'disconnected' | 'manual'.
     */
    function connectionState(o) {
        if (o.simConnected) return 'connected';
        var seen = o.lastSeen || 0;
        if (seen <= 0) return 'manual';        // never connected this session
        var age = o.now - seen;
        if (age < RECONNECT_GRACE) return o.simInitializing ? 'initializing' : 'reconnecting';
        return 'disconnected';
    }

    return { escHtml: escHtml, connectionState: connectionState, RECONNECT_GRACE: RECONNECT_GRACE };
}));
