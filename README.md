# 💨 Fog Controller

RF433 Nebelmaschinen-Steuerung via Raspberry Pi mit modernem Web-Interface.

![Fog Controller Mockup](fog-controller-mockup-1.png)

## Features

- **🌫️ RF433 Steuerung**: Kontrolle von Nebelmaschinen über 433MHz RF-Signale mit GPIO 17
- **💻 Web Interface**: Modernes, responsives Dark-Theme PWA-Interface mit Touch-Support
- **✨ Realistische Nebel-Animation**: Animierte Hintergrund-Effekte beim Aktivieren der Nebelmaschine
- **🤖 Auto-Fog**: Automatische Aktivierung alle 2/5/10 Minuten mit 1h Auto-Deaktivierung
- **📊 MySQL Integration**: Persistente Speicherung aller Aktivierungen mit Zeitstempel
- **📈 Usage Analytics**: 24h-Verlaufsgrafik mit Peak-Hour-Analyse
- **📱 Status-Tracking**: Live-Updates, Aktivierungszähler und Statistiken
- **🔄 PWA-Support**: Installierbar als Progressive Web App mit Offline-Funktionen
- **🌐 REST API**: Vollständige RESTful API für externe Integration
- **⚡ PM2 Integration**: Automatischer Start, Monitoring und Restart-Management

## Hardware Setup

### RF433 Modul Anschluss
- **GPIO 17 (Pin 11)**: RF433 Sender DATA Pin
- **5V (Pin 2/4)**: RF433 VCC
- **GND (Pin 6/9)**: RF433 GND

### RF433 Codes (Hardware-spezifisch)
- **ON Code**: 4543756 (0x455B7C)
- **OFF Code**: 4543792 (0x455B90)
- **Protocol**: 1 (RCSwitch kompatibel)
- **Pulse Length**: 320 µs
- **Bit Length**: 24 bits
- **Repeat Count**: 10x für Zuverlässigkeit

## Installation

```bash
# Repository klonen
git clone <repository-url>
cd fog-controller

# Dependencies installieren
npm install

# MySQL Setup (optional, für Analytics)
mysql -u root -p < setup-database.sql

# Umgebungsvariablen für MySQL (optional)
export DB_HOST=127.0.0.1
export DB_USER=fog_user
export DB_PASSWORD=fog_password
export DB_NAME=fog_controller

# WICHTIG: Nach Verzeichnis-Änderungen fog-config.json prüfen
# Der Python-Script-Pfad wird automatisch gesetzt, kann aber angepasst werden

# Direkt starten (erfordert sudo für GPIO-Zugriff)
sudo npm start

# Oder mit PM2 (empfohlen)
npm run pm2:start
```

## Web Interface

Das Interface ist unter `http://fog.pi.local` oder `http://localhost:5003` erreichbar und bietet:

### Hauptfunktionen
- **💨 Großer Fog-Button**: Toggle-Funktion zum Ein-/Ausschalten
  - Visueller Status mit Pulsing-Animation bei aktiver Nebelmaschine
  - Sendet einmaliges RF433-Signal pro Klick
  - Live-Status-Updates alle 5 Sekunden
  
- **✨ Verbesserte Nebel-Animation**: Dramatische, aber dezente Hintergrund-Effekte
  - 5 animierte Nebel-Schichten mit realistischen Bewegungsmustern
  - Nebel schwebt flüssig von beiden Seiten zur Mitte des Bildschirms
  - Sichtbare Erscheinungs-Animation mit Zoom-Effekt (1.8s)
  - Längere Auflöse-Animation mit Expansions-Effekt (5-6s)
  - Hardware-beschleunigte CSS3-Animationen ohne GUI-Flackern
  - Performance-optimiert mit `will-change` Properties
  
- **🤖 Auto-Fog System**: Automatisierte Aktivierung mit Zeitsteuerung
  - Intervall-Optionen: 2, 5 oder 10 Minuten
  - Automatische Deaktivierung nach 1 Stunde
  - Separate Steuerung unabhängig vom manuellen Button
  - Live-Status mit verbleibender Zeit

### Interface-Features
- **📊 Live-Statistiken**: Aktivierungszähler, letzte Aktivierung, Peak-Hour
- **📈 24h-Analytics**: Interaktive Stunden-Balkendiagramm der Nutzung
- **🔴 Notaus-Button**: Sofortige Deaktivierung
- **📱 PWA-Ready**: Installierbar auf Mobilgeräten
- **🌙 Dark Theme**: Modernes, augenfreundliches Design mit Nebel-Effekten
- **📲 Installierbar**: Als App auf Smartphone-Homescreen mit korrekten Favicons

## API Endpoints

### Health & Status
```bash
GET /api/health                       # Service Health Check
GET /api/status                       # Aktueller Fog-Status + Statistiken
```

### Nebelmaschine steuern
```bash
POST /api/fog/on                      # Einschalten
POST /api/fog/off                     # Ausschalten  
POST /api/fog/toggle                  # Toggle (empfohlen)
POST /api/fog/custom                  # Custom RF-Code senden
     # Body: {"code": "4543756"}
```

### Auto-Fog System
```bash
GET /api/auto-fog/status              # Auto-Fog Status + Timer
POST /api/auto-fog/enable             # Auto-Fog aktivieren
     # Body: {"interval": 5}           # 2, 5 oder 10 Minuten
POST /api/auto-fog/disable            # Auto-Fog deaktivieren
```

### Analytics & Statistiken
```bash
GET /api/analytics/usage              # 24h-Nutzungsstatistiken
POST /api/stats/reset                 # Statistiken zurücksetzen
```

## Python Script (fog-controller.py)

Das RF433-Python-Script kann auch direkt verwendet werden:

