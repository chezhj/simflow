#!/usr/bin/env python3
"""
Time the SimFlow server from the machine the sim runs on.

Written because a flight log showed plugin state POSTs at a 593 ms floor with
83% of samples inside a 100 ms band. That shape is not contention — contention
is fast when uncontended and slow when not. A hard floor means every request
pays the same cost unconditionally.

Three endpoints, chosen so the difference between them isolates that cost:

    /                       read-only. No auth, no write. Baseline for
                            network + TLS + Django overhead.
    /api/plugin/session/    3 queries, one of which is a WRITE. No rule
                            evaluation, no watch list.
    /api/plugin/state/      15 queries, one write, plus rule evaluation.
                            What the plugin actually calls at 2 Hz.

Read it as:
    all three fast                  -> the slowness is not the server
    "/" fast, the other two slow    -> the write is the cost (fsync per commit)
    only /state/ slow               -> something specific to that view

Also times a reused connection against a fresh one, because the plugin calls
requests.post at module level and so opens a new connection every time.

    python3 probe_server.py --key fvw_... [--url https://simflow.vdwaal.net]

The key is read from --key or the XFLOW_API_KEY environment variable; it is
never written to disk or echoed.

Side effect: /api/plugin/session/ stamps last_plugin_contact and
plugin_version on the active FlightSession, exactly as the real plugin does
every tick. --state additionally exercises the rule engine and may auto-check
items, so it is opt-in and should not be pointed at a session you care about.
"""

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

try:
    import requests
except ImportError:
    requests = None

DEFAULT_URL = "https://simflow.vdwaal.net"
DEFAULT_PLUGIN_VERSION = "1.1.2"


