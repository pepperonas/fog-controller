# Fog Controller

<div align="center">

![License](https://img.shields.io/badge/License-MIT-blue.svg)
![Node.js](https://img.shields.io/badge/Node.js-18+-339933.svg?logo=nodedotjs&logoColor=white)
![Express](https://img.shields.io/badge/Express-4.x-000000.svg?logo=express&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Raspberry%20Pi-C51A4A.svg?logo=raspberrypi&logoColor=white)

RF433 fog machine controller for Raspberry Pi with a modern web interface, auto-fog scheduling, and usage analytics.

</div>

## Features

- **RF433 Control** — Trigger fog machines over 433 MHz RF signals via GPIO
- **Web Interface** — Responsive dark-theme PWA with touch support and fog animation
- **Auto-Fog** — Automatic activation at configurable intervals (2/5/10 min) with 1h auto-off
- **Usage Analytics** — MySQL-backed 24h usage history with peak-hour analysis
- **REST API** — Full RESTful API for external integrations
- **PM2 Managed** — Auto-start, monitoring, and crash recovery

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
npm install
npm start
```

The web interface is available at `http://<pi-ip>:5003`.

## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Health check |
| `GET` | `/api/status` | Current fog status |
| `POST` | `/api/fog/on` | Turn fog on |
| `POST` | `/api/fog/off` | Turn fog off |
| `POST` | `/api/fog/toggle` | Toggle fog |
| `POST` | `/api/auto-fog/enable` | Enable auto-fog |
| `POST` | `/api/auto-fog/disable` | Disable auto-fog |
| `GET` | `/api/analytics/usage` | Usage analytics |

## Tech Stack

- **Runtime** — Node.js 18+, Express 4
- **Database** — MySQL (optional, for analytics)
- **Hardware** — RF433 transmitter, lgpio
- **Process Manager** — PM2

## Author

**Martin Pfeffer** — [celox.io](https://celox.io)

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
