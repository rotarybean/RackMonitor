# RackMonitor

An analog-CRT status display for a self-hosted game-server box. A single-board
computer scrapes [Prometheus node_exporter](https://github.com/prometheus/node_exporter)
over the LAN and renders a live status screen straight to the framebuffer on a
small composite-video tube — which game is running, machine and service uptime,
RAM, and the servers on standby.

Built for a [Libre Computer "Le Potato"](https://libre.computer/products/aml-s905x-cc/)
(Amlogic S905X) driving a 4" BNC broadcast monitor over CVBS, but it's just
`PIL` writing BGRA frames to `/dev/fb0`, so anything with a framebuffer works.

![zone + safe-area calibration overlay](docs/calibration_preview.png)

```
 game-server host                    single-board computer           composite CRT
┌────────────────────────┐         ┌──────────────────┐          ┌──────────┐
│ node_exporter :9100    │  HTTP   │ monitor.py       │  CVBS    │ 720x576  │
│  + systemd collector   │◄────────│  poll thread     │─────────►│  PAL     │
│                        │  poll   │  render @10fps   │ /dev/fb0 │          │
│ asa-server.service     │   5s    │                  │          │          │
│ rust-server.service    │         └──────────────────┘          └──────────┘
│ sotf-server.service    │
└────────────────────────┘
```

The SBC never talks to Docker or systemd on the host directly. It reads six
numbers out of `/metrics` and draws them — no agent, no SSH, no Prometheus
server, no database. The scraper is Python stdlib `urllib` plus a small
Prometheus text-format parser; the only third-party dependency is Pillow.

## What's on screen

| Element | Source metric |
|---|---|
| Center icon — which game is running | `node_systemd_unit_state{state="active"} == 1` |
| Top-left — machine uptime | `node_time_seconds − node_boot_time_seconds` |
| Top-right — service uptime | `node_time_seconds − node_systemd_unit_start_time_seconds` |
| Bottom-left — standby games | the configured units that are *not* active |
| Bottom-right — RAM % | `(1 − MemAvailable / MemTotal) × 100` |
| Host unreachable | the center icon switches to an offline graphic and shakes; the standby zone hides |

The running game's icon bobs; the offline icon shakes. Motion is the fastest
"is it up?" read on a small tube.

### A note on clocks

Both ends of every uptime subtraction come from the **host's** clock
(`node_time_seconds`), never the SBC's. Many SBCs have no RTC, so their own wall
clock is wrong until NTP lands — mixing it in would make uptimes jump at boot.
Between polls the display extrapolates with `time.monotonic()`, so the seconds
tick smoothly instead of stepping every poll interval.

## Repo layout

```
gameserver/   # runs on the host being monitored
  install_node_exporter.sh        # pinned install + systemd collector + self-check
  node_exporter.service           # unit, LAN-bound, systemd collector enabled
  EXAMPLE-newgame-server.service  # template for adding a game (bidirectional Conflicts)
lepotato/     # runs on the SBC driving the CRT
  monitor.py                      # the whole display: scraper, parser, renderer
  config.example.ini              # copy to config.ini and edit
  rackmonitor.service             # unit that owns tty1 / the framebuffer
assets/       # you supply the PNGs (see assets/README.md)
docs/
```

## Quick start

### 1. On the host — node_exporter

```bash
scp -r gameserver/ YOURHOST:~/
ssh YOURHOST && cd gameserver
# edit the vars at the top: GAMESERVER_IP, LEPOTATO_IP, ARCH if not x86_64
sudo ./install_node_exporter.sh
```

Three flags do the work, set in `node_exporter.service`:

- `--collector.systemd` → `node_systemd_unit_state{name,state}`
- `--collector.systemd.enable-start-time-metrics` → `node_systemd_unit_start_time_seconds`
  — **off by default**; without it the service-uptime zone shows `--`
- `--collector.systemd.unit-include=.*-server\.service` → only enumerates your
  game units over D-Bus, keeping scrapes cheap. It's a *convention*: any unit
  named `*-server.service` is picked up, so new games need no exporter change.

The installer scrapes once and verifies every metric the display needs, and
prints `node_scrape_collector_success` for the relevant collectors as the
authoritative check.

### 2. Artwork

See [`assets/README.md`](assets/README.md). You supply the PNGs — the game logos
are trademarked and deliberately **not** shipped here. Minimum: `background.png`
(720×576) and `offline.png` (160×160), plus one 160×160 logo per game.

### 3. On the SBC — the display

```bash
sudo apt install -y python3-pil fonts-dejavu-core
sudo mkdir -p /opt/rackmonitor
sudo cp lepotato/monitor.py /opt/rackmonitor/
sudo cp lepotato/config.example.ini /opt/rackmonitor/config.ini   # then edit host IP + paths
sudo cp -r assets /opt/rackmonitor/
sudo cp lepotato/rackmonitor.service /etc/systemd/system/
```

Dial in and test before enabling — no network or live server needed:

```bash
python3 monitor.py --doctor      # cross-checks config ↔ assets ↔ what the host exports
sudo python3 monitor.py --calibrate   # draws safe-area + zone outlines on the tube
sudo python3 monitor.py --fake        # cycles every state, 6s each
```

`--calibrate` matters: a lot of broadcast monitors overscan 5–10% and will eat
text placed too close to the edge. Nudge the zone `x`/`y`/`width`/`height` in
`config.ini` until the yellow zone boxes sit inside the blue action-safe box.

Then:

```bash
sudo systemctl disable --now getty@tty1   # stop the login console drawing over you
sudo systemctl daemon-reload
sudo systemctl enable --now rackmonitor
journalctl -u rackmonitor -f
```

## Adding a game

Three files, no code:

1. **Host:** a `*-server.service` unit. Copy `gameserver/EXAMPLE-newgame-server.service`.
   Name it `<game>-server.service` so the exporter regex catches it. `Conflicts=`
   only needs to list the *existing* game units — systemd makes it bidirectional,
   so the old units learn about the new one automatically.
2. **SBC:** one line in `config.ini` under `[services]`.
3. **SBC:** drop `<game>.png` in `assets/`.

Then `monitor.py --doctor` to confirm all three agree, and restart the service.
The standby-zone grid re-solves itself for the new game count — it searches
rows × columns for the largest icons that fit, and falls back to a `+N` badge if
you cram in more than the zone can show (your cue to enlarge the zone).

## Flags

| Flag | Does |
|---|---|
| *(none)* | normal operation — poll and render to the framebuffer |
| `--doctor` | validate config + assets against the host's live metrics, then exit |
| `--calibrate` | draw the safe-area / zone overlay instead of live data |
| `--fake` | cycle through every display state with no network |
| `--preview out.png` | render a single frame to a PNG (no framebuffer needed) |
| `-c / --config PATH` | use a specific config file |

## Requirements

- **Host:** Linux with systemd; node_exporter 1.11.1 (installer pins it).
- **SBC:** Python 3.8+, Pillow, a working framebuffer (`/dev/fb0`), a DejaVu (or
  other TTF) font. No pip packages beyond Pillow.

## License

MIT — see [LICENSE](LICENSE).
