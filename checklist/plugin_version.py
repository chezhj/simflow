"""
The xFlow plugin version this release of the app was built alongside.

GENERATED — `scripts/bump_plugin.py` rewrites the string below in the same
commit that bumps `xplane_plugin/xFlow/PI_xFlow.py`. Do not edit by hand;
`test_the_marker_matches_the_plugin_source` fails if the two drift.

This file exists because the app has to know the current plugin version at
runtime to classify the one a client reports, and it cannot read it from the
plugin: `.github/workflows/release-deploy.yaml` excludes `xplane_plugin` from
the rsync, so `PI_xFlow.py` is not present in a deployed release at all.
Parsing it would work in dev and in tests and quietly do nothing in
production, which is the worst of the three.

It is a derived fact, not a policy knob. The blocking and warning thresholds
are no longer configured — see plugin_status_for_version.
"""

CURRENT_PLUGIN_VERSION = "1.2.0"
