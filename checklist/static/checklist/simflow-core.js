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

    /** The document, or null under `node --test` where there isn't one. */
    function defaultDoc(doc) {
        if (doc) return doc;
        return typeof document !== 'undefined' ? document : null;
    }

    /**
     * Read the CSRF token out of a cookie header string.
     *
     * Kept separate from any DOM so it can be tested directly. Anchored on a
     * boundary so a cookie merely *ending* in "csrftoken" cannot match.
     */
    function parseCsrfCookie(cookieString) {
        var m = /(?:^|;\s*)csrftoken=([^;]*)/.exec(cookieString || '');
        return m ? decodeURIComponent(m[1]) : '';
    }

    /**
     * The CSRF token for this page: the rendered hidden input first, the cookie
     * as fallback.
     *
     * The two pages used to do one each. detail.html read the input, which
     * idle.html does not render, so idle.html read the cookie — which only
     * works while CSRF_COOKIE_HTTPONLY is false. Hardening that setting would
     * have broken one page and not the other. Trying both is correct on every
     * page and under either setting.
     */
    function csrfToken(doc) {
        doc = defaultDoc(doc);
        if (!doc) return '';
        var el = doc.querySelector('[name=csrfmiddlewaretoken]');
        if (el && el.value) return el.value;
        return parseCsrfCookie(doc.cookie);
    }

    // Fallback poll interval if the config element is missing or unparseable.
    // Fallback only — the server supplies POLL_INTERVAL_MS through #js-config
    // on every page. Kept in step with settings/base.py so a page that somehow
    // loses the config does not silently poll at a different rate.
    var DEFAULT_POLL_INTERVAL_MS = 750;

    /**
     * Turn the #js-config element into a config object.
     *
     * Takes the element rather than looking it up, so it can be tested with a
     * plain `{dataset: {...}}` stand-in. Server-rendered URLs reach static
     * JavaScript this way because a static file cannot use {% url %} — the same
     * data-attribute channel wakelock.js already uses.
     */
    function readConfig(el) {
        var d = (el && el.dataset) || {};
        var interval = parseInt(d.pollIntervalMs, 10);
        return {
            pollIntervalMs: interval > 0 ? interval : DEFAULT_POLL_INTERVAL_MS,
            urls: {
                poll:           d.urlPoll || '',
                check:          d.urlCheck || '',
                uncheck:        d.urlUncheck || '',
                attrTransition: d.urlAttrTransition || '',
            },
        };
    }

    /**
     * Decide what the plugin-update banner should say, from a poll response.
     * Returns null when there is nothing to show.
     *
     * Pure so it can be tested without a DOM — the caller applies the result.
     * A blocked plugin is not merely outdated: the server refuses it session
     * data, so the checklist cannot follow the sim at all, and the wording has
     * to say so rather than suggest an optional update.
     */
    function pluginUpdateNotice(data) {
        if (!data) { return null; }
        var status = data.plugin_status;
        if (status !== 'warn' && status !== 'blocked') { return null; }

        var version = data.plugin_version || '';
        var suffix = version ? ' (v' + version + ')' : '';
        return {
            status: status,
            url: data.plugin_update_url || '',
            text: status === 'blocked'
                ? 'This plugin version is no longer supported' + suffix +
                  ' \u2014 the checklist cannot follow the sim until it is updated'
                : 'A newer xFlow plugin is available' + suffix,
        };
    }

    /** readConfig() against the live document. */
    function config(doc) {
        doc = defaultDoc(doc);
        return readConfig(doc && doc.getElementById('js-config'));
    }

    return {
        escHtml: escHtml,
        connectionState: connectionState,
        parseCsrfCookie: parseCsrfCookie,
        csrfToken: csrfToken,
        readConfig: readConfig,
        config: config,
        RECONNECT_GRACE: RECONNECT_GRACE,
        pluginUpdateNotice: pluginUpdateNotice,
        DEFAULT_POLL_INTERVAL_MS: DEFAULT_POLL_INTERVAL_MS,
    };
}));