```bash
# Nebelmaschine einschalten
sudo python3 fog-controller.py --command on

# Nebelmaschine ausschalten
sudo python3 fog-controller.py --command off

# Custom RF-Code senden (Hex oder Dezimal)
sudo python3 fog-controller.py --command custom --code 4543756
sudo python3 fog-controller.py --command custom --code 0x455B7C

# Erweiterte Optionen
sudo python3 fog-controller.py --command on --gpio 18 --repeats 5
```

### Script-Parameter
- `--command`: on/off/custom (erforderlich)
- `--code`: RF-Code für custom (nur bei custom erforderlich)
- `--gpio`: GPIO-Pin (Standard: 17)
- `--repeats`: Wiederholungen (Standard: 10)

## PM2 Deployment

Die App ist vollständig für PM2 vorbereitet mit automatischer Überwachung und Neustart.

```bash
# PM2 starten
npm run pm2:start

# Status prüfen
pm2 status

# Beim Boot starten
npm run pm2:save
npm run pm2:startup
# Folge den Anweisungen

# Logs anzeigen
npm run pm2:logs

# Stoppen
npm run pm2:stop

# Neustarten
npm run pm2:restart
```

### PM2 Konfiguration (ecosystem.config.js)

- **Instanzen**: 1 (einzelne Instanz für exklusiven GPIO-Zugriff)
- **Memory Limit**: 200MB mit automatischem Restart
- **Error Handling**: Automatischer Neustart bei Crashes (max. 10x)
- **Restart Delay**: 4 Sekunden zwischen Restarts
- **Log Management**: Separate Error-, Output- und Combined-Logs mit Zeitstempel
- **Graceful Shutdown**: Sauberes Beenden mit Auto-Fog-Cleanup
- **Health Checks**: Listen timeout und uptime monitoring

## Entwicklung

```bash
# Development Mode mit Auto-Reload
npm run dev

# PM2 Logs
npm run pm2:logs

# PM2 Restart
npm run pm2:restart
```

### Package.json Scripts

- `npm start` - Startet den Server direkt
- `npm run dev` - Development Mode mit nodemon
- `npm run pm2:start` - PM2 Start mit ecosystem.config.js
- `npm run pm2:stop` - PM2 Stop
- `npm run pm2:restart` - PM2 Restart
- `npm run pm2:logs` - PM2 Logs anzeigen
- `npm run pm2:save` - PM2 Konfiguration speichern
- `npm run pm2:startup` - PM2 Startup-Script generieren

## Technische Details

### Architektur
- **Frontend**: Vanilla JavaScript, CSS Grid, Dark Theme, PWA mit Service Worker
- **Animation**: Optimierte CSS3-Nebel-Effekte mit GPU-Beschleunigung und flackerfreier Performance
- **Backend**: Node.js/Express Server (Port 5003) mit CORS-Support
- **Hardware**: Python 3 mit RPi.GPIO für präzise RF433-Timing
- **Database**: MySQL 8+ für Analytics und Logging (optional, graceful fallback)
- **Deployment**: PM2 für Production-Deployment mit automatischem Restart
- **Scheduling**: Node-cron für Auto-Fog mit 1h Auto-Disable
- **Security**: Input-Validierung, execFile statt exec, Hex-Code-Validierung

### RF433 Protokoll-Implementation
Das System implementiert das RCSwitch-kompatible Protokoll 1 mit:
- **MSB-first Übertragung** (Most Significant Bit zuerst)
- **24-bit RF-Codes** mit Hardware-spezifischen Werten
- **Präzises Timing**: 320µs Pulse-Length mit GPIO-Hardware-Timing
- **10x Wiederholung** für maximale Zuverlässigkeit in 433MHz-Band
- **Sync-Signal**: 1:31 Ratio für Frame-Synchronisation
- **Bit-Encoding**: 1:3 für '0', 3:1 für '1' (High:Low Ratio)

### Sicherheit & Stabilität
- **Privilegien**: Minimal-Privilegien - nur sudo für GPIO-Zugriff erforderlich  
- **Input-Validierung**: Strikte Validierung aller API-Parameter und RF-Codes
- **Command Injection**: Schutz durch execFile statt exec mit Array-Parametern
- **Hex-Code-Validierung**: Regex-basierte Validierung für Custom-Codes
- **Graceful Shutdown**: Sauberes Beenden mit Auto-Fog-Stop und GPIO-Cleanup
- **Error Handling**: Robuste Fehlerbehandlung mit automatischen Fallbacks

### Monitoring & Logging
- **PM2 Process Monitoring**: Memory-Limits, Crash-Detection, Auto-Restart
- **Structured Logging**: Separate Error-, Output- und Combined-Logs mit Timestamps
- **Database-Logging**: Persistente Aktivierungs-Historie (optional, graceful fallback)
- **Health-Check Endpoint**: `/api/health` für externe Monitoring-Systeme
- **Live-Updates**: 5s Status-Updates, 10s Auto-Fog-Updates im Frontend

### Troubleshooting
- **GPIO-Berechtigungen**: `sudo` erforderlich für Hardware-Zugriff
- **Datenbank-Verbindung**: MySQL auf 127.0.0.1:3306, läuft ohne DB weiter
- **Port-Konflikte**: Standard-Port 5003, konfigurierbar über PORT env var
- **RF-Reichweite**: Optimale Reichweite bei freier Sicht, 433MHz-Interferenzen beachten
- **PM2-Management**: `pm2 restart fog-controller` nach Config-Änderungen

## Lizenz

MIT License

## Autor

Martin Pfeffer, 2025 Berlin

---

*Dieses Projekt ist Teil einer Smart-Home-Controller-Suite für Raspberry Pi.*