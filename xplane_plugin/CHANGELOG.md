# xFlow Plugin Changelog

## [1.0.0] — 2026-07-08

### Changed

- Major release & rename
- added command to report mismatch

## [0.6.0] — 2026-05-10

### Changed

- Minor changes in logic & testing bump

---

## [0.5.0] — 2026-05-09

### Added

- `PLUGIN_VERSION` constant — plugin now identifies itself to the server via
  `X-Plugin-Version` request header on all API calls.
- Version compatibility handshake: server responds with `plugin_status`
  (`ok` | `warn` | `blocked`). Plugin logs a warning or stops polling
  accordingly.
- SOP metadata logging on connect: `[xFlow] connected — B738 SOP v1.0.0 | plugin v1.0.0`.
- `_blocked` flag — when server rejects the plugin version the flight loop
  halts cleanly without flooding the server with retries.

### Fixed

- Install instructions in the module docstring were incorrect (said to copy
  the `xFlow/` folder; `PI_xFlow.py` must be placed directly in `PythonPlugins/`).