def _get_json(url, headers, body=None):
    """One request, returning parsed JSON or None. Used for discovery, not timing."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if body is not None else "GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read() or b"{}")
    except Exception:
        return None


def _discover_session(base, headers):
    """The session endpoint already returns the id, so do not make the user find it."""
    data = _get_json(f"{base}/api/plugin/session/", headers)
    return (data or {}).get("session_id")


def _discover_watch(base, headers, session_id):
    """
    Ask the server which datarefs it wants, so the timed requests carry a
    realistic payload. An empty datarefs object makes every rule fail on the
    first comparison, which would flatter the endpoint.

    Returns None if the discovery request itself failed — distinct from a
    successful response that carries an empty watch list, which is a real
    answer (nothing left to watch in this phase).
    """
    data = _get_json(f"{base}/api/plugin/state/", headers,
                     {"session_id": session_id, "datarefs": {}})
    if data is None:
        return None
    return data.get("watch", [])


def _summarise(name, samples, codes=None, note=""):
    if not samples:
        print(f"  {name:<34} no successful samples  {note}")
        return
    ordered = sorted(samples)
    p90 = ordered[max(0, int(len(ordered) * 0.9) - 1)]
    tail = f"  [{', '.join(f'{c}x{n}' for c, n in sorted((codes or {}).items()))}]" if codes else ""
    print(
        f"  {name:<34} min {min(ordered):7.1f}  median {statistics.median(ordered):7.1f}"
        f"  p90 {p90:7.1f}  max {max(ordered):7.1f} ms{tail}  {note}"
    )
    # A 404 returns BEFORE the write in both authenticated views
    # (plugin_views.py: the 404 is four lines above the UPDATE), so timing one
    # measures auth and a failed lookup and nothing else. This is exactly how
    # an earlier baseline came back at 0.31 s and misled us for days.
    if codes and codes.get(404):
        print("       ^^ 404: no ACTIVE FlightSession, so the request returned "
              "before the write.\n"
              "          Start a checklist in the web UI, then run this again — "
              "otherwise this number means nothing.")


def _time_urllib(url, headers, body, n):
    """
    Fresh connection per request, the way the plugin's fallback path works.

    Returns (samples, status_code_counts, transport_failures). The status codes
    matter as much as the timings: a 404 is timed like any other response but
    returns before the database write, so it measures nothing we care about.
    """
    samples, codes, failures = [], {}, 0
    for _ in range(n):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, headers=headers, method="POST" if body is not None else "GET"
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
                code = resp.status
            samples.append((time.perf_counter() - started) * 1000)
            codes[code] = codes.get(code, 0) + 1
        except urllib.error.HTTPError as exc:
            samples.append((time.perf_counter() - started) * 1000)
            codes[exc.code] = codes.get(exc.code, 0) + 1
        except Exception:
            failures += 1
    return samples, codes, failures


def _time_requests(url, headers, body, n, session=None):
    caller = session or requests
    samples, failures = [], 0
    for _ in range(n):
        started = time.perf_counter()
        try:
            if body is None:
                caller.get(url, headers=headers, timeout=15)
            else:
                caller.post(url, headers=headers, json=body, timeout=15)
            samples.append((time.perf_counter() - started) * 1000)
        except Exception:
            failures += 1
    return samples, failures


def _probe_state(base, headers, args):
    """
    Time /api/plugin/state/ with a payload the server actually asked for.

    Split out of main() so an aborted discovery skips only this measurement;
    the connection-reuse numbers below it are still worth having.
    """
    session_id = args.session_id or _discover_session(base, headers)
    if not session_id:
        print("  could not determine a session id — start a checklist in the "
              "web UI, or pass --session-id")
        return

    # First POST with no datarefs, purely to learn the watch list the server
    # wants. Timing this would understate the endpoint: with nothing to
    # evaluate against, every rule fails on its first comparison.
    watch = _discover_watch(base, headers, session_id)
    if watch is None:
        print("  the discovery POST to /api/plugin/state/ failed — timing it "
              "now would measure an error path, not the rule engine. Check "
              "the session is active and retry.")
        return

    payload = {path: 0.0 for path in watch}
    print(f"  (session {session_id}, {len(payload)} datarefs in the watch list)")
    body = {"session_id": session_id, "datarefs": payload}
    s, c, f = _time_urllib(f"{base}/api/plugin/state/", headers, body, args.n)
    _summarise("POST /api/plugin/state/ (write+rules)", s, c,
               f"{f} failed" if f else "")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--key", default=os.environ.get("XFLOW_API_KEY", ""))
    ap.add_argument("-n", type=int, default=15, help="requests per measurement")
    ap.add_argument("--plugin-version", default=DEFAULT_PLUGIN_VERSION,
                    help="sent as X-Plugin-Version; keep it at or above "
                         "PLUGIN_WARN_BELOW or the web UI will show an update banner")
    ap.add_argument("--state", action="store_true",
                    help="also probe /api/plugin/state/ — HAS SIDE EFFECTS: it sends "
                         "the real watch list and can auto-check items. Reset the "
                         "procedure afterwards.")
    ap.add_argument("--session-id", type=int, default=0,
                    help="optional; discovered from /api/plugin/session/ if omitted")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    auth = {
        "Authorization": f"Bearer {args.key}",
        "X-Plugin-Version": args.plugin_version,
        "Content-Type": "application/json",
    }

    print(f"target : {base}")
    print(f"python : {sys.version.split()[0]}   requests: "
          f"{'yes' if requests else 'no (urllib only)'}")
    print(f"samples: {args.n} per measurement")
    print()

    print("fresh connection each time (what the plugin does today)")
    s, c, f = _time_urllib(f"{base}/", {}, None, args.n)
    _summarise("GET /  (read-only, no auth)", s, c, f"{f} failed" if f else "")

    if args.key:
        s, c, f = _time_urllib(f"{base}/api/plugin/session/", auth, None, args.n)
        _summarise("GET /api/plugin/session/  (1 write)", s, c, f"{f} failed" if f else "")

        if args.state:
            _probe_state(base, auth, args)
    else:
        print("  (no --key / XFLOW_API_KEY, skipping the authenticated endpoints)")

    if requests is not None:
        print()
        print("connection reuse — does pooling help?")
        s, f = _time_requests(f"{base}/", {}, None, args.n)
        _summarise("GET /  new connection each time", s)
        with requests.Session() as sess:
            try:
                sess.get(f"{base}/", timeout=15)  # warm the pool, not measured
            except Exception as exc:
                # An unreachable host must not take the probe down — the
                # measurements above are still worth printing.
                print(f"  could not warm the connection pool: "
                      f"{type(exc).__name__}")
            else:
                s, f = _time_requests(f"{base}/", {}, None, args.n, session=sess)
                _summarise("GET /  reused connection", s)

    print()
    print("Measured on 2026-09-24 from the dev machine: '/' 59-64 ms median,")
    print("/api/plugin/session/ 64-76 ms. The write costs 5-10 ms, so it is not")
    print("the 593 ms floor. What remains untimed there is /state/'s own work:")
    print("run with --state and compare against those two numbers.")


if __name__ == "__main__":
    main()
