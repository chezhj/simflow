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


def _summarise(name, samples, note=""):
    if not samples:
        print(f"  {name:<34} no successful samples")
        return
    ordered = sorted(samples)
    p90 = ordered[max(0, int(len(ordered) * 0.9) - 1)]
    print(
        f"  {name:<34} min {min(ordered):7.1f}  median {statistics.median(ordered):7.1f}"
        f"  p90 {p90:7.1f}  max {max(ordered):7.1f} ms  {note}"
    )


def _time_urllib(url, headers, body, n):
    """Fresh connection per request, the way the plugin's fallback path works."""
    samples, failures = [], 0
    for _ in range(n):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, headers=headers, method="POST" if body is not None else "GET"
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            samples.append((time.perf_counter() - started) * 1000)
        except urllib.error.HTTPError as exc:
            # A 4xx still exercised the whole path up to the response.
            samples.append((time.perf_counter() - started) * 1000)
            if exc.code not in (200, 404):
                failures += 1
        except Exception:
            failures += 1
    return samples, failures


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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--key", default=os.environ.get("XFLOW_API_KEY", ""))
    ap.add_argument("-n", type=int, default=15, help="requests per measurement")
    ap.add_argument("--plugin-version", default=DEFAULT_PLUGIN_VERSION,
                    help="sent as X-Plugin-Version; keep it at or above "
                         "PLUGIN_WARN_BELOW or the web UI will show an update banner")
    ap.add_argument("--state", action="store_true",
                    help="also probe /api/plugin/state/ — has side effects, see module docstring")
    ap.add_argument("--session-id", type=int, default=0,
                    help="required with --state; the FlightSession to post against")
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
    s, f = _time_urllib(f"{base}/", {}, None, args.n)
    _summarise("GET /  (read-only, no auth)", s, f"{f} failed" if f else "")

    if args.key:
        s, f = _time_urllib(f"{base}/api/plugin/session/", auth, None, args.n)
        _summarise("GET /api/plugin/session/  (1 write)", s, f"{f} failed" if f else "")

        if args.state:
            if not args.session_id:
                print("  --state needs --session-id")
            else:
                body = {"session_id": args.session_id, "datarefs": {}}
                s, f = _time_urllib(f"{base}/api/plugin/state/", auth, body, args.n)
                _summarise("POST /api/plugin/state/ (1 write+rules)", s,
                           f"{f} failed" if f else "")
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
    print("Read the gap between '/' and the authenticated endpoints: that is what")
    print("the database write costs, with network and TLS already subtracted.")


if __name__ == "__main__":
    main()
