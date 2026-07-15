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
    // base.html owns the shared <body>; the preference flag lives on the
    // procedure-execution container instead.
    const root = document.querySelector('.proc-layout');
    const enabled = root && root.dataset.keepScreenOn === 'true';
    if (!enabled) return;
    requestWakeLock();
    document.addEventListener('visibilitychange', handleVisibilityChange);
  });
})();
