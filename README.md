# SimFlow

**A smart checklist for the Zibo 737 in X-Plane 12.** Open it on a tablet or
second screen, and it follows the aircraft: flip a switch in the cockpit and
the item ticks itself.

Not a replacement for the real checklist — a companion that keeps your place
in it, so you can fly the aeroplane instead of scrolling a PDF.

---

## What it does

- **Follows the sim.** A small X-Plane plugin reports switch positions, and
  items check themselves as you set them. Items you can't automate, you tap.
- **Knows the phase.** The checklist moves with the flight — before start,
  taxi, takeoff, cruise, approach — and reveals procedures when they apply.
- **Adapts to the flight.** Tell it cold and dark or turnaround, single or
  dual pilot, and it hides what doesn't apply. Pull a SimBrief OFP and it
  picks up your runway, temperature and flap setting.
- **Splits PF/PM.** Flying with someone else, each screen dims the items that
  belong to the other seat.
- **Works without the plugin**, in manual mode — you just tap everything.

## Requirements

| | |
|---|---|
| Aircraft | **Zibo B738** (the checklist content is 737-specific) |
| Sim | X-Plane 12 |
| Plugin | [XPPython3](https://xppython3.readthedocs.io/) — needed for the sim link |
| Browser | anything current, on any device on your network |

## Getting started

**1. Make an account** at [simflow.vdwaal.net](https://simflow.vdwaal.net) and
confirm your email.

**2. Install the plugin.** Download `xflow-plugin.zip` from the
[plugin releases](https://github.com/chezhj/simflow/releases?q=plugin&expanded=true),
then:

```
X-Plane 12/Resources/plugins/PythonPlugins/
├── PI_xFlow.py
└── xFlow/
    └── config.ini        ← rename config.ini.example to this
```

**3. Paste your API key.** Generate one on your SimFlow profile page and put it
in `config.ini`:

```ini
[xflow]
api_key = fvw_your_key_here
backend_url = https://simflow.vdwaal.net
log_level = INFO
poll_interval = 0.5
```

The key is how the plugin proves the sim is yours — treat it like a password,
and regenerate it on the profile page if it leaks.

**4. Fly.** Start a checklist in the browser, load the aircraft, and the
connection dot goes green.

### Useful X-Plane commands

Bind these under Settings → Keyboard (search `xFlow`):

| Command | Does |
|---|---|
| `xFlow/check_next_item` | Check the next item — handy on the yoke |
| `xFlow/report_miss` | Report an item that should have auto-checked but didn't |
| `xFlow/dump_watch` | Log the current datarefs, for troubleshooting |
| `xFlow/net_probe` | Time the connection to the server |

`report_miss` is the useful one if automation misses something: it records the
rule and the dataref values at that moment, which is what a fix needs.

## Troubleshooting

**The dot stays grey.** Check `api_key` and `backend_url` in `config.ini`, and
look in `Log.txt` / `XPPython3Log.txt` for lines starting `[xFlow]`. An
authentication failure says so explicitly.

**"Update your plugin" banner.** Your plugin is a minor version behind the
server. Download the current one from the link above.

**Items don't auto-check.** Confirm the aircraft is the Zibo B738 — the rules
read its specific datarefs. If it is, use `xFlow/report_miss` on the item.

## Contributing

Issues and pull requests welcome. It's a hobby project run by one person, so
responses come when flying and work allow.

```bash
poetry install
python manage.py migrate
python manage.py runserver
pytest                              # 540 tests
djlint checklist/templates/         # template lint
```

`CLAUDE.md` is the fastest way into the architecture. `docs/SPEC.md` is the
implementation plan.

## Licence

[MIT](LICENSE). Do what you like with it.

Not affiliated with Laminar Research, Zibo, or SimBrief. The checklist content
follows published 737 procedures and is for simulation only — **not for real
world flight**.
