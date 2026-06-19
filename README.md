# Fog Controller

> **⚡ Update 2026-06 — Tank-Füllstand, Stack & UI**
>
> - **Tank / Füllstand (neu):** Vertikale Tankanzeige mit **selbstlernender Verbrauchsschätzung**. Jede Aktivierung ist ein diskreter Fog-Burst; das System zählt die Bursts seit dem letzten Nachfüllen und kalibriert aus deinem Refill-Feedback (Tank war leer / Restmenge) den echten **ml-pro-Aktivierung**-Wert (EWMA). Der Live-Füllstand wird daraus berechnet. Persistenz in einer **eigenen SQLite-DB** (`fog-tank.db`) — getrennt von MariaDB, keine zusätzlichen Dependencies. Nachfüllen & Tankgröße direkt in der GUI.
> - **Backend:** Python/**Flask** (migriert von Node/Express). RF433-Bit-Bang (`fog-controller.py`, RPi.GPIO, GPIO 17) läuft **in-process als `pi`** (kein sudo/Subprozess), Usage-Logging via **PyMySQL** (MariaDB), Auto-Fog im Hintergrund-Thread. **systemd** `fog-controller`. ~35 MB. Sicherheit: Fog startet nie automatisch.
> - **UI:** **Material Design 3 Expressive** + Spring-Animationen. Nutzungs-Chart als sauberes SVG (Achsen, Gridlines, Tooltips). Button-Texte mit MD3-„on-container"-Kontrast (dunkler Text auf hellen Akzentflächen). Favicon = 💨 (Dampf); alle Icon-/Manifest-Pfade relativ (laufen hinter dem `/app/fog/`-Reverse-Proxy).
> - **Auto-Fog-Intervalle:** 5 / 15 / 30 / 60 / 120 min, 1 h Auto-Off.
> - **Deploy:** `git pull && sudo systemctl restart fog-controller`

<div align="center">

![License](https://img.shields.io/badge/License-MIT-blue.svg)
![Python](https://img.shields.io/badge/Python-3.11-3776AB.svg?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-2.x-000000.svg?logo=flask&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Raspberry%20Pi-C51A4A.svg?logo=raspberrypi&logoColor=white)

RF433 fog machine controller for Raspberry Pi with a modern web interface, auto-fog scheduling, self-calibrating tank fill-level tracking, and usage analytics.

</div>

## Features

- **RF433 Control** — Trigger fog machines over 433 MHz RF signals via GPIO
- **Web Interface** — Responsive MD3-Expressive dark-theme PWA with touch support and fog animation
- **Auto-Fog** — Automatic activation at configurable intervals (5/15/30/60/120 min) with 1h auto-off
- **Tank Fill-Level** — Vertical tank gauge with a **self-calibrating** ml-per-activation estimate (learns from your refill feedback); refill & tank-capacity entry in the GUI; persisted in a dedicated **SQLite** DB (`fog-tank.db`)
- **Usage Analytics** — MariaDB-backed 24h usage history (SVG chart) with peak-hour analysis
- **REST API** — Full RESTful API for external integrations
- **systemd Managed** — Auto-start, monitoring, and crash recovery

## Wiring Diagram

```
    Raspberry Pi                         RF433 Transmitter Module
    ┌──────────────┐                     ┌────────────────────┐
    │              │                     │                    │
    │  GPIO17(11) ─┼─────────────────────┤── DATA             │
    │              │                     │                    │
    │    5V   (2) ─┼─────────────────────┤── VCC              │┐
    │              │                     │                    ││ Antenna
    │   GND   (6) ─┼─────────────────────┤── GND              ││ (17cm wire)
    │              │                     │                    │┘
    └──────────────┘                     └────────────────────┘
                                                  ·  ·  ·
                                            433 MHz wireless
                                                  ·  ·  ·
                                         ┌────────────────────┐
                                         │    Fog Machine     │
                                         │   (433MHz Rx)      │
                                         │                    │
                                         │  ON:  0x454B8C     │
                                         │  OFF: 0x454BB0     │
                                         └────────────────────┘

    Protocol 1 · 320µs pulse · 24-bit codes · 10x repeat

    ┌──────────┬──────────┬──────────────────────────────────┐
    │ Pi Pin   │ GPIO     │ Connection                       │
    ├──────────┼──────────┼──────────────────────────────────┤
    │ Pin 11   │ GPIO 17  │ RF433 DATA                       │
    │ Pin 2    │ 5V       │ RF433 VCC                        │
    │ Pin 6    │ GND      │ RF433 GND                        │
    └──────────┴──────────┴──────────────────────────────────┘
```

> **Note:** For better range, solder a 17cm straight wire as antenna to the RF433 transmitter. Uses lgpio for GPIO access.

## Quick Start

```bash
git clone https://github.com/pepperonas/fog-controller.git
cd fog-controller
python3 -m venv --system-site-packages venv
venv/bin/pip install -r requirements.txt
venv/bin/python server.py
```

The web interface is available at `http://<pi-ip>:5003`. In production it runs as the
`fog-controller` **systemd** service (see `fog-controller.service`).

> MariaDB (usage analytics) is optional — the app runs without it. The SQLite tank DB
> (`fog-tank.db`) is created automatically on first start; no setup required.

## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Health check |
| `GET` | `/api/status` | Current fog status + activation count + peak hour |
| `POST` | `/api/fog/on` | Turn fog on (counts as one activation/burst) |
| `POST` | `/api/fog/off` | Turn fog off |
| `POST` | `/api/fog/toggle` | Toggle fog |
| `POST` | `/api/auto-fog/enable` | Enable auto-fog (`{interval: 5\|15\|30\|60\|120}`) |
| `POST` | `/api/auto-fog/disable` | Disable auto-fog |
| `GET` | `/api/analytics/usage` | 24h usage analytics (hourly + peak hour) |
| `GET` | `/api/tank` | Tank state: level (ml/%), est. activations left, ml/activation, calibrated flag |
| `POST` | `/api/tank/refill` | Log a refill: `{full}` and/or `{amount_ml, remaining_ml}` or `{was_empty}` → recalibrates |
| `POST` | `/api/tank/config` | Set tank capacity: `{capacity_ml}` (50–5000) |
| `GET` | `/api/tank/history` | Recent refills + calibration samples |

### Tank fill-level & self-calibration

Each `on` is one discrete fog burst that consumes roughly a fixed amount of fluid, so
consumption is tracked **per activation**. The live level is derived (never polled):

```
level_ml = level_at_refill_ml − activations_since_refill × ml_per_activation
```

On refill you tell the app how empty the tank was (`was_empty`, or a `remaining_ml`
estimate). The finished cycle yields a calibration sample
`ml_per_activation = consumed / activations`, blended into the running estimate via an
EWMA (an empty tank is weighted higher as the strongest signal). Until the first sample
the value is a seed and the UI shows a *"noch nicht kalibriert"* badge. All of this lives
in `fog-tank.db` (SQLite, `tank` + `refills` tables); MariaDB is untouched.

## Tech Stack

- **Runtime** — Python 3.11, Flask 2
- **Databases** — MariaDB via PyMySQL (usage analytics, optional) · SQLite (tank/fill-level, auto-created)
- **Hardware** — RF433 transmitter on GPIO 17, RPi.GPIO (in-process, no sudo)
- **Process Manager** — systemd (`fog-controller.service`)

## Author

**Martin Pfeffer** — [celox.io](https://celox.io)

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
