// simflow-transition.js
// The "conditions changed" dialog, shared by detail.html and idle.html.
//
// Before any navigation the page asks /api/attribute-transition/ whether the
// sim's conditions no longer match the profile. If so the pilot gets one
// choice: match the profile to the conditions, or keep it as it is. Either way
// they then continue to where they were going. (Keeping the profile is
// remembered for the session, so the same attribute is not asked about again.)
//
// DOM and fetch live here; the logic worth testing is in simflow-core.js
// (summarizeTransition, transitionDecisions). Needs simflow-core.js loaded first.
(function () {
    'use strict';

    function init(opts) {
        var url       = opts.url;
        var csrf      = opts.csrfToken;
        var modal     = document.getElementById('attr-modal');
        if (!modal) { return; }
        var reasonsEl = document.getElementById('attr-modal-reasons');
        var changesEl = document.getElementById('attr-modal-changes');
        var keepBtn   = document.getElementById('attr-modal-keep');
        var matchBtn  = document.getElementById('attr-modal-match');
        var esc       = SimFlow.escHtml;
        var pending   = null;   /* {prompts, targetUrl} while the dialog is open */

        function show(prompts, targetUrl) {
            var summary = SimFlow.summarizeTransition(prompts);
            reasonsEl.innerHTML = summary.reasons.map(function (r) {
                return '<p class="attr-modal-reason">' + esc(r) + '</p>';
            }).join('');
            changesEl.innerHTML = summary.changes.map(function (c) {
                return '<li class="attr-modal-change">'
                    + '<span class="attr-modal-verb">' + (c.activate ? 'Activate' : 'Deactivate') + '</span> '
                    + esc(c.title) + '</li>';
            }).join('');
            pending = {prompts: prompts, targetUrl: targetUrl};
            modal.classList.remove('js-hidden');
            matchBtn.focus();
        }

        async function decide(accepted) {
            if (!pending) { return; }
            var p = pending;
            pending = null;
            matchBtn.disabled = keepBtn.disabled = true;
            try {
                await fetch(p.targetUrl, {
                    method:  'POST',
                    headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf()},
                    body:    JSON.stringify({decisions: SimFlow.transitionDecisions(p.prompts, accepted)})
                });
            } catch (_) { /* navigate anyway */ }
            modal.classList.add('js-hidden');
            matchBtn.disabled = keepBtn.disabled = false;
            window.location.href = p.targetUrl;
        }

        async function navigateWithTransition(targetUrl) {
            try {
                var res  = await fetch(url, {headers: {'X-CSRFToken': csrf()}});
                var data = await res.json();
                if (data.prompts && data.prompts.length > 0) {
                    show(data.prompts, targetUrl);
                    return;
                }
            } catch (_) { /* on error fall through to plain navigate */ }
            window.location.href = targetUrl;
        }

        matchBtn.addEventListener('click', function () { decide(true); });
        keepBtn.addEventListener('click',  function () { decide(false); });

        document.querySelectorAll('.js-nav-btn').forEach(function (btn) {
            btn.addEventListener('click', function (e) {
                var target = btn.getAttribute('href');
                if (!target || target === '#') { return; }
                e.preventDefault();
                navigateWithTransition(target);
            });
        });
    }

    window.SimFlow = window.SimFlow || {};
    window.SimFlow.initTransition = init;
}());
